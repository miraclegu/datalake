#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""从备份的 DDL 重建 lake.db（视图 + 表宏），并按新目录结构替换路径。

用法:
    python3 datalake/build/rebuild_lake_db.py [--verify]

设计:
  · lake.db 只放视图与宏，数据全在 parquet —— 所以重建是幂等的、秒级的
  · 视图有依赖关系，用「反复重试直到无进展」做拓扑排序，不用手写顺序
  · 路径映射按【具体在前、笼统在后】的顺序替换，否则 l0/ 的笼统规则会吃掉 l0/kline
"""
import io, os, re, sys, json
import duckdb

ROOT   = '/Users/guhao/finacial'
DDL    = os.path.join(ROOT, 'datalake/_backup/pit_rebuild.sql')
CODEMAP= os.path.join(ROOT, 'datalake/_backup/_code_map.parquet')
BASE   = os.path.join(ROOT, 'datalake/_backup/baseline.json')
TARGET = os.path.join(ROOT, 'datalake/lake.db')

# 顺序敏感：具体路径必须在笼统路径之前
PATH_MAP = [
    ('/Users/guhao/finacial/pitdb/l0/kline',                 '/Users/guhao/finacial/datalake/raw/tdx/kline'),
    ('/Users/guhao/finacial/pitdb/l0/basic',                 '/Users/guhao/finacial/datalake/raw/tdx/basic'),
    ('/Users/guhao/finacial/pitdb/l0/adjust_factor.parquet',  '/Users/guhao/finacial/datalake/raw/tdx/adjust_factor.parquet'),
    ('/Users/guhao/finacial/pitdb/l0/financials',            '/Users/guhao/finacial/datalake/raw/jq/financials'),
    ('/Users/guhao/finacial/pitdb/l0/',                      '/Users/guhao/finacial/datalake/raw/jq/'),
    ('/Users/guhao/finacial/pitdb/l1',                       '/Users/guhao/finacial/datalake/std'),
]

def remap(sql):
    for old, new in PATH_MAP:
        sql = sql.replace(old, new)
    return sql

def main():
    raw = io.open(DDL, encoding='utf-8').read()
    stmts = [s.strip() for s in raw.split(';') if s.strip() and not s.strip().startswith('--\n')]
    # 拆成 (kind, name, sql)
    items = []
    for blk in re.split(r'\n\s*\n', raw):
        m = re.search(r'--\s*=====\s*(VIEW|MACRO)\s+(\S+)\s*=====', blk)
        if not m:
            continue
        body = re.sub(r'--\s*=====.*?=====\s*', '', blk).strip().rstrip(';')
        if body:
            items.append((m.group(1), m.group(2), remap(body)))
    views  = [(n, s) for k, n, s in items if k == 'VIEW']
    macros = [(n, s) for k, n, s in items if k == 'MACRO']
    print('待建：视图 %d 个，表宏 %d 个' % (len(views), len(macros)))

    # 未映射到的残留【旧】路径检查（旧 = pitdb/*，出现即说明 PATH_MAP 有遗漏）
    leftover = set()
    for _, s in views + macros:
        leftover |= set(re.findall(r'/Users/guhao/finacial/pitdb/[^\']*', s))
    if leftover:
        print('❌ 仍有未映射的旧路径，中止：')
        for p in sorted(leftover):
            print('   ', p)
        sys.exit(1)
    print('✅ 无残留旧路径')

    if os.path.exists(TARGET):
        os.remove(TARGET)
    con = duckdb.connect(TARGET)

    # 基表 _code_map
    con.execute("CREATE TABLE _code_map AS SELECT * FROM read_parquet('%s')" % CODEMAP)
    print('  _code_map: %d 行' % con.execute('select count(*) from _code_map').fetchone()[0])

    # 视图：反复重试直到无进展（隐式拓扑排序）
    pending, rnd = list(views), 0
    while pending:
        rnd += 1
        failed, made = [], 0
        for n, s in pending:
            try:
                con.execute(s)
                made += 1
            except Exception as e:
                failed.append((n, s, str(e)[:90]))
        print('  轮%d: 成功 %d，待重试 %d' % (rnd, made, len(failed)))
        if made == 0:
            print('❌ 无进展，剩余无法创建：')
            for n, _, e in failed:
                print('   %-24s %s' % (n, e))
            sys.exit(1)
        pending = [(n, s) for n, s, _ in failed]
    print('✅ 视图全部创建')

    # 表宏之间也有依赖（dividend_ttm_at 依赖 dividend_visible_at），同样重试
    pending, rnd = list(macros), 0
    while pending:
        rnd += 1
        failed, made = [], 0
        for n, s in pending:
            try:
                con.execute(s)
                made += 1
            except Exception as e:
                failed.append((n, s, str(e)[:90]))
        print('  宏轮%d: 成功 %d，待重试 %d' % (rnd, made, len(failed)))
        if made == 0:
            print('❌ 宏无进展：')
            for n, _, e in failed:
                print('   %-24s %s' % (n, e))
            sys.exit(1)
        pending = [(n, s) for n, s, _ in failed]
    print('✅ 表宏 %d 个创建' % len(macros))
    con.close()

    if '--verify' in sys.argv:
        verify()

def verify():
    base = json.loads(io.open(BASE, encoding='utf-8').read())
    con = duckdb.connect(TARGET, read_only=True)
    bad = []
    print('\n=== 比对基线 ===')
    for k, expect in sorted(base.items()):
        kind, name = k.split(':', 1)
        try:
            if kind in ('view', 'table'):
                got = con.execute('select count(*) from "%s"' % name).fetchone()[0]
            elif kind == 'macro':
                fn, arg = name.split('(')
                got = con.execute("select count(*) from %s(DATE '%s')"
                                  % (fn, arg.rstrip(')'))).fetchone()[0]
            else:  # pit 语义
                d = name.split('@')[1]
                got = con.execute("""select name from security_name where code='601766.XSHG'
                    and valid_from <= DATE '%s' and (valid_to is null or valid_to > DATE '%s')
                    and known_from <= DATE '%s'""" % (d, d, d)).fetchone()[0]
        except Exception as e:
            got = 'ERR: %s' % str(e)[:60]
        ok = (got == expect)
        if not ok:
            bad.append((k, expect, got))
        if not ok or (isinstance(got, int) and got > 100000) or kind in ('macro', 'pit'):
            print('  %s %-44s 期望 %-12s 实际 %s' % ('✅' if ok else '❌', k, expect, got))
    con.close()
    print()
    if bad:
        print('❌ %d 项不一致：' % len(bad))
        for k, e, g in bad:
            print('   %-44s 期望 %s 实际 %s' % (k, e, g))
        sys.exit(1)
    print('✅ 全部 %d 项与基线一致' % len(base))

if __name__ == '__main__':
    main()
