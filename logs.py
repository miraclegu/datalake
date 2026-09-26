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
import datetime
import glob
import io
import os
import re

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
