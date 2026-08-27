#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""限售股解禁明细 + 财报预约披露日 -> std/{share_unlock,report_disclosure}.parquet

## ★ 源数据有裸 \\r，必须先清洗

聚宽 `STK_LIMITED_SHARES_LIST` 的 `shareholder_name` 等文本字段里嵌了
**孤立的回车符**（不是 \\r\\n），例如：

    长沙中\\r南升华\\r科技发\\r展有限\\r公司

`to_csv` 的 QUOTE_MINIMAL 只对含分隔符/引号/\\n 的字段加引号，**不管裸 \\r**，
于是写出去的 CSV 里一条逻辑行被拆成多行。后果是**静默多读行**：

| 年 | 抽取时写入 | 直接读 | 裸 \\r 个数 |
|---|---|---|---|
| 2009 | 2,619 | 2,630 | **11** ✓ 精确匹配 |
| 2015 | 4,246 | 4,251 | 5 |

而且 DuckDB 的 `read_csv_auto` 在这些文件上**直接探测失败**（9/20 个文件），
只有 `strict_mode=false` 能读 —— 但那会把拆开的行当成独立行，
行数对不上却不报错。**这正是最危险的一类：能跑、但数据是错的。**

所以本脚本先按字节把裸 \\r 去掉（不动 \\r\\n），再交给解析器，
并**断言行数与抽取时一致**。
"""
import glob
import gzip
import io as _io
import os
import re
import sys

import duckdb
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, 'raw', 'jq', '_ingest', 'downloads')
STD = os.path.join(ROOT, 'std')

# 抽取时每年写入的行数（用于断言清洗后行数一致，防止静默多读/少读）
EXPECT_UNLOCK = {
    2007: 16, 2008: 1110, 2009: 2619, 2010: 4123, 2011: 7330, 2012: 946,
    2013: 1353, 2014: 2205, 2015: 4246, 2016: 3616, 2017: 4215, 2018: 3960,
    2019: 3187, 2020: 3404, 2021: 4271, 2022: 4201, 2023: 3443, 2024: 2119,
    2025: 2707, 2026: 1708,
}
_BARE_CR = re.compile(rb'\r(?!\n)')


def read_clean(path):
    """读 .csv.gz，先去掉裸 \\r 再解析。"""
    raw = gzip.open(path, 'rb').read()
    n_cr = len(_BARE_CR.findall(raw))
    if n_cr:
        raw = _BARE_CR.sub(b'', raw)
    df = pd.read_csv(_io.BytesIO(raw), low_memory=False)
    return df, n_cr


def load(prefix, out_name, expect=None, date_cols=()):
    fs = sorted(glob.glob(os.path.join(SRC, '%s_[12][0-9][0-9][0-9].csv.gz' % prefix)))
    if not fs:
        sys.exit('缺少 %s/%s_YYYY.csv.gz' % (SRC, prefix))
    parts, total_cr, bad = [], 0, []
    for f in fs:
        y = int(re.search(r'_(\d{4})\.csv\.gz$', f).group(1))
        df, n_cr = read_clean(f)
        total_cr += n_cr
        if expect and y in expect and len(df) != expect[y]:
            bad.append('%d: 读到 %d 行，抽取时写入 %d 行' % (y, len(df), expect[y]))
        parts.append(df)
    allf = pd.concat(parts, ignore_index=True)
    for c in date_cols:
        if c in allf.columns:
            allf[c] = pd.to_datetime(allf[c], errors='coerce')
    out = os.path.join(STD, out_name)
    allf.to_parquet(out, index=False, compression='zstd')
    print('std/%s: %s 行 %d 列，清掉裸 \\r %d 个'
          % (out_name, format(len(allf), ','), len(allf.columns), total_cr))
    if bad:
        # 行数对不上就是解析没修干净 —— 必须失败，不能产出「看着正常」的数据
        print('  ❌ 行数与抽取时不一致:')
        for b in bad:
            print('     ' + b)
        sys.exit(1)
    print('  ✓ 每年行数与抽取时一致')
    return out


def main():
    con = duckdb.connect(':memory:')

    # ---- 1) 限售股解禁明细 ----
    p = load('limited_shares', 'share_unlock.parquet', EXPECT_UNLOCK,
             ('pub_date', 'expected_unlimited_date', 'actual_unlimited_date'))
    print(con.execute("""SELECT count(*) 行, count(DISTINCT code) 股票,
        sum((actual_unlimited_date IS NULL)::INT) 无实际解禁日,
        round(100.0*avg((actual_unlimited_date IS NULL)::INT),1) 占比,
        min(expected_unlimited_date)::DATE 最早预计,
        max(expected_unlimited_date)::DATE 最晚预计
        FROM read_parquet('%s')""" % p).df().to_string(index=False))
    print(con.execute("""SELECT limited_reason 限售原因, count(*) 行数,
        sum((actual_unlimited_date IS NOT NULL)::INT) 有实际解禁日
        FROM read_parquet('%s') GROUP BY 1 ORDER BY 2 DESC LIMIT 8""" % p)
          .df().to_string(index=False))

    # ---- 2) 财报预约披露日 ----
    q = load('report_disclose', 'report_disclosure.parquet', None,
             ('end_date', 'appoint_date', 'first_date', 'second_date',
              'third_date', 'pub_date'))
    print(con.execute("""SELECT count(*) 行, count(DISTINCT code) 股票,
        min(end_date)::DATE 最早报告期, max(end_date)::DATE 最晚报告期,
        sum((first_date IS NOT NULL)::INT) 改期过1次,
        sum((second_date IS NOT NULL)::INT) 改期过2次,
        sum((third_date IS NOT NULL)::INT) 改期过3次
        FROM read_parquet('%s')""" % q).df().to_string(index=False))
    # 预约日 vs 实际公告日：这决定它能不能当「已知何时披露」用
    print(con.execute("""SELECT count(*) 可比,
        round(median(date_diff('day',
          coalesce(third_date, second_date, first_date, appoint_date),
          pub_date))) 最终预约到实际_天数中位,
        round(100.0*avg((pub_date <= coalesce(third_date, second_date,
          first_date, appoint_date))::INT),1) 按期或提前披露占比
        FROM read_parquet('%s') WHERE pub_date IS NOT NULL
          AND appoint_date IS NOT NULL""" % q).df().to_string(index=False))


if __name__ == '__main__':
    main()
