"""聚宽 → 限售股解禁明细 + 财报预约披露日（在研究环境跑）。

把整个文件粘进一个 cell 执行。**跑完请执行文末的清理 cell。**

两张表都小，一次抽完。表名与列名**已由全表探测确认**（见 raw/jq/_ingest/jq_tables.csv），
不是猜的。

## 1) STK_LIMITED_SHARES_LIST —— 限售股解禁明细（15 列）

    code, pub_date, shareholder_name,
    expected_unlimited_date / _number / _ratio     预计解禁
    actual_unlimited_date   / _number / _ratio     实际解禁
    limited_reason, trade_condition

**为什么要它**：裁判「流通股本」到底 tdx 对还是聚宽 valuation 对。
此前卡在这里 —— 资产负债表只有【总】股本，没有流通股本，
两个源各自 100% 内部自洽（tdx 的 turnover 列 ↔ tdx 股本 100%；
聚宽 turnover_ratio ↔ circulating_cap 99.95%），**没有独立裁判**。
解禁明细给出每笔解禁的日期与数量，累加即可重建每日真实流通股本。

**PIT 上很干净**：`expected_*` 可提前知道，`actual_*` 才是真生效 ——
两者分开，不必像别的表那样纠结「哪个日期算已知」。

## 2) STK_REPORT_DISCLOSURE —— 财报预约披露日（8 列）

    code, end_date, appoint_date, first_date, second_date, third_date, pub_date

**为什么要它**：现在我们只能等 `pub_date` 到了才用财务数据。
有了预约披露日，可以严格区分「已知【何时会】披露」与「已披露」——
前者本身是可交易信息（临近披露的不确定性），后者才是数据可用。
`first/second/third_date` 是变更后的预约日（A股允许改期），
所以**要用最后一次变更**，不能只看 appoint_date。

## 抽取方式

两张表都按 `id` 分页（探测确认都有 id 列）。
`STK_LIMITED_SHARES_LIST` 预计几十万行、`STK_REPORT_DISCLOSURE` 十几万行，
按【年】切分抽取以便中断续跑（每年一个文件，存在即跳过）。
"""
import gc
import os
import tarfile
import time

import pandas as pd
from jqdata import *          # noqa: F401,F403  星号导入必须在模块级
from jqdata import finance

OUT = 'jq_shares_disclosure'
PAGE = 3000
MIN_FREE_MB = 200

# (标签, 表, 用于按年切分的日期列)
# 日期列名由全表探测确认，不是猜的。
TABLES = [
    ('limited_shares', finance.STK_LIMITED_SHARES_LIST, 'pub_date'),
    ('report_disclose', finance.STK_REPORT_DISCLOSURE, 'end_date'),
]
YEARS = list(range(2003, 2027))

QUOTA_WORDS = ('额度不足', '额度已用', '配额', 'quota exceeded', 'quota limit',
               '超过限制', '调用次数', 'rate limit', 'too many requests')


def free_mb(path='.'):
    """剩余磁盘 MB。研究环境的 statvfs 曾返回 1e9 MB(1PB) 这种明显不可信的值，
    超过 100TB 一律视为不可用 —— 返回 None 让调用方跳过检查并说明，
    而不是假装检查通过。假的校验比没有校验更危险。"""
    try:
        st = os.statvfs(path)
        mb = st.f_bavail * st.f_frsize / 1048576.0
    except Exception:                                       # noqa: BLE001
        return None
    return None if mb > 100 * 1024 * 1024 else mb


def is_quota_error(e):
    m = ('%s %s' % (type(e).__name__, e)).lower()
    return any(w.lower() in m for w in QUOTA_WORDS)


def probe(label, tbl, date_col):
    """探一行确认列名与日期列存在 —— schema 错误在这里就拦住，
    不要等抽到一半才撞 AttributeError。"""
    df = finance.run_query(query(tbl).limit(1))
    cols = list(df.columns)
    ok_id = 'id' in cols
    ok_dt = date_col in cols
    print('  %-16s %d 列 | id=%s | %s=%s'
          % (label, len(cols), '有' if ok_id else '❌无',
             date_col, '有' if ok_dt else '❌无'))
    if not ok_dt:
        print('     实际的日期类列: %s' % [c for c in cols if c.endswith('_date')])
    if not ok_id:
        print('     ⚠ 无 id 列，无法按 id 分页；将退化为单页抽取，可能不完整')
    return cols, ok_id, ok_dt


def fetch_year(tbl, date_col, year, has_id):
    lo, hi = '%d-01-01' % year, '%d-12-31' % year
    dcol = getattr(tbl, date_col)
    frames, last_id = [], -1
    while True:
        conds = [dcol >= lo, dcol <= hi]
        q = query(tbl).filter(*conds)
        if has_id:
            q = q.filter(tbl.id > last_id).order_by(tbl.id)
        df = finance.run_query(q.limit(PAGE))
        if df is None or len(df) == 0:
            break
        frames.append(df)
        if not has_id:
            # 无 id 无法安全分页 —— 明确说明只取到一页，不假装抽全了
            if len(df) == PAGE:
                print('      ⚠ %d 年满页且无 id，数据可能被截断' % year)
            break
        last_id = int(df['id'].max())
        if len(df) < PAGE:
            break
    return pd.concat(frames, ignore_index=True) if frames else None


def main():
    if not os.path.exists(OUT):
        os.makedirs(OUT)
    print('=== 探测 schema ===')
    meta = {}
    for label, tbl, dc in TABLES:
        try:
            meta[label] = probe(label, tbl, dc)
        except Exception as e:                              # noqa: BLE001
            print('  %-16s ❌ 探测失败: %s: %s' % (label, type(e).__name__, str(e)[:70]))

    for label, tbl, dc in TABLES:
        if label not in meta:
            continue
        cols, has_id, ok_dt = meta[label]
        if not ok_dt:
            print('\n%s: 缺日期列 %s，跳过' % (label, dc))
            continue
        print('\n' + '=' * 72)
        print('%s (按 %s 分年)' % (label, dc))
        print('=' * 72)
        done = skipped = 0
        for y in YEARS:
            path = os.path.join(OUT, '%s_%d.csv.gz' % (label, y))
            if os.path.exists(path):
                skipped += 1
                continue
            mb = free_mb(OUT)
            if mb is not None and mb < MIN_FREE_MB:
                print('  磁盘剩余 %.0f MB < %d MB，停止。重跑即续。' % (mb, MIN_FREE_MB))
                return
            t0 = time.time()
            try:
                df = fetch_year(tbl, dc, y, has_id)
            except Exception as e:                          # noqa: BLE001
                if is_quota_error(e):
                    print('  %d: 额度受限(%s)。明天重跑即续。' % (y, e))
                    return
                # 不是额度问题就别谎称是 —— 明天重跑只会一模一样地失败
                print('  %d: ❌ %s: %s —— 跳过该年' % (y, type(e).__name__, e))
                continue
            if df is None or df.empty:
                continue
            df.to_csv(path, index=False, compression='gzip')
            print('  %d: %6d 行 (%.1f MB, %.0fs)'
                  % (y, len(df), os.path.getsize(path) / 1048576.0, time.time() - t0))
            done += 1
            del df
            gc.collect()
        print('  完成: 新抽 %d 年, 跳过 %d 年' % (done, skipped))

    # 打包：研究环境一次只能下载一个文件。
    # 用 tar 不用 zip —— 里面的 .csv.gz 已经压过了。
    tar = '%s.tar' % OUT
    with tarfile.open(tar, 'w') as tf:
        for f in sorted(os.listdir(OUT)):
            tf.add(os.path.join(OUT, f), arcname=f)
    print('\n已打包 %s (%.1f MB) —— 下载它'
          % (tar, os.path.getsize(tar) / 1048576.0))

    # ---- 自带裁判用例：宁波银行 2019 的解禁 ----
    # 权威股本变动表说 2019-10-08「限售股份上市」使流通A股 50.51亿 -> 56.22亿。
    # 解禁明细里应能看到对应的一笔（约 5.7 亿股）。对上了才说明这张表能用来重建流通股本。
    try:
        d = finance.run_query(
            query(finance.STK_LIMITED_SHARES_LIST)
            .filter(finance.STK_LIMITED_SHARES_LIST.code == '002142.XSHE',
                    finance.STK_LIMITED_SHARES_LIST.actual_unlimited_date >= '2019-01-01',
                    finance.STK_LIMITED_SHARES_LIST.actual_unlimited_date <= '2019-12-31'))
        print('\n★ 裁判用例 002142.XSHE(宁波银行) 2019 实际解禁 %d 笔:' % len(d))
        if len(d):
            k = [c for c in d.columns if c in
                 ('code', 'shareholder_name', 'expected_unlimited_date',
                  'actual_unlimited_date', 'actual_unlimited_number',
                  'actual_unlimited_ratio', 'limited_reason')]
            print(d[k].to_string(index=False))
            print('\n  → 权威股本变动表说 2019-10-08「限售股份上市」使流通A股')
            print('    50.5059亿 -> 56.2157亿（+5.71亿股）。')
            print('    看这里的解禁数量能否对上 —— 对上了才能用它重建每日流通股本。')
    except Exception as e:                                  # noqa: BLE001
        print('\n裁判用例查询失败: %s: %s' % (type(e).__name__, str(e)[:80]))


main()

# ============================================================
# 清理 cell（跑完单独执行）
# ------------------------------------------------------------
# import gc
# from IPython import get_ipython
# get_ipython().user_ns['Out'].clear()
# gc.collect()
# ============================================================
