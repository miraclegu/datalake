#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""finance.STK_CAPITAL_CHANGE -> std/share_change.parquet

## 为什么要这张表

裁判「流通股本」到底 tdx 对还是聚宽 valuation 对。
此前的结论链（全部实测）：
  · `valuation.pb_ratio` 含 **6.58% 前视**（用了当日未公告的财报）→ 本地 pb 更正确
  · `valuation.capitalization`（总股本）**tdx 更准** ——
    用 balance.paidin_capital 裁判，本地更接近会计口径 70.6%、JQ 仅 29.4%
  · `circulating_cap`（流通股本）**当时无法判定** ——
    两个源各自 100% 内部自洽，而资产负债表只有【总】股本、没有流通股本

这张事件表给出解禁/增发的权威日期与数量，是唯一的独立裁判。

## ★ 关键：这张表里有【两个】流通股口径，别混用

    share_rmb          流通【A股】（人民币普通股）
    share_trade_total  流通股【总计】（可能含 B 股 / H 股）

对同时有 A+B 股（万科A/B、深纺织A/B 等）或 A+H 的公司，两者**不相等**。
A 股策略按市值选股用的是【流通A股市值】，所以基准口径应是 `share_rmb`。
**tdx 与聚宽 valuation 的 3.75% 差异很可能就是这个口径差** ——
即两边都没错，只是选了不同定义。本脚本会把这一点量化出来。

## 语义（实测后填）

`change_date` = 股本【变动生效】日；`pub_date` = 公告日。
做 PIT as-of 时该用哪个取决于用途：
  · 复现「当时的流通市值」→ 用 change_date（市场当天就按新股本计价）
  · 严格避免未来函数 → 用 greatest(change_date, pub_date)
本脚本两个都保留，由消费方选。
"""
import glob
import os
import sys

import duckdb

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, 'raw', 'jq', '_ingest', 'downloads')
OUT = os.path.join(ROOT, 'std', 'share_change.parquet')

# 只留与股本口径相关的列 —— 49 列里大部分是股东类型细分，用不上
KEEP = ['code', 'change_date', 'pub_date', 'change_reason',
        'share_total',        # 总股本
        'share_trade_total',  # 流通股总计（可能含 B/H）
        'share_rmb',          # 流通 A 股 ★ A 股策略的基准口径
        'share_non_trade',    # 非流通股
        'share_limited',      # 限售股
        'share_b', 'share_h', # B 股 / H 股（用于解释 share_trade_total 与 share_rmb 的差）
        'share_management',   # 管理层持股
        ]


def main():
    fs = sorted(glob.glob(os.path.join(SRC, 'stk_capital_change*.csv.gz')))
    if not fs:
        sys.exit('缺少 %s/stk_capital_change*.csv.gz —— 先在研究环境跑 '
                 'raw/jq/_ingest/extract_jq_share_change.py' % SRC)
    con = duckdb.connect(':memory:')
    lst = "','".join(fs)
    cols = [r[0] for r in con.execute(
        "DESCRIBE SELECT * FROM read_csv_auto(['%s'], union_by_name=true) LIMIT 1"
        % lst).fetchall()]
    keep = [c for c in KEEP if c in cols]
    missing = [c for c in KEEP if c not in cols]
    if missing:
        print('⚠ 源表缺以下列（将跳过）: %s' % missing)
    # 同一 (code, change_date) 可能多条（多次公告修正）—— 取公告日最新的那条。
    # 与 ASOF 右表键唯一这条不变量一致（见 build_panel_daily.py 文件头）。
    con.execute("""COPY (
        SELECT * EXCLUDE (_rn) FROM (
          SELECT %s, row_number() OVER (
                   PARTITION BY code, change_date::DATE ORDER BY pub_date DESC) AS _rn
          FROM read_csv_auto(['%s'], union_by_name=true)
          WHERE code IS NOT NULL AND change_date IS NOT NULL
        ) WHERE _rn = 1
        ORDER BY code, change_date) TO '%s' (FORMAT parquet, COMPRESSION zstd)"""
                % (', '.join(keep), lst, OUT))

    n, ncode, d0, d1 = con.execute(
        "SELECT count(*), count(DISTINCT code), min(change_date), max(change_date) "
        "FROM read_parquet('%s')" % OUT).fetchone()
    print('std/share_change.parquet: %s 行, %s 只, %s ~ %s'
          % (format(n, ','), format(ncode, ','), str(d0)[:10], str(d1)[:10]))

    # ---- 两个流通口径差多少：这决定 tdx/聚宽的 3.75% 分歧是不是口径差 ----
    if 'share_rmb' in keep and 'share_trade_total' in keep:
        r = con.execute("""SELECT count(*) n,
            sum((abs(share_trade_total - share_rmb) > 0.001*share_rmb)::INT) AS 两口径不等,
            count(DISTINCT CASE WHEN abs(share_trade_total - share_rmb) > 0.001*share_rmb
                  THEN code END) AS 涉及股票
            FROM read_parquet('%s') WHERE share_rmb > 0""" % OUT).fetchone()
        print('  流通A股 vs 流通股总计: %d/%d 行不等 (%.2f%%), 涉及 %d 只'
              % (r[1], r[0], 100.0 * r[1] / max(r[0], 1), r[2]))

    # ---- PIT 前提：change_date 与 pub_date 的关系 ----
    if 'pub_date' in keep:
        r = con.execute("""SELECT
            sum((pub_date IS NULL)::INT),
            sum((pub_date::DATE < change_date::DATE)::INT),
            round(median(date_diff('day', change_date::DATE, pub_date::DATE)))
            FROM read_parquet('%s')""" % OUT).fetchone()
        print('  pub_date 缺失 %d | 公告早于生效 %d 条 | 生效到公告中位 %s 天'
              % (r[0] or 0, r[1] or 0, r[2]))

    dup = con.execute("""SELECT count(*) FROM (SELECT code, change_date FROM read_parquet('%s')
        GROUP BY 1,2 HAVING count(*)>1)""" % OUT).fetchone()[0]
    print('  (code, change_date) 重复组 %d %s' % (dup, '✓' if dup == 0 else '❌ as-of 会放大行数'))
    if dup:
        sys.exit(1)


if __name__ == '__main__':
    main()
