#!/bin/bash
# ============================================================================
# 每日数据同步 —— A 腿（tdx 行情）全自动，跑完直接触发实盘出信号。
#
#   bash datalake/sync_daily.sh            # 正常跑
#   bash datalake/sync_daily.sh --no-live  # 只同步数据，不触发信号
#   bash datalake/sync_daily.sh --dry      # 只打印会做什么
#
# ## 为什么定时不放 serve.py 里
#
# 看板的设计前提是「纯读、随时重启无代价」。而 daily_snapshot.py 是
# **漏一天永久丢失**的（tdx 的名称/分类/板块成分是 type-1 覆盖写，
# 当天状态错过就再也重建不出来）。把它挂在「看板恰好开着」上不可靠：
# 看板会被关、机器会睡。所以调度用系统级的 launchd。
#
# ## 为什么跑完要直接触发出信号（而不是让 live 自己定时）
#
# 信号必须用最新数据。靠「同步 18:10 / live 19:00」两个时间常量隔开，
# 同步一慢就错位，而错位的表现是【信号静默用了昨天的数据】。
# 把依赖写进调用顺序比写进两个常量可靠。live 侧的 tick_time 只当兜底。
#
# ## 幂等
#
# 每一步都可重复执行：tdx2db cron 增量、daily_snapshot 内容哈希去重、
# load_tdx_kline 覆盖写、panel 按年重建、beta 全量重算。
# 中途失败就重跑整条，不需要判断上次断在哪。
#
# ## B 腿（聚宽财务）不在这里
#
# 聚宽研究环境没有本地 API，必须人工导出。本脚本只**检查它落后多少**
# 并在落后时响亮提示 —— 这正是数据字典 E-0「数据新鲜度错配」那条陷阱：
# 行情天天新、财务停在三个月前，回测照跑、报告看着完全正常。
# ============================================================================
set -o pipefail

DL="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(dirname "$DL")"
TDX="$ROOT/tdx2db"
LOGDIR="$DL/_manifest/sync_logs"
STATUS="$DL/_manifest/sync_status.json"
STAMP=$(date +%Y%m%d-%H%M%S)
LOG="$LOGDIR/$STAMP.log"
mkdir -p "$LOGDIR"

NO_LIVE=0; DRY=0
for a in "$@"; do
  [ "$a" = "--no-live" ] && NO_LIVE=1
  [ "$a" = "--dry" ] && DRY=1
done

STEPS_OK=(); STEPS_BAD=(); T0=$(date +%s)

say(){ echo "$@" | tee -a "$LOG"; }

run(){   # run <名字> <工作目录> <命令...>
  local name="$1" wd="$2"; shift 2
  say ""
  say "───── $name ─────"
  say "\$ (cd $wd && $*)"
  if [ "$DRY" = "1" ]; then STEPS_OK+=("$name(dry)"); return 0; fi
  local t=$(date +%s)
  if (cd "$wd" && "$@") >>"$LOG" 2>&1; then
    say "✅ $name  $(( $(date +%s) - t ))s"
    STEPS_OK+=("$name")
    return 0
  fi
  say "❌ $name 失败（$(( $(date +%s) - t ))s）—— 见 $LOG"
  tail -20 "$LOG" | sed 's/^/    /'
  STEPS_BAD+=("$name")
  return 1
}

say "======================================================================"
say " 每日数据同步  $(date '+%F %T')"
say "======================================================================"

# ---- A 腿 ----
# ★ 不走 tdx2db/scripts/update.sh 与 full_update.sh：两者都调
#   scripts/fast_update_indicators.py，而该文件【不存在】—— 脚本是坏的。
#   而且 datalake 只用 tdx.db 的 raw_kline_daily / raw_adjust_factor /
#   raw_basic_daily / raw_symbol_class 四张表（load_tdx_kline.py 里查得到），
#   技术指标那步与本链无关。所以直接调 tdx2db cron。
run "1/6 tdx2db cron（抓日线+复权因子）" "$TDX" ./tdx2db cron --dburi "duckdb://./tdx.db"

# ★ 这一步【漏一天永久丢失】，所以即使前面失败也要跑：它读的是 tdx.db
#   的当前状态，与 cron 是否成功无关。
run "2/6 PIT 快照（漏一天不可逆）" "$TDX" python3 daily_snapshot.py

if [ ${#STEPS_BAD[@]} -eq 0 ]; then
  run "3/6 tdx -> raw/std" "$ROOT" python3 datalake/build/load_tdx_kline.py \
    && run "4/6 交易日历（含未来，带对数）" "$ROOT" python3 datalake/build/build_trade_calendar.py \
    && run "5/6 面板（本年增量）" "$ROOT" python3 datalake/build/build_panel_daily.py --year "$(date +%Y)" \
    && run "6/6 beta" "$ROOT" python3 datalake/build/build_beta_daily.py
else
  say ""
  say "⚠ 前置步骤失败，跳过 3~6（不在坏数据上继续加工）"
fi

# ---- 新鲜度 + B 腿落后多少 ----
say ""
say "───── 新鲜度 ─────"
FRESH=$(python3 "$DL/build/sync_status.py" --write "$STATUS" --log "$LOG" 2>&1)
say "$FRESH"

# ---- 触发实盘出信号 ----
if [ "$NO_LIVE" = "1" ]; then
  say ""; say "（--no-live：跳过出信号）"
elif [ ${#STEPS_BAD[@]} -ne 0 ]; then
  say ""; say "⚠ 同步有失败，**不出信号** —— 宁可没有信号，也不要用半截数据算出来的信号"
elif [ "$DRY" != "1" ]; then
  say ""
  say "───── 实盘出信号 ─────"
  (cd "$ROOT/assay" && python3 -c "
import sys; sys.path.insert(0,'.')
from assay import live
for r in live.tick(force=True):
    if r.get('error'):
        print('  [%s] 失败: %s' % (r.get('account'), r['error']))
    else:
        print('  [%s] %s  卖%d 买%d 持有%d' % (r['account'], r['for_date'],
              len(r.get('sell') or []), len(r.get('buy') or []), len(r.get('hold') or [])))
") 2>&1 | tee -a "$LOG"
fi

say ""
say "======================================================================"
say " 完成 $(( $(date +%s) - T0 ))s   成功 ${#STEPS_OK[@]}  失败 ${#STEPS_BAD[@]}"
[ ${#STEPS_BAD[@]} -ne 0 ] && say " 失败步骤: ${STEPS_BAD[*]}"
say " 日志 $LOG"
say "======================================================================"

# 只保留最近 60 份日志
ls -1t "$LOGDIR"/*.log 2>/dev/null | tail -n +61 | xargs rm -f 2>/dev/null

[ ${#STEPS_BAD[@]} -eq 0 ]
