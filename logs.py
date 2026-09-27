# -*- coding: utf-8 -*-
"""日志保留策略的【唯一】实现 —— 按【天数】裁，两条链共用。

用户 2026-09-26："现在我们的系统每天运行是不是会产生很多日志？有没有自动
清理的机制，比如只保留 7 天、30 天之类，防止日志膨胀"

量了一遍，真正**无界**的只有 launchd 那四个文件（`sync_logs/` 那半早就有
轮转了，只是按**份数**不是按天）：

    _manifest/sync_logs/*.log     520 KB / 60 份   ✅ 有轮转（本轮改成按天）
    _manifest/launchd-sync.out    1.6 MB / 19 天   🔴 永不轮转 ~84 KB/天
    _manifest/launchd-tick.out    190 KB /  8 天   🔴 永不轮转 ~24 KB/天
    _manifest/launchd-*.err       几 KB            🔴 永不轮转（但只在出错时写）

## 🔴🔴 两个坑，都是实测出来的，都不报错

**① launchd 持有 fd —— 只能【原地截断】，不能改名/删除。**
  `StandardOutPath` 是 launchd 自己打开并一直持有的。把文件 rename 或
  unlink 之后，它会继续往**那个 inode** 写 —— 磁盘空间不释放、新文件永远
  是空的，而**日志看着"清干净了"**。所以这里只 `open(path, 'w')` 原地写回。
  ★ 前提是它以 `O_APPEND` 打开（每次 write 原子地落到 EOF），否则截断之后
    它会按旧偏移写、中间留一大段 NUL 空洞。**这条是真跑一次验的**：
    截断到 21 KB 之后等下一个 launchd 点位，文件里 NUL 字节数必须是 0。

**② 按 `\n` 切，不能用 `splitlines()`。**
  日志里有 `\r`（进度行原地刷新用的），而 `splitlines()` 把 `\r` 也当换行。
  实测同一个文件：`wc -l` 是 **7860**，`splitlines()` 是 **19560** ——
  拿后者去"保留最后 N 行"就会**切在半行上**，而它不报错。

★ 放在 `datalake/` 根上是因为两个仓库都要用：`sync_daily.py`（本仓）与
  `assay/tick_daily.py`（它本来就把 datalake 根插进了 sys.path）。
  各写一份的话保留天数迟早对不上（同「一件事只许有一份实现」）。
"""
import atexit
import datetime
import glob
import io
import os
import re
import sys

# `[09-26 17:40] …`（sync 链）与 `信号重算  2026-09-26 17:00:07`（tick 链）
_TS_FULL = re.compile(r'(\d{4})-(\d{2})-(\d{2})[ T](\d{2}):(\d{2})')
_TS_MD = re.compile(r'^\[(\d{2})-(\d{2}) (\d{2}):(\d{2})\]')


def _line_time(ln, now):
    """这一行是什么时候写的 —— 认不出来返回 None（**不猜**）。

    🔴 `[MM-DD HH:MM]` 没有年份，跨年时会歧义。做法是「取**不晚于现在**的
      那个年份」：同年解不出来（比现在晚）就退到去年。猜错的方向是
      "算得更老 -> 被裁掉"，而那只是少留几行日志。
    """
    m = _TS_FULL.search(ln)
    if m:
        try:
            return datetime.datetime(*[int(x) for x in m.groups()])
        except ValueError:
            return None
    m = _TS_MD.match(ln)
    if m:
        mo, d, h, mi = [int(x) for x in m.groups()]
        for y in (now.year, now.year - 1):
            try:
                t = datetime.datetime(y, mo, d, h, mi)
            except ValueError:
                continue
            if t <= now + datetime.timedelta(days=1):
                return t
    return None


def trim_by_days(path, days=30, keep_min_lines=200, min_bytes=64 * 1024,
                 now=None):
    """把一个**追加型**日志裁到最近 `days` 天 —— 原地截断。

    返回 `(裁掉多少字节, 为什么)`，从不抛异常（日志是给人看的，
    清理失败不许影响主链 —— 同 `progress.py` 那条）。

    ★ `min_bytes` 是快速路径：小于它就什么都不做，免得每轮都读一遍文件。
    ★ `keep_min_lines` 是**下限**：认不出时间戳（比如 `.err` 里的纯
      traceback）时退回"保留最后这么多行"，不会把文件裁到近乎空。
    """
    try:
        if not os.path.isfile(path):
            return 0, '不存在'
        size = os.path.getsize(path)
        if size < min_bytes:
            return 0, '还小（%d 字节）' % size
        now = now or datetime.datetime.now()
        cut = now - datetime.timedelta(days=days)
        # 🔴 `newline=''` 不能少：文本模式默认做 **universal newlines**
        #   翻译，会把 `\r` 读成 `\n` —— 于是下面那句 `split('\n')` 在读的
        #   这一步就已经被绕过了，进度行照样被切成好几段。实测抓到过。
        with io.open(path, encoding='utf-8', errors='replace',
                     newline='') as f:
            txt = f.read()
        # 🔴 按 `\n` 切 —— 日志里有 `\r`，`splitlines()` 会切在半行上
        lines = txt.split('\n')
        keep_from = None
        for i, ln in enumerate(lines):
            t = _line_time(ln, now)
            if t is not None and t >= cut:
                keep_from = i
                break
        why = '按 %d 天' % days
        if keep_from is None:
            # 一行时间戳都认不出来（`.err` 里的纯 traceback 就是这样）——
            # 退回"保留最后 N 行"。🔴 **行数下限只在这条兜底路上生效**：
            #   按天那条路上也套下限的话，"行少字多"的日志永远裁不动
            #   （实测：61 天 × 2 行 × 2000 字符 = 124 KB，而 122 < 200
            #   于是一行都不裁，**且什么都不报**）。按天裁就按天裁。
            keep_from = max(0, len(lines) - keep_min_lines)
            why = '认不出时间戳，退回保留最后 %d 行' % keep_min_lines
        if keep_from <= 0:
            return 0, '都在 %d 天内' % days
        out = '\n'.join(lines[keep_from:])
        # 🔴🔴 **原地写回，绝不 rename/unlink** —— launchd 持有 fd（见文件头）
        with io.open(path, 'w', encoding='utf-8', newline='') as f:
            f.write(out)
        return size - len(out.encode('utf-8')), why
    except Exception as e:                                  # noqa: BLE001
        return 0, '裁不动：%s' % e


def prune_dir_by_days(d, pattern='*.log', days=30, keep_min=5, now=None):
    """目录里按天清（`sync_logs/` 那种"一次跑一份"的）。

    ★ `keep_min` 是防**系统时间跳变**把整个目录清空 —— 最近这几份一律留。
    """
    try:
        fs = sorted(glob.glob(os.path.join(d, pattern)),
                    key=os.path.getmtime, reverse=True)
        cut = (now or datetime.datetime.now()) - datetime.timedelta(days=days)
        gone = 0
        for f in fs[keep_min:]:
            if datetime.datetime.fromtimestamp(os.path.getmtime(f)) < cut:
                os.remove(f)
                gone += 1
        return gone
    except OSError:
        return 0


# ------------------------------------------------ 按天切分的日志（两仓共用）
#
# 🔴 用户（2026-09-27）："因为程序不在本机执行，可能有各种错误，所以一定要
#   打印充分的日志，以排查困难。assay 和 datalake 的日志。数据初始化的日志
#   可以打印在一个单独的一个文件中，后续日常运行的日志打印在一个文件中，
#   可以按天切分文件，每天生成一个新的日志文件，清理超出时间的日志文件。"
#
# 改之前的四处，形态各不相同，而**两处在 Windows 上根本不存在**：
#
#   sync_daily.py     sync_logs/<时间戳>.log   每次跑一份（269 份）
#   装配 _run_job     sync_logs/setup-*.log    每次跑一份
#   tick_daily.py     只有 launchd 的 .out     🔴 Windows 上没有
#   serve.py          **只往终端打**            🔴 窗口一关就没，而它是长跑那个
#
# 现在两个文件，按天：
#
#   _manifest/logs/setup-YYYY-MM-DD.log   建本地数据（七个阶段）
#   _manifest/logs/daily-YYYY-MM-DD.log   日常：同步链 / 信号重算 / 看板
#
# ★ 日常那份混着三个来源，所以每行带 `[来源]` —— 不标的话
#   "这句是谁说的"查不出来，而这个文件存在的理由就是排查。

LOG_DIR = ('_manifest', 'logs')
KIND_SETUP = 'setup'
KIND_DAILY = 'daily'

# 🔴 **可重定向** —— 守卫与手工验证不许写生产目录（同 `progress.DIR`、
#   同 `lv.LIVE` / `ASSAY_RUNS` / `ASSAY_INSTALL_LOG` 那套）。
#   env 那条是给**子进程**用的：serve.py / sync_daily.py 是另起的进程，
#   改模块属性对它们无效。
DIR = os.environ.get('ASSAY_LOG_DIR') or None


def day_dir(root):
    return DIR or os.path.join(root, *LOG_DIR)


def day_path(kind, root, now=None):
    """<root>/_manifest/logs/<kind>-YYYY-MM-DD.log"""
    t = now or datetime.datetime.now()
    return os.path.join(day_dir(root), '%s-%s.log' % (kind, t.strftime('%Y-%m-%d')))


class DayLog(object):
    """按天切分 + 连续重复行折叠。

    🔴 **不缓存 fd**：长跑进程（serve.py 一开就是几天）跨午夜必须换文件，
      而"换没换天"只能每次写之前现问一次（同「判据永远是现在的状态，
      不是记录」）。开销是一次 open/close，而这条链一秒也没几行。

    🔴 **连续重复行折叠**：实测 `launchd-sync.out` 7879 行 / 1.6 MB 里，
      绝大多数是每 10 分钟一条的「今天不是交易日 —— 什么都不做」。
      那不是"日志充分"，那是**把真正的错误埋掉**（同「天天报的告警
      等于没有告警」）。所以同一句连着出现时只写第一条，
      **等它变了再补一句 `↑ 同上，共 N 次（末次 HH:MM:SS）`** ——
      ★ 折叠不是丢弃：次数与末次时间都留着，"链条还活着"照样看得出来。

    ⚠ 并发：sync / tick / serve 可能同时写 daily。用 append + 单次 write，
      POSIX 上 O_APPEND 的小写入是原子的；Windows 上小于缓冲区时也是。
      **这是明知的取舍** —— 真要严格就得上锁，而那会让三个进程互相拖。
    """

    def __init__(self, kind, root, tag=''):
        self.kind, self.root, self.tag = kind, root, tag
        self._last = None          # 上一条的正文（用来折叠）
        self._n = 0                # 连着重复了几次
        self._at = ''              # 末次时间
        self._path = None          # 当前在写哪个文件（跨午夜要先结账）

    def _emit(self, text, path=None):
        try:
            d = day_dir(self.root)
            if not os.path.isdir(d):
                os.makedirs(d, exist_ok=True)
            with io.open(path or day_path(self.kind, self.root), 'a',
                         encoding='utf-8', errors='replace') as f:
                f.write(text)
        except Exception:                                   # noqa: BLE001
            # ★ 写不进去（只读盘 / 没权限）不许把业务搞挂 —— 但也**不能
            #   静默**：调用方看到返回 False 就知道该说一句。
            return False
        return True

    def _flush_dup(self):
        # 🔴 折叠出来的那句要写进【末次那条所在的文件】，不是"现在这一天"的。
        #   不带 path 的话，跨午夜时昨天那段的计数会落到今天的文件里
        #   —— 守卫第一次跑就抓到了这个。
        if self._n > 1:
            self._emit('    ↑ 同上，共 %d 次（末次 %s）\n' % (self._n, self._at),
                       self._path)
        self._last, self._n = None, 0

    def line(self, msg):
        """写一行（自带时间戳与来源标记）。"""
        now = datetime.datetime.now()
        p = day_path(self.kind, self.root, now)
        if self._path is not None and p != self._path:
            self._flush_dup()       # 跨午夜：先把昨天那段结账，再开新文件
        self._path = p
        body = msg.rstrip('\n')
        if body and body == self._last:
            self._n += 1
            self._at = now.strftime('%H:%M:%S')
            return True
        self._flush_dup()
        self._last, self._n = body, 1
        self._at = now.strftime('%H:%M:%S')
        pre = '[%s]' % now.strftime('%m-%d %H:%M:%S')
        if self.tag:
            pre += ' [%s]' % self.tag
        return self._emit('%s %s\n' % (pre, body), p)

    def close(self):
        self._flush_dup()


class Tee(object):
    """把一个流【同时】写进终端与当天的日志。

    🔴 **tee 不是重定向**：人在前台跑 `python3 serve.py` 时还要看得见
      （同 install.py 那条 `_tee` —— 那是同一条纪律的另一处实现，
      但那边是"读子进程的管道"，这边是"接管自己的 sys.stdout"，
      两者形状不同，不硬并）。
    """

    def __init__(self, stream, daylog):
        self._s, self._d = stream, daylog
        self._buf = ''

    def write(self, s):
        self._s.write(s)
        # ★ 进度行（\r 原地刷新）不进日志 —— 它每秒刷几次，
        #   落盘之后是一整片看不懂的乱码（同 `\r` 咬过两次那条）。
        self._buf += s
        while '\n' in self._buf:
            ln, self._buf = self._buf.split('\n', 1)
            ln = ln.rsplit('\r', 1)[-1]
            if ln.strip():
                self._d.line(ln)
        return len(s)

    def flush(self):
        self._s.flush()

    def isatty(self):
        try:
            return self._s.isatty()
        except Exception:                                   # noqa: BLE001
            return False

    def fileno(self):
        return self._s.fileno()


def tee_stdio(kind, root, tag=''):
    """接管 sys.stdout / sys.stderr，返回 (DayLog, 还原用的函数)。"""
    d = DayLog(kind, root, tag)
    # 🔴 进程退出时要把最后那段折叠计数写出去 —— 长跑的 serve.py 根本不会
    #   调 restore()，不注册的话"最后那句重复了多少次"永远落不了盘。
    #   `close()` 幂等（`_flush_dup` 清了计数），与 restore() 重复调无害。
    atexit.register(d.close)
    so, se = sys.stdout, sys.stderr
    sys.stdout, sys.stderr = Tee(so, d), Tee(se, d)

    def restore():
        d.close()
        sys.stdout, sys.stderr = so, se
    return d, restore


def prune_day_logs(root, days=30, now=None):
    """两类都按天清 —— 用户要的"清理超出时间的日志文件"。"""
    d = day_dir(root)
    if not os.path.isdir(d):
        return []
    n = 0
    for kind in (KIND_SETUP, KIND_DAILY):
        # ★ `prune_dir_by_days` 返回的是**个数**（int），不是清单 ——
        #   第一版我按清单 `+=`，当场 TypeError（被那句"不许静默"抓到）。
        n += prune_dir_by_days(d, '%s-*.log' % kind, days=days,
                               keep_min=3, now=now)
    return n
