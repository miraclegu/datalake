#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""业绩快报 / 预披露 / 追溯调整 -> std/fin_express.parquet

## 数据本来就在，只是被主财务表的过滤剔掉了

`load_jq_financials.py` 有一条「只要 source='定期报告'」的过滤。
那条过滤对**主财务表是对的** —— 实测聚宽 `get_fundamentals` 的 pub_date
**99.8% 对齐定期报告**（业绩快报仅 11 行、追溯调整仅 3 行），
即聚宽自己也不用快报，我们跟它一致才能对标。

但快报作为**独立数据源**是有价值的，所以单独落一张表：

| source | 行数 | 股票 | 报告期范围 | 是什么 |
|---|---|---|---|---|
| 业绩快报 | 27,349 | 4,060 | 2004-12-31 ~ 2026-06-30 | 正式财报**之前**的预披露 |
| 预披露公告 | 14,814 | 2,996 | 2006-12-31 ~ 2026-03-31 | 更早的预披露 |
| 追溯调整 | 4,204 | 1,550 | 2005-12-31 ~ 2025-09-30 | 事后重述 |

实测 27,285/27,349 条业绩快报是该 (code, report_date) 的**首版**
（即比定期报告早），所以它的用途是「抢先」——
而 4,204 条追溯调整里 2,525 是最新版，用途是「看财报被改了什么」。

## ★ 用它做策略必须想清两件事

1. **不要和主财务表混用同一个 as-of**。主表是「定期报告」口径；
   快报数值口径可能不同（未审计），混起来会让同一指标在时间轴上跳变。
   正确用法是**单独取快报、单独判断**，或显式标注数据来源。
2. **快报本身可能被修正**。同一 (code, report_date) 可能先出快报、
   再出定期报告、数值不同。所以这张表保留 `source` 列，消费方自己决定
   信哪个 —— 不在加载时替它决定。
"""
import glob
import os
import sys

import duckdb

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, 'raw', 'jq', '_ingest', 'downloads')
OUT = os.path.join(ROOT, 'std', 'fin_express.parquet')

SOURCES = ('业绩快报', '预披露公告', '追溯调整')
# 只留策略可能用到的核心列（原表 53 列，多是股东类型细分）
KEEP = ['code', 'source', 'report_date', 'pub_date',
        'net_profit', 'main_income', 'total_assets', 'equities',
        'eps', 'roe', 'bps', 'adjusted_profit',
        'np_yoy_prev', 'inc_ratio']


def main():
    # ★ glob 必须锚定 4 位年份：`indicator_*.csv.gz` 会误匹配 P1-a 的
    #   `indicator_q_2003.csv.gz`（同目录、不同 schema）。这个坑踩过一次。
    fs = sorted(glob.glob(os.path.join(SRC, 'indicator_[12][0-9][0-9][0-9].csv.gz')))
    if not fs:
        sys.exit('缺少 %s/indicator_YYYY.csv.gz' % SRC)
    con = duckdb.connect(':memory:')
    lst = "','".join(fs)
    cols = [r[0] for r in con.execute(
        "DESCRIBE SELECT * FROM read_csv_auto(['%s'], union_by_name=true) LIMIT 1"
        % lst).fetchall()]

    # 原表列名带 _this_year/_last_year 后缀，映射到干净名字
    ren = {'net_profit_this_year': 'net_profit', 'main_income_this_year': 'main_income',
           'total_assets_this_year': 'total_assets', 'equities_this_year': 'equities',
           'eps_this_year': 'eps', 'roe_this_year': 'roe', 'bps_this_year': 'bps',
           'adjusted_profit_this_year': 'adjusted_profit',
           'net_profit_last_year': 'net_profit_prev', 'end_date': 'report_date'}
    sel = ['code', 'source']
    if 'end_date' in cols:
        sel.append('end_date::DATE AS report_date')
    if 'pub_date' in cols:
        sel.append('pub_date::DATE AS pub_date')
    for src, dst in ren.items():
        if src in cols and dst not in ('report_date',):
            sel.append('"%s" AS %s' % (src, dst))
    # roe 原表是百分数 -> 转小数，与 std/fin_indicator_q 的约定一致
    sel = [x.replace('"roe_this_year" AS roe', '"roe_this_year" / 100.0 AS roe')
           for x in sel]

    srcs = ','.join("'%s'" % s for s in SOURCES)
    con.execute("""COPY (
        SELECT * EXCLUDE (_rn) FROM (
          SELECT %s, row_number() OVER (
                   PARTITION BY code, end_date::DATE, source, pub_date::DATE
                   ORDER BY source_id NULLS LAST) AS _rn
          FROM read_csv_auto(['%s'], union_by_name=true)
          WHERE source IN (%s) AND code IS NOT NULL AND end_date IS NOT NULL
        ) WHERE _rn = 1
        ORDER BY code, report_date, pub_date) TO '%s' (FORMAT parquet, COMPRESSION zstd)"""
                % (', '.join(sel), lst, srcs, OUT))

    r = con.execute("""SELECT count(*), count(DISTINCT code),
        min(report_date), max(report_date),
        sum((pub_date IS NULL)::INT), sum((pub_date <= report_date)::INT)
        FROM read_parquet('%s')""" % OUT).fetchone()
    print('std/fin_express.parquet: %s 行, %s 只, 报告期 %s ~ %s'
          % (format(r[0], ','), format(r[1], ','), str(r[2])[:10], str(r[3])[:10]))
    print('  pub_date 缺失 %d | 占位(pub_date <= report_date) %d' % (r[4], r[5]))
    print(con.execute("""SELECT source, count(*) AS 行数, count(DISTINCT code) AS 股票,
        min(report_date) AS 最早期, max(report_date) AS 最晚期
        FROM read_parquet('%s') GROUP BY 1 ORDER BY 2 DESC""" % OUT).df().to_string(index=False))

    # ---- 快报比定期报告早多少天：这是「抢先」的价值所在，必须量出来 ----
    fin = os.path.join(ROOT, 'raw', 'jq', 'financials', 'indicator.parquet')
    if os.path.exists(fin):
        print()
        print(con.execute("""
          SELECT e.source, count(*) AS 可比,
            round(median(date_diff('day', e.pub_date, f.pub_date::DATE))) AS 领先定期报告_天数中位,
            round(quantile_cont(date_diff('day', e.pub_date, f.pub_date::DATE), 0.1)) AS P10,
            round(quantile_cont(date_diff('day', e.pub_date, f.pub_date::DATE), 0.9)) AS P90
          FROM read_parquet('%s') e
          JOIN read_parquet('%s') f ON f.code=e.code AND f.report_date::DATE=e.report_date
          WHERE e.pub_date IS NOT NULL GROUP BY 1 ORDER BY 2 DESC"""
                          % (OUT, fin)).df().to_string(index=False))
        print('  （正数 = 该 source 比定期报告【更早】公布）')

    dup = con.execute("""SELECT count(*) FROM (
        SELECT code, report_date, source, pub_date FROM read_parquet('%s')
        GROUP BY 1,2,3,4 HAVING count(*)>1)""" % OUT).fetchone()[0]
    print('\n  (code, report_date, source, pub_date) 重复组 %d %s'
          % (dup, '✓' if dup == 0 else '❌'))
    if dup:
        sys.exit(1)


if __name__ == '__main__':
    main()
