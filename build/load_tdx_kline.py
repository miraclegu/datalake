#!/usr/bin/env python3
"""把 tdx2db 的日线接入 PIT 库（L0 原样 + L1 代码映射/日历 + 复权视图）。

用法:
    python datalake/build/load_tdx_kline.py              # 加载 + 校验
    python datalake/build/load_tdx_kline.py --demo       # 额外跑完整 PIT 查询演示

范围（2026-08-25 更新）:
    · 品种: stock + etf + index + block
        index = 官方指数(sh00*/sz39*), 用于基准与市场状态闸门
        block = 通达信自定义板块指数(sh88*), 1128 只 / 714 万行, 用于行业动量与板块轮动
    · ⚠️ kline_raw / kline_bfq|hfq|qfq 混合四个品种（沿用 index 既有做法）,
      消费方**必须** JOIN code_map 并按 class 过滤, 否则股票截面里会混进板块指数
    · 起点: 2003-01-01                   (与 PIT 维度、财务的可信起点对齐)
    · **北交所(bj*) 排除**               (有日线但聚宽这批 PIT 数据不含它们, 见下)

复权口径: 精确复刻 tdx 视图公式, 含 round(,2) —— 这样与线上 screener 结果逐位一致。
    hfq = round(price * COALESCE(hfq_factor, 1), 2)
    qfq = round(price * COALESCE(hfq_factor, 1) / latest_hfq, 2)
只存原始价 + 因子, 不存复权价（2026-08-23 已验证 hfq 稳定、qfq 会整体平移）。
"""
import argparse
import os
import sys

import duckdb

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
L0 = os.path.join(ROOT, 'raw', 'tdx')      # 冻结快照落点
L1 = os.path.join(ROOT, 'std')
DB = os.path.join(ROOT, 'lake.db')
TDX = '/Users/guhao/finacial/tdx2db/tdx.db'

START = '2003-01-01'
CLASSES = ('stock', 'etf', 'index', 'block')
EXCLUDE_PREFIX = ('bj',)          # 北交所: 有日线但无 PIT 维度, 本轮排除
FAILURES = []


def check(cond, msg):
    print(('  ✓ ' if cond else '  ✗ ') + msg)
    if not cond:
        FAILURES.append(msg)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--demo', action='store_true')
    args = ap.parse_args()
    for d in (os.path.join(L0, 'kline'), os.path.join(L0, 'basic'), L1):
        if not os.path.exists(d):
            os.makedirs(d)

    con = duckdb.connect(DB)
    con.execute("ATTACH '%s' AS tdx (READ_ONLY)" % TDX)

    # ------------------------------------------------------- L1: 代码映射表
    # tdx 用 sz000001 / sh600000 / bj430017; 聚宽用 000001.XSHE / 600000.XSHG。
    # 机械映射后仍有 775 只对不上, 必须逐类判定去处而不是一句"映射一下"糊过去。
    print('=' * 72)
    print('L1: 代码映射表')
    print('=' * 72)
    con.execute("""
    CREATE OR REPLACE TABLE _code_map AS
    WITH k AS (
        SELECT DISTINCT c.class, k.symbol AS tdx_symbol,
               CASE WHEN k.symbol LIKE 'sh%' THEN substr(k.symbol,3) || '.XSHG'
                    WHEN k.symbol LIKE 'sz%' THEN substr(k.symbol,3) || '.XSHE'
                    WHEN k.symbol LIKE 'bj%' THEN substr(k.symbol,3) || '.BJSE'
               END AS jq_code
        FROM tdx.raw_kline_daily k
        JOIN tdx.raw_symbol_class c ON c.symbol = k.symbol
        WHERE c.class IN ('stock','etf','index','block')
    )
    SELECT COALESCE(k.class, 'pit_only')                       AS class,
           k.tdx_symbol, COALESCE(k.jq_code, u.code)           AS jq_code,
           CASE
             WHEN k.tdx_symbol IS NULL                THEN 'pit_only'
             WHEN u.code IS NOT NULL                  THEN 'matched'
             WHEN k.tdx_symbol LIKE 'bj%'             THEN 'kline_only_bj'
             WHEN k.tdx_symbol LIKE 'sh900%'
               OR k.tdx_symbol LIKE 'sz200%'          THEN 'kline_only_bshare'
             WHEN k.class = 'etf'                     THEN 'kline_only_etf'
             WHEN k.class = 'index'                   THEN 'kline_only_index'
             WHEN k.class = 'block'                   THEN 'kline_only_block'
             ELSE 'kline_only_other'
           END                                                 AS match_type,
           (k.tdx_symbol IS NOT NULL
            AND NOT (k.tdx_symbol LIKE 'bj%'))                 AS included
    FROM k FULL OUTER JOIN security_universe u ON u.code = k.jq_code
    """)
    m = con.execute("""SELECT match_type, count(*) n FROM _code_map
                       GROUP BY 1 ORDER BY 2 DESC""").fetchall()
    for mt, n in m:
        print('  %-20s %6d' % (mt, n))
    con.execute("COPY _code_map TO '%s' (FORMAT parquet, COMPRESSION zstd)"
                % os.path.join(L1, 'code_map.parquet'))
    inc = con.execute('SELECT count(*) FROM _code_map WHERE included').fetchone()[0]
    print('  → 本轮纳入 %d 只 (排除北交所与 pit_only)' % inc)

    # ------------------------------------------------------------ L0: 日线
    print('\n' + '=' * 72)
    print('L0: 日线 (%s 起, 排除 %s)' % (START, EXCLUDE_PREFIX))
    print('=' * 72)
    excl = ' AND '.join("k.symbol NOT LIKE '%s%%'" % p for p in EXCLUDE_PREFIX)
    total_rows = 0
    for cls in CLASSES:
        yrs = con.execute("""
            SELECT DISTINCT year(k.date) y FROM tdx.raw_kline_daily k
            JOIN tdx.raw_symbol_class c ON c.symbol=k.symbol AND c.class=?
            WHERE k.date >= ? AND %s ORDER BY 1""" % excl, [cls, START]).fetchall()
        n_cls = 0
        for (y,) in yrs:
            out = os.path.join(L0, 'kline', '%s_%d.parquet' % (cls, y))
            con.execute("""
                COPY (SELECT k.symbol, k.date, k.open, k.high, k.low, k.close,
                             k.volume, k.amount
                      FROM tdx.raw_kline_daily k
                      JOIN tdx.raw_symbol_class c ON c.symbol=k.symbol AND c.class=?
                      WHERE year(k.date)=? AND %s
                      ORDER BY k.symbol, k.date)
                TO '%s' (FORMAT parquet, COMPRESSION zstd)""" % (excl, out), [cls, y])
            n_cls += con.execute("SELECT count(*) FROM read_parquet('%s')" % out).fetchone()[0]
        mb = sum(os.path.getsize(os.path.join(L0, 'kline', f))
                 for f in os.listdir(os.path.join(L0, 'kline'))
                 if f.startswith(cls + '_')) / 1048576.0
        total_rows += n_cls
        print('  %-7s %2d 年 %12s 行  %7.1f MB' % (cls, len(yrs), format(n_cls, ','), mb))

    # ------------------------------------------- L0: basic_daily(市值/换手等)
    print('\nL0: basic_daily (preclose/turnover/floatmv/totalmv/change_pct)')
    for cls in ('stock', 'etf'):        # 实测只覆盖 stock 与 etf
        out = os.path.join(L0, 'basic', '%s.parquet' % cls)
        con.execute("""
            COPY (SELECT b.* FROM tdx.raw_basic_daily b
                  JOIN tdx.raw_symbol_class c ON c.symbol=b.symbol AND c.class=?
                  WHERE b.date >= ? AND %s
                  ORDER BY b.symbol, b.date)
            TO '%s' (FORMAT parquet, COMPRESSION zstd)"""
            % (excl.replace('k.symbol', 'b.symbol'), out), [cls, START])
        n = con.execute("SELECT count(*) FROM read_parquet('%s')" % out).fetchone()[0]
        print('  %-7s %12s 行  %6.1f MB' % (cls, format(n, ','),
              os.path.getsize(out) / 1048576.0))

    # ------------------------------------------------------- L0: 复权因子
    out = os.path.join(L0, 'adjust_factor.parquet')
    con.execute("""COPY (SELECT f.* FROM tdx.raw_adjust_factor f
                         WHERE f.date >= ? AND %s
                         ORDER BY f.symbol, f.date)
                   TO '%s' (FORMAT parquet, COMPRESSION zstd)"""
                % (excl.replace('k.symbol', 'f.symbol'), out), [START])
    nf = con.execute("SELECT count(*) FROM read_parquet('%s')" % out).fetchone()[0]
    print('\nL0: adjust_factor %s 行  %.1f MB' % (format(nf, ','),
          os.path.getsize(out) / 1048576.0))

    # -------------------------------------------------------- L1: 交易日历
    # 今天发现的"9.9% sell_date 落在周末"就是缺这张表。做成一等公民。
    cal = os.path.join(L1, 'trading_calendar.parquet')
    # 注意: DISTINCT 必须在子查询里先去重再编号 —— 写成
    # `SELECT DISTINCT date, row_number() OVER (...)` 是无效的, 因为 row_number
    # 每行唯一, DISTINCT 作用于整行等于没去重(会得到 1600 万行而不是 5800 行)。
    con.execute("""COPY (
        SELECT date, row_number() OVER (ORDER BY date) AS seq
        FROM (SELECT DISTINCT k.date AS date FROM read_parquet('%s') k)
        ORDER BY date)
        TO '%s' (FORMAT parquet, COMPRESSION zstd)"""
        % (os.path.join(L0, 'kline', 'stock_*.parquet'), cal))
    nc = con.execute("SELECT count(*) FROM read_parquet('%s')" % cal).fetchone()[0]
    print('L1: trading_calendar %d 个交易日' % nc)

    # ---------------------------------------------------------- 视图层
    print('\n' + '=' * 72)
    print('视图层 (复权公式精确复刻 tdx, 含 round(,2))')
    print('=' * 72)
    KL = os.path.join(L0, 'kline', '*.parquet')
    AF = os.path.join(L0, 'adjust_factor.parquet')
    BS = os.path.join(L0, 'basic', '*.parquet')
    for v in ('kline_raw', 'kline_bfq', 'kline_hfq', 'kline_qfq',
              'basic_daily', 'adjust_factor', 'trading_calendar', 'code_map'):
        con.execute('DROP VIEW IF EXISTS %s' % v)
    con.execute("CREATE VIEW kline_raw AS SELECT * FROM read_parquet('%s')" % KL)
    con.execute("CREATE VIEW adjust_factor AS SELECT * FROM read_parquet('%s')" % AF)
    con.execute("CREATE VIEW basic_daily AS SELECT * FROM read_parquet('%s')" % BS)
    con.execute("CREATE VIEW trading_calendar AS SELECT * FROM read_parquet('%s')" % cal)
    con.execute("CREATE VIEW code_map AS SELECT * FROM read_parquet('%s')"
                % os.path.join(L1, 'code_map.parquet'))
    con.execute("""CREATE VIEW kline_bfq AS
        SELECT k.symbol, m.jq_code, k.date, k.open, k.high, k.low, k.close,
               k.volume, k.amount, COALESCE(f.hfq_factor, 1) AS hfq_factor
        FROM kline_raw k
        LEFT JOIN adjust_factor f ON f.symbol=k.symbol AND f.date=k.date
        LEFT JOIN code_map m ON m.tdx_symbol=k.symbol""")
    con.execute("""CREATE VIEW kline_hfq AS
        SELECT k.symbol, m.jq_code, k.date,
               round(k.open  * COALESCE(f.hfq_factor,1), 2) AS open,
               round(k.high  * COALESCE(f.hfq_factor,1), 2) AS high,
               round(k.low   * COALESCE(f.hfq_factor,1), 2) AS low,
               round(k.close * COALESCE(f.hfq_factor,1), 2) AS close,
               k.volume, k.amount, COALESCE(f.hfq_factor,1) AS hfq_factor
        FROM kline_raw k
        LEFT JOIN adjust_factor f ON f.symbol=k.symbol AND f.date=k.date
        LEFT JOIN code_map m ON m.tdx_symbol=k.symbol""")
    con.execute("""CREATE VIEW kline_qfq AS
        WITH latest AS (SELECT symbol, argmax(hfq_factor, date) AS lf
                        FROM adjust_factor GROUP BY symbol)
        SELECT k.symbol, m.jq_code, k.date,
               round(k.open  * COALESCE(f.hfq_factor,1)/COALESCE(l.lf,1), 2) AS open,
               round(k.high  * COALESCE(f.hfq_factor,1)/COALESCE(l.lf,1), 2) AS high,
               round(k.low   * COALESCE(f.hfq_factor,1)/COALESCE(l.lf,1), 2) AS low,
               round(k.close * COALESCE(f.hfq_factor,1)/COALESCE(l.lf,1), 2) AS close,
               k.volume, k.amount
        FROM kline_raw k
        LEFT JOIN adjust_factor f ON f.symbol=k.symbol AND f.date=k.date
        LEFT JOIN latest l ON l.symbol=k.symbol
        LEFT JOIN code_map m ON m.tdx_symbol=k.symbol""")
    vs = [r[0] for r in con.execute("SELECT view_name FROM duckdb_views() "
                                    "WHERE NOT internal ORDER BY 1").fetchall()]
    print('  共 %d 个视图: %s' % (len(vs), vs))

    # ------------------------------------------------------------------ 校验
    print('\n' + '=' * 72)
    print('校验')
    print('=' * 72)
    n_new = con.execute('SELECT count(*) FROM kline_raw').fetchone()[0]
    n_tdx = con.execute("""SELECT count(*) FROM tdx.raw_kline_daily k
        JOIN tdx.raw_symbol_class c ON c.symbol=k.symbol
        WHERE c.class IN ('stock','etf','index','block') AND k.date >= ?
          AND k.symbol NOT LIKE 'bj%'""", [START]).fetchone()[0]
    check(n_new == n_tdx, '日线行数与 tdx 一致 (%s vs %s)'
          % (format(n_new, ','), format(n_tdx, ',')))

    # 复权口径: 与 tdx 视图逐行比对(抽样)
    d = con.execute("""SELECT count(*) n,
          sum(CASE WHEN abs(a.close - b.close) < 0.011 THEN 1 ELSE 0 END) same
        FROM kline_hfq a JOIN tdx.v_stock_hfq b
          ON b.symbol=a.symbol AND b.date=a.date
        WHERE a.date >= DATE '2020-01-01'""").fetchone()
    check(d[0] > 0 and d[1] == d[0], '后复权价与 tdx v_stock_hfq 一致 (%s/%s)'
          % (format(d[1], ','), format(d[0], ',')))

    # 存活偏差交叉验证: 2015 年末在市数 vs PIT 维度
    n15k = con.execute("""SELECT count(DISTINCT k.symbol) FROM kline_raw k
        JOIN code_map m ON m.tdx_symbol=k.symbol
        WHERE m.class='stock' AND m.match_type='matched'
          AND k.date BETWEEN DATE '2015-12-01' AND DATE '2015-12-31'""").fetchone()[0]
    n15p = con.execute("SELECT count(*) FROM l0_dim_security_asof "
                       "WHERE as_of='2015-12-31'").fetchone()[0]
    check(abs(n15k - n15p) / float(n15p) < 0.06,
          '2015-12 有日线的股票 %d vs PIT 在市 %d (差 %.1f%%)'
          % (n15k, n15p, 100.0 * abs(n15k - n15p) / n15p))

    # 交易日历: 不应有周末
    wk = con.execute("SELECT count(*) FROM trading_calendar "
                     "WHERE dayofweek(date) IN (0,6)").fetchone()[0]
    check(wk == 0, '交易日历无周末 (异常 %d)' % wk)

    # 刻度突变检测(2026-05-25 ETF ×10 事件应已被 tdx 侧修复)
    j = con.execute("""
        WITH p AS (SELECT k.symbol, k.date, k.close,
                          lag(k.close) OVER (PARTITION BY k.symbol ORDER BY k.date) pc
                   FROM kline_raw k JOIN code_map m ON m.tdx_symbol=k.symbol
                   WHERE m.class='etf' AND k.date BETWEEN DATE '2026-05-15' AND DATE '2026-06-05')
        SELECT count(*) FROM p WHERE pc>0 AND (close/pc > 5 OR close/pc < 0.2)""").fetchone()[0]
    check(j == 0, 'ETF 在 2026-05-25 附近无 10 倍刻度跳变 (异常 %d)' % j)

    # ------------------------------------------------------------------ 演示
    if args.demo:
        print('\n' + '=' * 72)
        print('完整 PIT 查询: 2015-06-30 那天, 我能看到什么')
        print('=' * 72)
        # 注意: 不要用 `WITH d AS (SELECT DATE '...' AS dt)` 再到处 (SELECT dt FROM d) ——
        # 那种写法在本版 DuckDB 上会触发内部绑定错误
        # (INTERNAL Error: inequal types UBIGINT != BOOLEAN)。直接用字面量。
        D = "DATE '2015-06-30'"
        print(con.execute("""
        WITH vis AS (          -- 当时已公告的最新一期财报
            SELECT code, report_date, pub_date, np_parent_company_owners,
                   row_number() OVER (PARTITION BY code ORDER BY report_date DESC) rn
            FROM fin_income
            -- 用 `pub_date > report_date` 而不是 pub_date_is_placeholder 列:
            -- 二者等价(标记的定义就是 pub_date <= report_date), 但在本版 DuckDB 上
            -- 把这个 BOOLEAN 列放进 CTE + 多个 LEFT JOIN 会触发内部断言失败
            -- (ColumnBindingResolver: inequal types UBIGINT != BOOLEAN) —— 那是
            -- DuckDB 的 bug, 不是 SQL 写错。用条件表达式绕开。
            WHERE pub_date <= {D} AND pub_date > report_date
        )
        SELECT k.jq_code, n.name AS 当时名称, i.sw_l1_name AS 当时行业,
               k.close AS 后复权收盘,
               round(b.totalmv/1e8, 1) AS 总市值亿,
               round(f.np_parent_company_owners/1e8, 2) AS 可见归母净利亿,
               f.report_date AS 报告期, f.pub_date AS 公告日
        FROM kline_hfq k
        JOIN code_map m ON m.tdx_symbol = k.symbol AND m.match_type = 'matched'
        LEFT JOIN basic_daily b ON b.symbol = k.symbol AND b.date = k.date
        LEFT JOIN security_name n ON n.code = k.jq_code
             AND n.valid_from <= {D} AND (n.valid_to IS NULL OR n.valid_to > {D})
             AND n.known_from <= {D}
        LEFT JOIN security_industry i ON i.code = k.jq_code
             AND i.valid_from <= {D} AND (i.valid_to IS NULL OR i.valid_to > {D})
        LEFT JOIN vis f ON f.code = k.jq_code AND f.rn = 1
        WHERE k.date = {D} AND m.class = 'stock'
        ORDER BY b.totalmv DESC NULLS LAST LIMIT 10""".format(D=D)).df().to_string(index=False))
        print('\n↑ 名称/行业是当时的, 财报是当时已公告的最新一期 —— 全部无未来函数')

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
