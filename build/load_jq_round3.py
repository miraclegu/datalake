#!/usr/bin/env python3
"""加载第三轮抽取：分红送配 STK_XR_XD（+ 业绩预告）。

用法:
    python datalake/build/load_jq_round3.py            # 加载 + 校验 + 与 tdx gbbq 对账
    python datalake/build/load_jq_round3.py --demo     # 额外跑演示

输入（`datalake/raw/jq/_ingest/downloads/`，来自 extract_jq_round3.py）:
    stk_xr_xd.csv          分红送配全表
    stk_fin_forcast.csv    业绩预告（可选）

产出:
    l0/stk_xr_xd.parquet       原样(全字符串)
    l1/dividend.parquet        定型 + 标注可见日
    视图: dividend / dividend_forecast
    表宏: dividend_visible_at(d)   —— 截至 d 已公告且未取消的分红
          dividend_ttm_at(d)       —— 截至 d 可见的滚动 12 个月派现总额

━━━ 语义要点（都写进列名或注释，别靠记）━━━
· bonus_amount_rmb 单位是**万元**。策略里的算式是
      dividend_ratio = (bonus_amount_rmb / 10000) / market_cap(亿元)
  即 万元/1e4 = 亿元, 与亿元市值相除得股息率。本层不做换算, 只标注。
· 可见日锚点是 **board_plan_pub_date（董事会预案公告日）**, 不是除权日。
  用除权日会让"该买的时候看不到分红", 是反向未来函数。
· 同一方案在库里有多条记录(预案/股东大会/实施), 必须按 plan_progress 去重,
  否则派现金额被重复累加。本层**不去重**, 把去重留给 dividend_visible_at 宏,
  因为"取哪个阶段"是策略决策 —— 不缓存决策, 只缓存原语。
"""
import argparse
import os
import sys

import duckdb

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, 'raw', 'jq', '_ingest', 'downloads')
L0 = os.path.join(ROOT, 'raw', 'jq')
L1 = os.path.join(ROOT, 'std')
DB = os.path.join(ROOT, 'lake.db')
TDX = os.path.join(os.path.dirname(ROOT), 'tdx2db', 'tdx.db')
FAILURES = []

DATE_COLS = ('report_date', 'board_plan_pub_date', 'shareholders_plan_pub_date',
             'implementation_pub_date', 'bonus_cancel_pub_date',
             'a_registration_date', 'a_xr_date', 'a_bonus_date')
NUM_COLS = ('bonus_amount_rmb', 'bonus_ratio_rmb', 'at_bonus_ratio_rmb',
            'dividend_ratio', 'transfer_ratio', 'dividend_number', 'transfer_number')


def check(cond, msg):
    print(('  ✓ ' if cond else '  ✗ ') + msg)
    if not cond:
        FAILURES.append(msg)


def cols_of(con, path):
    # DESCRIBE 的结果列名各版本不一致(name / column_name), 取第一列而不是按名字取
    return [r[0] for r in con.execute(
        "DESCRIBE SELECT * FROM read_csv_auto(?, all_varchar=true)", [path]).fetchall()]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--demo', action='store_true')
    args = ap.parse_args()
    for d in (L0, L1):
        if not os.path.exists(d):
            os.makedirs(d)

    src = os.path.join(SRC, 'stk_xr_xd.csv')
    if not os.path.exists(src):
        print('❌ 找不到 %s' % src)
        print('   先在聚宽研究环境跑 datalake/raw/jq/_ingest/extract_jq_round3.py, 下载 tar 后解压到 jqdata/')
        return 1

    con = duckdb.connect(DB)
    print('=' * 72)
    print('① 分红送配 STK_XR_XD')
    print('=' * 72)

    have = cols_of(con, src)
    print('  CSV 实际列(%d): %s' % (len(have), have))
    need = ['code', 'report_date', 'board_plan_pub_date', 'bonus_amount_rmb', 'plan_progress']
    missing = [c for c in need if c not in have]
    if missing:
        print('  ❌ 缺必需列 %s —— 抽取脚本的列清单与本加载器对不上, 停止' % missing)
        con.close()
        return 1

    # ---- L0: 原样, 全字符串。不做任何解析, 保证可回溯到源头
    l0p = os.path.join(L0, 'stk_xr_xd.parquet')
    # COPY 的 TO 子句不接受参数绑定, 用字面量(路径是本脚本自己算的, 无注入面)
    con.execute("""COPY (SELECT * FROM read_csv_auto('%s', all_varchar=true))
                   TO '%s' (FORMAT parquet, COMPRESSION zstd)""" % (src, l0p))
    n0 = con.execute('SELECT count(*) FROM read_parquet(?)', [l0p]).fetchone()[0]
    print('  L0 %s 行 → %s (%.1f MB)' % (format(n0, ','), os.path.basename(l0p),
                                         os.path.getsize(l0p) / 1048576.0))

    # ---- L1: 定型。日期转 DATE, 金额转 DOUBLE, 并显式派生可见日
    sel = ['code']
    sel += ['try_cast(%s AS DATE) AS %s' % (c, c) for c in DATE_COLS if c in have]
    sel += ['try_cast(%s AS DOUBLE) AS %s' % (c, c) for c in NUM_COLS if c in have]
    for c in ('bonus_type', 'plan_progress', 'plan_progress_code', 'company_id'):
        if c in have:
            sel.append(c)
    # 可见日: 优先预案公告日; 早年若缺失, 退到股东大会/实施公告日; 都没有才退到登记日。
    # 退到登记日的记录**打上标记**, 因为那已经不是严格 PIT 了, 使用方必须能看见这件事。
    fallbacks = [c for c in ('board_plan_pub_date', 'shareholders_plan_pub_date',
                             'implementation_pub_date') if c in have]
    vis = 'coalesce(%s)' % ', '.join('try_cast(%s AS DATE)' % c for c in fallbacks)
    if 'a_registration_date' in have:
        sel.append('coalesce(%s, try_cast(a_registration_date AS DATE)) AS visible_date' % vis)
        sel.append('(%s IS NULL) AS visible_date_is_fallback' % vis)
    else:
        sel.append('%s AS visible_date' % vis)
        sel.append('FALSE AS visible_date_is_fallback')

    l1p = os.path.join(L1, 'dividend.parquet')
    con.execute("""COPY (SELECT %s FROM read_csv_auto('%s', all_varchar=true))
                   TO '%s' (FORMAT parquet, COMPRESSION zstd)"""
                % (', '.join(sel), src, l1p))
    con.execute("CREATE OR REPLACE VIEW dividend AS SELECT * FROM read_parquet('%s')" % l1p)
    n1 = con.execute('SELECT count(*) FROM dividend').fetchone()[0]
    print('  L1 %s 行 → dividend' % format(n1, ','))

    # ---------------------------------------------------------------- 事实
    print('\n' + '=' * 72)
    print('② 数据事实（决定哪些年份能用于 PIT 回测）')
    print('=' * 72)
    r = con.execute("""
        SELECT count(*) n, count(DISTINCT code) codes,
               sum(CASE WHEN board_plan_pub_date IS NOT NULL THEN 1 ELSE 0 END) has_plan,
               sum(CASE WHEN visible_date_is_fallback THEN 1 ELSE 0 END) fb,
               sum(CASE WHEN bonus_amount_rmb > 0 THEN 1 ELSE 0 END) has_amt,
               min(report_date) rd0, max(report_date) rd1
        FROM dividend""").fetchone()
    print('  总行数 %s / 股票 %s 只 / report_date %s ~ %s'
          % (format(r[0], ','), format(r[1], ','), r[5], r[6]))
    print('  有预案公告日   %s (%.1f%%)' % (format(r[2], ','), 100.0 * r[2] / r[0]))
    print('  可见日走了回退 %s (%.1f%%)  ← 这些不是严格 PIT' % (format(r[3], ','),
                                                              100.0 * r[3] / r[0]))
    print('  有派现金额>0   %s (%.1f%%)' % (format(r[4], ','), 100.0 * r[4] / r[0]))

    print('\n  按年份看预案公告日覆盖率（决定回测起始年）:')
    print(con.execute("""
        SELECT year(report_date) AS 年,
               count(*) AS 记录数,
               round(100.0*sum(CASE WHEN board_plan_pub_date IS NOT NULL THEN 1 ELSE 0 END)
                     /count(*), 1) AS 预案日覆盖pct,
               round(avg(CASE WHEN board_plan_pub_date IS NOT NULL AND a_xr_date IS NOT NULL
                    THEN date_diff('day', board_plan_pub_date, a_xr_date) END), 0) AS 预案到除权天数
        FROM dividend WHERE report_date IS NOT NULL AND year(report_date) >= 1995
        GROUP BY 1 ORDER BY 1""").df().to_string(index=False))
    print('  ↑「预案到除权天数」就是用除权日当可见日会造成的时序偏移量')

    print('\n  plan_progress 取值分布（策略的 STAGE_ORDER / CANCEL_STATES 必须覆盖这些字面量）:')
    print(con.execute("""SELECT plan_progress AS 进度, count(*) AS 记录数
                         FROM dividend GROUP BY 1 ORDER BY 2 DESC""").df().to_string(index=False))

    # ------------------------------------------------------------ L2 表宏
    print('\n' + '=' * 72)
    print('③ PIT 表宏')
    print('=' * 72)
    # 去重: 同一 (code, report_date, bonus_type) 只留可见日最晚的那条(流程最靠后)。
    # 不用 plan_progress 文本排序 —— 文本有变体时 map 会静默失效; 用日期天然单调。
    con.execute('DROP MACRO TABLE IF EXISTS dividend_visible_at')
    con.execute("""
    CREATE MACRO dividend_visible_at(d) AS TABLE
    SELECT * EXCLUDE (rn) FROM (
        SELECT v.*, row_number() OVER (
                   PARTITION BY v.code, v.report_date, v.bonus_type
                   ORDER BY v.visible_date DESC) AS rn
        FROM dividend v
        WHERE v.visible_date IS NOT NULL
          AND v.visible_date <= d
          AND (v.bonus_cancel_pub_date IS NULL OR v.bonus_cancel_pub_date > d)
    ) WHERE rn = 1""")
    print('  dividend_visible_at(d)  截至 d 已公告、未取消, 且同方案只留最新一条')

    con.execute('DROP MACRO TABLE IF EXISTS dividend_ttm_at')
    con.execute("""
    CREATE MACRO dividend_ttm_at(d) AS TABLE
    SELECT code, sum(bonus_amount_rmb) AS bonus_amount_wan_rmb, count(*) AS n_plan
    FROM dividend_visible_at(d)
    WHERE bonus_amount_rmb > 0 AND visible_date > (d - INTERVAL 365 DAY)
    GROUP BY code""")
    print('  dividend_ttm_at(d)      截至 d 可见的滚动 12 个月派现(单位: 万元)')

    # -------------------------------------------------- 与 tdx gbbq 独立对账
    print('\n' + '=' * 72)
    print('④ 与通达信 gbbq 独立对账（两个来源, 互不知情）')
    print('=' * 72)
    if 'a_xr_date' not in have:
        print('  ⚠ 本次抽取没有 a_xr_date 列, 无法对账。')
        print('    请更新 extract_jq_round3.py 的列清单后重抽 —— 这是唯一的连接键。')
    elif not os.path.exists(TDX):
        print('  ⚠ 找不到 %s, 跳过' % TDX)
    else:
        con.execute("ATTACH '%s' AS tdx (READ_ONLY)" % TDX)
        # gbbq: category=1, c1 = 每10股派现(元)。JQ: bonus_ratio_rmb 单位未知 ——
        # 不假设, 直接量两边的比值, 让数据自己说单位是多少。
        rec = con.execute("""
            SELECT count(*) n,
                   median(g.c1 / nullif(j.bonus_ratio_rmb,0)) ratio,
                   quantile_cont(g.c1 / nullif(j.bonus_ratio_rmb,0), 0.1) q10,
                   quantile_cont(g.c1 / nullif(j.bonus_ratio_rmb,0), 0.9) q90
            FROM dividend j
            JOIN code_map m ON m.jq_code = j.code
            JOIN tdx.raw_gbbq g ON g.symbol = m.tdx_symbol
                 AND g.date = j.a_xr_date AND g.category = '1'
            WHERE j.bonus_ratio_rmb > 0 AND g.c1 > 0""").fetchone()
        if not rec[0]:
            check(False, '除权日能对上的记录数为 0 —— 连接键或口径有问题')
        else:
            print('  可对账记录 %s 条' % format(rec[0], ','))
            print('  gbbq.c1 / jq.bonus_ratio_rmb  中位数 %.4f  (10%%分位 %.4f, 90%%分位 %.4f)'
                  % (rec[1], rec[2], rec[3]))
            near = min(abs(rec[1] - t) for t in (1.0, 10.0))
            check(near < 0.2, '两源派现比例呈整数倍关系(比值 %.4f) —— 口径相同' % rec[1])
            # ★ 比值 1.0 只说明两源口径**相同**, 完全没说单位是什么。
            #   用第三个独立量(股本)把绝对单位钉死: 若 ratio 是每 N 股派现, 则
            #       bonus_amount_rmb(万元) / bonus_ratio_rmb * N = 股本(万股)
            #   gbbq category=5 是股本变动, c4 = 变动后总股本(万股), 与本表无关, 可作独立第三方。
            unit = con.execute("""
                SELECT median(s.c4 / (j.bonus_amount_rmb / j.bonus_ratio_rmb)) AS n_shares
                FROM dividend j
                JOIN code_map m ON m.jq_code = j.code
                JOIN tdx.raw_gbbq s ON s.symbol = m.tdx_symbol AND s.category = '5'
                     AND s.date = (SELECT max(x.date) FROM tdx.raw_gbbq x
                                   WHERE x.symbol = m.tdx_symbol AND x.category = '5'
                                     AND x.date <= j.a_xr_date)
                WHERE j.bonus_ratio_rmb > 0 AND j.bonus_amount_rmb > 0 AND s.c4 > 0""").fetchone()[0]
            if unit:
                near_u = min(abs(unit - t) for t in (1.0, 10.0))
                lbl = ('每股' if abs(unit - 1.0) < 0.15 else
                       '每10股' if abs(unit - 10.0) < 1.5 else '未知(%.2f 股)' % unit)
                check(near_u < 1.5,
                      '派现比例的绝对单位 = **%s派现**(由股本反推, 隐含 %.2f 股)' % (lbl, unit))
                print('      推导: bonus_amount_rmb(万元) / bonus_ratio_rmb × N = 股本(万股)')
                print('      两源比值相同 ≠ 知道单位 —— 单位必须靠第三个量定')
            # 一致率: 按上面量出来的倍数归一后比对
            k = 10.0 if abs(rec[1] - 10.0) < abs(rec[1] - 1.0) else 1.0
            agree = con.execute("""
                SELECT round(100.0*avg(CASE WHEN abs(g.c1 - ? * j.bonus_ratio_rmb)
                             <= 0.01 * ? * j.bonus_ratio_rmb THEN 1 ELSE 0 END), 2)
                FROM dividend j
                JOIN code_map m ON m.jq_code = j.code
                JOIN tdx.raw_gbbq g ON g.symbol = m.tdx_symbol
                     AND g.date = j.a_xr_date AND g.category = '1'
                WHERE j.bonus_ratio_rmb > 0 AND g.c1 > 0""", [k, k]).fetchone()[0]
            check(agree is not None and agree > 95,
                  '两源派现金额在 1%% 容差内一致的比例 %.2f%%' % (agree or 0))
        con.execute('DETACH tdx')

    # ---------------------------------------------------------- 业绩预告
    fc = os.path.join(SRC, 'stk_fin_forcast.csv')
    print('\n' + '=' * 72)
    print('⑤ 业绩预告 STK_FIN_FORCAST')
    print('=' * 72)
    if not os.path.exists(fc):
        print('  未抽取, 跳过（只有 1 处策略引用, 可选）')
    else:
        fp = os.path.join(L0, 'stk_fin_forcast.parquet')
        # 🔴 这个 CSV 是【上游写坏的】：content 是大段中文正文，含换行、
        #   含千分位逗号，且有 10 条记录引号未闭合。三个解析器给三个答案，
        #   其中两个**不报错**：
        #     csv.reader  50,650 行（从未闭合的引号处开始串行）
        #     pandas     124,094 行（静默产出 11 行垃圾：id 列装着正文碎片）
        #     DuckDB     直接报 state machine invalid
        #   权威条数是 124,083（build/csv_repair.py 按记录头判据切 + 内容校验）。
        #
        # ★ 原实现用 pandas 读，且只校验「落盘行数 == 读入行数」——
        #   两边一样错，检查照样通过，于是 11 行垃圾静默进库并存活了一周。
        #   **行数对上不等于读对了。** 现在改成校验【内容】。
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        from csv_repair import read_forcast_df
        fdf, _st = read_forcast_df(fc)
        con.register('_fc', fdf)
        con.execute("COPY (SELECT * FROM _fc) TO '%s' (FORMAT parquet, COMPRESSION zstd)" % fp)
        con.unregister('_fc')
        _n = con.execute("SELECT count(*) FROM read_parquet('%s')" % fp).fetchone()[0]
        check(_n == len(fdf), '预告落盘 %s 行 = 读入行数' % format(_n, ','))
        # 内容校验（读侧已经抛错了，这里再核一遍落盘结果，防写入环节出错）
        _bad = con.execute(
            "SELECT count(*) FROM read_parquet('%s') "
            "WHERE try_cast(id AS BIGINT) IS NULL "
            "   OR NOT regexp_matches(code, '^[0-9]{6}[.]XSH[EG]$')" % fp).fetchone()[0]
        check(_bad == 0, '预告无垃圾行（id 全数字、code 全合法）')
        del fdf
        con.execute(
            "CREATE OR REPLACE VIEW dividend_forecast AS SELECT * FROM read_parquet('%s')" % fp)
        nf = con.execute('SELECT count(*) FROM dividend_forecast').fetchone()[0]
        print('  %s 行 → dividend_forecast' % format(nf, ','))

    # ---------------------------------------------------------------- 校验
    print('\n' + '=' * 72)
    print('⑥ 校验')
    print('=' * 72)
    check(n1 == n0, 'L1 与 L0 行数一致 (%s)' % format(n1, ','))
    bad = con.execute("""SELECT count(*) FROM dividend
                         WHERE visible_date IS NOT NULL AND report_date IS NOT NULL
                           AND visible_date < report_date""").fetchone()[0]
    check(100.0 * bad / n1 < 1, '可见日早于报告期的记录 %d 条 (%.2f%%)' % (bad, 100.0 * bad / n1))
    n_v = con.execute("SELECT count(*) FROM dividend_visible_at(DATE '2015-06-30')").fetchone()[0]
    n_a = con.execute("SELECT count(*) FROM dividend_visible_at(DATE '2026-06-30')").fetchone()[0]
    check(0 < n_v < n_a, 'PIT 宏单调: 2015 可见 %s 条 < 2026 可见 %s 条'
          % (format(n_v, ','), format(n_a, ',')))
    dup = con.execute("""SELECT count(*) FROM (
              SELECT code, report_date, bonus_type FROM dividend_visible_at(DATE '2026-06-30')
              GROUP BY 1,2,3 HAVING count(*) > 1)""").fetchone()[0]
    check(dup == 0, '宏内去重生效: 同 (code, report_date, bonus_type) 无重复 (%d)' % dup)

    if args.demo:
        print('\n' + '=' * 72)
        print('演示: 2015-06-30 视角下股息率最高的 10 只')
        print('=' * 72)
        print(con.execute("""
        SELECT t.code,
               n.name AS 当时名称,
               round(t.bonus_amount_wan_rmb / 1e4, 2) AS 派现亿元,
               round(b.totalmv / 1e8, 2) AS 总市值亿元,
               round(100.0 * (t.bonus_amount_wan_rmb / 1e4) / (b.totalmv / 1e8), 2) AS 股息率pct
        FROM dividend_ttm_at(DATE '2015-06-30') t
        JOIN code_map m ON m.jq_code = t.code
        JOIN basic_daily b ON b.symbol = m.tdx_symbol AND b.date = DATE '2015-06-30'
        LEFT JOIN security_name n ON n.code = t.code
             AND n.valid_from <= DATE '2015-06-30'
             AND (n.valid_to IS NULL OR n.valid_to > DATE '2015-06-30')
        WHERE b.totalmv > 0
        ORDER BY 股息率pct DESC LIMIT 10""").df().to_string(index=False))
        print('\n↑ 用的是当时已公告的分红 + 当时的名称, 没有用到 2015 之后的任何信息')

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
