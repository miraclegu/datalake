#!/usr/bin/env python3
"""把聚宽抽出的 CSV 加载成 PIT 维度库（L0 原样 + L1 规范化）。

用法:
    python datalake/build/load_jq_dimensions.py            # 加载 + 校验
    python datalake/build/load_jq_dimensions.py --demo     # 额外跑演示查询

产出:
    datalake/raw/jq/*.parquet      源数据原样(仅转格式), 保留一切字段
    datalake/std/*.parquet      规范化: 完整全集 + 生效期区间
    datalake/lake.db            DuckDB, 只建视图指向 parquet(不复制数据)

设计原则(来自 2026-08-23 的教训):
  · L0 原样保存, 任何加工都在 L1, 源数据永远可回溯
  · 只落"原语", 不落派生量; 查询靠视图
  · 不用单文件大库: parquet 分区 + DuckDB 视图, 拷目录即可复制
  · 校验失败即非零退出, 不静默
"""
import argparse
import os
import sys

import duckdb
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, 'raw', 'jq', '_ingest', 'downloads')
L0 = os.path.join(ROOT, 'raw', 'jq')
L1 = os.path.join(ROOT, 'std')
DB = os.path.join(ROOT, 'lake.db')

FILES = ['dim_security', 'dim_security_asof', 'dim_name_history',
         'dim_status_change', 'dim_industry_asof', 'idx_weight_month']

FAILURES = []

# L1 里需要转成真日期的列。L0 保持全字符串(原样性), L1 有类型(可查询) ——
# 这就是分层的意义: 源数据永远可回溯, 加工层可用。
DATE_COLS = ('valid_from', 'valid_to', 'known_from', 'list_date', 'delist_date',
             'as_of', 'start_date', 'pub_date', 'change_date', 'end_date')


def to_dates(df, label):
    """把日期列转 datetime。转不动的记 coerce 计数并打印 —— 不静默。"""
    for c in df.columns:
        if c in DATE_COLS:
            before = df[c].notna().sum()
            df[c] = pd.to_datetime(df[c], errors='coerce')
            lost = before - df[c].notna().sum()
            if lost:
                print('    ⚠ %s.%s: %d 个值无法解析为日期, 已置 NaT' % (label, c, lost))
    return df


def check(cond, msg):
    """校验断言。失败记录下来, 最后一起报并非零退出 —— 不静默。"""
    if cond:
        print('  ✓ %s' % msg)
    else:
        print('  ✗ %s' % msg)
        FAILURES.append(msg)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--demo', action='store_true', help='额外跑演示查询')
    args = ap.parse_args()

    for d in (L0, L1):
        if not os.path.exists(d):
            os.makedirs(d)

    # ---------------------------------------------------------------- L0
    print('=' * 72)
    print('L0: 源数据原样转 parquet')
    print('=' * 72)
    raw = {}
    for name in FILES:
        path = os.path.join(SRC, name + '.csv')
        if not os.path.exists(path):
            print('  ⚠ 缺失 %s.csv, 跳过' % name)
            continue
        # utf-8-sig 去掉 BOM; low_memory=False 避免混合类型警告
        df = pd.read_csv(path, encoding='utf-8-sig', low_memory=False, dtype=str)
        raw[name] = df
        out = os.path.join(L0, name + '.parquet')
        df.to_parquet(out, index=False, compression='zstd')
        print('  %-22s %8d 行 × %2d 列  → %6.1f KB' % (
            name, len(df), len(df.columns), os.path.getsize(out) / 1024.0))

    # ---------------------------------------------------------------- L1
    print('\n' + '=' * 72)
    print('L1: 规范化')
    print('=' * 72)

    # 1) 完整证券全集 = 三表并集
    #    实测: 年末快照会漏 (a)2003 年前退市的 (b)同年上市又退市的, 共 267 只
    codes = set()
    for k, col in (('dim_security', 'code'), ('dim_name_history', 'code'),
                   ('dim_status_change', 'code')):
        if k in raw:
            codes |= set(raw[k][col].dropna())
    base = raw['dim_security'].drop_duplicates('code').set_index('code')
    rows = []
    for c in sorted(codes):
        if c in base.index:
            r = base.loc[c]
            rows.append({'code': c, 'display_name_current': r['display_name'],
                         'name_pinyin': r['name'], 'list_date': r['start_date'],
                         'delist_date': r['end_date'], 'sec_type': r['type'],
                         'in_yearend_snapshot': True})
        else:
            rows.append({'code': c, 'display_name_current': None, 'name_pinyin': None,
                         'list_date': None, 'delist_date': None, 'sec_type': None,
                         'in_yearend_snapshot': False})
    universe = to_dates(pd.DataFrame(rows), 'security_universe')
    universe.to_parquet(os.path.join(L1, 'security_universe.parquet'),
                        index=False, compression='zstd')
    print('  security_universe: %d 只 (其中年末快照未覆盖 %d 只)' % (
        len(universe), (~universe['in_yearend_snapshot']).sum()))

    # 2) 名称历史 → 生效期区间。start_date=valid_from, pub_date=known_from
    nh = raw['dim_name_history'][['code', 'company_id', 'new_name',
                                  'start_date', 'pub_date', 'reason']].copy()
    nh = nh.sort_values(['code', 'start_date'])
    nh['valid_from'] = nh['start_date']
    nh['valid_to'] = nh.groupby('code')['start_date'].shift(-1)   # 下一次改名即失效
    nh['known_from'] = nh['pub_date']
    nh = nh.rename(columns={'new_name': 'name'})
    to_dates(nh, 'security_name')[
        ['code', 'company_id', 'name', 'valid_from', 'valid_to', 'known_from', 'reason']] \
        .to_parquet(os.path.join(L1, 'security_name.parquet'), index=False, compression='zstd')
    print('  security_name: %d 条区间, 覆盖 %d 只' % (len(nh), nh['code'].nunique()))

    # 3) 行业快照 → 生效期区间（申万一/二/三级 + 证监会，实测覆盖均 100%;
    #    jq_l1 仅 62.76% 早期缺, 不要用）
    ia = raw['dim_industry_asof'][['code', 'as_of',
                                   'sw_l1_code', 'sw_l1_name',
                                   'sw_l2_code', 'sw_l2_name',
                                   'sw_l3_code', 'sw_l3_name',
                                   'zjw_code', 'zjw_name']].copy()
    ia = ia.sort_values(['code', 'as_of'])
    # 🔴🔴 **判"有没有变"必须把三级拼起来**，不能只看一级。
    #   只看 `sw_l1_code` 的话：一级没变、二级/三级变了 -> 不开新区间 ->
    #   那条区间里的 l2/l3 永远停在**开区间那一刻**的值，此后再不更新，
    #   而且**不报错**（区间数、覆盖率、面板行数全都正常）。
    #   实测区间数：只看 l1 = 8737；l1+l2 = 10760；l1+l2+l3 = 12417 ——
    #   差的那 3680 条正是"一级没变而下级变了"的换挡。
    # ★ 用 `fillna('')` 而不是直接拼：NaN 参与字符串拼接会整条变 NaN，
    #   于是 `!=` 恒为真、每个快照都开一条新区间（压缩比从 23 倍掉到 1 倍）。
    _key = (ia['sw_l1_code'].fillna('').astype(str) + '|'
            + ia['sw_l2_code'].fillna('').astype(str) + '|'
            + ia['sw_l3_code'].fillna('').astype(str))
    ia['is_change'] = (_key != _key.groupby(ia['code']).shift(1))
    seg = ia[ia['is_change']].copy()
    seg['valid_from'] = seg['as_of']
    seg['valid_to'] = seg.groupby('code')['as_of'].shift(-1)
    seg['is_backfilled'] = True    # 季度快照推断出的区间, 非公告驱动 → 显式标记
    to_dates(seg, 'security_industry')[
        ['code', 'valid_from', 'valid_to',
         'sw_l1_code', 'sw_l1_name', 'sw_l2_code', 'sw_l2_name',
         'sw_l3_code', 'sw_l3_name', 'zjw_code', 'zjw_name', 'is_backfilled']] \
        .to_parquet(os.path.join(L1, 'security_industry.parquet'), index=False, compression='zstd')
    print('  security_industry: %d 行快照 → %d 条区间 (压缩 %.1f 倍)' % (
        len(ia), len(seg), len(ia) / max(len(seg), 1)))

    # 4) 状态变更原样进 L1(已是事件形态, 双日期)
    sc = raw['dim_status_change'][['code', 'company_id', 'name', 'change_date',
                                   'pub_date', 'change_type', 'public_status',
                                   'change_reason']].copy()
    sc = sc.rename(columns={'change_date': 'valid_from', 'pub_date': 'known_from'})
    sc = to_dates(sc, 'security_status')
    sc.to_parquet(os.path.join(L1, 'security_status.parquet'), index=False, compression='zstd')
    print('  security_status: %d 条事件' % len(sc))

    # ------------------------------------------------------------- DuckDB 视图
    print('\n' + '=' * 72)
    print('DuckDB 视图层(不复制数据, 只指向 parquet)')
    print('=' * 72)
    # 🔴🔴 **不许删掉整个 lake.db。**（2026-09-29 事故）
    #
    #   原来这里 `os.remove(DB)` 再只按 L0/L1 的 parquet 重建 —— 于是
    #   `fin_income` / `fin_core` / `fin_ratio`（由 `load_jq_financials.py`
    #   + `build_canonical_views.py` 建，指向 `raw/jq/financials/`）、
    #   `kline_*` / `adjust_factor`（`load_tdx_kline.py` 建）**整批消失**。
    #
    #   实测后果：每日同步第 7 步「面板（本年增量）」报
    #   `Catalog Error: Table with name fin_core does not exist!` ——
    #   而那句话指不到"上一个 loader 把库删了"这件事，看着像面板脚本坏了。
    #   17:30 / 18:00 / 19:10 三轮同步连着失败，面板一整天没更新。
    #
    # 🔴 **`rebuild_lake_db.py` 也这么干**（按 2026-08-25 的 DDL 基线重建，
    #   丢掉之后新增的 13 个视图）—— 两个"删库重建"各自只知道自己那一半，
    #   谁后跑谁说了算，而**丢了不报错**：库还在、视图少一半。
    #
    # ★ 改法：`CREATE OR REPLACE`，只负责自己这一批，谁先谁后都不丢东西。
    #   实测两套重叠的 9 个视图定义**完全一样**（都是同一个 parquet 的
    #   `SELECT *`），所以取并集没有歧义。
    # ★ 代价：某个 parquet 被删掉时，它的视图会**留在库里指向不存在的文件**。
    #   这比"静默删掉另一半"轻得多，而且 `t_lake_views_union` 钉住了
    #   "库里的视图集 == 两个正本的并集"，多了少了都报。
    con = duckdb.connect(DB)
    for f in sorted(os.listdir(L0)):
        if f.endswith('.parquet'):
            con.execute("CREATE OR REPLACE VIEW l0_%s AS SELECT * FROM read_parquet('%s')"
                        % (f[:-8], os.path.join(L0, f)))
    for f in sorted(os.listdir(L1)):
        if f.endswith('.parquet'):
            con.execute("CREATE OR REPLACE VIEW %s AS SELECT * FROM read_parquet('%s')"
                        % (f[:-8], os.path.join(L1, f)))
    views = [r[0] for r in con.execute(
        'SELECT view_name FROM duckdb_views() WHERE NOT internal ORDER BY 1').fetchall()]
    print('  建了 %d 个视图: %s' % (len(views), views))

    # ------------------------------------------------------------------ 校验
    print('\n' + '=' * 72)
    print('校验')
    print('=' * 72)

    n_uni = con.execute('SELECT count(*) FROM security_universe').fetchone()[0]
    check(n_uni > 5700, '证券全集 %d 只 (应 >5700, 三表并集)' % n_uni)

    # 存活偏差: 2015 年末在市数应与年末快照一致, 且现实约 2800
    n15 = con.execute("SELECT count(*) FROM l0_dim_security_asof "
                      "WHERE as_of='2015-12-31'").fetchone()[0]
    check(2700 < n15 < 2900, '2015-12-31 在市 %d 只 (现实约 2800)' % n15)

    # 行业区间不应有重叠
    ov = con.execute("""SELECT count(*) FROM (
        SELECT code, valid_from, valid_to,
               lag(valid_to) OVER (PARTITION BY code ORDER BY valid_from) prev_to
        FROM security_industry) WHERE prev_to > valid_from""").fetchone()[0]
    check(ov == 0, '行业区间无重叠 (重叠 %d 处)' % ov)

    # 名称区间不应有重叠
    ov2 = con.execute("""SELECT count(*) FROM (
        SELECT code, valid_from, valid_to,
               lag(valid_to) OVER (PARTITION BY code ORDER BY valid_from) prev_to
        FROM security_name) WHERE prev_to > valid_from""").fetchone()[0]
    check(ov2 == 0, '名称区间无重叠 (重叠 %d 处)' % ov2)

    # 申万一级覆盖率
    cov = con.execute("SELECT 1.0*sum(CASE WHEN sw_l1_code IS NOT NULL THEN 1 ELSE 0 END)"
                      "/count(*) FROM l0_dim_industry_asof").fetchone()[0]
    check(cov > 0.99, '申万一级覆盖率 %.2f%%' % (100 * cov))

    # 已知不完整项: 指数权重(整数过滤 bug 导致只抽到 1600 行)
    n_iw = con.execute('SELECT count(*) FROM l0_idx_weight_month').fetchone()[0]
    print('  ⚠ idx_weight_month 仅 %d 行 —— 已知不完整(整数过滤 bug), '
          '成分请改用 get_index_stocks 重抽' % n_iw)

    # -------------------------------------------------------------------- 演示
    if args.demo:
        print('\n' + '=' * 72)
        print("演示: 这是 tdx2db 答不了的问题 —— \"2015-06-30 那天有哪些股票, 叫什么, 属什么行业\"")
        print('=' * 72)
        q = """
        WITH d AS (SELECT DATE '2015-06-30' AS d)
        SELECT s.code,
               n.name        AS name_at_the_time,
               u.display_name_current AS name_today,
               i.sw_l1_name  AS industry_at_the_time,
               u.delist_date
        FROM (SELECT DISTINCT code FROM l0_dim_security_asof WHERE as_of='2015-12-31') s
        JOIN security_universe u ON u.code = s.code
        LEFT JOIN security_name n ON n.code = s.code
             AND n.valid_from <= (SELECT d FROM d)
             AND (n.valid_to IS NULL OR n.valid_to > (SELECT d FROM d))
             AND n.known_from <= (SELECT d FROM d)
        LEFT JOIN security_industry i ON i.code = s.code
             AND i.valid_from <= (SELECT d FROM d)
             AND (i.valid_to IS NULL OR i.valid_to > (SELECT d FROM d))
        WHERE u.delist_date < DATE '2026-01-01'
        ORDER BY u.delist_date
        LIMIT 15"""
        print(con.execute(q).df().to_string(index=False))
        print("\n↑ 全是已退市股票, 但当时的名字和行业都在 —— 这就是 PIT")

    con.close()

    print('\n' + '=' * 72)
    if FAILURES:
        print('❌ %d 项校验失败:' % len(FAILURES))
        for f in FAILURES:
            print('   - %s' % f)
        return 1
    print('✅ 全部校验通过')
    print('   L0: %s' % L0)
    print('   L1: %s' % L1)
    print('   DuckDB: %s (只有视图, 数据在 parquet 里)' % DB)
    return 0


if __name__ == '__main__':
    sys.exit(main())
