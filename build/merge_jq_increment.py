#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把聚宽增量 tar 合并进 raw/jq/，再提示跑哪些 loader。

用法：
    python3 datalake/build/merge_jq_increment.py <jq_increment_YYYYMMDD.tar>
    python3 datalake/build/merge_jq_increment.py <tar> --dry-run    # 只看会改什么

## 为什么必须【合并】而不是覆盖

`load_jq_indicator_q.py` 是 `COPY (SELECT ... FROM read_parquet(raw)) TO std`
—— std 直接由 raw 全量转换。raw 只放增量的话，历史会被整段抹掉。
所以这里按【自然键】去重合并：新行覆盖同键旧行，其余保留。

## 自然键（都实测过唯一性，见下方 KEYS）

    fundamentals_indicator_q   (code, statDate)
    stk_xr_xd                  (code, report_date, bonus_type)
    其余带聚宽 id 列的         (id,)

## 护栏

合并后行数【只增不减】。变少 = 增量把历史挤掉了，直接报错退出，
并且不写任何文件 —— 宁可不合，也不能悄悄丢历史。
"""
import argparse
import os
import shutil
import sys
import tarfile
import tempfile

import duckdb

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RAW = os.path.join(ROOT, 'raw', 'jq')

# 增量文件名 -> (目标 raw 文件, 自然键)。键为 None 表示用 'id'
TARGETS = {
    'fundamentals_indicator_q': ('fundamentals_indicator_q.parquet', ['code', 'statDate']),
    'stk_xr_xd':                ('stk_xr_xd.parquet', ['code', 'report_date', 'bonus_type']),
    'dim_name_history':         ('dim_name_history.parquet', None),
    'dim_status_change':        ('dim_status_change.parquet', None),
    'stk_fin_forcast':          ('stk_fin_forcast.parquet', None),
    'share_change':             (os.path.join('financials', 'share_change.parquet'), None),
    'fin_income':               (os.path.join('financials', 'income.parquet'), None),
    'fin_balance':              (os.path.join('financials', 'balance.parquet'), None),
    'fin_cash_flow':            (os.path.join('financials', 'cash_flow.parquet'), None),
}

# 合并完该跑哪些 loader（顺序有依赖）
LOADERS = [
    ('fundamentals_indicator_q', 'build/load_jq_indicator_q.py'),
    ('stk_xr_xd',                'build/load_jq_round3.py'),
    ('fin_income',               'build/load_jq_financials.py'),
    ('dim_name_history',         'build/load_jq_dimensions.py'),
    ('dim_status_change',        'build/load_jq_dimensions.py'),
    ('share_change',             'build/load_jq_share_change.py'),
]


def _stem(fn):
    """去掉扩展名。单独提出来是因为 .csv.gz 是【两级】扩展名 ——
    第一版在两处各硬写了一次切片长度，对 .parquet 对、对 .csv.gz 少切一个字符，
    把 fundamentals_indicator_q 显示成了 fundamentals_indicator_。"""
    for ext in ('.csv.gz', '.parquet', '.csv'):
        if fn.endswith(ext):
            return fn[:-len(ext)]
    return fn


def _src(path):
    """增量可能是 .csv.gz 也可能是 .parquet —— 用对应的读法。

    [!] csv.gz 一律 all_varchar=true 读进来，再按目标表的列类型 CAST。
        不这么做的话，DuckDB 会按内容猜类型：同一列在增量里全是数字、
        在旧表里是字符串（如 '002054' 会被猜成 2054），合并时就静默错位。
    """
    if path.endswith('.csv.gz'):
        return "read_csv_auto('%s', all_varchar=true, compression='gzip')" % path
    return "read_parquet('%s')" % path


def merge_one(con, inc_path, dst_path, keys, dry):
    name = os.path.basename(dst_path)
    src = _src(inc_path)
    n_inc = con.execute("SELECT count(*) FROM %s" % src).fetchone()[0]
    if not os.path.exists(dst_path):
        print('  %-30s ❌ 目标不存在 —— 增量包不能用来建表' % name)
        print('     （全 varchar 的 csv 落地会把所有列类型做错，且没有旧表可对照）')
        print('     先用对应的全量 extract 脚本建一次，再用增量补。')
        return None, None

    cols_old = [r[0] for r in con.execute(
        "DESCRIBE SELECT * FROM read_parquet(?) LIMIT 1", [dst_path]).fetchall()]
    cols_inc = [r[0] for r in con.execute(
        "DESCRIBE SELECT * FROM %s LIMIT 1" % src).fetchall()]
    missing = [c for c in cols_old if c not in cols_inc]
    extra = [c for c in cols_inc if c not in cols_old]
    if missing or extra:
        # 列不一致就不合 —— 结构变了要人来看，不能猜
        print('  %-30s ❌ 列不一致：增量缺 %s / 多出 %s' % (name, missing[:4], extra[:4]))
        return None, None

    k = keys or (['id'] if 'id' in cols_old else None)
    if not k:
        print('  %-30s ❌ 没有可用的自然键（无 id 列且未在 TARGETS 指定）' % name)
        return None, None

    n_old = con.execute("SELECT count(*) FROM read_parquet(?)", [dst_path]).fetchone()[0]
    # ★ 增量的每一列都 CAST 成旧表的类型 —— csv 读进来是全 varchar，
    #   不 CAST 的话 UNION ALL 会把整表拖成 varchar，写回去类型就全毁了。
    types = {r[0]: r[1] for r in con.execute(
        "DESCRIBE SELECT * FROM read_parquet(?) LIMIT 1", [dst_path]).fetchall()}
    sel_inc = ','.join('try_cast("%s" AS %s) AS "%s"' % (c, types[c], c)
                       for c in cols_old)
    sel_old = ','.join('"%s"' % c for c in cols_old)
    on = ' AND '.join('o."%s"::VARCHAR IS NOT DISTINCT FROM i."%s"::VARCHAR'
                      % (c, c) for c in k)
    sql = """
      SELECT %s FROM %s i
      UNION ALL
      SELECT %s FROM read_parquet('%s') o
      WHERE NOT EXISTS (SELECT 1 FROM %s i WHERE %s)
    """ % (sel_inc, src, sel_old, dst_path, src, on)
    n_new = con.execute("SELECT count(*) FROM (%s)" % sql).fetchone()[0]
    delta = n_new - n_old
    print('  %-30s %8d -> %8d  (%+d，增量 %d 行，键 %s)'
          % (name, n_old, n_new, delta, n_inc, k))
    if n_new < n_old:
        print('     ❌ 合并后【变少】了 —— 增量把历史挤掉了，拒绝写入')
        return None, None
    if not dry:
        tmp = dst_path + '.tmp'
        con.execute("COPY (%s) TO '%s' (FORMAT PARQUET)" % (sql, tmp))
        shutil.move(dst_path, dst_path + '.bak')
        shutil.move(tmp, dst_path)
    return n_old, n_new


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('tar')
    ap.add_argument('--dry-run', action='store_true')
    a = ap.parse_args()
    if not os.path.exists(a.tar):
        print('找不到 %s' % a.tar)
        return 1

    tmpd = tempfile.mkdtemp(prefix='jqinc_')
    with tarfile.open(a.tar) as t:
        t.extractall(tmpd)
    # 增量包里是 .csv.gz（聚宽研究环境没有 pyarrow，见 extract 脚本的环境约束）。
    # 兼容 .parquet 是为了以后环境变了不用改这里。
    incs = sorted(f for f in os.listdir(tmpd)
                  if f.endswith('.csv.gz') or f.endswith('.parquet'))
    print('=' * 74)
    print('合并聚宽增量  %s%s' % (os.path.basename(a.tar), '  [dry-run]' if a.dry_run else ''))
    print('=' * 74)
    print('包内 %d 个文件: %s' % (len(incs), [_stem(x) for x in incs]))
    print()

    con = duckdb.connect(':memory:')
    bad, touched = 0, []
    for fn in incs:
        stem = _stem(fn)
        if stem not in TARGETS:
            print('  %-30s ⚠ 不在 TARGETS 里，跳过' % stem)
            continue
        rel, keys = TARGETS[stem]
        o, n = merge_one(con, os.path.join(tmpd, fn), os.path.join(RAW, rel),
                         keys, a.dry_run)
        if o is None:
            bad += 1
        elif n != o:
            touched.append(stem)
    shutil.rmtree(tmpd, ignore_errors=True)

    print()
    if bad:
        print('❌ %d 个文件没合上（见上），先解决再跑' % bad)
        return 1
    if not touched:
        print('✅ 没有新增行 —— 本地已是最新')
        return 0
    print('✅ 合并完成，有变化的：%s' % touched)
    print()
    print('接下来按顺序跑（旧文件已备份成 *.bak）：')
    seen = []
    for stem, script in LOADERS:
        if stem in touched and script not in seen:
            seen.append(script)
    for s in seen:
        print('    python3 %s' % s)
    print('    python3 datalake/build/rebuild_lake_db.py --verify')
    print('    python3 datalake/build/build_panel_daily.py            # 不要加 --verify！')
    print('    python3 datalake/build/build_panel_daily.py --verify')
    print('    python3 datalake/build/build_beta_daily.py')
    print()
    print('[!] build_panel_daily.py 的 --verify 是【只校验不构建】，')
    print('    与 rebuild_lake_db.py 的「构建后校验」语义相反 —— 别混。')
    return 0


if __name__ == '__main__':
    sys.exit(main())
