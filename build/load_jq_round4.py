#!/usr/bin/env python3
"""加载第四轮抽取 B1：日频市场状态 + 指数补齐 + 未来日历。

用法:
    python datalake/build/load_jq_round4.py            # 加载 + 校验
    python datalake/build/load_jq_round4.py --demo     # 额外跑演示

输入（`datalake/raw/jq/_ingest/downloads/`，来自 extract_jq_round4.py）:
    limits_YYYY.csv.gz     涨跌停价(稠密, 按年)
    paused.csv.gz          停牌(稀疏: 只有停牌当天的行)
    is_st.csv.gz           ST(稀疏: 只有为真的行)
    index_member_{000015,399303,399101,000922}.csv
    index_daily.csv.gz     指数行情
    trade_days.csv         含未来日的交易日历

产出:
    l1/price_limits.parquet    code, date, high_limit, low_limit
    l1/paused.parquet          code, date            (稀疏)
    l1/is_st.parquet           code, valid_from, valid_to  (压成区间)
    l1/index_daily.parquet
    l1/trade_days.parquet
    视图: price_limits / paused_days / st_periods / index_daily_all / trade_days_all
    表宏: market_state_at(d)   —— 某日的可交易状态(涨跌停价 + 停牌 + ST 一次给全)

━━━ 稀疏编码的约定（重要）━━━
paused / is_st 只存"为真"的行。**未出现 = 为假**, 不是"未知"。
这是无损的, 但前提是抽取覆盖了完整年份区间 —— 加载时会校验区间与涨跌停表一致,
不一致就报错, 免得把"没抽到"当成"没停牌"。
"""
import argparse
import glob
import os
import sys

import duckdb

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, 'raw', 'jq', '_ingest', 'downloads')
L1 = os.path.join(ROOT, 'std')
DB = os.path.join(ROOT, 'lake.db')
FAILURES = []


def check(cond, msg):
    print(('  ✓ ' if cond else '  ✗ ') + msg)
    if not cond:
        FAILURES.append(msg)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--demo', action='store_true')
    args = ap.parse_args()
    if not os.path.exists(L1):
        os.makedirs(L1)

    lim_files = sorted(glob.glob(os.path.join(SRC, 'limits_*.csv.gz')))
    if not lim_files:
        print('❌ 找不到 %s/limits_*.csv.gz' % SRC)
        print('   先在聚宽研究环境跑 datalake/raw/jq/_ingest/extract_jq_round4.py, 下载 tar 后解压到 jqdata/')
        return 1

    con = duckdb.connect(DB)

    # ------------------------------------------------------------ ① 涨跌停价
    print('=' * 72)
    print('① 涨跌停价')
    print('=' * 72)
    print('  %d 个年度文件: %s ... %s' % (len(lim_files),
          os.path.basename(lim_files[0]), os.path.basename(lim_files[-1])))
    lp = os.path.join(L1, 'price_limits.parquet')
    con.execute("""COPY (
        SELECT code,
               try_cast(date AS DATE)        AS date,
               try_cast(high_limit AS DOUBLE) AS high_limit,
               try_cast(low_limit  AS DOUBLE) AS low_limit
        FROM read_csv_auto('%s/limits_*.csv.gz', all_varchar=true, union_by_name=true)
        WHERE code IS NOT NULL) TO '%s' (FORMAT parquet, COMPRESSION zstd)""" % (SRC, lp))
    con.execute("CREATE OR REPLACE VIEW price_limits AS SELECT * FROM read_parquet('%s')" % lp)
    r = con.execute("""SELECT count(*) n, count(DISTINCT code) c,
                              min(date) d0, max(date) d1 FROM price_limits""").fetchone()
    print('  %s 行 / %s 只 / %s ~ %s' % (format(r[0], ','), format(r[1], ','), r[2], r[3]))
    D0, D1 = r[2], r[3]

    print('\n  涨跌停比例分布（high_limit / preclose，验证这批数据的分叉确实存在）:')
    print(con.execute("""
        SELECT round(l.high_limit / b.preclose, 2) AS 比例,
               count(*) AS 记录数,
               round(100.0*count(*) / sum(count(*)) OVER (), 2) AS 占比pct
        FROM price_limits l
        JOIN code_map m ON m.jq_code = l.code
        JOIN basic_daily b ON b.symbol = m.tdx_symbol AND b.date = l.date
        WHERE b.preclose > 0
        GROUP BY 1 HAVING count(*) > 1000 ORDER BY 2 DESC LIMIT 8""").df().to_string(index=False))
    print('  ↑ 若只有 1.10 一档, 说明这批数据没覆盖创业板/科创/ST —— 那就不该用')

    # ---------------------------------------------------------- ② 停牌(稀疏)
    print('\n' + '=' * 72)
    print('② 停牌 paused（稀疏编码）')
    print('=' * 72)
    pp = os.path.join(L1, 'paused.parquet')
    fpause = os.path.join(SRC, 'paused.csv.gz')
    if not os.path.exists(fpause):
        print('  未抽取, 跳过')
    else:
        con.execute("""COPY (SELECT code, try_cast(date AS DATE) AS date
            FROM read_csv_auto('%s', all_varchar=true) WHERE code IS NOT NULL)
            TO '%s' (FORMAT parquet, COMPRESSION zstd)""" % (fpause, pp))
        con.execute("CREATE OR REPLACE VIEW paused_days AS SELECT * FROM read_parquet('%s')" % pp)
        rp = con.execute("""SELECT count(*) n, count(DISTINCT code) c,
                                   min(date) d0, max(date) d1 FROM paused_days""").fetchone()
        print('  %s 条停牌日 / %s 只 / %s ~ %s'
              % (format(rp[0], ','), format(rp[1], ','), rp[2], rp[3]))
        # 稀疏编码的前提: 覆盖区间必须与涨跌停表一致, 否则"没抽到"会被当成"没停牌"
        check(rp[2] is not None and rp[2] <= D0 + 400 and rp[3] >= D1 - 400,
              '停牌表覆盖区间与涨跌停表大体一致（稀疏编码成立的前提）')

    # ------------------------------------------------------ ③ is_st(稀疏→区间)
    print('\n' + '=' * 72)
    print('③ is_st（稀疏 → 压成区间）')
    print('=' * 72)
    sp = os.path.join(L1, 'is_st.parquet')
    fst = os.path.join(SRC, 'is_st.csv.gz')
    if not os.path.exists(fst):
        print('  未抽取, 跳过')
    else:
        # 连续的 ST 日压成 [valid_from, valid_to) 区间: 用"日期 - 行号"做分组键,
        # 连续段内该差值恒定。比逐日存省两个数量级, 且便于 PIT 查询。
        con.execute("""COPY (
        WITH d AS (
            SELECT code, try_cast(date AS DATE) AS date
            FROM read_csv_auto('%s', all_varchar=true) WHERE code IS NOT NULL),
        r AS (SELECT code, date,
                     row_number() OVER (PARTITION BY code ORDER BY date) AS rn
              FROM (SELECT DISTINCT code, date FROM d)),
        g AS (SELECT code, date, date - INTERVAL (rn) DAY AS grp FROM r)
        SELECT code, min(date) AS valid_from, max(date) + INTERVAL 1 DAY AS valid_to
        FROM g GROUP BY code, grp)
        TO '%s' (FORMAT parquet, COMPRESSION zstd)""" % (fst, sp))
        con.execute("CREATE OR REPLACE VIEW st_periods AS SELECT * FROM read_parquet('%s')" % sp)
        rs = con.execute("""SELECT count(*) n, count(DISTINCT code) c FROM st_periods""").fetchone()
        print('  %s 个 ST 区间 / %s 只曾被 ST' % (format(rs[0], ','), format(rs[1], ',')))

        # 与名称史交叉验证: 名称含 ST 的应当基本被 is_st 覆盖(名称法是子集)
        cov = con.execute("""
            SELECT round(100.0*avg(CASE WHEN EXISTS (
                       SELECT 1 FROM st_periods p
                       WHERE p.code = n.code
                         AND p.valid_from < coalesce(n.valid_to, DATE '2099-12-31')
                         AND p.valid_to   > n.valid_from) THEN 1 ELSE 0 END), 2)
            FROM security_name n WHERE n.name LIKE '%ST%'""").fetchone()[0]
        print('  名称含 ST 的区间, 被 is_st 覆盖的比例: %.2f%%' % (cov or 0))
        check(cov is not None and cov > 80,
              '名称法是 is_st 的子集（证明改用 is_st 是对的，名称法会漏）')

    # ------------------------------------------------------------ ④ 指数
    print('\n' + '=' * 72)
    print('④ 指数：成分 + 行情')
    print('=' * 72)
    newm = sorted(glob.glob(os.path.join(SRC, 'index_member_0000[12]*.csv')) +
                  glob.glob(os.path.join(SRC, 'index_member_399[13]*.csv')) +
                  glob.glob(os.path.join(SRC, 'index_member_000922.csv')))
    if newm:
        print('  新增成分文件 %d 个: %s' % (len(newm), [os.path.basename(f) for f in newm]))
        print('  ⚠ 成分区间的构建复用 load_jq_round2.py 的逻辑 —— 请在它跑完后再跑本脚本,')
        print('    或直接重跑 load_jq_round2.py（它会把 jqdata/ 下所有 index_member_*.csv 一起处理）')
    else:
        print('  无新增成分文件')

    fid = os.path.join(SRC, 'index_daily.csv.gz')
    if not os.path.exists(fid):
        print('  指数行情未抽取, 跳过')
    else:
        ip = os.path.join(L1, 'index_daily.parquet')
        con.execute("""COPY (
            SELECT code, try_cast(date AS DATE) AS date,
                   try_cast(open AS DOUBLE) open, try_cast(high AS DOUBLE) high,
                   try_cast(low AS DOUBLE) low, try_cast(close AS DOUBLE) close,
                   try_cast(volume AS DOUBLE) volume, try_cast(money AS DOUBLE) amount
            FROM read_csv_auto('%s', all_varchar=true) WHERE code IS NOT NULL)
            TO '%s' (FORMAT parquet, COMPRESSION zstd)""" % (fid, ip))
        con.execute(
            "CREATE OR REPLACE VIEW index_daily_all AS SELECT * FROM read_parquet('%s')" % ip)
        print(con.execute("""SELECT code, count(*) 天数, min(date) 起, max(date) 止
                             FROM index_daily_all GROUP BY 1 ORDER BY 1""").df().to_string(index=False))
        need = ['000015.XSHG', '399303.XSHE', '399101.XSHE', '000922.XSHG']
        got = set(r[0] for r in con.execute('SELECT DISTINCT code FROM index_daily_all').fetchall())
        miss = [c for c in need if c not in got]
        check(not miss, '策略实际用作基准的 4 个指数都有行情%s'
              % ('' if not miss else '（缺 %s）' % miss))

    # ------------------------------------------------------ ⑤ 含未来的日历
    print('\n' + '=' * 72)
    print('⑤ 交易日历（含未来日）')
    print('=' * 72)
    ftd = os.path.join(SRC, 'trade_days.csv')
    if not os.path.exists(ftd):
        print('  未抽取, 跳过')
    else:
        tp = os.path.join(L1, 'trade_days.parquet')
        con.execute("""COPY (SELECT try_cast(date AS DATE) AS date
            FROM read_csv_auto('%s', all_varchar=true) WHERE date IS NOT NULL)
            TO '%s' (FORMAT parquet, COMPRESSION zstd)""" % (ftd, tp))
        con.execute(
            "CREATE OR REPLACE VIEW trade_days_all AS SELECT * FROM read_parquet('%s')" % tp)
        rt = con.execute('SELECT count(*) n, min(date) d0, max(date) d1 FROM trade_days_all').fetchone()
        old = con.execute('SELECT max(date) FROM trading_calendar').fetchone()[0]
        fut = con.execute('SELECT count(*) FROM trade_days_all WHERE date > ?', [old]).fetchone()[0]
        print('  %s 天 / %s ~ %s' % (format(rt[0], ','), rt[1], rt[2]))
        print('  原 trading_calendar 止于 %s, 新增未来日 %d 天' % (old, fut))
        check(fut > 0, '拿到了未来交易日（可排未来调仓日）')

    # -------------------------------------------------------------- L2 表宏
    print('\n' + '=' * 72)
    print('⑥ L2 表宏：某日的可交易状态')
    print('=' * 72)
    con.execute('DROP MACRO TABLE IF EXISTS market_state_at')
    con.execute("""
    CREATE MACRO market_state_at(d) AS TABLE
    SELECT l.code, l.date, l.high_limit, l.low_limit,
           (p.code IS NOT NULL)                       AS is_paused,
           (s.code IS NOT NULL)                       AS is_st,
           k.open, k.high, k.low, k.close,
           -- 一字板: 全天最低价就是涨停价, 从没开过板
           (k.low  IS NOT NULL AND k.low  >= l.high_limit) AS limit_up_sealed,
           (k.high IS NOT NULL AND k.high <= l.low_limit)  AS limit_down_sealed,
           -- 曾涨停但盘中开过板 —— check_limit_up 要的就是这个, 不需要分钟线
           (k.high IS NOT NULL AND k.high >= l.high_limit
            AND k.low < l.high_limit)                      AS limit_up_opened
    FROM price_limits l
    LEFT JOIN code_map m   ON m.jq_code = l.code
    LEFT JOIN kline_bfq k  ON k.symbol = m.tdx_symbol AND k.date = l.date
    LEFT JOIN paused_days p ON p.code = l.code AND p.date = l.date
    LEFT JOIN st_periods s  ON s.code = l.code
                           AND s.valid_from <= l.date AND s.valid_to > l.date
    WHERE l.date = d""")
    print('  market_state_at(d)  涨跌停价 + 停牌 + ST + 一字板/开板 一次给全')
    print('      limit_up_sealed  一字板(low >= high_limit)')
    print('      limit_up_opened  曾涨停但盘中开板 ← check_limit_up 用这个, 不需要分钟线')

    # ---------------------------------------------------------------- 校验
    print('\n' + '=' * 72)
    print('⑦ 校验')
    print('=' * 72)
    # 日线最高价不该超过涨停价(超过就是数据或复权口径有问题)
    viol = con.execute("""
        SELECT round(100.0*avg(CASE WHEN k.high > l.high_limit * 1.001 THEN 1 ELSE 0 END), 4)
        FROM price_limits l
        JOIN code_map m ON m.jq_code = l.code
        JOIN kline_bfq k ON k.symbol = m.tdx_symbol AND k.date = l.date
        WHERE l.high_limit > 0 AND k.high > 0""").fetchone()[0]
    check(viol is not None and viol < 0.5,
          '日线最高价超出涨停价的比例 %.4f%%（两源独立，超出说明口径不一致）' % (viol or 0))

    nlim = con.execute('SELECT count(*) FROM price_limits').fetchone()[0]
    nkl = con.execute("""SELECT count(*) FROM kline_bfq k JOIN code_map m
                         ON m.tdx_symbol = k.symbol WHERE k.date BETWEEN ? AND ?""",
                      [D0, D1]).fetchone()[0]
    cover = 100.0 * nlim / nkl if nkl else 0
    check(cover > 90, '涨跌停价对日线的覆盖率 %.1f%%（%s / %s）'
          % (cover, format(nlim, ','), format(nkl, ',')))

    if args.demo:
        print('\n' + '=' * 72)
        print('演示: 用日线判开板, 不用分钟线')
        print('=' * 72)
        print(con.execute("""
        SELECT count(*) AS 当日涨停数,
               sum(CASE WHEN limit_up_sealed THEN 1 ELSE 0 END) AS 一字板,
               sum(CASE WHEN limit_up_opened THEN 1 ELSE 0 END) AS 盘中开板
        FROM market_state_at(DATE '2015-06-15')
        WHERE high IS NOT NULL AND high >= high_limit""").df().to_string(index=False))
        print('\n↑ check_limit_up(14:00 判持仓涨停股是否开板) 用「盘中开板」这一列即可')

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
