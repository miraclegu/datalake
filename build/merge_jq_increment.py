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
import re
import shutil
import sys
import tarfile
import tempfile

import duckdb

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = ROOT

# 增量文件名 -> (目标文件[相对 datalake 根], 自然键)
#
# ★★ 目标必须是【loader 的输入】而不是它的输出，否则合了会被 loader 冲掉。
#    实测踩过：把 stk_xr_xd 合进 raw/jq/stk_xr_xd.parquet（+2977 行），
#    跑 load_jq_round3.py 后又变回 152193 —— 因为那个 loader 的 SRC 是
#    _ingest/downloads/，raw/jq/*.parquet 是它的【产物 L0】。
#
#    各 loader 的源（已逐个确认）：
#      load_jq_indicator_q.py  SRC = raw/jq/fundamentals_indicator_q.parquet  ← 唯一以 raw 为源
#      load_jq_round3.py       SRC = raw/jq/_ingest/downloads/
#      load_jq_dimensions.py   SRC = raw/jq/_ingest/downloads/
#      load_jq_financials.py   SRC = raw/jq/_ingest/downloads/（按年分片 income_YYYY.csv.gz）
DL = os.path.join('raw', 'jq', '_ingest', 'downloads')
TARGETS = {
    'fundamentals_indicator_q': (os.path.join('raw', 'jq',
                                 'fundamentals_indicator_q.parquet'),
                                 ['code', 'statDate']),
    'stk_xr_xd':        (os.path.join(DL, 'stk_xr_xd.csv'),
                         ['code', 'report_date', 'bonus_type']),
    'dim_name_history': (os.path.join(DL, 'dim_name_history.csv'), None),
    'dim_status_change': (os.path.join(DL, 'dim_status_change.csv'), None),
    'stk_fin_forcast':  (os.path.join(DL, 'stk_fin_forcast.csv'), None),
    # share_change 的源是 stk_capital_change.csv.gz，走 load_jq_share_change.py
    'share_change':     (os.path.join(DL, 'stk_capital_change.csv.gz'), None),
}

# ★ 三大报表走【整文件替换】而不是行级合并。
#   增量脚本按报告期年份整年重抽，文件名与全量一致（income_2026.csv.gz），
#   而 load_jq_financials.py 是 glob 所有年份文件重建 parquet ——
#   所以换掉当年那几个文件、重跑 loader 就是完整刷新。
#   这样就完全绕开了「缺列 / 类型错位 / 去重键选错」那一类风险：没有合并，
#   就没有合并的坑。代价是每次多传几 MB（2026 三张表约 5 MB）。
REPLACE_RE = re.compile(r'^(income|balance|cashflow|indicator|indicator_q)'
                        r'_[12][0-9]{3}$')


def replace_file(inc_path, dst_path, dry):
    """整文件替换。旧文件留 .bak —— 万一新抽的有问题还能退回去。"""
    name = os.path.basename(dst_path)
    n_new = _count(inc_path)
    n_old = _count(dst_path) if os.path.exists(dst_path) else 0
    if n_new is None:
        print('  %-30s ❌ 增量读不出行数' % name)
        return None, None
    # ★ 整文件替换最大的风险是【新的比旧的少】—— 抽取中途配额耗尽会给出
    #   一个"看着正常"的小文件，替换掉就静默丢数据。所以行数缩水直接拒绝。
    if n_old and n_new < n_old * 0.95:
        print('  %-30s ❌ 拒绝替换：新 %s 行 < 旧 %s 行的 95%%'
              % (name, format(n_new, ','), format(n_old, ',')))
        print('     整年重抽本该 >= 旧的（重述只会增行）。多半是抽取中断/配额耗尽。')
        return None, None
    if dry:
        print('  %-30s [dry] 替换 %s -> %s 行'
              % (name, format(n_old, ','), format(n_new, ',')))
        return n_old, n_new
    if os.path.exists(dst_path):
        shutil.move(dst_path, dst_path + '.bak')
    shutil.copy(inc_path, dst_path)
    print('  %-30s ✓ 整文件替换 %s -> %s 行'
          % (name, format(n_old, ','), format(n_new, ',')))
    return n_old, n_new


def _count(path):
    try:
        con = duckdb.connect()
        return con.execute('SELECT count(*) FROM %s' % _dst_read(path)).fetchone()[0]
    except Exception:                                           # noqa: BLE001
        return None


# 合并完该跑哪些 loader（顺序有依赖）
LOADERS = [
    ('fundamentals_indicator_q', 'build/load_jq_indicator_q.py'),
    ('stk_xr_xd',                'build/load_jq_round3.py'),
    ('income_2026',              'build/load_jq_financials.py'),
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


# ★ 这些表的 CSV【DuckDB 读不了】—— 必须走 pandas。
#   stk_fin_forcast 的 content 是大段中文预告正文，含换行；多行字段虽然加了
#   引号，但文件里还有 6 处**裸换行**（未加引号），DuckDB 的状态机直接报
#   "reached an invalid state"，而它上一层的 try/except 会把整张表跳过 ——
#   于是"业绩预告没更新"这件事只在日志里留一行，很容易被忽略。
#   pandas 能原样往返（文件本来就是 pandas 写的），实测 124,094 行无损。
#   ❌ 不用 ignore_errors / strict_mode=false：那会静默丢行且丢多少不知道。
PANDAS_CSV = {'stk_fin_forcast'}


def _read_pandas(path):
    """用【修读器】读成 DataFrame（全字符串），供 register 进 DuckDB。

    ★ 不用裸 pandas.read_csv —— 它在这个文件上不报错但读错（静默产出
      11 行垃圾）。csv_repair 会做内容校验，读不干净就抛。
    """
    import sys as _sys
    _sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from csv_repair import read_forcast_df
    return read_forcast_df(path, verbose=False)[0]


def _src(path):
    """增量可能是 .csv.gz 也可能是 .parquet —— 用对应的读法。

    [!] csv.gz 一律 all_varchar=true 读进来，再按目标表的列类型 CAST。
        不这么做的话，DuckDB 会按内容猜类型：同一列在增量里全是数字、
        在旧表里是字符串（如 '002054' 会被猜成 2054），合并时就静默错位。
    """
    if path.endswith('.csv.gz'):
        return "read_csv_auto('%s', all_varchar=true, compression='gzip')" % path
    return "read_parquet('%s')" % path


def _dst_read(path):
    """目标文件也可能是 csv / csv.gz（downloads 下的源就是 csv）。

    [!] 目标是 csv 时【必须也 all_varchar】：这些 csv 是 L0 原样存档，
        loader 自己会做类型解析。这里按 varchar 读写，原样进原样出。
    """
    if path.endswith('.csv.gz'):
        return "read_csv_auto('%s', all_varchar=true, compression='gzip')" % path
    if path.endswith('.csv'):
        return "read_csv_auto('%s', all_varchar=true)" % path
    return "read_parquet('%s')" % path


def _copy_to(path):
    if path.endswith('.csv.gz'):
        return "(FORMAT CSV, COMPRESSION GZIP, HEADER)"
    if path.endswith('.csv'):
        return "(FORMAT CSV, HEADER)"
    return "(FORMAT PARQUET)"


def merge_one(con, inc_path, dst_path, keys, dry, use_pandas=False):
    name = os.path.basename(dst_path)
    if use_pandas:
        # 两边都用 pandas 读，注册成临时视图后走【同一套】合并 SQL ——
        # 不为这张表另写一条合并路径，否则去重/CAST/行数校验都要写第二遍。
        con.register('_inc_pd', _read_pandas(inc_path))
        src = '(SELECT * FROM _inc_pd)'
        dst = '(SELECT * FROM _dst_pd)' if os.path.exists(dst_path) else None
        if dst:
            con.register('_dst_pd', _read_pandas(dst_path))
    else:
        src = _src(inc_path)
        dst = _dst_read(dst_path)
    n_inc = con.execute("SELECT count(*) FROM %s" % src).fetchone()[0]
    if not os.path.exists(dst_path) or dst is None:
        print('  %-30s ❌ 目标不存在 —— 增量包不能用来建表' % name)
        print('     （全 varchar 的 csv 落地会把所有列类型做错，且没有旧表可对照）')
        print('     先用对应的全量 extract 脚本建一次，再用增量补。')
        return None, None

    cols_old = [r[0] for r in con.execute(
        "DESCRIBE SELECT * FROM %s LIMIT 1" % dst).fetchall()]
    cols_inc = [r[0] for r in con.execute(
        "DESCRIBE SELECT * FROM %s LIMIT 1" % src).fetchall()]
    missing = [c for c in cols_old if c not in cols_inc]
    extra = [c for c in cols_inc if c not in cols_old]
    if missing:
        # 缺列是致命的：合进去这些列会变 NULL，静默污染历史
        print('  %-30s ❌ 增量【缺列】%s' % (name, missing[:6]))
        print('     多半是抽取用错了源表（如 get_fundamentals(query(income))'
              ' vs finance.STK_INCOME_STATEMENT，两者 schema 不同）')
        return None, None
    if extra:
        # ★ 多出的列【忽略即可】，不是错。实测两个来源：
        #   · get_fundamentals 返回两个 statDate，pandas 自动改名 statDate.1
        #   · finance.run_query 默认返回的列比当初抽取时保存的多
        #   下面 SELECT 只取 cols_old，多出来的自然被丢掉。
        print('  %-30s （增量多出 %d 列，忽略：%s）'
              % (name, len(extra), extra[:4]))

    k = keys or (['id'] if 'id' in cols_old else None)
    if not k:
        print('  %-30s ❌ 没有可用的自然键（无 id 列且未在 TARGETS 指定）' % name)
        return None, None

    n_old = con.execute("SELECT count(*) FROM %s" % dst).fetchone()[0]
    # ★ 增量的每一列都 CAST 成旧表的类型 —— csv 读进来是全 varchar，
    #   不 CAST 的话 UNION ALL 会把整表拖成 varchar，写回去类型就全毁了。
    types = {r[0]: r[1] for r in con.execute(
        "DESCRIBE SELECT * FROM %s LIMIT 1" % dst).fetchall()}
    sel_inc = ','.join('try_cast("%s" AS %s) AS "%s"' % (c, types[c], c)
                       for c in cols_old)
    sel_old = ','.join('"%s"' % c for c in cols_old)
    on = ' AND '.join('o."%s"::VARCHAR IS NOT DISTINCT FROM i."%s"::VARCHAR'
                      % (c, c) for c in k)
    sql = """
      SELECT %s FROM %s i
      UNION ALL
      SELECT %s FROM %s o
      WHERE NOT EXISTS (SELECT 1 FROM %s i WHERE %s)
    """ % (sel_inc, src, sel_old, dst, src, on)
    n_new = con.execute("SELECT count(*) FROM (%s)" % sql).fetchone()[0]
    delta = n_new - n_old
    print('  %-30s %8d -> %8d  (%+d，增量 %d 行，键 %s)'
          % (name, n_old, n_new, delta, n_inc, k))
    if n_new < n_old:
        print('     ❌ 合并后【变少】了 —— 增量把历史挤掉了，拒绝写入')
        return None, None
    if not dry:
        # tmp 要保留原扩展名 —— DuckDB 的 COPY 会按扩展名推格式，
        # 直接加 .tmp 会让 .csv.gz 变成无扩展名而推成 parquet
        base, ext = (dst_path[:-7], '.csv.gz') if dst_path.endswith('.csv.gz') \
            else os.path.splitext(dst_path)
        tmp = base + '.tmp' + ext
        con.execute("COPY (%s) TO '%s' %s" % (sql, tmp, _copy_to(dst_path)))
        shutil.move(dst_path, dst_path + '.bak')
        shutil.move(tmp, dst_path)
    for v in ('_inc_pd', '_dst_pd'):
        try:
            con.unregister(v)
        except Exception:                                       # noqa: BLE001
            pass
    return n_old, n_new


# ★ 交易日历不进 duckdb —— 它不是行情表，消费者是【实盘模块】
#   assay/live.py，需要判断"下一个交易日是哪天"。面板只有有行情的日子，
#   推不出未来；春节/国庆也无法从星期推。所以从聚宽抽来直接落到
#   assay/live/（入版本控制），拿不到就让实盘模块响亮报错。
CAL_DST = os.path.join(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))), 'assay', 'live',
    'trade_calendar.json')


def _calendar(tmpd, dry):
    src = os.path.join(tmpd, 'trade_calendar.json')
    if not os.path.exists(src):
        return
    import json
    d = json.load(open(src))
    n, mx = len(d.get('days') or []), d.get('max')
    if dry:
        print('  %-30s [dry] %d 天，最远 %s' % ('trade_calendar', n, mx))
        return
    os.makedirs(os.path.dirname(CAL_DST), exist_ok=True)
    tmp = CAL_DST + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(d, f, ensure_ascii=False)
    os.replace(tmp, CAL_DST)
    print('  %-30s ✓ %d 天，最远 %s  -> %s'
          % ('trade_calendar', n, mx, os.path.relpath(CAL_DST)))


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
    _calendar(tmpd, a.dry_run)
    print('=' * 74)
    print('合并聚宽增量  %s%s' % (os.path.basename(a.tar), '  [dry-run]' if a.dry_run else ''))
    print('=' * 74)
    print('包内 %d 个文件: %s' % (len(incs), [_stem(x) for x in incs]))
    print()

    con = duckdb.connect(':memory:')
    bad, touched = 0, []
    for fn in incs:
        stem = _stem(fn)
        if REPLACE_RE.match(stem):
            # 按年分片的表：整文件替换，不走合并
            try:
                o, n = replace_file(os.path.join(tmpd, fn),
                                    os.path.join(DATA, DL, fn), a.dry_run)
            except Exception as e:                              # noqa: BLE001
                print('  %-30s ❌ 替换异常：%s' % (stem, str(e).split(chr(10))[0][:70]))
                o, n = None, None
            if o is None:
                bad += 1
            elif n != o:
                touched.append(stem)
            continue
        if stem not in TARGETS:
            print('  %-30s ⚠ 不在 TARGETS 里，跳过' % stem)
            continue
        rel, keys = TARGETS[stem]
        if rel is None:
            print('  %-30s ⏭ 该表走独立 loader，增量包里的不合（见 TARGETS 注释）' % stem)
            continue
        # ★ 每个文件独立 try/except —— 一张表解析失败不该打断整轮。
        #   实测：stk_fin_forcast.csv（业绩预告，含自由文本）触发
        #   "CSV Parser state machine reached an invalid state"，
        #   在没有这层保护时它把后面的 stk_xr_xd 也一起带没了。
        try:
            o, n = merge_one(con, os.path.join(tmpd, fn), os.path.join(DATA, rel),
                             keys, a.dry_run, use_pandas=(stem in PANDAS_CSV))
        except Exception as e:                                  # noqa: BLE001
            print('  %-30s ❌ 合并异常，跳过：%s' % (stem, str(e).split(chr(10))[0][:70]))
            o, n = None, None
        if o is None:
            bad += 1
        elif n != o:
            touched.append(stem)
    shutil.rmtree(tmpd, ignore_errors=True)

    print()
    if bad:
        # ★ 成功的那些【已经写进去了】—— 必须把它们的 loader 也列出来，
        #   否则人会以为整批没生效而不去 load，raw 与 std 就此不一致。
        print('%d 个文件没合上（见上）。' % bad)
        if touched:
            print('   但下面这些【已经合进 raw】，仍需跑对应 loader：%s' % touched)
        else:
            print('   没有任何表被改动。')
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
