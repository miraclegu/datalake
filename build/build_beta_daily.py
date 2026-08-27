#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""std/beta_daily.parquet —— 个股 beta（滚动回归），红利低波/指数增强线需要。

## 为什么本地算

聚宽的 `jqfactor.get_factor_values(..., 'beta')` 是预计算因子，本地没有。
但 beta **不是专有因子** —— 它就是个股日收益对基准日收益的回归系数，
我们有个股 `ret_1d`（panel，后复权口径）和指数日线（raw/tdx/kline/index_*）。

## ⚠️ 定义未与聚宽对标，故把三个自由度都做成显式参数

聚宽文档没给我们能核对的公式细节，所以下面三项是**我们的选择**，不是复刻：

  WINDOW = 252    回归窗口（交易日）
  BENCH  = sh000300  基准指数（沪深300）
  最少样本 = 120   窗口内有效样本不足则置 NULL（避免次新股用 20 个点回归出的噪声）

**红利低波/指数增强是按 beta 排序取前 50%，排序对定义差异敏感** ——
所以同时落多个窗口（60/120/252）的 beta，让策略侧可以 sweep，
用结果反推哪个定义更接近聚宽，而不是猜一个就用。

beta 用【后复权收益】算：ret_1d = close_hfq/prev_close_hfq - 1，
除权日不会产生假跳变。基准指数不复权（指数本身已含成分调整）。
"""
import os
import sys

import duckdb

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, 'std', 'beta_daily.parquet')
PANEL = "read_parquet('%s/mart/panel_daily/panel_*.parquet')" % ROOT
IDX = "read_parquet('%s/raw/tdx/kline/index_*.parquet')" % ROOT

BENCH = 'sh000300'
WINDOWS = (60, 120, 252)
MIN_OBS = {60: 40, 120: 80, 252: 120}
START = '2013-01-01'          # 提前 3 年起算，保证 2016 年就有满窗口的 252 日 beta


def main():
    con = duckdb.connect(':memory:')
    con.execute('SET preserve_insertion_order=false')
    con.execute("""CREATE TEMP TABLE bench AS
        SELECT date, close / lag(close) OVER (ORDER BY date) - 1 AS mret
        FROM %s WHERE symbol = '%s' AND date >= DATE '%s'""" % (IDX, BENCH, START))
    n_b = con.execute('SELECT count(*) FROM bench WHERE mret IS NOT NULL').fetchone()[0]
    print('基准 %s: %s 个交易日' % (BENCH, format(n_b, ',')))

    con.execute("""CREATE TEMP TABLE stk AS
        SELECT jq_code AS code, date, ret_1d AS sret FROM %s
        WHERE date >= DATE '%s' AND ret_1d IS NOT NULL""" % (PANEL, START))

    cols = []
    for w in WINDOWS:
        cols.append("""regr_slope(s.sret, b.mret) OVER (PARTITION BY s.code ORDER BY s.date
              ROWS BETWEEN %d PRECEDING AND CURRENT ROW) AS beta_%d""" % (w - 1, w))
        cols.append("""count(*) FILTER (s.sret IS NOT NULL AND b.mret IS NOT NULL)
              OVER (PARTITION BY s.code ORDER BY s.date
              ROWS BETWEEN %d PRECEDING AND CURRENT ROW) AS n_%d""" % (w - 1, w))
    sel = ',\n           '.join(cols)
    # 样本不足时置 NULL —— 次新股用 20 个点回归出来的 beta 是噪声，
    # 而下游是【按 beta 排序取前 50%】，噪声会直接污染选股。
    outc = ', '.join('CASE WHEN n_%d >= %d THEN round(beta_%d, 6) END AS beta_%d'
                     % (w, MIN_OBS[w], w, w) for w in WINDOWS)
    con.execute("""COPY (
        SELECT code, date, %s FROM (
          SELECT s.code, s.date,
           %s
          FROM stk s JOIN bench b USING (date)
        )) TO '%s' (FORMAT parquet, COMPRESSION zstd)""" % (outc, sel, OUT))

    r = con.execute("""SELECT count(*) n, count(DISTINCT code) codes,
        min(date) d0, max(date) d1, %s FROM read_parquet('%s')"""
                    % (', '.join('round(100.0*avg((beta_%d IS NOT NULL)::INT),1) AS "beta_%d 覆盖"'
                                 % (w, w) for w in WINDOWS), OUT)).df()
    print('std/beta_daily.parquet:')
    print(r.to_string(index=False))
    q = con.execute("""SELECT %s FROM read_parquet('%s') WHERE date >= DATE '2016-01-01'"""
                    % (', '.join('round(median(beta_%d),3) AS "beta_%d 中位"' % (w, w)
                                 for w in WINDOWS), OUT)).df()
    print('  中位值（应在 0.8~1.2 附近 —— beta 的定义就是相对基准）:')
    print(q.to_string(index=False))
    bad = con.execute("SELECT count(*) FROM read_parquet('%s') "
                      "GROUP BY code, date HAVING count(*)>1" % OUT).fetchall()
    print('  (code, date) 重复组 %d %s' % (len(bad), '✓' if not bad else '❌'))
    if bad:
        sys.exit(1)


if __name__ == '__main__':
    main()
