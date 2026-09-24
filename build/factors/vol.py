# -*- coding: utf-8 -*-
"""量能 / 资金流（22 条，来自规格文档 21 行）—— 全部 ✅。

公式出处：`因子实现规格.md` 的「量能 / 资金流」一节。

★ 22 vs 21：「计算VMACD因子的中间变量」在 `factors.xlsx` 里出现 **2 次**，
  规格文档合成了一行写「两条中间量 DIF_v 与 DEA_v」。这里**拆成两条**并都
  标 `src_dup` —— 光看中文名分不出哪条是哪条，落地前要拿到聚宽的 factor code。
  硬塞成一条的话，另一条就悄悄没了。

🔴 **成交量与成交额【不复权】**：`volume_shares` 是实际成交股数、`amount` 是
  实际成交金额，它们本来就不受除权影响。乘因子反而是错的。
  而这一族里凡是碰价格的（TP / R）一律后复权 —— 两件事别混。
"""
from . import Spec, register

_EMA_V = [(5, 'v_ema5', '成交量的5日指数移动平均'),
          (10, 'v_ema10', '成交量的10日指数移动平均'),
          (26, 'v_ema26', '成交量的26日指数移动平均')]


def _vr26(x):
    """VR(26) = Σ₂₆(涨日的 V) / Σ₂₆(跌日的 V)。

    ⚠ **平盘日不计入**（另一种流行口径是"各计一半"）。这是个取舍，
      写在这里免得下次被当成 bug。
    """
    import numpy as np
    r, v = x.col('_ret'), x.col('volume_shares')
    d = x.df
    d['_v_up'] = np.where(r > 0, v, 0.0)
    d['_v_dn'] = np.where(r < 0, v, 0.0)
    up, dn = x.rsum('_v_up', 26), x.rsum('_v_dn', 26)
    return up / dn.replace(0.0, np.nan)


def _mfi14(x):
    """MFI(14) = 100 × 正向资金流 / (正向 + 负向)，方向按 TP 的涨跌分。"""
    import numpy as np
    d = x.df
    mf = d['_tp'] * d['volume_shares']
    up = x._by(d['_tp']).diff()
    d['_mf_pos'] = np.where(up > 0, mf, 0.0)
    d['_mf_neg'] = np.where(up < 0, mf, 0.0)
    p, n = x.rsum('_mf_pos', 14), x.rsum('_mf_neg', 14)
    tot = p + n
    return 100.0 * p / tot.replace(0.0, np.nan)


def _pvt_day(x):
    """单日价量趋势 = 当日收益 × 成交量。累计版另有一条。"""
    if '_pvt_d' not in x.df.columns:
        x.df['_pvt_d'] = x.col('_ret') * x.col('volume_shares')
    return '_pvt_d'


register(
    [Spec(fid, cn, 'vol', 'EMA(V, %d)　首值用 SMA(%d) 起步' % (n, n),
          '成交量的指数加权均线，比同周期 MA 更贴近最近几天的量',
          '股', ('volume_shares',), n * 4,
          (lambda n: lambda x: x.ema('volume_shares', n))(n))
     for n, fid, cn in _EMA_V] +
    [
        Spec('vosc', '成交量震荡', 'vol',
             '(MA(V,12) − MA(V,26)) / MA(V,12)',
             '短期量均与长期量均的相对差：正 = 最近在放量。'
             '★ 分母是 MA(V,12) 不是 MA(V,26)，所以它是"相对短期量"的比例',
             '小数', ('volume_shares',), 26,
             lambda x: (x.ma('volume_shares', 12) - x.ma('volume_shares', 26))
                       / x.ma('volume_shares', 12)),

        Spec('vmacd_dif', '计算VMACD因子的中间变量', 'vol',
             'DIF_v = EMA(V,12) − EMA(V,26)',
             'VMACD 的快慢线之差。⚠ 这个中文名在 factors.xlsx 里出现两次，'
             '多半是 DIF_v 与 DEA_v 两条，但原表没区分 —— 对回聚宽之前'
             '别假定这一条就是它要的那个',
             '股', ('volume_shares',), 26 * 3,
             lambda x: x.ema('volume_shares', 12) - x.ema('volume_shares', 26),
             src_dup=True),

        Spec('vmacd_dea', '计算VMACD因子的中间变量', 'vol',
             'DEA_v = EMA(DIF_v, 9)',
             'VMACD 的信号线。⚠ 同上，名字与 vmacd_dif 在原表里是同一个',
             '股', ('volume_shares',), 26 * 3 + 9,
             lambda x: x._memo('ema_dif', 'volume_shares', 9, lambda: x._ema_impl(
                 x.ema('volume_shares', 12) - x.ema('volume_shares', 26), 9)),
             src_dup=True),

        Spec('v_std10', '10日成交量标准差', 'vol', 'STD_样本(V, 10)',
             '成交量的波动：大说明放量缩量交替频繁',
             '股', ('volume_shares',), 10,
             lambda x: x.std_samp('volume_shares', 10)),
        Spec('v_std20', '20日成交量标准差', 'vol', 'STD_样本(V, 20)',
             '同 v_std10，窗口 20 天',
             '股', ('volume_shares',), 20,
             lambda x: x.std_samp('volume_shares', 20)),
        Spec('v_ma12', '12日成交量的移动平均值', 'vol', 'MA(V, 12)',
             '12 日均量，最常用的"近期常态成交量"基准',
             '股', ('volume_shares',), 12,
             lambda x: x.ma('volume_shares', 12)),

        Spec('vr26', '成交量比率（Volume Ratio）', 'vol',
             'VR(26) = Σ₂₆(涨日 V) / Σ₂₆(跌日 V)',
             '涨日成交与跌日成交的比：>1 说明买盘更活跃。'
             '⚠ 平盘日【不计入】（另一种口径是各计一半）；'
             '跌日成交为 0 时给空值而不是无穷大',
             '倍', ('volume_shares', 'close_hfq'), 27,
             _vr26),

        Spec('vroc6', '6日量变动速率指标', 'vol', 'V / V₋6 − 1',
             '成交量 6 天的变化率。🔴 量纲是小数（0.5 = 放量五成）',
             '小数', ('volume_shares',), 7,
             lambda x: x.col('volume_shares') / x.shift('volume_shares', 6) - 1.0),
        Spec('vroc12', '12日量变动速率指标', 'vol', 'V / V₋12 − 1',
             '同 vroc6，窗口 12 天',
             '小数', ('volume_shares',), 13,
             lambda x: x.col('volume_shares') / x.shift('volume_shares', 12) - 1.0),

        Spec('vol_ret20', '当前交易量相比过去1个月日均交易量 与过去过去20日日均收益率乘积', 'vol',
             '(V / MA(V,20)) × MA(R, 20)',
             '把"今天放量多少倍"与"最近 20 天平均每天涨多少"乘起来 —— '
             '量价同向时绝对值大。⚠ 原名里有重复的"过去过去"，照抄不改：'
             '改了就对不回 factors.xlsx 那一行',
             '小数', ('volume_shares', 'close_hfq'), 21,
             lambda x: (x.col('volume_shares') / x.ma('volume_shares', 20))
                       * x.ma('_ret', 20)),

        Spec('a_ma6', '6日成交金额的移动平均值', 'vol', 'MA(A, 6)',
             '6 日均成交额。比均量更适合跨股票比（额已经含了价）',
             '元(金额)', ('amount',), 6, lambda x: x.ma('amount', 6)),
        Spec('a_ma20', '20日成交金额的移动平均值', 'vol', 'MA(A, 20)',
             '20 日均成交额，选股里最常用的流动性门槛就是它',
             '元(金额)', ('amount',), 20, lambda x: x.ma('amount', 20)),
        Spec('a_std6', '6日成交金额的标准差', 'vol', 'STD_样本(A, 6)',
             '成交额的短期波动', '元(金额)', ('amount',), 6,
             lambda x: x.std_samp('amount', 6)),
        Spec('a_std20', '20日成交金额的标准差', 'vol', 'STD_样本(A, 20)',
             '成交额的波动，窗口 20 天', '元(金额)', ('amount',), 20,
             lambda x: x.std_samp('amount', 20)),

        Spec('mfi14', '资金流量指标', 'vol',
             'TP=(H+L+C)/3；MF = TP×V；MFI(14) = 100 × ΣMF(TP涨) / [ΣMF(TP涨) + ΣMF(TP跌)]',
             '带成交量的 RSI：0~100，看资金流入流出的力量对比。'
             '🔴 方向按 【TP】 的涨跌分，不是按收盘价',
             '0~100', ('high', 'low', 'close_hfq', 'hfq_factor', 'volume_shares'), 15,
             _mfi14),

        Spec('mf_sum20', '20日资金流量', 'vol', 'Σ₂₀ (TP × V)',
             '20 天的资金流总量（不分方向）。它与成交额的差别是用 TP 不是均价',
             '元(金额)', ('high', 'low', 'close_hfq', 'hfq_factor', 'volume_shares'), 20,
             lambda x: x.rsum(_tpv(x), 20)),

        Spec('pvt', '单日价量趋势', 'vol',
             'PVT_日 = R × V；PVT = Σ 累计（从上市起累加）',
             '价量趋势的累计值：涨日加量、跌日减量。'
             '🔴 它是【从上市起累加】的，所以绝对值没有横向可比性 —— '
             '要比得看它的变化或先做横截面标准化',
             '股', ('close_hfq', 'volume_shares'), 2,
             lambda x: x.cumsum(_pvt_day(x))),
        Spec('pvt_ma6', '单日价量趋势6日均值', 'vol', 'MA(PVT_日, 6)',
             '★ 这一条用的是【单日】值不是累计值 —— 累计值的均线没有意义',
             '股', ('close_hfq', 'volume_shares'), 7,
             lambda x: x.ma(_pvt_day(x), 6)),
        Spec('pvt_ma12', '单日价量趋势12均值', 'vol', 'MA(PVT_日, 12)',
             '同 pvt_ma6，窗口 12 天', '股', ('close_hfq', 'volume_shares'), 13,
             lambda x: x.ma(_pvt_day(x), 12)),
    ]
)


def _tpv(x):
    if '_tpv' not in x.df.columns:
        x.df['_tpv'] = x.col('_tp') * x.col('volume_shares')
    return '_tpv'
