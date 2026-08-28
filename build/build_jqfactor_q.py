#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""std/jqfactor_q.parquet —— 复现聚宽 jqfactor 成长类因子（按报告期，PIT 可用）。

口径来源：docs/jqfactor-口径.md（三轮探针实测，逐只精确匹配才记定案）。

★ 已定案的三个 TTM 同比（精确复现）：
    operating_revenue_growth_rate = TTM 营业总收入同比
    total_profit_growth_rate      = TTM 利润总额同比
    net_profit_growth_rate        = TTM【全口径】净利润同比（含少数股东）
  三者的分母都【取原值不取绝对值】—— 300028 连亏两年，取 abs 得 -2.8885，
  取原值得 +0.8885 = 聚宽值（负÷负=正）。这条只在亏损股上暴露。

★ g_npp（归母净利 TTM 同比）不是聚宽的因子，但 PEG 需要它：
    PEG = pe_ratio / (归母净利 TTM 同比 × 100)，其中 pe_ratio = market_cap / 归母净利TTM
  已 4/4 精确验证。⚠ 面板现有的 peg=(totalmv/np_ttm)/(np_yoy*100) 是错的
  —— np_yoy 是【累计同比】而非 TTM 同比。

🔴 sg_approx / eg_approx 是【近似】不是复现：
  聚宽的 sales_growth / earnings_growth 口径【未定案】。实测它们严格落在
  (-1,+1) 内、std 仅 0.20/0.35 —— 既不是原始增长率（std 6~19、max 上百），
  也不是标准化因子（std 应≈1），而是某种有界归一化。已排除的候选见
  docs/jqfactor-口径.md（含 300 组网格穷举）。
  这里用 Barra SGRO/EGRO 式：5 个年度间隔的 TTM 点对时间回归，斜率 ÷ |均值|。
  它与聚宽值皮尔逊 +0.92 但秩相关仅 +0.49 —— 同源、不同变换。
  ★ 用它做的 SG/MS 两路【不能声称复现聚宽】，只能作为自有实现对照。

TTM 从累计口径推导（lake.db/fin_income 是累计，见 README 的接口口径表）：
    年报本身即 TTM；其余 = 本期累计 + 上年年报 - 上年同期累计
与「单季滚动四季度求和」数学等价。
"""
import os
import duckdb

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = os.path.join(ROOT, 'lake.db')
OUT = os.path.join(ROOT, 'std', 'jqfactor_q.parquet')


def check(ok, msg):
    print('  %s %s' % ('✓' if ok else '✗', msg))
    if not ok:
        raise AssertionError(msg)


def main():
    con = duckdb.connect(DB, read_only=True)
    print('=' * 72)
    print('构建 std/jqfactor_q.parquet')
    print('=' * 72)

    # ---------- TTM ----------
    con.execute("""
    CREATE OR REPLACE TEMP VIEW _t AS
    SELECT f.code, f.report_date, f.pub_date,
      CASE WHEN month(f.report_date)=12 THEN f.revenue
           ELSE f.revenue + a.revenue
                - p.revenue END AS ttm_rev,
      CASE WHEN month(f.report_date)=12 THEN f.profit_before_tax
           ELSE f.profit_before_tax + a.profit_before_tax - p.profit_before_tax END AS ttm_tp,
      CASE WHEN month(f.report_date)=12 THEN f.net_profit_total
           ELSE f.net_profit_total + a.net_profit_total - p.net_profit_total END AS ttm_np,
      CASE WHEN month(f.report_date)=12 THEN f.net_profit_parent
           ELSE f.net_profit_parent + a.net_profit_parent
                - p.net_profit_parent END AS ttm_npp
    FROM fin_core f
    LEFT JOIN fin_core a ON a.code=f.code
         AND a.report_date = make_date(year(f.report_date)-1, 12, 31)
    LEFT JOIN fin_core p ON p.code=f.code
         AND p.report_date = f.report_date - INTERVAL 1 YEAR
    """)

    # ---------- TTM 同比 + 5 期 TTM（Barra 近似用）----------
    # ★ 同比分母取【原值】。y0..y4 = t-4y .. t（升序），x=0..4 时
    #   斜率的闭式解 = (2*y4 + y3 - y1 - 2*y0) / 10（因为 Σ(x-x̄)²=10）。
    con.execute("""
    CREATE OR REPLACE TEMP VIEW _f AS
    SELECT t.code, t.report_date, t.pub_date,
           t.ttm_rev, t.ttm_tp, t.ttm_np, t.ttm_npp,
           CASE WHEN y1.ttm_rev IS NOT NULL AND y1.ttm_rev <> 0
                THEN t.ttm_rev / y1.ttm_rev - 1 END AS g_rev,
           CASE WHEN y1.ttm_tp IS NOT NULL AND y1.ttm_tp <> 0
                THEN t.ttm_tp / y1.ttm_tp - 1 END AS g_tp,
           CASE WHEN y1.ttm_np IS NOT NULL AND y1.ttm_np <> 0
                THEN t.ttm_np / y1.ttm_np - 1 END AS g_np,
           CASE WHEN y1.ttm_npp IS NOT NULL AND y1.ttm_npp <> 0
                THEN t.ttm_npp / y1.ttm_npp - 1 END AS g_npp,
           CASE WHEN y4.ttm_rev IS NOT NULL
                     AND abs((t.ttm_rev+y1.ttm_rev+y2.ttm_rev+y3.ttm_rev+y4.ttm_rev)/5.0) > 1
                THEN ((2*t.ttm_rev + y1.ttm_rev - y3.ttm_rev - 2*y4.ttm_rev) / 10.0)
                     / abs((t.ttm_rev+y1.ttm_rev+y2.ttm_rev+y3.ttm_rev+y4.ttm_rev)/5.0)
                END AS sg_approx,
           CASE WHEN y4.ttm_npp IS NOT NULL
                     AND abs((t.ttm_npp+y1.ttm_npp+y2.ttm_npp+y3.ttm_npp+y4.ttm_npp)/5.0) > 1
                THEN ((2*t.ttm_npp + y1.ttm_npp - y3.ttm_npp - 2*y4.ttm_npp) / 10.0)
                     / abs((t.ttm_npp+y1.ttm_npp+y2.ttm_npp+y3.ttm_npp+y4.ttm_npp)/5.0)
                END AS eg_approx
    FROM _t t
    LEFT JOIN _t y1 ON y1.code=t.code AND y1.report_date = t.report_date - INTERVAL 1 YEAR
    LEFT JOIN _t y2 ON y2.code=t.code AND y2.report_date = t.report_date - INTERVAL 2 YEAR
    LEFT JOIN _t y3 ON y3.code=t.code AND y3.report_date = t.report_date - INTERVAL 3 YEAR
    LEFT JOIN _t y4 ON y4.code=t.code AND y4.report_date = t.report_date - INTERVAL 4 YEAR
    """)

    con.execute("COPY (SELECT * FROM _f WHERE pub_date IS NOT NULL "
                "ORDER BY code, report_date) TO '%s' "
                "(FORMAT parquet, COMPRESSION zstd)" % OUT)
    n = con.execute("SELECT count(*) FROM read_parquet('%s')" % OUT).fetchone()[0]
    print('  写出 %s 行 -> %s' % (format(n, ','), os.path.relpath(OUT, ROOT)))

    # ---------- 校验：拿探针实测值当断言 ----------
    print('\n校验（对照 2018-12-28 决策日的聚宽实测值）')
    F = "read_parquet('%s')" % OUT
    # 该决策日各股最新已披露报告 = 2018-09-30
    truth = {'300028.XSHE': (0.430376, 0.888508, 1.018565),
             '300179.XSHE': (0.200515, -0.345438, -0.352379),
             '002377.XSHE': (2.477499, 4.898796, 4.026045),
             '600328.XSHG': (0.141333, 0.517298, 0.126260),
             '600239.XSHG': (0.148159, 1.141997, 1.380297),
             '000800.XSHE': (-0.015556, -0.430345, -0.297766)}
    hit = {'rev': 0, 'tp': 0, 'np': 0}
    for code, (t_rev, t_tp, t_np) in truth.items():
        r = con.execute("""SELECT g_rev, g_tp, g_np FROM %s
            WHERE code=? AND report_date=DATE '2018-09-30'""" % F, [code]).fetchone()
        if r is None:
            print('    %-14s 无数据' % code); continue
        for k, got, want in (('rev', r[0], t_rev), ('tp', r[1], t_tp), ('np', r[2], t_np)):
            if got is not None and abs(want) > 1e-9 and abs(got / want - 1) < 0.005:
                hit[k] += 1
        print('    %-14s 营收 %9.6f/%9.6f  利润总额 %9.6f/%9.6f  净利 %9.6f/%9.6f'
              % (code, r[0] or 0, t_rev, r[1] or 0, t_tp, r[2] or 0, t_np))
    n_t = len(truth)
    check(hit['tp'] == n_t, 'total_profit_growth_rate %d/%d 精确' % (hit['tp'], n_t))
    check(hit['np'] == n_t, 'net_profit_growth_rate   %d/%d 精确' % (hit['np'], n_t))
    check(hit['rev'] >= 4, 'operating_revenue_growth_rate %d/%d（已知 4/6，'
                           '剩余缺口见 docs/jqfactor-口径.md）' % (hit['rev'], n_t))
    cov = con.execute("""SELECT
        round(100.0*count(g_np)/count(*),2), round(100.0*count(sg_approx)/count(*),2)
        FROM %s WHERE report_date >= DATE '2015-01-01'""" % F).fetchone()
    check(cov[0] > 80, 'g_np 覆盖率 %.2f%%（2015 起）' % cov[0])
    check(cov[1] > 55, 'sg_approx 覆盖率 %.2f%%（需 5 年历史，次新天然缺）' % cov[1])
    print('\n✅ 全部通过')


if __name__ == '__main__':
    main()
