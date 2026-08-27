#!/usr/bin/env python3
"""建立语义无歧义的规范视图（L2）。

为什么需要这一层:
    原始列名保留了数据源的命名, 而有些名字**不足以表达语义**, 会让正确的误用看起来完全合理:

        SELECT i.net_profit_this_year / b.total_owner_equities AS roe   -- 看着没问题

    但 `net_profit_this_year` 实测是**归母**净利润(对 income.np_parent_company_owners
    匹配 99.75%, 对 income.net_profit 只有 33.2%), 而 `total_owner_equities` 是全口径
    所有者权益 —— 分子归母、分母全口径, 差约 5%, 而且完全不会报错。

    加载时的对账只在建库那一刻跑一次, **拦不住后续的误用**。
    所以把语义写进列名, 让人不必记住这些坑。

原则: 只重命名与显式标注, **不做任何计算**。派生指标交给使用方, 这里只保证语义清晰。
"""
import os
import sys

import duckdb

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = os.path.join(ROOT, 'lake.db')
FAILURES = []


def check(cond, msg):
    print(('  ✓ ' if cond else '  ✗ ') + msg)
    if not cond:
        FAILURES.append(msg)


def main():
    con = duckdb.connect(DB)

    print('=' * 72)
    print('L2 规范视图: 把语义写进列名')
    print('=' * 72)

    # ---------------------------------------------------- 财务: 消歧后的核心字段
    # 命名规则: 归母 = _parent, 全口径(含少数股东) = _total。看名字就知道口径。
    con.execute('DROP VIEW IF EXISTS fin_core')
    con.execute("""
    CREATE VIEW fin_core AS
    SELECT
        i.code,
        i.report_date,
        i.pub_date,
        -- PIT 三要素: 报告期 / 公告日 / 公告日是否为占位值
        (i.pub_date <= i.report_date)         AS pub_date_is_placeholder,

        -- 利润表(已确认 report_type=0 合并报表)
        i.total_operating_revenue             AS revenue,
        i.operating_profit                    AS operating_profit,
        i.total_profit                        AS profit_before_tax,
        i.net_profit                          AS net_profit_total,     -- 含少数股东
        i.np_parent_company_owners            AS net_profit_parent,    -- 归母 ★
        i.minority_profit                     AS net_profit_minority,
        i.basic_eps                           AS eps_basic,
        i.rd_expenses                         AS rd_expense
    FROM fin_income i
    """)
    n = con.execute('SELECT count(*) FROM fin_core').fetchone()[0]
    print('  fin_core            %s 行  (利润表核心字段, 口径写进列名)' % format(n, ','))
    print('      net_profit_total  = 含少数股东')
    print('      net_profit_parent = 归母 ★ 大多数因子该用这个')

    # -------------------------------------- indicator: 列名标注真实口径
    con.execute('DROP VIEW IF EXISTS fin_ratio')
    con.execute("""
    CREATE VIEW fin_ratio AS
    SELECT
        code,
        report_date,
        pub_date,
        (pub_date <= report_date)             AS pub_date_is_placeholder,
        -- ★ 实测: net_profit_this_year 是**归母**净利润(对 income.np_parent_company_owners
        --   匹配 99.75%; 对 income.net_profit 仅 33.2%)。列名显式标出, 避免误配全口径分母。
        net_profit_this_year                  AS net_profit_parent,
        main_income_this_year                 AS main_revenue,
        total_assets_this_year                AS total_assets,
        equities_this_year                    AS equities,
        roe_this_year                         AS roe_parent,      -- 分子是归母
        roe_weighted_this_year                AS roe_parent_weighted,
        eps_this_year                         AS eps_parent,
        bps_this_year                         AS bps,
        nocf_per_share_this_year              AS nocf_per_share,
        nocf_this_year                        AS nocf,
        rd_expense,
        rd_expense_ratio
    FROM fin_indicator
    """)
    n = con.execute('SELECT count(*) FROM fin_ratio').fetchone()[0]
    print('  fin_ratio           %s 行  (财务指标, 归母口径已写进列名)' % format(n, ','))

    # ------------------------------------------------ PIT 财务: 直接可用于回测
    # 把"当时能看到的最新一期"封装掉, 使用方不必每次自己写 pub_date 条件。
    con.execute('DROP MACRO TABLE IF EXISTS fin_visible_at')
    con.execute("""
    CREATE MACRO fin_visible_at(d) AS TABLE
    SELECT * FROM (
        SELECT c.*,
               row_number() OVER (PARTITION BY c.code ORDER BY c.report_date DESC) AS rn
        FROM fin_core c
        WHERE c.report_date <= d
          AND c.pub_date    <= d
          AND c.pub_date    >  c.report_date     -- 排除占位公告日
    ) WHERE rn = 1
    """)
    print('  fin_visible_at(d)   表宏: 传入日期, 返回"当时已公告的最新一期"')
    print('      用法: SELECT * FROM fin_visible_at(DATE \'2015-06-30\')')

    # ------------------------------------------------------------------ 校验
    print('\n' + '=' * 72)
    print('校验')
    print('=' * 72)

    # 归母 vs 全口径确实不同 —— 证明这个区分不是多余的
    r = con.execute("""
        SELECT count(*) n,
               sum(CASE WHEN abs(net_profit_parent - net_profit_total)
                        > 0.005*abs(net_profit_total) THEN 1 ELSE 0 END) diff
        FROM fin_core WHERE net_profit_total IS NOT NULL AND net_profit_total <> 0
    """).fetchone()
    pct = 100.0 * r[1] / r[0]
    check(pct > 10, '归母与全口径净利实际有差异的比例 %.1f%% (证明必须区分)' % pct)

    # fin_ratio 的归母口径与 fin_core 一致
    r2 = con.execute("""
        SELECT count(*) n,
               round(100.0*sum(CASE WHEN abs(r.net_profit_parent - c.net_profit_parent)
                     <= 0.005*abs(c.net_profit_parent) THEN 1 ELSE 0 END)/count(*), 2) rate
        FROM fin_ratio r JOIN fin_core c USING (code, report_date)
        WHERE r.net_profit_parent IS NOT NULL AND c.net_profit_parent IS NOT NULL
          AND c.net_profit_parent <> 0
    """).fetchone()
    check(r2[1] > 95, 'fin_ratio 与 fin_core 的归母口径一致 (%.2f%%)' % r2[1])

    # PIT 宏: 2016-03-15 只应看到 13% 的 2015 年报
    n_vis = con.execute("""SELECT count(*) FROM fin_visible_at(DATE '2016-03-15')
                           WHERE report_date = DATE '2015-12-31'""").fetchone()[0]
    n_all = con.execute("""SELECT count(*) FROM fin_core
                           WHERE report_date = DATE '2015-12-31'
                             AND pub_date > report_date""").fetchone()[0]
    ratio = 100.0 * n_vis / n_all
    check(5 < ratio < 25, 'PIT 宏生效: 2016-03-15 只见 %d/%d 份 2015 年报 (%.1f%%)'
          % (n_vis, n_all, ratio))

    print('\n' + '=' * 72)
    print('演示: 同一个 ROE, 用错口径差多少')
    print('=' * 72)
    print(con.execute("""
        SELECT c.code, c.report_date,
               round(c.net_profit_parent/1e8, 2) AS 归母净利亿,
               round(c.net_profit_total /1e8, 2) AS 全口径净利亿,
               round(100.0*(c.net_profit_total - c.net_profit_parent)
                     / nullif(abs(c.net_profit_parent),0), 1) AS 差异pct
        FROM fin_core c
        WHERE c.report_date = DATE '2015-12-31' AND c.net_profit_parent > 1e9
        ORDER BY abs(c.net_profit_total - c.net_profit_parent) DESC LIMIT 6
    """).df().to_string(index=False))
    print('\n↑ 用全口径净利配归母净资产算 ROE, 这些标的会高估上面这个百分比')

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
