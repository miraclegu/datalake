# -*- coding: utf-8 -*-
"""TTM 金额（20）+ 市值估值（8）+ 现金流质量（9）= 37 条。

公式出处：`因子实现规格.md` 的同名三节。

🔴 **TTM 金额那 20 条单位全是「元」-> 横截面不可比**：营业收入 TTM 的
  "排第几"就是排公司大小。它们的价值在**时序**（这家公司的收入在增长吗）
  与**当分子**（除以市值/总资产之后才可比）。
★ 而「市值估值」与「现金流质量」是比率，横截面可比。
"""
from . import Spec, register
from .expr import expr, deps_of

TTM = [
    ('ttm_np', '净利润TTM', 't_net_profit', '全口径净利润（含少数股东）'),
    ('ttm_np_parent', '归属于母公司股东的净利润TTM', 't_np_parent_company_owners',
     '归母净利润。🔴 与全口径是两个量 —— 实测 std/fin_quarterly.np_ttm 就是'
     '这一个（250,631 行逐位一致），而全口径那版只对上 15.4%'),
    ('ttm_tp', '利润总额TTM', 't_total_profit', '税前利润'),
    ('ttm_op', '营业利润TTM', 't_operating_profit', '主营业务的利润'),
    ('ttm_rev', '营业收入TTM', 't_operating_revenue', '主营收入'),
    ('ttm_trev', '营业总收入TTM', 't_total_operating_revenue', '含其它业务'),
    ('ttm_cost', '营业成本TTM', 't_operating_cost', '主营成本'),
    ('ttm_tcost', '营业总成本TTM', 't_total_operating_cost', '含期间费用'),
    ('ttm_gross', '毛利TTM', 't_operating_revenue - t_operating_cost', '收入减成本'),
    ('ttm_fin_exp', '财务费用TTM', 't_financial_expense', '利息等'),
    ('ttm_adm_exp', '管理费用TTM', 't_administration_expense', '管理费用'),
    ('ttm_sale_exp', '销售费用TTM', 't_sale_expense', '销售费用'),
    ('ttm_nonop', '营业外收支净额TTM',
     't_non_operating_revenue - t_non_operating_expense', '营业外收入减支出'),
    ('ttm_ocf', '经营活动现金流量净额TTM', 't_net_operate_cash_flow', '经营现金流'),
    ('ttm_icf', '投资活动现金流量净额TTM', 't_net_invest_cash_flow', '投资现金流'),
    ('ttm_fcf', '筹资活动现金流量净额TTM', 't_net_finance_cash_flow', '筹资现金流'),
    ('ttm_goods_cash', '销售商品提供劳务收到的现金',
     't_goods_sale_and_service_render_cash',
     '收现。★ 原表这一条是累计口径，这里给 TTM'),
    ('ttm_value_chg', '价值变动净收益TTM',
     't_fair_value_variable_income + t_investment_income',
     '⚠ `fair_value_variable_income` 非空率只有 38.99% —— 当 0 用会把'
     '"没披露"当成"没有公允价值变动"'),
    ('ttm_impair', '资产减值损失TTM', 't_asset_impairment_loss',
     '⚠ 非空率 77.8%，同上'),
]

register([Spec(fid, cn, 'ttm', e, d, '元(金额)', deps_of(e), 1, expr(e))
          for fid, cn, e, d in TTM])

EV = '(totalmv + %s - z(b_cash_equivalents))' % (
    '(z(b_shortterm_loan) + z(b_longterm_loan) + z(b_bonds_payable)'
    ' + z(b_non_current_liability_in_one_year))')

register([
    Spec('ttm_ocf_ev', '经营活动产生的现金流量净额与企业价值之比TTM', 'ttm',
         't_net_operate_cash_flow / %s' % EV,
         '经营现金流相对企业价值（市值 + 净债务）—— 比"现金流市值比"多考虑了'
         '负债，收购视角下更合理', '小数',
         deps_of('t_net_operate_cash_flow / %s' % EV), 1,
         expr('t_net_operate_cash_flow / %s' % EV)),
])

VAL = [
    ('mv', '市值', 'totalmv', '总市值。★ 横截面【可比】—— 元是全市场共同标度，'
     '而本仓库 froec 的第三层就是按流通市值升序取 10', '元(金额)'),
    ('mv_float', '流通市值', 'floatmv',
     '⚠ 两个口径：`share_rmb`=流通 A 股 vs `share_trade_total`=含 B/H。'
     '面板取的是前者。⚠ 绝对额', '元(金额)'),
    ('ln_mv', '对数总市值', 'log(totalmv)',
     '取对数之后分布接近正态，是 Barra SIZE 的定义。★ 取对数【不改变序】，'
     '所以它与市值的横截面 IC 完全相同 —— 差别在做回归时', '对数元'),
    ('ep', '利润市值比', 't_net_profit / totalmv', '= 1/PE。盈利收益率', '小数'),
    ('sp', '营收市值比', 't_operating_revenue / totalmv', '= 1/PS', '小数'),
    ('cfp', '现金流市值比', 't_net_operate_cash_flow / totalmv',
     '经营现金流相对市值 —— 比 EP 更难粉饰', '小数'),
    ('fcfp', '现金流量市值比',
     '(t_net_operate_cash_flow - t_fix_intan_other_asset_acqui_cash) / totalmv',
     '自由现金流口径：经营现金流减资本开支', '小数'),
]
register([Spec(fid, cn, 'val', e, d, u, deps_of(e), 1, expr(e))
          for fid, cn, e, d, u in VAL])

# 🔴 PEG 单列出来：它是这批里**唯一**的 ✅✅（逐只实测与聚宽精确一致），
#   而且面板里就有权威值 —— 自己用 pe/增长率 再算一遍是第二份实现，
#   而两者的分母口径（增长率取原值还是 abs）差一点就完全不同。
register([Spec('peg', 'PEG', 'val', 'peg',
               '市盈率 ÷ 盈利增长率。取面板的权威值（`peg` 列），'
               '不自己用 pe/增长率 重算 —— 分母取原值还是取绝对值差一点'
               '结果就完全不同，而它不报错',
               '倍', ('peg',), 1, expr('peg'), tier='exact')])

CFQ = [
    ('ocf_np', '净利润现金含量', 't_net_operate_cash_flow / t_net_profit',
     '赚的利润里有多少真变成了现金。🔴 净利润为负时这个比率翻号，'
     '要连净利润一起看'),
    ('ocf_rev', '经营活动产生的现金流量净额与营业收入之比',
     't_net_operate_cash_flow / t_operating_revenue', '每块钱收入收回多少现金'),
    ('ocf_ta', '总资产现金回收率', 't_net_operate_cash_flow / b_total_assets',
     '经营现金流相对总资产'),
    ('ocf_liab', '经营活动产生的现金流量净额/负债合计',
     't_net_operate_cash_flow / b_total_liability', '现金流能覆盖多少负债'),
    ('ocf_cl', '现金流动负债比',
     't_net_operate_cash_flow / b_total_current_liability', '短期偿债的现金视角'),
    ('ocf_netdebt', '经营活动产生现金流量净额/净债务',
     't_net_operate_cash_flow / (z(b_shortterm_loan) + z(b_longterm_loan)'
     ' + z(b_bonds_payable) + z(b_non_current_liability_in_one_year)'
     ' - z(b_cash_equivalents))',
     '现金流相对净债务。🔴 净债务为负（现金多于有息负债）时翻号'),
    ('goods_rev', '销售商品提供劳务收到的现金与营业收入之比',
     't_goods_sale_and_service_render_cash / t_operating_revenue',
     '收现比 —— 低说明赊销多'),
    ('ocf_opinc', '经营活动产生的现金流量净额与经营活动净收益之比',
     't_net_operate_cash_flow / (t_total_operating_revenue - t_total_operating_cost)',
     '现金流相对经营净收益'),
    ('cfroa_gap', '现金流资产比和资产回报率之差',
     't_net_operate_cash_flow / b_total_assets - t_net_profit / b_total_assets',
     '现金回报与账面回报的差 —— 正说明利润的现金支撑好'),
]
register([Spec(fid, cn, 'cfq', e, d, '小数', deps_of(e), 1, expr(e))
          for fid, cn, e, d in CFQ])
