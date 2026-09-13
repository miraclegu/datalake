#!/usr/bin/env python3
"""修正通达信 ETF 价格 1/10 问题(2026-05-25 起 TDX 改了 ETF 价格编码, tdx2db 二进制未跟上)。

对 ETF、date >= CUTOFF 的价格 ×10:
  · raw_kline_daily: open/high/low/close ×10
  · raw_basic_daily: close/floatmv/totalmv ×10; preclose/change_pct/amplitude 用修正后的 kline 重算
    (跨档那天 05-25 的 preclose 来自 05-22 的正确价, 必须重算而非简单 ×10)

幂等: 已修的 (symbol,date) 记进 _etf_scale_fixed, 再跑不会重复放大。
既能一次性修历史(05-25→至今), 也能每天 cron 后修当天新增的 ETF 行。

⚠️ 这是给"未修复的 tdx2db 二进制"打的补丁。将来若改用修复版二进制(会直接产出正确ETF价),
必须停用本步骤, 否则会把正确数据再 ×10 —— 更新体检 check_data_anomaly.py 会兜底拦下。

用法:
  python fix_etf_price_scale.py --db ./tdx.db --dry-run   # 先看影响面
  python fix_etf_price_scale.py --db ./tdx.db             # 实修
"""
from __future__ import annotations

import argparse
import sys

import duckdb

CUTOFF = "2026-05-25"   # ETF 编码变更起始日
FACTOR = 10.0


def main(argv=None):
    ap = argparse.ArgumentParser(description="修正 ETF 价格 1/10 问题(×10, 幂等)")
    ap.add_argument("--db", required=True)
    ap.add_argument("--cutoff", default=CUTOFF, help=f"起始日期(默认 {CUTOFF})")
    ap.add_argument("--dry-run", action="store_true", help="只统计不写库")
    args = ap.parse_args(argv)

    con = duckdb.connect(args.db, read_only=args.dry_run)

    # 待修集合: ETF ∩ date>=cutoff ∩ 未修过
    fixed_exists = con.execute(
        "SELECT count(*) FROM information_schema.tables WHERE table_name='_etf_scale_fixed'"
    ).fetchone()[0]
    fixed_join = (
        "LEFT JOIN _etf_scale_fixed f ON f.symbol=k.symbol AND f.date=k.date"
        if fixed_exists else ""
    )
    fixed_cond = "AND f.symbol IS NULL" if fixed_exists else ""

    todo = con.execute(f"""
        SELECT k.symbol, k.date
        FROM raw_kline_daily k
        JOIN raw_symbol_class sc ON sc.symbol=k.symbol AND sc.class='etf'
        {fixed_join}
        WHERE k.date >= DATE '{args.cutoff}' {fixed_cond}
    """).df()

    n = len(todo)
    n_sym = todo["symbol"].nunique() if n else 0
    print(f"待修 ETF 行: {n}  (涉及 {n_sym} 只标的, date >= {args.cutoff})")
    if n == 0:
        print("✓ 无待修行, 跳过"); return 0
    if args.dry_run:
        # 🔴 预览必须取【真正会被改的那一行】。
        #   原来是先取一个 symbol，再查它 `date>=cutoff` 的**最早一行** ——
        #   而那一行很可能早就修过、根本不在待修集合里。实测它显示
        #   「sh501078 2026-05-25 2.89 -> 28.9」，看着像要把 5 月的数据再放大
        #   一次，而实际待修的全是 09-01 之后。**预览说谎比没有预览更糟**：
        #   人会照着它做"要不要执行"的决定。
        print("[DRY-RUN] 待修区间: %s ~ %s"
              % (todo["date"].min(), todo["date"].max()))
        row = con.execute(f"""
            SELECT k.symbol, k.date, round(k.close, 3) AS close_now,
                   round(k.close * {FACTOR}, 3) AS close_fixed
            FROM raw_kline_daily k
            JOIN raw_symbol_class sc ON sc.symbol=k.symbol AND sc.class='etf'
            {fixed_join}
            WHERE k.date >= DATE '{args.cutoff}' {fixed_cond}
            ORDER BY k.date, k.symbol LIMIT 3""").df()
        print("[DRY-RUN] 样例(真实待修行):\n" + row.to_string(index=False))
        print("[DRY-RUN] 未写库")
        return 0

    con.execute("BEGIN")
    con.execute(
        "CREATE TABLE IF NOT EXISTS _etf_scale_fixed "
        "(symbol VARCHAR, date DATE, PRIMARY KEY(symbol, date))"
    )
    con.execute("""
        CREATE OR REPLACE TEMP TABLE _todo AS
        SELECT k.symbol, k.date
        FROM raw_kline_daily k
        JOIN raw_symbol_class sc ON sc.symbol=k.symbol AND sc.class='etf'
        LEFT JOIN _etf_scale_fixed f ON f.symbol=k.symbol AND f.date=k.date
        WHERE k.date >= DATE '{}' AND f.symbol IS NULL
    """.format(args.cutoff))

    # 1) kline OHLC ×10
    con.execute(f"""
        UPDATE raw_kline_daily SET
            open=round(open*{FACTOR},3), high=round(high*{FACTOR},3),
            low=round(low*{FACTOR},3), close=round(close*{FACTOR},3)
        WHERE (symbol, date) IN (SELECT symbol, date FROM _todo)
    """)

    # 2) basic_daily: 市值类 ×10
    con.execute(f"""
        UPDATE raw_basic_daily SET
            close=round(close*{FACTOR},3), floatmv=round(floatmv*{FACTOR},2),
            totalmv=round(totalmv*{FACTOR},2)
        WHERE (symbol, date) IN (SELECT symbol, date FROM _todo)
    """)

    # 3) basic_daily: preclose/change_pct/amplitude 用修正后的 kline 全序列重算(仅回写待修行)
    con.execute("""
        CREATE OR REPLACE TEMP TABLE _recalc AS
        SELECT symbol, date,
               LAG(close) OVER (PARTITION BY symbol ORDER BY date) AS pc,
               high, low, close
        FROM raw_kline_daily
        WHERE symbol IN (SELECT DISTINCT symbol FROM _todo)
    """)
    con.execute("""
        UPDATE raw_basic_daily AS b SET
            preclose = r.pc,
            change_pct = CASE WHEN r.pc>0 THEN round((r.close-r.pc)/r.pc*100,2) ELSE 0 END,
            amplitude  = CASE WHEN r.pc>0 THEN round((r.high-r.low)/r.pc*100,2) ELSE 0 END
        FROM _recalc r
        WHERE b.symbol=r.symbol AND b.date=r.date
          AND (b.symbol, b.date) IN (SELECT symbol, date FROM _todo)
    """)

    # 4) 标记已修
    con.execute("INSERT OR REPLACE INTO _etf_scale_fixed SELECT symbol, date FROM _todo")
    con.execute("COMMIT")

    print(f"✓ 已修正 {n} 行 ETF 价格(×{FACTOR:.0f}), 并更新 basic_daily / 标记幂等表")
    return 0


if __name__ == "__main__":
    sys.exit(main())
