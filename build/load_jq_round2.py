#!/usr/bin/env python3
"""加载第二轮抽取：指数成分史 / 财务快照 / ETF+基金+指数维度。

用法:
    python datalake/build/load_jq_round2.py            # 加载 + 校验
    python datalake/build/load_jq_round2.py --demo     # 额外跑演示

输入（`datalake/raw/jq/_ingest/downloads/`，来自 extract_jq_round2.py）:
    index_member_{000300,000905,000852,000906,000016,399006,000688}.csv
    fin_snapshot_YYYY-MM-DD.csv        每季度一份, 累积存放, 用于将来识别重述
    universe_{etf,fund,lof,index}.csv  年末快照

产出:
    l1/index_member.parquet       成分区间(把季度快照压成 valid_from/valid_to)
    l1/security_universe_fund.parquet  ETF/基金/指数 的全集与上市退市日期
    l0/fin_snapshots.parquet      财务快照原样堆叠(多个 snapshot_date)
    视图: index_member / index_member_asof / fund_universe / fin_snapshots
"""
import argparse
import glob
import os
import re
import sys

import duckdb
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, 'raw', 'jq', '_ingest', 'downloads')
L0 = os.path.join(ROOT, 'raw', 'jq')
L1 = os.path.join(ROOT, 'std')
DB = os.path.join(ROOT, 'lake.db')
FAILURES = []

# 指数名, 用于校验成分数是否合理
INDEX_NAME = {'000300': ('沪深300', 300), '000905': ('中证500', 500),
              '000852': ('中证1000', 1000), '000906': ('中证800', 800),
              '000016': ('上证50', 50), '399006': ('创业板指', 100),
              '000688': ('科创50', 50)}


def check(cond, msg):
    print(('  ✓ ' if cond else '  ✗ ') + msg)
    if not cond:
        FAILURES.append(msg)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--demo', action='store_true')
    args = ap.parse_args()
    for d in (L0, L1):
        if not os.path.exists(d):
            os.makedirs(d)

    # ------------------------------------------------------ ① 指数成分历史
    print('=' * 72)
    print('① 指数成分历史')
    print('=' * 72)
    files = sorted(glob.glob(os.path.join(SRC, 'index_member_*.csv')))
    if not files:
        print('  ⚠ 无文件, 跳过')
        members = None
    else:
        parts = []
        for f in files:
            df = pd.read_csv(f, encoding='utf-8-sig', dtype=str)
            parts.append(df)
            ic = os.path.basename(f)[13:-4]
            nm, sz = INDEX_NAME.get(ic, ('?', 0))
            per = df.groupby('as_of').size()
            print('  %-8s %-10s %6d 行, %3d 个时点, 每期成分数 %d~%d'
                  % (ic, nm, len(df), len(per), per.min(), per.max()))
        asof = pd.concat(parts, ignore_index=True)
        del parts
        asof['as_of'] = pd.to_datetime(asof['as_of'])
        asof.to_parquet(os.path.join(L1, 'index_member_asof.parquet'),
                        index=False, compression='zstd')

        # 季度快照 → 成分区间。同一 (index, stock) 连续在册则合并成一段。
        asof = asof.sort_values(['index_code', 'stock_code', 'as_of'])
        g = asof.groupby(['index_code', 'stock_code'])
        asof['prev'] = g['as_of'].shift(1)
        # 相邻两期间隔 > 100 天视为中断(季度约 91 天), 开新区间
        asof['gap'] = (asof['as_of'] - asof['prev']).dt.days
        asof['new_seg'] = asof['prev'].isna() | (asof['gap'] > 100)
        asof['seg'] = asof.groupby(['index_code', 'stock_code'])['new_seg'].cumsum()
        seg = (asof.groupby(['index_code', 'stock_code', 'seg'])
                   .agg({'as_of': ['min', 'max']}))
        seg.columns = ['valid_from', 'last_seen']
        seg = seg.reset_index().drop(columns=['seg'])
        # valid_to = 最后一次在册之后的下一个季末(近似); 仍在册则为 NULL
        maxq = asof['as_of'].max()
        seg['valid_to'] = seg['last_seen'].where(seg['last_seen'] < maxq)
        seg['is_current'] = seg['last_seen'] >= maxq
        seg['is_quarterly_inferred'] = True     # 由季度快照推断, 非公告驱动 → 标记
        members = seg
        members.to_parquet(os.path.join(L1, 'index_member.parquet'),
                           index=False, compression='zstd')
        print('  %d 行季度快照 → %d 条成分区间 (压缩 %.1f 倍)'
              % (len(asof), len(members), len(asof) / float(len(members))))
        del asof, seg, g

    # ------------------------------------------------------- ② 财务快照
    print('\n' + '=' * 72)
    print('② 财务快照 (累积存放, 用于将来识别重述)')
    print('=' * 72)
    snaps = sorted(glob.glob(os.path.join(SRC, 'fin_snapshot_*.csv')))
    if not snaps:
        print('  ⚠ 无文件, 跳过')
        snap_all = None
    else:
        parts = []
        for f in snaps:
            df = pd.read_csv(f, encoding='utf-8-sig', low_memory=False,
                             dtype={'code': str, 'source': str})
            d = os.path.basename(f)[13:-4]
            print('  %s: %6d 行, %2d 个报告期 (%s ~ %s)'
                  % (d, len(df), df['report_date'].nunique(),
                     df['report_date'].min(), df['report_date'].max()))
            parts.append(df)
        snap_all = pd.concat(parts, ignore_index=True)
        del parts
        for c in ('report_date', 'pub_date'):
            snap_all[c] = pd.to_datetime(snap_all[c], errors='coerce')
        snap_all['snapshot_date'] = pd.to_datetime(snap_all['snapshot_date'])
        snap_all.to_parquet(os.path.join(L0, 'fin_snapshots.parquet'),
                            index=False, compression='zstd')
        print('  合计 %d 行, %d 个快照日' % (len(snap_all),
              snap_all['snapshot_date'].nunique()))
        if snap_all['snapshot_date'].nunique() < 2:
            print('  ⓘ 只有 1 个快照日 —— 重述比对需要至少 2 个。'
                  '每季度跑一次 extract_jq_round2.py 的 ② 段即可累积')

    # -------------------------------------------- ③ ETF/基金/指数 维度
    print('\n' + '=' * 72)
    print('③ ETF / 基金 / 指数 维度')
    print('=' * 72)
    ufiles = sorted(glob.glob(os.path.join(SRC, 'universe_*.csv')))
    fund_uni = None
    if not ufiles:
        print('  ⚠ 无文件, 跳过')
    else:
        parts = []
        for f in ufiles:
            t = os.path.basename(f)[9:-4]
            if t in ('stock', 'open_fund'):
                print('  %s: 跳过 (%s)' % (t, 'stock 已在第一轮' if t == 'stock'
                                          else '场外基金, 本库不需要'))
                continue
            df = pd.read_csv(f, encoding='utf-8-sig', dtype=str)
            df['sec_kind'] = t
            print('  %-6s %6d 行, 去重 %d 只' % (t, len(df), df['code'].nunique()))
            parts.append(df)
        if parts:
            u = pd.concat(parts, ignore_index=True)
            del parts
            # 同一 code 可能同时出现在 etf 与 fund(fund 是超集) —— 保留最具体的那个
            PREF = {'etf': 0, 'lof': 1, 'index': 2, 'fund': 3}
            u['_p'] = u['sec_kind'].map(PREF).fillna(9)
            u = (u.sort_values(['code', '_p'])
                  .drop_duplicates('code', keep='first').drop(columns=['_p']))
            for c in ('start_date', 'end_date', 'as_of'):
                if c in u.columns:
                    u[c] = pd.to_datetime(u[c], errors='coerce')
            fund_uni = u
            u.to_parquet(os.path.join(L1, 'security_universe_fund.parquet'),
                         index=False, compression='zstd')
            print('  → 去重后 %d 只 (etf/lof/index 优先于 fund)' % len(u))
            print('    分布: %s' % u['sec_kind'].value_counts().to_dict())

    # -------------------------------------------------------------- 视图
    print('\n' + '=' * 72)
    print('挂进视图层')
    print('=' * 72)
    con = duckdb.connect(DB)
    mapping = [('index_member', os.path.join(L1, 'index_member.parquet')),
               ('index_member_asof', os.path.join(L1, 'index_member_asof.parquet')),
               ('fund_universe', os.path.join(L1, 'security_universe_fund.parquet')),
               ('fin_snapshots', os.path.join(L0, 'fin_snapshots.parquet'))]
    for v, path in mapping:
        con.execute('DROP VIEW IF EXISTS %s' % v)
        if os.path.exists(path):
            con.execute("CREATE VIEW %s AS SELECT * FROM read_parquet('%s')" % (v, path))
            print('  %s' % v)

    # 表宏: 某日的指数成分
    con.execute('DROP MACRO TABLE IF EXISTS index_members_at')
    con.execute("""
    CREATE MACRO index_members_at(idx, d) AS TABLE
    SELECT index_code, stock_code, valid_from, last_seen
    FROM index_member
    WHERE index_code = idx
      AND valid_from <= d
      AND (valid_to IS NULL OR valid_to >= d)
    """)
    print('  index_members_at(idx, d)  表宏')

    # ------------------------------------------------------------------ 校验
    print('\n' + '=' * 72)
    print('校验')
    print('=' * 72)
    if members is not None:
        # 每期成分数应接近指数的标称规模
        bad = []
        for ic, (nm, sz) in INDEX_NAME.items():
            r = con.execute("""SELECT count(*) FROM index_members_at(?, DATE '2020-06-30')""",
                            ['%s.%s' % (ic, 'XSHE' if ic.startswith('399') else 'XSHG')]
                            ).fetchone()[0]
            if sz and r and abs(r - sz) / float(sz) > 0.15:
                bad.append('%s(%s) 2020-06-30 成分 %d, 标称 %d' % (ic, nm, r, sz))
        check(not bad, '各指数成分数与标称规模相符' + (' —— 异常: %s' % bad if bad else ''))

        ov = con.execute("""SELECT count(*) FROM (
            SELECT index_code, stock_code, valid_from,
                   lag(last_seen) OVER (PARTITION BY index_code, stock_code
                                        ORDER BY valid_from) prev
            FROM index_member) WHERE prev >= valid_from""").fetchone()[0]
        check(ov == 0, '成分区间无重叠 (重叠 %d)' % ov)

        # 成分股应都在证券全集里
        miss = con.execute("""SELECT count(DISTINCT m.stock_code) FROM index_member m
            LEFT JOIN security_universe u ON u.code = m.stock_code
            WHERE u.code IS NULL""").fetchone()[0]
        check(miss == 0, '成分股全部在 security_universe 里 (缺 %d)' % miss)

    if snap_all is not None:
        n_rt = con.execute('SELECT count(*) FROM fin_snapshots '
                           'WHERE report_type <> 0').fetchone()[0]
        check(n_rt == 0, '财务快照全为合并报表 (异常 %d)' % n_rt)
        np_ = con.execute('SELECT count(DISTINCT report_date) FROM fin_snapshots').fetchone()[0]
        check(np_ >= 10, '财务快照覆盖 %d 个报告期 (应 >=10)' % np_)

    if fund_uni is not None:
        # ETF 覆盖度: tdx 有 3934 只 etf 日线, 聚宽这边有多少能对上
        r = con.execute("""
            SELECT count(*) tdx_etf,
                   sum(CASE WHEN f.code IS NOT NULL THEN 1 ELSE 0 END) n_matched  -- matched 是 DuckDB 保留字(MERGE 语法), 不能直接当别名
            FROM (SELECT DISTINCT jq_code FROM code_map WHERE class='etf') m
            LEFT JOIN fund_universe f ON f.code = m.jq_code""").fetchone()
        pct = 100.0 * r[1] / r[0] if r[0] else 0
        print('  ⓘ tdx 的 %d 只 ETF 中, %d 只在聚宽基金全集里 (%.1f%%)'
              % (r[0], r[1], pct))
        print('     差额是聚宽不覆盖的品种(部分 LOF/封闭基金/已清算), 日线仍可用')

    # ---------------------------------------------------------------- 演示
    if args.demo and members is not None:
        print('\n' + '=' * 72)
        print('演示: 沪深300 成分的历史变迁 (PIT)')
        print('=' * 72)
        print(con.execute("""
        SELECT d AS 观察日,
               (SELECT count(*) FROM index_members_at('000300.XSHG', d)) AS 成分数
        FROM (VALUES (DATE '2006-06-30'), (DATE '2010-06-30'), (DATE '2015-06-30'),
                     (DATE '2020-06-30'), (DATE '2026-06-30')) t(d) ORDER BY d
        """).df().to_string(index=False))
        print('\n2015-06-30 在沪深300、但 2026 已不在的标的(前 8 只):')
        print(con.execute("""
        SELECT m.stock_code, n.name AS 当时名称, u.delist_date
        FROM index_members_at('000300.XSHG', DATE '2015-06-30') m
        LEFT JOIN security_universe u ON u.code = m.stock_code
        LEFT JOIN security_name n ON n.code = m.stock_code
             AND n.valid_from <= DATE '2015-06-30'
             AND (n.valid_to IS NULL OR n.valid_to > DATE '2015-06-30')
        WHERE m.stock_code NOT IN (
            SELECT stock_code FROM index_members_at('000300.XSHG', DATE '2026-06-30'))
        ORDER BY u.delist_date NULLS LAST LIMIT 8""").df().to_string(index=False))
        print('\n↑ 只用当时的成分名单回测, 才不会有"用今天的成分股回测十年前"的偏差')

    con.close()
    print('\n' + '=' * 72)
    if FAILURES:
        print('❌ %d 项校验失败:' % len(FAILURES))
        for f in FAILURES:
            print('   - %s' % f)
        return 1
    print('✅ 全部校验通过')
    return 0


if __name__ == '__main__':
    sys.exit(main())
