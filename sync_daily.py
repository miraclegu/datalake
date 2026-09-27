# -*- coding: utf-8 -*-
"""每日数据同步的【正本】—— 13 步，跨平台（Windows / macOS / Linux）。

🔴 **为什么从 `sync_daily.sh` 搬过来**：那份是 bash，而 Windows 上没有
  bash，也没有 `date -v-10d` / `tail` / `sed` / `ls -1t | xargs` 这条
  POSIX 工具链。而「两份实现」是本项目最硬的那条禁令 —— 两台机器跑出
  不同的面板且不报错，比多写一份麻烦得多。
★ `sync_daily.sh` **保留**，内容只剩一行转发：命令行是**产品契约**
  （CLAUDE.md、launchd plist、肌肉记忆里全是它）。同「selftest.py 拆成
  tests/ 而入口一个字没改」「live.py 变门面」那两次。

用法（与 .sh 完全一致）：
    python3 datalake/sync_daily.py              # 跑一次
    python3 datalake/sync_daily.py --if-stale   # 齐了就秒退（轮询用）
    python3 datalake/sync_daily.py --no-live    # 跑完不触发出信号
    python3 datalake/sync_daily.py --dry        # 只打印要跑什么

----------------------------------------------------------------------
🔴🔴 **`run` 与 `run_soft` 的分工别记反** —— 这是整条链最要紧的一条。

    run       失败 -> STEPS_BAD -> 跳过 5~13 **且当天不出信号**
              1~9 步（行情与面板）用它：宁可没有信号，也不要用半截数据
              算出来的信号。
    run_soft  失败只告警（STEPS_WARN，末尾单列一行），不影响出信号、
              不影响退出码。10~13（公司行动 + 因子那三步）用它。

★ 为什么因子那几步是软的：因子面板是**研究用**的，实盘出信号、模拟盘
  推进一个字都不读它。让它失败去掐掉实盘信号，是把两条互不相干的链
  绑在一起。
⚠ **9/13 ETF lake 仍是 `run`（硬）** —— 也就是它失败会掐掉实盘信号。
  那是既有行为，这次搬运**没有动它**（改它是另一个决定：ETF lake 只喂
  ETF 策略的回测与模拟盘，掐掉股票策略的信号确实过宽）。别下次当成"漏了"。
⚠ **10/13 gbbq 走 run_soft，而它确实喂实盘**（按它调成本与股数）——
  分工是：出信号不读 gbbq（信号只回答"明天买卖什么"），而**调成本读它**。
  所以失败只告警是不够的，消费侧（`assay/lv/corp.py`）必须自己查这份
  parquet 的新鲜度并把落后说给页面听。
----------------------------------------------------------------------
## 为什么有 --if-stale：把「几点跑」换成「齐没齐」

通达信什么时候放出当天数据是**它说了算**的，写死 18:10 有两种坏法：
定早了抓不到（而 tdx2db cron 不会因此报错，只是库里没有当天的行）、
定晚了白等两小时。所以 16:00 起每 10 分钟问一次，不齐就试着抓 ——
判据落在"数据现在是什么状态"上，而不是"到点没到点"。
判据本身在 `build/is_stale.py`（可单独跑、可单独测）。

## 为什么定时不放 serve.py 里

看板的设计前提是「纯读、随时重启无代价」。而 `daily_snapshot.py` 是
**漏一天永久丢失**的（tdx 的名称/分类/板块成分是 type-1 覆盖写，当天
状态错过就再也重建不出来）。把它挂在「看板恰好开着」上不可靠：看板会被
关、机器会睡。所以调度用系统级的 launchd（macOS）/ 任务计划程序（Windows）。

## 为什么跑完要直接触发出信号（而不是让 live 自己定时）

信号必须用最新数据。靠「同步 18:10 / live 19:00」两个时间常量隔开，同步
一慢就错位，而错位的表现是**信号静默用了昨天的数据**。把依赖写进调用
顺序比写进两个常量可靠。live 侧的 `tick_time` 只当兜底。

## 幂等

每一步都可重复执行：tdx2db cron 增量、daily_snapshot 内容哈希去重、
load_tdx_kline 覆盖写、panel 按年重建、beta 全量重算。中途失败就重跑
整条，不需要判断上次断在哪。

## B 腿（聚宽财务）不在这里

聚宽研究环境没有本地 API，必须人工导出。本脚本只**检查它落后多少**并在
落后时响亮提示 —— 那正是数据字典 E-0「数据新鲜度错配」那条陷阱：
行情天天新、财务停在三个月前，回测照跑、报告看着完全正常。

⚠ **步号从 `1/12` 改成 `1/13`**：`.sh` 里前九步一直写着 `x/12`、后四步写
  `x/13`（链从 12 步长到 13 步时只改了新加的那几行）。搬运时统一成 13 ——
  这是**纯文案**修正，命令一个字没动（等价性验证逐条比的是命令）。
----------------------------------------------------------------------
"""

import argparse
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta

DL = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(DL)
# ---- datalake 侧路径的正本：`datalake/paths.py` ----
# 🔴 **往上找它，不数 dirname 层数** —— 层数跟着"这个文件放在哪"变，
#   搬一次就要改一次，而改漏了不报错（同 assay/paths.py 那条）。找不到就一路
#   走到文件系统根，导入正本时抛 ImportError —— **响亮失败**，不会静默
#   退回某个猜出来的路径。
_d = os.path.dirname(os.path.abspath(__file__))
while _d != os.path.dirname(_d) and not os.path.isfile(
        os.path.join(_d, 'paths.py')):
    _d = os.path.dirname(_d)
sys.path.insert(0, _d)
# ---- 输出编码（Windows 上不做这件事整条链会崩，见 datalake/console.py）----
_d = os.path.dirname(os.path.abspath(__file__))
while _d != os.path.dirname(_d) and not os.path.isfile(
        os.path.join(_d, 'console.py')):
    _d = os.path.dirname(_d)
sys.path.insert(0, _d)
import console as _console                                  # noqa: E402
_console.setup()
from paths import TDX_DIR as TDX, tdx2db_bin, launchd_logs                    # noqa: E402
import logs as _logs                                                          # noqa: E402
LOGDIR = os.path.join(DL, '_manifest', 'sync_logs')
STATUS = os.path.join(DL, '_manifest', 'sync_status.json')
PY = sys.executable
# 🔴 **按【天】保留，不按份数。** 轮询是每 10 分钟一个点位，而"齐了就
#   秒退不建文件" —— 于是"60 份"在忙的日子只盖得住两三天、闲的日子盖住
#   半年，**而它不报错**，只是"留多久"这件事说不清。
KEEP_DAYS = 30


# 🔴 **链有几步只写这一处。** 搬运前 `.sh` 里前九步写 `x/12`、后四步写
#   `x/13` —— 链从 12 长到 13 时只改了新加的那几行，于是屏幕上的分母
#   自相矛盾，**而它不报错**。现在步号由 `Runner` 生成，这里是唯一的真值；
#   末尾还有一条自证（跑完整了就必须正好 N_STEPS 步）。
N_STEPS = 13


class Runner(object):
    def __init__(self, log_path, dry=False, total=0, job=None, title=''):
        self.log = log_path
        self.dry = dry
        self.ok, self.bad, self.warn = [], [], []
        self.t0 = time.time()
        self.total, self.n = total, 0
        # ★ `--dry` **不写进度文件** —— 否则页面上会冒出一条"正在同步"，
        #   而什么都没在跑（同「不给一个点了没反应的按钮」）。
        self.pg = None
        if job and not dry:
            try:
                import progress
                self.pg = progress.Progress(job, title, total, log_path)
            except Exception:                               # noqa: BLE001
                pass            # 进度是给人看的，坏了不许影响主链

    def say(self, s=''):
        print(s, flush=True)
        if self.log:
            with open(self.log, 'a', encoding='utf-8') as f:
                f.write(s + '\n')

    def _tail(self, n=20):
        """失败时把日志尾部贴出来 —— 裸一句"失败了"指不到原因。"""
        try:
            with open(self.log, encoding='utf-8', errors='replace') as f:
                for ln in f.read().splitlines()[-n:]:
                    print('    ' + ln, flush=True)
        except Exception:                                   # noqa: BLE001
            pass

    def _run(self, name, wd, cmd, soft):
        self.n += 1
        # 🔴 进度里存**裸名字**，`i/total` 只给日志那一行 —— 进度里也带前缀
        #   的话页面上会是「第 3 / 13 步 · 3/13 ETF 价格…」，**同一份信息
        #   两处看**（同「汇总数字只在 KPI 板出现一次」那条）。
        if self.pg:
            self.pg.step(name)
        name = '%d/%d %s' % (self.n, self.total or N_STEPS, name)
        self.say('')
        self.say('───── %s ─────' % name)
        self.say('$ (cd %s && %s)' % (wd, ' '.join(cmd)))
        if self.dry:
            self.ok.append(name + '(dry)')
            return True
        t = time.time()
        try:
            with open(self.log, 'a', encoding='utf-8') as f:
                rc = subprocess.call(cmd, cwd=wd, stdout=f, stderr=subprocess.STDOUT)
        except OSError as e:                # 文件不存在 / 不可执行
            with open(self.log, 'a', encoding='utf-8') as f:
                f.write('%r\n' % (e,))
            rc = 127
        el = int(time.time() - t)
        if rc == 0:
            self.say('✅ %s  %ds' % (name, el))
            self.ok.append(name)
            if self.pg:
                self.pg.finish_step('ok')
            return True
        if soft:
            self.say('⚠️ %s 失败（%ds）—— 研究链，**不影响出信号**；见 %s'
                     % (name, el, self.log))
            self._tail()
            self.warn.append(name)
            if self.pg:
                self.pg.finish_step('warn')
            return True                      # 软失败不影响调用方的链式判断
        self.say('❌ %s 失败（%ds）—— 见 %s' % (name, el, self.log))
        self._tail()
        self.bad.append(name)
        if self.pg:
            self.pg.finish_step('bad')
        return False

    def run(self, name, wd, cmd):
        return self._run(name, wd, cmd, soft=False)

    def run_soft(self, name, wd, cmd):
        return self._run(name, wd, cmd, soft=True)


def _if_stale():
    """--if-stale 的分流 —— 判据全在 `build/is_stale.py`（那里写了为什么）。

        rc 0 已齐 / rc 2 现在不该跑（非交易日、未收盘、上一轮正在抓）-> 都不做
        rc 1 该跑 -> 往下走

    🔴 这两种"不做"的情况**不建日志文件**：轮询每 10 分钟一次，大部分时候
      不用跑，每次都建日志会把真正有内容的那些冲掉（只留最近 %d 份）。
    """
    p = subprocess.run([PY, os.path.join(DL, 'build', 'is_stale.py')],
                       capture_output=True, text=True)
    out = (p.stdout + p.stderr).strip()
    print('[%s] %s' % (datetime.now().strftime('%m-%d %H:%M'), out), flush=True)
    return p.returncode == 1


def main():
    ap = argparse.ArgumentParser(description='每日数据同步（13 步）')
    ap.add_argument('--no-live', action='store_true', help='跑完不触发实盘出信号')
    ap.add_argument('--dry', action='store_true', help='只打印要跑什么')
    ap.add_argument('--if-stale', action='store_true', help='数据齐了就秒退')
    a = ap.parse_args()

    # 🔴🔴 **按天汇总的日志要在【最前面】接上，而且在 `--if-stale` 之前。**
    #   轮询每 10 分钟一个点位，绝大多数时候走的就是那条提前返回 ——
    #   接在后面的话**最常走的那条路一个字都不落盘**（同上面裁日志那条）。
    # ★ 这里进的是**骨架**（每步开始 / ✅❌ / 失败时的尾部），子进程的完整
    #   输出仍然直接写 `sync_logs/<时间戳>.log` —— 两者分工别混：
    #       daily-<天>.log   今天整个系统说了什么（四个来源汇总）
    #       sync_logs/<ts>   那一次跑的全部细节（数据页点进去看）
    # ★ tee 不是重定向：launchd 的 .out 与前台终端照样看得见。
    # 🔴 **接日志与清日志分成两个 try** —— 混在一起的话"清理失败"会报成
    #   "日志没接上"，而日志其实好好地在写（实测第一版就这么报了一轮，
    #   同「报错必须指向真正的原因」）。
    try:
        _logs.tee_stdio(_logs.KIND_DAILY, DL, 'sync')
    except Exception as _e:                                 # noqa: BLE001
        # 🔴 不许静默：日志没接上时人事后翻不到任何东西，而屏幕上一切正常。
        print('⚠ 按天日志没接上：%s: %s' % (type(_e).__name__, _e), flush=True)
    try:
        _logs.prune_day_logs(DL, days=KEEP_DAYS)
    except Exception as _e:                                 # noqa: BLE001
        print('⚠ 按天日志没清：%s: %s' % (type(_e).__name__, _e), flush=True)

    # 🔴 **裁日志要排在 `--if-stale` 提前返回【之前】。** 轮询每 10 分钟一个
    #   点位，绝大多数时候走的就是那条提前返回，而它也会往 launchd 的 .out
    #   里写一行 —— 放到后面的话**最常走的那条路永远裁不到**，日志照旧涨，
    #   而它不报错（同 tick 里「模拟盘推进要排在提前返回之前」那条）。
    for _p in launchd_logs('sync'):
        _logs.trim_by_days(_p, days=KEEP_DAYS)

    if a.if_stale and not _if_stale():
        return 0

    os.makedirs(LOGDIR, exist_ok=True)
    log = os.path.join(LOGDIR, datetime.now().strftime('%Y%m%d-%H%M%S') + '.log')
    r = Runner(log, dry=a.dry, total=N_STEPS, job='sync',
               title='每日数据同步（增量补全到最新）')
    r.say('=' * 70)
    r.say(' 每日数据同步  %s' % datetime.now().strftime('%F %T'))
    r.say('=' * 70)

    # ---- A 腿：1~4 ----
    # ★ 不走 tdx2db/scripts/update.sh 与 full_update.sh：两者都调
    #   scripts/fast_update_indicators.py，而该文件【不存在】，脚本是坏的。
    r.run('tdx2db cron（抓日线+复权因子）', TDX,
          [tdx2db_bin(), 'cron', '--dburi', 'duckdb://./tdx.db'])
    # ★ 这一步【漏一天永久丢失】，所以即使前面失败也要跑：它读的是 tdx.db
    #   的当前状态，与 cron 成没成功无关。
    r.run('PIT 快照（漏一天不可逆）', TDX, [PY, 'daily_snapshot.py'])
    # 🔴 ETF 价格按 .day 正本重写 —— 必须每天跑、且必须在 load_tdx_kline 之前。
    #   判据是「和正本比」不是「坏成什么样」（详见脚本 docstring）。
    r.run('ETF 价格按 .day 正本重写', TDX,
          [PY, os.path.join('scripts', 'fix_etf_price_from_dayfile.py'),
           '--db', './tdx.db'])
    # 🔴 更新体检 —— 上一步的守卫，也是整条 A 腿的守卫：查"某天大批标的
    #   价格/量/额整体跳变"（单位或编码变更的指纹）。失败即进 STEPS_BAD，
    #   于是 5~13 整体跳过，**不在坏数据上继续加工**。
    since = (datetime.now() - timedelta(days=10)).strftime('%Y-%m-%d')
    r.run('更新体检（大批跳变即中止）', TDX,
          [PY, os.path.join('scripts', 'check_data_anomaly.py'),
           '--db', './tdx.db', '--since', since])

    if not r.bad:
        b = os.path.join('datalake', 'build')
        # 5~8 是**链式**的：前一步失败就不往下走（后面每步都吃前一步的产物）
        (r.run('tdx -> raw/std', ROOT, [PY, os.path.join(b, 'load_tdx_kline.py')])
         and r.run('交易日历（含未来，带对数）', ROOT,
                   [PY, os.path.join(b, 'build_trade_calendar.py')])
         and r.run('面板（本年增量）', ROOT,
                   [PY, os.path.join(b, 'build_panel_daily.py'),
                    '--year', datetime.now().strftime('%Y')])
         and r.run('beta', ROOT, [PY, os.path.join(b, 'build_beta_daily.py')]))
        # 🔴 ETF lake 排在 5 之后（吃 raw/tdx/kline），且放链尾：它失败不影响
        #   主面板，而主面板才是 is_stale 的判据。
        r.run('ETF lake（ETF 策略跑在它上面）', ROOT,
              [PY, os.path.join(b, 'build_etf_lake.py')])
        # 🔴 gbbq 排在 1 之后 —— 它读的就是那一步写进 tdx.db 的 raw_gbbq；
        #   挪到前面读的是**昨天**的，而那不报错。
        r.run_soft('公司行动 gbbq（实盘按它调成本）', ROOT,
                   [PY, os.path.join(b, 'load_tdx_gbbq.py')])
        # ★ 目录表排在因子面板之前：它只读注册表、0.1 秒，先落下来的话
        #   即使面板那步挂了，"有哪些因子、怎么算的"仍然是最新的。
        r.run_soft('因子目录表', ROOT,
                   [PY, os.path.join(b, 'build_factor_catalog.py')])
        # 🔴 必须排在 7（面板）之后 —— 它吃 mart/panel_daily。
        r.run_soft('因子值面板（162 个因子）', ROOT,
                   [PY, os.path.join(b, 'build_factor_daily.py')])
        # 🔴 必须排在 12 之后 —— 它吃 mart/factor_daily。增量，稳态约 4.5 分钟。
        r.run_soft('因子评价（分池 IC / 换手，增量）', ROOT,
                   [PY, os.path.join('assay', 'assay', 'factor_eval.py'), '--build-only'])
    else:
        r.say('')
        r.say('⚠ 前置步骤失败，跳过 5~13（不在坏数据上继续加工）')

    # ---- 新鲜度 + B 腿落后多少 ----
    r.say('')
    r.say('───── 新鲜度 ─────')
    p = subprocess.run([PY, os.path.join(DL, 'build', 'sync_status.py'),
                        '--write', STATUS, '--log', log],
                       capture_output=True, text=True)
    r.say((p.stdout + p.stderr).strip())

    # ---- 触发实盘出信号 ----
    if a.no_live:
        r.say('')
        r.say('（--no-live：跳过出信号）')
    elif r.bad:
        r.say('')
        r.say('⚠ 同步有失败，**不出信号** —— 宁可没有信号，'
              '也不要用半截数据算出来的信号')
    elif not a.dry:
        r.say('')
        r.say('───── 实盘出信号 ─────')
        p = subprocess.run([PY, '-c', _TICK], cwd=os.path.join(ROOT, 'assay'),
                           capture_output=True, text=True)
        r.say((p.stdout + p.stderr).rstrip())

    r.say('')
    r.say('=' * 70)
    r.say(' 完成 %ds   成功 %d  失败 %d  研究链告警 %d'
          % (int(time.time() - r.t0), len(r.ok), len(r.bad), len(r.warn)))
    if r.bad:
        r.say(' 失败步骤: %s' % ' '.join(r.bad))
    # ⚠ 单列一行而不是混进"失败" —— 混在一起的话，"实盘今天没信号"与
    #   "因子面板没更新"看起来一样严重，而它们要做的事完全不同。
    if r.warn:
        r.say(' ⚠️ 研究链失败（不影响出信号）: %s' % ' '.join(r.warn))
    r.say(' 日志 %s' % log)
    r.say('=' * 70)

    # 🔴 **自证：整条链跑完就必须正好 N_STEPS 步。** 加一步而忘了改
    #   `N_STEPS`，表现是屏幕上"12/13 完成"——分母与实际对不上，
    #   **而它不报错**（那正是 `.sh` 时代 12/13 混用的坏法）。
    #   前置失败时 5~13 整体跳过，那是设计，所以只在没有硬失败时查。
    if not r.bad and not a.dry and r.n != N_STEPS:
        r.say(' 🔴 步数自证不通过：跑了 %d 步，而 N_STEPS 写的是 %d'
              % (r.n, N_STEPS))
    if r.pg:
        r.pg.finish(0 if not r.bad else 1)

    # 只保留最近 KEEP_DAYS 天（`keep_min` 防系统时间跳变把目录清空）
    _logs.prune_dir_by_days(LOGDIR, '*.log', days=KEEP_DAYS, keep_min=5)
    return 0 if not r.bad else 1


_TICK = """import sys
sys.path.insert(0, '.')
from assay import live
for r in live.tick(force=True):
    if r.get('error'):
        print('  [%s] 失败: %s' % (r.get('account'), r['error']))
    else:
        print('  [%s] %s  卖%d 买%d 持有%d' % (r['account'], r['for_date'],
              len(r.get('sell') or []), len(r.get('buy') or []), len(r.get('hold') or [])))
"""

if __name__ == '__main__':
    sys.exit(main())
