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
# ★ 起点由【基准指数的数据起点】决定，不是随便设的：
#   sh000300 覆盖 2005-01-04 ~ 今（基日 2004-12-31，发布前可回溯计算）。
#   配 MIN_OBS[252]=120，最早可用的 beta_252 落在 2005 年年中。
#
#   曾错设为 2013-01-01（当时只为对标 JQ 的 2016 起回测，往前留 3 年缓冲）。
#   后果：红利低波把 beta_daily 做【内连接】，2005-2012 匹配不到行
#   -> 候选池被清空 -> 连续 8 年空仓，而回测照常出报告、
#   年化 10.85% 看着完全合理。占 38% 的时间空仓却毫无提示 ——
#   这是本项目又一次「静默失败」。
#   不要为了某个具体对标区间设这个常量，它应当由数据可得性决定。
#   想再往前只能换基准（上证综指有 2003 起），但那会让 beta 语义
#   中途改变、跳期不可比 —— 不做。
START = '2005-01-01'

# ★★ 【回测起点应从 2006-01 起，不是 2005-01】
#   沪深300 基日 2004-12-31、首个发布值 2005-01-04 —— 这个指数在那之前
#   **不存在**，所以 beta 无法往前补（tdx 里 sh000300/sz399300 都是 2005-01-04 起，
#   786 个指数里没有任何沪深300 变体更早）。
#   配 MIN_OBS[252]=120，beta_252 覆盖率实测爬坡：
#       2005-01 ~ 06   0.0%      <- 完全没有
#       2005-07       71.1%
#       2005-08 起    98%+       <- 稳定
#   所以任何依赖 beta_252 的策略，回测起点早于 2005-08 都会欠配/空仓，
#   指标被现金稀释而报告看着正常（实测组合版 2005 年平均仓位仅 74.3%）。
#   **建议起点 2006-01-01**（比爬坡结束再留 5 个月缓冲）。
#   想更早只能换基准（上证综指有 2003 起），但那会让 beta 语义中途改变、
#   跨期不可比 —— 不做。


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

    n_s = con.execute('SELECT count(*) FROM stk').fetchone()[0]
    d0 = con.execute('SELECT min(date) FROM stk').fetchone()[0]
    print('个股样本: %s 行, 起点 %s' % (format(n_s, ','), d0))
    assert str(d0)[:4] <= '2005', \
        '个股样本起点 %s 晚于 2005 —— 面板或 START 被改窄' % d0

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
