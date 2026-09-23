# -*- coding: utf-8 -*-
"""偿债 / 杠杆 / 结构（27）—— 全部 ✅，纯资产负债表比率。

公式出处：`因子实现规格.md` 的「偿债 / 杠杆 / 结构」一节。

🔴 **全部用【时点余额】(`b_` 前缀)，一个都不做 TTM** —— `total_assets`
  是某一天的余额，把它 TTM 一下会得到一个四倍的资产，**而它不报错**。
🔴 **单位全是「倍 / 小数」，横截面可比**；而「净债务」「净运营资本」
  「金融负债」那几条单位是**元** -> `xs_comparable=False`，
  它们的横截面 IC 算的是公司规模不是信号。
"""
from . import Spec, register
from .expr import expr, deps_of

#: 有息负债 / 金融负债 / 金融资产 / 经营性资产 —— 多处复用，写一份
#: 有息负债。🔴 四个明细行都要 `z()`：公司没有应付债券时那一格是空的，
#:   不包的话 NULL 传染掉整个和 —— 实测非空率 17.7% vs 包了之后 96%。
DEBT = ('(z(b_shortterm_loan) + z(b_longterm_loan) + z(b_bonds_payable)'
        ' + z(b_non_current_liability_in_one_year))')
FINA = ('(z(b_cash_equivalents) + z(b_trading_assets)'
        ' + z(b_hold_to_maturity_investments))')


def _s(fid, cn, e, desc, unit='倍'):
    return Spec(fid, cn, 'lev', e, desc, unit, deps_of(e), 1, expr(e))


register([
    _s('curr_ratio', '流动比率(单季度)', 'b_total_current_assets / b_total_current_liability',
       '流动资产能覆盖几倍流动负债。★ 取【单期】余额不是 TTM'),
    _s('quick_ratio', '速动比率',
       '(b_total_current_assets - b_inventories) / b_total_current_liability',
       '把存货剔掉之后的短期偿债能力 —— 存货未必变得了现'),
    _s('superquick_ratio', '超速动比率',
       '(z(b_cash_equivalents) + z(b_trading_assets) + z(b_account_receivable))'
       ' / b_total_current_liability',
       '只留现金、交易性金融资产、应收账款 —— 最严的一档'),
    _s('cash_ratio', '现金比率', 'b_cash_equivalents / b_total_current_liability',
       '纯现金能覆盖多少流动负债'),
    _s('debt_asset', '资产负债率', 'b_total_liability / b_total_assets',
       '最常用的杠杆度量，0~1', '小数'),
    _s('equity_ratio_a', '股东权益比率', 'b_total_owner_equities / b_total_assets',
       '= 1 − 资产负债率', '小数'),
    _s('debt_equity', '产权比率', 'b_total_liability / b_total_owner_equities',
       '负债是权益的几倍'),
    _s('book_lev', '账面杠杆',
       '(b_total_owner_equities + z(b_total_non_current_liability))'
       ' / b_total_owner_equities',
       '把长期负债也算进"长期资本"之后，相对权益的倍数'),
    _s('mkt_lev', '市场杠杆',
       '(totalmv + z(b_preferred_shares_equity) + z(b_total_non_current_liability))'
       ' / totalmv',
       '账面杠杆的市值版：分母换成市值。⚠ `factors.xlsx` 里「市场杠杆」'
       '出现两次，光看名字分不出是哪一个'),
    _s('fixed_ratio', '固定资产比率', 'b_fixed_assets / b_total_assets',
       '重资产还是轻资产', '小数'),
    _s('intang_ratio', '无形资产比率', 'b_intangible_assets / b_total_assets',
       '无形资产占总资产的比', '小数'),
    _s('noncurr_ratio', '非流动资产比率', 'b_total_non_current_assets / b_total_assets',
       '非流动资产占比', '小数'),
    _s('debt_to_asset2', '债务总资产比', '%s / b_total_assets' % DEBT,
       '🔴 分子是【有息】负债（短借+长借+应付债券+一年内到期），'
       '不是全部负债 —— 应付账款那些不付利息', '小数'),
    _s('lt_loan_asset', '长期借款与资产总计之比', 'b_longterm_loan / b_total_assets',
       '⚠ `longterm_loan` 非空率只有 57.4% —— 没有长期借款的公司这一格是'
       '空的而不是 0，当 0 用会把"没披露"与"确实没有"混成一个', '小数'),
    _s('lt_liab_asset', '长期负债与资产总计之比',
       'z(b_total_non_current_liability) / b_total_assets', '长期负债占总资产', '小数'),
    _s('equity_fixed', '股东权益与固定资产比率',
       'b_total_owner_equities / b_fixed_assets', '权益是固定资产的几倍'),
    _s('tangible_debt', '有形净值债务率',
       'b_total_liability / (b_total_owner_equities - z(b_intangible_assets))',
       '把无形资产从净值里剔掉之后的负债倍数 —— 无形资产清算时值多少很难说'),
    _s('lt_liab_wc', '长期负债与营运资金比率',
       'b_total_non_current_liability'
       ' / (b_total_current_assets - b_total_current_liability)',
       '长期负债相对营运资金。🔴 营运资金为负时这个比率会翻号，'
       '读的时候要连营运资金一起看'),
    # ---- 单位是【元】的：横截面不可比 ----
    _s('net_debt', '净债务', '%s - z(b_cash_equivalents)' % DEBT,
       '金融负债减现金。🔴 单位是元，横截面排序排的是公司规模不是信号', '元'),
    _s('fin_liab', '金融负债', DEBT,
       '短借 + 长借 + 应付债券 + 一年内到期的非流动负债。🔴 单位元', '元'),
    _s('fin_asset', '金融资产', FINA,
       '现金 + 交易性金融资产 + 持有至到期投资。🔴 单位元', '元'),
    _s('oper_asset', '经营性资产', 'b_total_assets - %s' % FINA,
       '总资产减金融资产 —— 真正在经营里用的那部分。🔴 单位元', '元'),
    _s('oper_liab', '经营性负债', 'b_total_liability - %s' % DEBT,
       '总负债减有息负债 = 应付账款那些不付利息的。🔴 单位元', '元'),
    _s('net_wc', '净运营资本',
       'b_total_current_assets - b_total_current_liability',
       '流动资产减流动负债。🔴 单位元', '元'),
    _s('int_curr_liab', '带息流动负债',
       'z(b_shortterm_loan) + z(b_non_current_liability_in_one_year)',
       '短期借款 + 一年内到期的非流动负债。🔴 单位元', '元'),
    _s('no_int_curr_liab', '无息流动负债',
       'b_total_current_liability - z(b_shortterm_loan)'
       ' - z(b_non_current_liability_in_one_year)',
       '流动负债里不付利息的那部分。🔴 单位元', '元'),
    _s('retained', '留存收益', 'z(b_surplus_reserve_fund) + z(b_retained_profit)',
       '盈余公积 + 未分配利润。🔴 单位元', '元'),
])
