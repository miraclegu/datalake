#!/usr/bin/env python3
"""更新体检: 对比最新交易日的 价格/成交量/成交额 与历史, 明显异常则退出码 1(供 full_update 中止后续)。

针对的正是"某天全体/大批标的价格或量额整体跳变(如 2026-05-25 ETF 缩水10倍)"这类系统性脏数据 ——
个别标的的涨跌停/新股/退市不会触发中止(低于阈值只告警不拦)。

判定(对体检日, 每只标的):
  · 价格: |涨跌幅| = |close/前收-1| > --pct           (A股单日不可能, 基本必是脏数据)
  · 成交量: volume / 近N日中位量  超出 [1/r, r]         (r=--vol-ratio)
  · 成交额: amount / 近N日中位额  超出 [1/r, r]
异常标的去重计数; 若 >= --max-abnormal 判为系统性异常 -> 打印告警 + 退出 1。

用法:
  python check_data_anomaly.py --db ./tdx.db                 # 体检库中最新交易日
  python check_data_anomaly.py --db ./tdx.db --since 2026-07-18  # 体检该日之后每一天
"""
from __future__ import annotations

import argparse
import sys

import duckdb


# 白名单: 只体检价格有实义、且真正进入下游分析链路的品种。
#   block / index —— 通达信板块/概念指数(880xxx 等), 价格无实义、跳动剧烈
#   unknown       —— 债券/可转债/国债逆回购等 31,061 只。它们**只存在于 raw_kline_daily**,
#                    不进 raw_basic_daily / raw_adjust_factor / stock_indicators, 也被 6 个
#                    复权视图的 INNER JOIN class='stock'|'etf' 结构性排除, 所以刻度问题不会
#                    污染任何分析链路。而 2026-05-25 TDX 编码变更后, 这批品种的新数据是对的、
#                    旧数据 ×10 偏高; 它们成交又极稀疏(如 sh184334 四年仅 29 根 K 线),
#                    每有一只首次在新刻度下成交就会报一次 -90% 的假异常。
INCLUDED_CLASSES = ("stock", "etf")


def check_one_day(con, d: str, args) -> tuple[dict, str, bool]:
    """返回 (各项异常计数, 摘要文本, 是否触发中止)。"""
    incl = ", ".join(f"'{c}'" for c in INCLUDED_CLASSES)
    # 过滤下推进 CTE: 窗口函数按 symbol 分区, 整只剔除不影响其它标的的窗口,
    # 但能把参与计算的行数从全表 36.2M 降到 stock+etf 的 23.5M。
    rows = con.execute(f"""
        WITH base AS (
          SELECT k.symbol, k.date, k.close, k.volume, k.amount,
                 LAG(k.close) OVER (PARTITION BY k.symbol ORDER BY k.date) AS prev_close,
                 MEDIAN(k.volume) OVER (PARTITION BY k.symbol ORDER BY k.date
                     ROWS BETWEEN {args.window} PRECEDING AND 1 PRECEDING) AS vol_med,
                 MEDIAN(k.amount) OVER (PARTITION BY k.symbol ORDER BY k.date
                     ROWS BETWEEN {args.window} PRECEDING AND 1 PRECEDING) AS amt_med
          FROM raw_kline_daily k
          JOIN raw_symbol_class sc
            ON sc.symbol = k.symbol AND sc."class" IN ({incl})
          WHERE k.date <= DATE '{d}'
        )
        SELECT b.symbol, b.close, b.prev_close, b.volume, b.vol_med, b.amount, b.amt_med,
               CASE WHEN b.prev_close>0 THEN (b.close/b.prev_close-1)*100 END AS chg_pct,
               CASE WHEN b.vol_med>0 THEN b.volume/b.vol_med END AS vol_r,
               CASE WHEN b.amt_med>0 THEN b.amount/b.amt_med END AS amt_r
        FROM base b
        WHERE b.date = DATE '{d}'
    """).df()

    if rows.empty:
        return {"price": 0, "vol": 0, "amt": 0}, f"  {d}: 无数据", False

    r = args.vol_ratio
    price_bad = rows[(rows.prev_close > 0) & (rows.chg_pct.abs() > args.pct)]
    vol_bad = rows[rows.vol_r.notna() & ((rows.vol_r > r) | (rows.vol_r < 1 / r))]
    amt_bad = rows[rows.amt_r.notna() & ((rows.amt_r > r) | (rows.amt_r < 1 / r))]
    cnt = {"price": len(price_bad), "vol": len(vol_bad), "amt": len(amt_bad)}

    halt = (cnt["price"] >= args.max_abnormal
            or cnt["vol"] >= args.vol_max
            or cnt["amt"] >= args.amt_max)

    lines = [f"  {d}: 覆盖 {len(rows)} 标的 | 价格异常 {cnt['price']}"
             f"(阈值{args.max_abnormal}) · 量异常 {cnt['vol']}(阈值{args.vol_max})"
             f" · 额异常 {cnt['amt']}(阈值{args.amt_max})"]
    if len(price_bad):
        top = price_bad.reindex(price_bad.chg_pct.abs().sort_values(ascending=False).index).head(8)
        for _, x in top.iterrows():
            lines.append(f"      {x.symbol}: 涨跌幅 {x.chg_pct:+.1f}% "
                         f"(前收 {x.prev_close:.3f} → {x.close:.3f})")
    return cnt, "\n".join(lines), halt


def main(argv=None):
    ap = argparse.ArgumentParser(description="数据更新体检(价/量/额 与历史比对)")
    ap.add_argument("--db", required=True)
    ap.add_argument("--date", default=None, help="体检指定日期(默认库中最新交易日)")
    ap.add_argument("--since", default=None, help="体检该日期(不含)之后的所有交易日")
    ap.add_argument("--pct", type=float, default=50.0, help="单日涨跌幅异常阈值%%(默认50)")
    ap.add_argument("--vol-ratio", type=float, default=20.0,
                    help="量/额相对近N日中位数的异常倍数(默认20)")
    ap.add_argument("--window", type=int, default=20, help="中位数回看窗口(默认20)")
    ap.add_argument("--max-abnormal", type=int, default=50,
                    help="价格异常标的数达此值判系统性异常并中止(正常日<15, 默认50)")
    ap.add_argument("--vol-max", type=int, default=300,
                    help="成交量异常标的数中止阈值(默认300)")
    ap.add_argument("--amt-max", type=int, default=300,
                    help="成交额异常标的数中止阈值(默认300)")
    args = ap.parse_args(argv)

    con = duckdb.connect(args.db, read_only=True)

    if args.date:
        dates = [args.date]
    elif args.since:
        dates = [str(x[0]) for x in con.execute(
            # 🔴 `DATE ?` 不是合法 DuckDB 语法（DATE 后面只能跟字面量）。
            #   原来这么写，于是 `--since` 这条路径**一跑就抛 ParserException**
            #   —— 这个守卫从上线起就没真正体检过任何一天，而 2026-09-01 的
            #   ETF 缩 10 倍正是它该拦下的那类问题。
            "SELECT DISTINCT date FROM raw_kline_daily "
            "WHERE date > CAST(? AS DATE) ORDER BY date",
            [args.since]).fetchall()]
    else:
        latest = con.execute("SELECT max(date) FROM raw_kline_daily").fetchone()[0]
        dates = [str(latest)] if latest else []

    if not dates:
        print("⚠️ 找不到体检日期, 跳过体检"); return 0

    print("=" * 56)
    print(f"  数据更新体检(仅 {'/'.join(INCLUDED_CLASSES)}; 已排除板块/概念指数与债券)")
    print("=" * 56)

    any_halt = False
    for d in dates:
        _, summary, halt = check_one_day(con, d, args)
        print(summary)
        any_halt = any_halt or halt

    print("-" * 56)
    if any_halt:
        print("❌ 系统性异常: 价/量/额单日异常标的数超阈值, 疑似整体跳变(单位/编码变更等)。")
        print("   已中止后续流程(不再算指标/选股), 请人工核查数据后再继续。")
        return 1
    print("✓ 体检通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
