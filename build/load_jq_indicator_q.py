#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""raw/jq/fundamentals_indicator_q.parquet -> std/fin_indicator_q.parquet

这是 `get_fundamentals(query(indicator))` 的按季全市场抽取结果，
与策略里 `get_history_fundamentals(fields=[indicator.roe, indicator.eps])`
**同源同口径** —— 是权威值，不是本地推算。

## 已实证的口径（不是猜的）

  · `roe` / `eps` 都是**单季**值。
    茅台 2023 四期 roe = 10.00 / 7.25 / 8.07 / 10.08，四期相加 35.40
    ≈ 全年累计 34.65；eps 四期相加 59.49 ≈ 全年。累计值不会是这个形态。

  · `roe` 的分母是**平均净资产 (期初+期末)/2**，不是期末。
    257,525 条全样本比对（相对误差 <1% 记命中）：
        平均净资产  87.35%   ← 就是它
        期末净资产  41.22%   ← 本地原先用的，所以只能到 72.1% 选股命中率
        期初净资产  40.08%
    茅台 2023 逐条：权威 10.00/7.25/8.07/10.08，平均净资产算得
    10.0028/7.2451/8.0701/10.0849，对到小数点后四位。
    余下 12.65% 不吻合的估计是证监会「加权平均」(按月加权计资本变动)，
    与简单平均有差 —— 但既然权威值在手，**不再推算，直接用**。

  · `roe` 单位是**百分数**(10.00 = 10%)。本地 std 层统一存**小数**(0.10)，
    与既有 `roe_q` 约定一致，避免同名不同标尺。转换在本脚本完成，只此一处。

## 增长率字段的期间口径（已实测，2026-08-27）

`inc_*_year_on_year` 系列 = **单季同比**，不是累计同比。

判据用的是 **Q1 反证**（增长率不能像 roe/eps 那样"四期相加≈全年"来验）：
Q1 报告的累计值就等于单季值，所以两个候选口径在 Q1 上必须都命中；
往后三个报告期它们才分岔。实测命中率（±0.5pp，与 fin_quarterly 反推值比对）：

    报告期        n        累计口径    单季口径
    Q1 (03-31)   62,947    96.6%      96.6%     <- 两者相同，符合预期
    Q2 (06-30)   59,333     4.4%      95.7%
    Q3 (09-30)   59,055     3.2%      95.6%
    年报(12-31)   32,400     2.4%      94.3%

分子口径顺带查清：`inc_net_profit_year_on_year` 的分子是**净利润(全部，含少数股东)**，
本地只有归母，所以只对上 28%；归母那个字段 `inc_net_profit_to_shareholders_year_on_year`
对上 94.8%。要与本地 np_cum/np_q 比较时用后者。

`inc_*_annual` **口径仍不明**：既不是累计同比也不是单季同比（命中率均约 1%），
"本期累计/上年年报-1"(0.6%)、"三年 CAGR"(1.2%) 也都不是。分布看着像增长率
（中位 0.031 / p5 -0.538 / p95 1.061）。目前无下游使用，**保持不接**。

## 仍然不往下游接

即使期间口径已验，`inc_*_year_on_year` 也**不接进 panel 去替换 np_q_yoy/rev_q_yoy** ——
两者现在已知是同一口径（都是单季同比），没有替换的必要；真要替换需先比对覆盖率与修订史。

## 局限

`get_fundamentals(statDate=)` 对同一 (code, statDate) 只返回一条，
**没有多版本修订史**。修订史要走 finance.STK_* 原始表（已有）。
本表解决口径，不解决修订史。
"""
import os
import sys

import duckdb

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, 'raw', 'jq', 'fundamentals_indicator_q.parquet')
OUT = os.path.join(ROOT, 'std', 'fin_indicator_q.parquet')

# ★ 单位统一：std 层所有【比率】都存小数（0.05 = 5%），不存百分数。
#
# ⚠️ 原先用【白名单】PCT_COLS 只列 5 个字段，结果同一张表里两种标尺混放 ——
#    `inc_return`(水平值) 被转成小数，而 `inc_*_year_on_year`(增长率) 留在百分数。
#    红利价值策略写 `inc_return BETWEEN 5 AND 100` 时选出 0 只，才暴露出来。
#    白名单的问题是「漏了不会报错」；改成【例外清单】：默认全转，只列不该转的。
#
# 实测原表 27 个候选列【全部】是百分数（中位 0.78~100.84，见下），
# 只有下面 4 个是金额/绝对值：
NON_PCT = ('eps', 'adjusted_profit', 'operating_profit', 'value_change_profit')
_ID_COLS = ('id', 'code', 'statDate', 'pubDate', 'statDate.1')


def main():
    if not os.path.exists(SRC):
        sys.exit('缺少 %s —— 先在研究环境跑 raw/jq/_ingest/extract_jq_indicator_q.py' % SRC)
    con = duckdb.connect(':memory:')
    cols = [r[0] for r in con.execute(
        "DESCRIBE SELECT * FROM read_parquet('%s') LIMIT 1" % SRC).fetchall()]

    # 列名 -> snake_case；statDate/pubDate 改成 std 层通用的 report_date/pub_date
    ren = {'statDate': 'report_date', 'pubDate': 'pub_date'}
    sel = []
    for c in cols:
        if c == 'id':
            continue
        name = ren.get(c, c)
        if c not in NON_PCT and c not in _ID_COLS:
            sel.append('"%s" / 100.0 AS %s' % (c, name))      # 百分数 -> 小数
        elif c in ('statDate', 'pubDate'):
            sel.append('"%s"::DATE AS %s' % (c, name))
        else:
            sel.append('"%s" AS %s' % (c, name))

    con.execute("""COPY (SELECT %s FROM read_parquet('%s')
                   WHERE code IS NOT NULL AND statDate IS NOT NULL
                   ORDER BY code, statDate) TO '%s'
                   (FORMAT parquet, COMPRESSION zstd)"""
                % (', '.join(sel), SRC, OUT))

    q = con.execute("""SELECT count(*) n, count(DISTINCT code) codes,
        min(report_date) d0, max(report_date) d1,
        sum((pub_date IS NULL)::INT) no_pub,
        sum((pub_date <= report_date)::INT) pub_placeholder,
        sum((roe IS NOT NULL)::INT) roe_n, sum((eps IS NOT NULL)::INT) eps_n
        FROM read_parquet('%s')""" % OUT).fetchone()
    n, codes, d0, d1, no_pub, ph, roe_n, eps_n = q
    print('std/fin_indicator_q.parquet: %s 行, %s 只, %s ~ %s'
          % (format(n, ','), format(codes, ','), d0, d1))
    print('  roe 非空 %s (%.1f%%) | eps 非空 %s (%.1f%%)'
          % (format(roe_n, ','), 100.0 * roe_n / n, format(eps_n, ','), 100.0 * eps_n / n))

    # PIT 前提：pub_date 必须真实。pub_date <= report_date 说明是占位值，
    # 用它做 as-of 就是未来函数 —— 必须报出来，不能默默放过。
    print('  pub_date 缺失 %d | 占位(pub_date <= report_date) %d (%.2f%%)'
          % (no_pub, ph, 100.0 * ph / n))
    if no_pub:
        sys.exit('❌ pub_date 有缺失，本表不能用于 PIT as-of')
    dup = con.execute("""SELECT count(*) FROM (
        SELECT code, report_date FROM read_parquet('%s')
        GROUP BY 1,2 HAVING count(*) > 1)""" % OUT).fetchone()[0]
    print('  (code, report_date) 重复组 %d' % dup)
    if dup:
        sys.exit('❌ 存在重复主键，as-of join 会放大行数')
    print('  ✓ 可用于 PIT as-of')


if __name__ == '__main__':
    main()
