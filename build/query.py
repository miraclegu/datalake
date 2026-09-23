"""只读查询本地数据（CLI + 看板 /api/query 共用）。

    python3 datalake/build/query.py "SELECT * FROM kline_bfq LIMIT 5"
    python3 datalake/build/query.py --json --sql-stdin < q.sql
    python3 datalake/build/query.py --schema

## 为什么单独一个文件

页面上要能查、命令行也要能查，而**安全规则必须只有一份**。分两处写的话，
页面那份哪天漏掉一个关键字，就能从看板上把 `mart/` 写坏 —— 而那不会报错，
只会让之后每次回测都用坏面板（面板构建脚本会落 `_FAILED` 标记，但 COPY TO
覆盖出来的"看着正常"的文件不会）。

## 只读是怎么保证的（三层，缺一层都不够）

1. **DuckDB 连到 `:memory:`，lake.db 以 `read_only` ATTACH** ——
   引擎层面就写不进去。
2. **语句白名单 + 关键字黑名单**：只允许单条 SELECT/WITH/DESCRIBE/SUMMARIZE/
   SHOW/EXPLAIN/PRAGMA；`COPY`（能 `COPY ... TO 'file'` 写盘）、`ATTACH`、
   `INSTALL`、`LOAD`、`EXPORT`、DDL/DML 一律拒。
   ⚠ 只靠第 1 层不够：`COPY (SELECT ...) TO 'x.parquet'` **不需要**可写的
   数据库连接，它直接写文件系统。这条是本文件存在的主要理由。
3. **子进程 + 超时**：看板那边 kill 得掉。`SELECT * FROM kline_bfq`（3127 万行）
   不该把服务卡死。

## 行数上限

不写 LIMIT 的查询会**自动补一个**。原因同上：3127 万行 JSON 化会把内存吃光，
而"页面转圈"看起来像卡住不像查错了。补了的话返回里带 `limit_added=True`，
页面必须显示出来 —— 悄悄截断比不查更糟（少的那部分你不知道）。
"""
import argparse
import json
import os
import re
import sys
import time

import duckdb

DL = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_LIMIT = 500
MAX_LIMIT = 50000

# 允许的开头（单条语句）
_OK_HEAD = re.compile(
    r'^\s*(select|with|describe|desc|summarize|show|explain|pragma|table|from)\b',
    re.I)
# 拒的关键字：按【词】匹配，不用子串（否则 `create_time` 这种列名会被误杀）
_BAD = re.compile(
    r'\b(copy|attach|detach|install|load|export|import|create|insert|update|'
    r'delete|drop|alter|truncate|vacuum|checkpoint|set|reset|call|'
    r'read_text|read_blob)\b', re.I)


def _strip_comments(sql):
    """去掉注释再校验 —— `/* copy */` 之类挡不住，但 `-- 说明里写了 copy`
    会被误杀。所以先去注释，再按词匹配。"""
    sql = re.sub(r'/\*.*?\*/', ' ', sql, flags=re.S)
    sql = re.sub(r'--[^\n]*', ' ', sql)
    return sql


def check(sql):
    """校验；不合法就抛 ValueError，消息直接给人看。"""
    if not (sql or '').strip():
        raise ValueError('SQL 是空的')
    body = _strip_comments(sql).strip().rstrip(';').strip()
    if not body:
        raise ValueError('SQL 里只有注释')
    # ★ 单条语句：分号后面还有东西就拒。多语句能把 SELECT 和写操作串在一起。
    if ';' in body:
        raise ValueError('只允许一条语句（分号后面还有内容）')
    if not _OK_HEAD.match(body):
        raise ValueError(
            '只允许查询：开头得是 SELECT / WITH / DESCRIBE / SUMMARIZE / '
            'SHOW / EXPLAIN / PRAGMA。收到 %r' % body[:40])
    bad = _BAD.search(body)
    if bad:
        raise ValueError(
            '不允许 %s —— 这个页面是【只读】的。'
            '（COPY ... TO 能直接写文件，所以连它一起拒；'
            '要写数据请跑 datalake/build 下对应的构建脚本）' % bad.group(0).upper())
    return body


def _add_limit(body, limit):
    """没写 LIMIT 就补一个。返回 (sql, 是否补过)。"""
    # 末尾已有 LIMIT n（可能带 OFFSET）就不动
    if re.search(r'\blimit\s+\d+\s*(offset\s+\d+\s*)?$', body, re.I):
        return body, False
    return '%s\nLIMIT %d' % (body, limit), True


def connect():
    """:memory: + 只读挂 lake.db。引擎层面写不进去。"""
    con = duckdb.connect(':memory:')
    lake = os.path.join(DL, 'lake.db')
    if os.path.isfile(lake):
        con.execute("ATTACH '%s' AS lake (READ_ONLY)" % lake)
        con.execute('USE lake')
    return con


def run(sql, limit=DEFAULT_LIMIT):
    body = check(sql)
    limit = max(1, min(int(limit or DEFAULT_LIMIT), MAX_LIMIT))
    q, added = _add_limit(body, limit)
    con = connect()
    t0 = time.time()
    cur = con.execute(q)
    cols = [d[0] for d in cur.description]
    rows = cur.fetchall()
    ms = int((time.time() - t0) * 1000)
    return {'columns': cols,
            # date/Decimal 之类交给上层 json default=str，这里不猜类型
            'rows': [list(r) for r in rows],
            'n': len(rows), 'ms': ms, 'sql': q,
            'limit': limit, 'limit_added': added,
            # 恰好等于 limit 时【可能】被截断 —— 必须说出来
            'truncated': bool(added and len(rows) >= limit)}


# ---- 有哪些表可查（页面左边的目录 + 命令行 --schema）----
# mart 下的 parquet 不在 lake.db 里，得单独列出来 —— 面板是最常查的那张，
# 漏了它等于这个功能只覆盖一半数据。
PARQUET = [
    ('panel_daily', 'mart/panel_daily/panel_*.parquet', '日频面板（71 列，回测直接吃这张）'),
    ('paused_daily', 'mart/paused_daily/*.parquet', '停牌（稀疏：未出现=未停牌）'),
    ('factor_catalog', 'mart/factor_catalog.parquet',
     '因子目录：编号/中文名/公式/说明/单位/可信度。🔴 tier=own 是【自建定义】不是聚宽那个因子'),
]


def schema():
    con = connect()
    out = {'tables': [], 'parquet': []}
    try:
        rows = con.execute("""
            SELECT table_name, table_type FROM information_schema.tables
            WHERE table_schema NOT IN ('information_schema','pg_catalog')
            ORDER BY table_name""").fetchall()
    except Exception:                                       # noqa: BLE001
        rows = []
    for name, kind in rows:
        try:
            cols = [r[0] for r in con.execute(
                'SELECT column_name FROM information_schema.columns '
                "WHERE table_name = ? ORDER BY ordinal_position",
                [name]).fetchall()]
        except Exception:                                   # noqa: BLE001
            cols = []
        out['tables'].append({'name': name, 'kind': kind, 'columns': cols})
    for name, glob, note in PARQUET:
        p = os.path.join(DL, glob)
        try:
            cols = [d[0] for d in con.execute(
                "SELECT * FROM read_parquet('%s') LIMIT 0" % p).description]
        except Exception:                                   # noqa: BLE001
            cols = []
        out['parquet'].append({'name': name, 'path': glob, 'note': note,
                               'columns': cols, 'from': "read_parquet('%s')" % p})
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('sql', nargs='?')
    ap.add_argument('--sql-stdin', action='store_true',
                    help='从 stdin 读 SQL（避免 shell 引号地狱）')
    ap.add_argument('--limit', type=int, default=DEFAULT_LIMIT)
    ap.add_argument('--json', action='store_true')
    ap.add_argument('--schema', action='store_true')
    a = ap.parse_args()
    if a.schema:
        print(json.dumps(schema(), ensure_ascii=False, default=str))
        return 0
    sql = sys.stdin.read() if a.sql_stdin else (a.sql or '')
    try:
        r = run(sql, a.limit)
    except ValueError as e:
        if a.json:
            print(json.dumps({'error': str(e)}, ensure_ascii=False))
            return 0                # 校验失败是【预期】结果，不是崩溃
        print('拒绝执行：%s' % e, file=sys.stderr)
        return 2
    except Exception as e:                                  # noqa: BLE001
        if a.json:
            print(json.dumps({'error': '%s: %s' % (type(e).__name__, e)},
                             ensure_ascii=False))
            return 0
        print('%s: %s' % (type(e).__name__, e), file=sys.stderr)
        return 1
    if a.json:
        print(json.dumps(r, ensure_ascii=False, default=str))
        return 0
    print(' | '.join(r['columns']))
    for row in r['rows']:
        print(' | '.join('' if v is None else str(v) for v in row))
    print('-- %d 行 / %d ms%s' % (r['n'], r['ms'],
                                  '（已自动加 LIMIT %d，可能不全）' % r['limit']
                                  if r['truncated'] else ''))
    return 0


if __name__ == '__main__':
    sys.exit(main())
