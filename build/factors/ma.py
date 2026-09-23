# -*- coding: utf-8 -*-
"""均线 / 趋势（18）—— 全部 ✅，口径与 `assay/indicators.py` 同源。

公式出处：`datalake/docs/因子实现规格.md` 的「均线 / 趋势」一节。
🔴 EMA 一律 【SMA 起步】、BOLL 的 σ 一律【总体】标准差 —— 这两条不是风格
选择，是那边已经定了的口径；分叉的表现是"同一只票在看盘页和因子表里
对不上"，而它不报错。
"""
from . import Spec, register


def _macd(x, col):
    dif = x.ema(col, 12) - x.ema(col, 26)
    dea = x._memo('ema_dif', col, 9, lambda: x._ema_impl(dif, 9))
    return 2.0 * (dif - dea)


def _trix(x, n):
    e3 = x._memo('trix_e3', 'close_hfq', n,
                 lambda: x._ema_impl(x._ema_impl(x.ema('close_hfq', n), n), n))
    return x._by(e3).pct_change()


_MA_N = [(5, '5日移动均线'), (10, '10日移动均线'), (20, '20日移动均线'),
         (60, '60日移动均线'), (120, '120日移动均线')]
_EMA_N = [(5, '5日指数移动均线'), (10, '10日指数移动均线'),
          (12, '12日指数移动均线'), (20, '20日指数移动均线'),
          (26, '26日指数移动均线'), (120, '120日指数移动均线')]

register(
    [Spec('ma%d' % n, cn, 'ma',
          'MA(C, %d)' % n,
          '最近 %d 天后复权收盘价的算术平均。趋势线：几条均线的相对位置'
          '（多头/空头排列）说明趋势的方向与强弱' % n,
          '元', ('close_hfq',), n,
          (lambda n: lambda x: x.ma('close_hfq', n))(n))
     for n, cn in _MA_N] +

    [Spec('ema%d' % n, cn, 'ma',
          'EMA(C, %d)　首值用 SMA(%d) 起步' % (n, n),
          '指数加权均线，比同周期 MA 更贴近最新价。🔴 首值用 SMA 起步'
          '（与 assay/indicators.py 同口径），不是拿第一个收盘价当种子',
          '元', ('close_hfq',), n * 4,
          (lambda n: lambda x: x.ema('close_hfq', n))(n))
     for n, cn in _EMA_N] +

    [
        Spec('macd', '平滑异同移动平均线', 'ma',
             'DIF = EMA(C,12) − EMA(C,26)；DEA = EMA(DIF,9)；MACD = 2×(DIF − DEA)',
             '看趋势的转折与力度。这里取的是【柱】（DIF−DEA 的两倍），'
             '正负与大小合起来说明动能的方向与强弱',
             '元', ('close_hfq',), 26 * 3 + 9,
             lambda x: _macd(x, 'close_hfq')),

        Spec('vmacd', '成交量指数平滑异同移动平均线', 'ma',
             '把 MACD 的 C 换成 V：DIF_v = EMA(V,12) − EMA(V,26)；'
             'DEA_v = EMA(DIF_v,9)；VMACD = 2×(DIF_v − DEA_v)',
             '成交量版的 MACD，看的是量能的动能而不是价格的。'
             '⚠ `factors.xlsx` 里「计算VMACD因子的中间变量」出现过两次，'
             '那两个是 DIF_v / DEA_v 还是别的分不出来 —— 这里只给最终柱',
             '股', ('volume_shares',), 26 * 3 + 9,
             lambda x: _macd(x, 'volume_shares')),

        Spec('trix5', '5日终极指标TRIX', 'ma',
             'TR = EMA(EMA(EMA(C,5),5),5)；TRIX = TR / TR₋₁ − 1',
             '三重指数平滑之后的变化率，把短期噪声磨掉只留趋势。'
             '🔴 它是【变化率】不是价格，量纲是小数',
             '小数(0.01=1%)', ('close_hfq',), 5 * 9,
             lambda x: _trix(x, 5)),

        Spec('trix10', '10日终极指标TRIX', 'ma',
             'TR = EMA(EMA(EMA(C,10),10),10)；TRIX = TR / TR₋₁ − 1',
             '同 trix5，窗口 10 天，更钝也更稳',
             '小数(0.01=1%)', ('close_hfq',), 10 * 9,
             lambda x: _trix(x, 10)),

        Spec('boll_up', '上轨线（布林线）指标', 'ma',
             'MA(C,20) + 2 × STD_总体(C,20)',
             '布林通道上轨。🔴 σ 取【总体】标准差（除 N，不是 N−1）—— '
             '与 assay/indicators.py 同口径；用样本标准差算出来的通道'
             '会系统性偏宽，而它不报错',
             '元', ('close_hfq',), 20,
             lambda x: x.ma('close_hfq', 20) + 2 * x.std_pop('close_hfq', 20)),

        Spec('boll_dn', '下轨线（布林线）指标', 'ma',
             'MA(C,20) − 2 × STD_总体(C,20)',
             '布林通道下轨，口径同上轨',
             '元', ('close_hfq',), 20,
             lambda x: x.ma('close_hfq', 20) - 2 * x.std_pop('close_hfq', 20)),

        Spec('bbi', 'BBI 动量', 'ma',
             '(MA(C,3) + MA(C,6) + MA(C,12) + MA(C,24)) / 4',
             '四条不同周期均线的平均，相当于一条"多周期共识"的中枢线',
             '元', ('close_hfq',), 24,
             lambda x: (x.ma('close_hfq', 3) + x.ma('close_hfq', 6)
                        + x.ma('close_hfq', 12) + x.ma('close_hfq', 24)) / 4.0),
    ]
)
