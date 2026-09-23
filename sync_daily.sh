#!/bin/bash
# ============================================================================
# 每日数据同步 —— A 腿（tdx 行情）全自动，跑完直接触发实盘出信号。
#
#   bash datalake/sync_daily.sh            # 正常跑
#   bash datalake/sync_daily.sh --no-live  # 只同步数据，不触发信号
#   bash datalake/sync_daily.sh --dry      # 只打印会做什么
#   bash datalake/sync_daily.sh --if-stale # 数据已齐就直接退出（给轮询用）
#
# ## 为什么有 --if-stale：把「几点跑」换成「齐没齐」
#
# 通达信什么时候放出当天数据是**它说了算**的，写死 18:10 有两种坏法：
# 定早了抓不到（而 tdx2db cron 不会因此报错，只是库里没有当天的行）、
# 定晚了白等两小时。所以 16:00 起每 10 分钟问一次，不齐就试着抓 ——
# 判据落在"数据现在是什么状态"上，而不是"到点没到点"。
# 判据本身在 build/is_stale.py（可单独跑、可单独测）。
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

NO_LIVE=0; DRY=0; IF_STALE=0
for a in "$@"; do
  [ "$a" = "--no-live" ] && NO_LIVE=1
  [ "$a" = "--dry" ] && DRY=1
  [ "$a" = "--if-stale" ] && IF_STALE=1
done

# ---- --if-stale：先问"齐没齐"，齐了就直接退出 ----
# 判据全在 build/is_stale.py（那里写了为什么），这里只按退出码分流：
#   0 已齐 / 2 现在不该跑（非交易日、未收盘、上一轮正在抓）-> 都是什么都不做
#   1 该跑 -> 往下走
# ★ 这两种情况**不建日志文件**：轮询每 10 分钟一次，大部分时候"不用跑"，
#   每次都建一个日志会把真正有内容的那些冲掉（只保留最近 60 个）。
#   launchd 的 StandardOutPath 里仍有一行记录，够追溯。
if [ "$IF_STALE" = "1" ]; then
  OUT=$(python3 "$DL/build/is_stale.py" 2>&1); RC=$?
  if [ "$RC" = "0" ] || [ "$RC" = "2" ]; then
    echo "[$(date '+%m-%d %H:%M')] $OUT"
    exit 0
  fi
  echo "[$(date '+%m-%d %H:%M')] $OUT"
fi

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

# 🔴🔴 **研究链的步骤用 run_soft，不用 run。**
#
# `run` 失败会把名字塞进 STEPS_BAD，而末尾那句
#   `elif [ ${#STEPS_BAD[@]} -ne 0 ]; then say "⚠ 同步有失败，**不出信号**"`
# 会因此**让实盘当天没有信号**。对 1~8 步（行情与面板）那是对的 ——
# 宁可没有信号，也不要用半截数据算出来的信号。
#
# 但因子面板是**研究用**的：实盘出信号、模拟盘推进一个都不读它。
# 让它失败去掐掉实盘信号，是把两条互不相干的链绑在了一起。
# 所以研究链走 run_soft：失败**照样醒目地报出来**（进 STEPS_WARN，
# 末尾汇总里单列一行），但不影响 STEPS_BAD、不影响出信号、不影响退出码。
#
# ⚠ 9/11 ETF lake 目前仍用 `run` —— 也就是说**它失败会掐掉实盘信号**。
#   那是既有行为，这一轮**没有动它**（改它是另一个决定：ETF lake 只喂
#   ETF 策略的回测与模拟盘，掐掉股票策略的信号确实过宽）。记在这里，
#   别下次当成"漏了"。
STEPS_WARN=()
run_soft(){   # 同 run，但失败只告警：不进 STEPS_BAD、不影响出信号与退出码
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
  say "⚠️ $name 失败（$(( $(date +%s) - t ))s）—— 研究链，**不影响出信号**；见 $LOG"
  tail -20 "$LOG" | sed 's/^/    /'
  STEPS_WARN+=("$name")
  return 0
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
run "1/11 tdx2db cron（抓日线+复权因子）" "$TDX" ./tdx2db cron --dburi "duckdb://./tdx.db"

# ★ 这一步【漏一天永久丢失】，所以即使前面失败也要跑：它读的是 tdx.db
#   的当前状态，与 cron 是否成功无关。
run "2/11 PIT 快照（漏一天不可逆）" "$TDX" python3 daily_snapshot.py

# 🔴🔴 ETF 价格按【.day 正本】重写 —— **必须每天跑，且必须在 load_tdx_kline 之前**。
#
# 2026-09-17 定案：tdx2db 有两条取数路径，坏的是每天那条。
#
#     init（引导历史）  vipdoc/*.day                    ÷1000  ✅ 2019~2025 精度完好
#     cron（每日增量）  products/data/data/g4day/*.zip  ETF 按 ÷10000 再舍到 3 位 ❌
#
# 于是 `1107`（真值 1.107×1000）进库成了 `0.111`：**量级小 10 倍、而且第 3 位
# 小数被舍掉**。原来这一步是 `fix_etf_price_scale.py` 的 ×10 补丁 —— 它只把
# 0.111 抬回 1.11，**补在了错误的层上，那一位永远回不来**。
#
# 后果是"数据看着正常、只是不动了"：ETF 相邻交易日收盘完全相同的占比
# 从 3~4% 涨到 30.9%（红利低波那几只 40%+，对照指数 0.19% / 股票 2.44%）——
# 单价 1.1 元的 ETF 一分钱 = 0.9%，而它日内只波动 0.2~0.5%。
# 用户看到的是"收益曲线里选红利低波 ETF，好几天的值没有任何变化"。
#
# 现在直接取正本：`.day` 的编码从来没变过（sz159525：05-22=1079、05-25=1077
# 连续），÷1000 就是真值。只下 61 MB 而不是 500 MB —— ETF 成员在 vipdoc 里
# 是**连续的**，先取中央目录算出字节区间，再各市场发 1 个 Range 请求。
#
# 三道自证，任一不过就拒绝写入（详见脚本 docstring）：
#   ① 除数：待修区间【之前】那 120 个交易日必须逐位相同（那段这次不动）
#   ② 对齐：amount 不参与修正，它证明改的是同一行
#   ③ 修好没有：**还有几行与正本不符必须是 0**
# 🔴🔴 判据是「和正本比」，不是「坏成什么样」：最近 30 个交易日**无条件**
#    逐行核对，塌陷探测只负责把区间往回扩。只靠塌陷探测（数"有没有第 3 位
#    小数"）的话，它检测的是**那一次事故的指纹** —— ×10 补丁退役之后，
#    cron 写进来的是 `0.111`（÷10、仍带 3 位小数），探测器看不见它。
# 🔴 起点也不写死日期 —— 上一版守卫就是写死 `2026-05-15 ~ 06-05`
#    才在 09-01 再次发生时报"异常 0"。
#
# 幂等：写的是正本值，重复跑收敛（第二次报"无事可做"）。
run "3/11 ETF 价格按 .day 正本重写" "$TDX" \
    python3 scripts/fix_etf_price_from_dayfile.py --db ./tdx.db

# 🔴 更新体检 —— 上一步的**守卫**，也是整条 A 腿的守卫。
#   查"某天大批标的价格/量/额整体跳变"（单位或编码变更的指纹）。
#   失败即进 STEPS_BAD -> 下面 5~8 步整体跳过，**不在坏数据上继续加工**。
#   ★ 它本来就是为 2026-05-25 那次写的，但同样没接进链；而且 `--since`
#     那条路径有个 `DATE ?` 的语法错误，一跑就抛 ParserException ——
#     也就是说这个守卫从上线起就没体检过任何一天。已一并修好。
run "4/11 更新体检（大批跳变即中止）" "$TDX" \
    python3 scripts/check_data_anomaly.py --db ./tdx.db --since "$(date -v-10d +%F 2>/dev/null || date -d '10 days ago' +%F)"

if [ ${#STEPS_BAD[@]} -eq 0 ]; then
  run "5/11 tdx -> raw/std" "$ROOT" python3 datalake/build/load_tdx_kline.py \
    && run "6/11 交易日历（含未来，带对数）" "$ROOT" python3 datalake/build/build_trade_calendar.py \
    && run "7/11 面板（本年增量）" "$ROOT" python3 datalake/build/build_panel_daily.py --year "$(date +%Y)" \
    && run "8/11 beta" "$ROOT" python3 datalake/build/build_beta_daily.py
  # 🔴🔴 **ETF lake 也要每天建 —— 它一直是手工跑的，于是它停在哪天没人知道。**
  #   2026-09-18 实测：主数据到 09-17，而 `etf_lake` 停在 **09-11**
  #   （上次手工跑是 09-13）。ETF 模拟盘「推进到最新数据日」于是只能到 09-11，
  #   而页面上只写「推进到 09-11」—— 人拿它跟别处的 09-17 一比就以为推进坏了，
  #   **而它不报错**（同「靠人记得跑的步骤 = 迟早不跑」那条）。
  #   ★ 排在 5/9 之后：它吃的是 `raw/tdx/kline`（那一步的产物）。
  #   ★ 幂等、**1.6 秒**（实测三次数值指纹相同）—— 便宜到没有不每天跑的理由。
  #   ★ 放在链尾：它失败**不影响主面板**（5~8 已经跑完），而主面板才是
  #     `is_stale` 的判据，所以不会因为它失败就每 10 分钟重跑整条链。
  run "9/11 ETF lake（ETF 策略跑在它上面）" "$ROOT" \
    python3 datalake/build/build_etf_lake.py

  # 🔴 **必须排在 7/11（面板）之后** —— 它吃 `mart/panel_daily`。
  #   挪到前面读的是**昨天**的面板，而那不报错，只是整份因子值晚一天
  #   （同 ETF lake 排在 load_tdx_kline 之后那条）。
  # ★ 两步都幂等：因子面板自己比对 panel 指纹与公式指纹，都没变就秒退，
  #   所以一天里轮询跑多次也只真算一次。
  # ★ 目录表排在面板之前：它只读注册表、0.1 秒，先落下来的话即使面板
  #   那步挂了，"有哪些因子、怎么算的"仍然是最新的。
  run_soft "10/11 因子目录表" "$ROOT" \
    python3 datalake/build/build_factor_catalog.py
  run_soft "11/11 因子值面板（162 个因子）" "$ROOT" \
    python3 datalake/build/build_factor_daily.py
else
  say ""
  say "⚠ 前置步骤失败，跳过 5~8（不在坏数据上继续加工）"
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
say " 完成 $(( $(date +%s) - T0 ))s   成功 ${#STEPS_OK[@]}  失败 ${#STEPS_BAD[@]}  研究链告警 ${#STEPS_WARN[@]}"
[ ${#STEPS_BAD[@]} -ne 0 ] && say " 失败步骤: ${STEPS_BAD[*]}"
# ⚠ 单列一行而不是混进"失败" —— 混在一起的话，"实盘今天没信号"与
#   "因子面板没更新"看起来一样严重，而它们要做的事完全不同。
[ ${#STEPS_WARN[@]} -ne 0 ] && say " ⚠️ 研究链失败（不影响出信号）: ${STEPS_WARN[*]}"
say " 日志 $LOG"
say "======================================================================"

# 只保留最近 60 份日志
ls -1t "$LOGDIR"/*.log 2>/dev/null | tail -n +61 | xargs rm -f 2>/dev/null

[ ${#STEPS_BAD[@]} -eq 0 ]
