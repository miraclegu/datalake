# -*- coding: utf-8 -*-
"""财务层：报告期算 TTM/同比 -> 按 `pub_date` as-of 到日频。

价量因子是「逐股 × 逐日滚动窗口」，财务因子是「逐股 × 逐**报告期**」再贴到
日频 —— 形状完全不同，所以这一层单独一个文件。产出的列被 `Ctx` 当成普通
派生列，于是财务因子就是**列运算**，`expr()` 直接能写。

## 🔴 TTM 不能用 `lag(x, k)` 按【行数】取

TTM = 本期累计 + 上年全年 − 上年同期累计。用 `lag(4)` 取"上年同期"的前提是
**每家公司每个季度都有一行且不缺期** —— 而实测 27.3 万行里缺期是常态
（业绩快报、停牌期、早年季报不强制）。缺一期的话 `lag(4)` 取到的是**再往前
一期**，TTM 算出来是个**看着完全正常的错值**，而它不报错。

所以这里按 `report_date` 做**显式自连接**：

    上年同期 = report_date − 1 年        上年全年 = (year−1)-12-31

任一边取不到就给 NULL（算不出来就说算不出来，不猜）。
★ Q4 那一行自动正确：上年同期 == 上年全年，两项相消，TTM = 本期累计。

## 🔴 资产负债表是【时点数】，不做 TTM

`total_assets` 那些是某一天的余额，TTM 一个余额没有意义。只有利润表与现金
流量表的**累计**字段要 TTM。混了的话算出来是个四倍的资产，**而它不报错**。

## 🔴 as-of 用 `pub_date` 不是 `report_date`

报告期在前、公告在后 —— 用 report_date 就是未来函数（同 `feed.fundamentals`
那条）。同一 report_date 有多条（修正稿）时留 **pub_date 最晚**的那条。
"""

#: 资产负债表：**时点数**，只 as-of 不做 TTM
BAL = (
    'total_assets', 'total_liability', 'total_owner_equities',
    'equities_parent_company_owners', 'total_current_assets',
    'total_current_liability', 'total_non_current_assets',
    'total_non_current_liability', 'fixed_assets', 'intangible_assets',
    'inventories', 'cash_equivalents', 'trading_assets', 'account_receivable',
    'accounts_payable', 'shortterm_loan', 'longterm_loan', 'bonds_payable',
    'non_current_liability_in_one_year', 'hold_to_maturity_investments',
    'preferred_shares_equity', 'surplus_reserve_fund', 'retained_profit',
    'capital_reserve_fund',
)
#: 利润表：**累计**字段，要 TTM
INC = (
    'total_profit', 'net_profit', 'np_parent_company_owners',
    'operating_revenue', 'operating_cost', 'operating_profit',
    'total_operating_revenue', 'total_operating_cost',
    'financial_expense', 'administration_expense', 'sale_expense',
    'non_operating_revenue', 'non_operating_expense',
    'operating_tax_surcharges', 'asset_impairment_loss',
    'fair_value_variable_income', 'investment_income',
    'invest_income_associates', 'interest_expense', 'interest_income',
)
#: 现金流量表：累计字段，要 TTM
CFL = (
    'net_operate_cash_flow', 'net_invest_cash_flow', 'net_finance_cash_flow',
    'cash_equivalent_increase', 'goods_sale_and_service_render_cash',
    'cash_and_equivalents_at_end', 'fix_intan_other_asset_acqui_cash',
    'fixed_assets_depreciation', 'intangible_assets_amortization',
)

#: 落到日频之后的列名前缀：`b_` 时点余额 / `t_` TTM / `c_` 当期累计
#: ★ 前缀不是装饰：`b_total_assets` 与 `t_net_profit` 摆在一起时，
#:   「这个数是余额还是十二个月的流量」一眼看得出来 —— 混用是这一层最常见的错。
PFX_BAL, PFX_TTM, PFX_CUM = 'b_', 't_', 'c_'


def _dedup(tbl, fields):
    """同一 (code, report_date) 留 pub_date 最晚的那条（修正稿）。"""
    fl = ', '.join(fields)
    return """
        SELECT code, report_date, pub_date, %s FROM (
          SELECT code, report_date, pub_date, %s,
                 row_number() OVER (PARTITION BY code, report_date
                                    ORDER BY pub_date DESC) rn
          FROM read_parquet('%s')) WHERE rn = 1
    """ % (fl, fl, tbl)


def quarterly_sql(root):
    """报告期层：余额（时点）+ TTM + 当期累计，一张表。

    ★ 先把三表并成 `base`（每个 (code, report_date) 一行），再对 `base`
      自连接取「上年同期」与「上年全年」—— 这样 TTM 的两个减项与被减项
      来自**同一张表**，不会出现"利润表按 A 对齐、现金流按 B 对齐"。
    """
    b = _dedup('%s/raw/jq/financials/balance.parquet' % root, BAL)
    i = _dedup('%s/raw/jq/financials/income.parquet' % root, INC)
    c = _dedup('%s/raw/jq/financials/cashflow.parquet' % root, CFL)
    flow = list(INC) + list(CFL)

    sel_b = ',\n      '.join('b.%s AS %s%s' % (f, PFX_BAL, f) for f in BAL)
    sel_f = ',\n      '.join(
        'coalesce(i.%s, c.%s) AS %s' % (f, f, f) if f in INC and f in CFL
        else ('i.%s' % f if f in INC else 'c.%s' % f) for f in flow)
    # TTM = 本期累计 + 上年全年 − 上年同期累计；任一边缺 -> NULL
    sel_ttm = ',\n      '.join(
        'x.%s + q4.%s - ly.%s AS %s%s' % (f, f, f, PFX_TTM, f) for f in flow)
    sel_cum = ',\n      '.join('x.%s AS %s%s' % (f, PFX_CUM, f) for f in flow)
    sel_bal = ',\n      '.join('x.%s%s' % (PFX_BAL, f) for f in BAL)

    return """
    WITH bal AS (%s), inc AS (%s), cfl AS (%s),
    base AS (
      SELECT coalesce(i.code, b.code, c.code) AS code,
             coalesce(i.report_date, b.report_date, c.report_date) AS report_date,
             -- 🔴 pub_date 取三表里【最晚】的：三张表可能分开公告，
             --   取最早的等于在资产负债表还没披露时就用了它 = 未来函数。
             greatest(coalesce(i.pub_date, DATE '1900-01-01'),
                      coalesce(b.pub_date, DATE '1900-01-01'),
                      coalesce(c.pub_date, DATE '1900-01-01')) AS pub_date,
             %s,
             %s
      FROM inc i
      FULL JOIN bal b ON b.code = i.code AND b.report_date = i.report_date
      FULL JOIN cfl c ON c.code = i.code AND c.report_date = i.report_date
    )
    SELECT x.code, x.report_date, x.pub_date,
      %s,
      %s,
      %s
    FROM base x
    LEFT JOIN base ly ON ly.code = x.code
         AND ly.report_date = x.report_date - INTERVAL 1 YEAR
    LEFT JOIN base q4 ON q4.code = x.code
         AND q4.report_date = make_date(year(x.report_date) - 1, 12, 31)
    """ % (b, i, c, sel_b, sel_f, sel_bal, sel_ttm, sel_cum)


def asof_sql(root, panel_sub):
    """`panel_sub`（含 jq_code/date 的子查询）-> 贴上财务列。

    🔴 ASOF 的条件是 `pub_date <= date` —— 用 report_date 就是未来函数
      （报告期在前、公告在后）。实测两年全市场 340 万行 0.1 秒，几乎免费。
    """
    cols = ([PFX_BAL + f for f in BAL]
            + [PFX_TTM + f for f in list(INC) + list(CFL)]
            + [PFX_CUM + f for f in list(INC) + list(CFL)])
    return """
    WITH fq AS (%s)
    SELECT p.*, %s, fq.report_date AS fin_rd, fq.pub_date AS fin_pd
    FROM (%s) p
    ASOF LEFT JOIN fq ON fq.code = p.jq_code AND fq.pub_date <= p.date
    """ % (quarterly_sql(root), ', '.join('fq.%s' % c for c in cols), panel_sub)


def daily_cols():
    """as-of 之后日频上多出来的列名（`Ctx` 把它们当普通派生列）。"""
    return ([PFX_BAL + f for f in BAL]
            + [PFX_TTM + f for f in list(INC) + list(CFL)]
            + [PFX_CUM + f for f in list(INC) + list(CFL)]
            + ['fin_rd', 'fin_pd'])
