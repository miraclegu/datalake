# -*- coding: utf-8 -*-
"""摆动 / 超买超卖（27）—— 全部 ✅。

公式出处：`因子实现规格.md` 的「摆动 / 超买超卖」一节。

## 🔴 两处口径分歧，都是【显式决定】不是默认跟某一边

| | 因子清单原文 | assay/indicators.py | 这里取 |
|---|---|---|---|
| ATR | `MA(TR, n)` | **Wilder** 递推（α=1/n） | **Wilder** |
| BIAS | `(C − MA)/MA`（小数） | `(C/MA − 1) × 100`（百分数） | **小数** |

- **ATR 取 Wilder**：那是 Welles Wilder 的原始定义，也是行情软件在用的；
  项目自己的纪律是「口径跟行情软件走，不跟教科书走 —— 对不上券商软件时，
  人会以为是**数据错了**」。而且这样才能与 `indicators.py` 逐值对数。
- **BIAS 取小数**：因子是拿去做横截面排序的，量纲统一成小数与本族其它
  比率一致；与 `indicators.py` 的 bias 差 **100 倍**，对数时乘 100 比。

🔴 **价格一律后复权**（`_high_hfq` / `_low_hfq` / `_open_hfq` / `_pre_hfq`）。
  面板里 open/high/low/preclose 都是不复权的，忘乘因子的话跨除权日会算出
  假的波幅，而它不报错。
"""
import numpy as np

from . import Spec, register

_CCI_N = [10, 15, 20, 88]
_BIAS_N = [5, 10, 20, 60]
_ROC_N = [6, 12, 20, 60, 120]


def _cci(x, n):
    """CCI = (TP − MA(TP,n)) / (0.015 × MAD(TP,n))，MAD 是平均**绝对**偏差。"""
    md = x.mad('_tp', n)
    return (x.col('_tp') - x.ma('_tp', n)) / (0.015 * md.replace(0.0, np.nan))


def _wvad_day(x):
    """WVAD_日 = (C − O) / (H − L) × V。

    🔴 **一字板时 H == L，分母为 0。** A 股这不是边角料 —— 涨跌停一字板
      每天都有几十只。不挡的话得到 ±inf，之后任何均值/排序都被它毁掉，
      **而 inf 不报错**（它是个合法的 float）。这里给空值。
    """
    if '_wvad_d' not in x.df.columns:
        rng = (x.col('_high_hfq') - x.col('_low_hfq')).replace(0.0, np.nan)
        x.df['_wvad_d'] = ((x.col('close_hfq') - x.col('_open_hfq')) / rng
                           * x.col('volume_shares'))
    return '_wvad_d'


def _ar26(x):
    d = x.df
    d['_ar_up'] = d['_high_hfq'] - d['_open_hfq']
    d['_ar_dn'] = d['_open_hfq'] - d['_low_hfq']
    return 100.0 * x.rsum('_ar_up', 26) / x.rsum('_ar_dn', 26).replace(0.0, np.nan)


def _br26(x):
    d = x.df
    d['_br_up'] = np.maximum(d['_high_hfq'] - d['_pre_hfq'], 0.0)
    d['_br_dn'] = np.maximum(d['_pre_hfq'] - d['_low_hfq'], 0.0)
    return 100.0 * x.rsum('_br_up', 26) / x.rsum('_br_dn', 26).replace(0.0, np.nan)


def _cr26(x):
    """CR 的中枢是**前一日**的 TP —— 用当日 TP 是最常见的抄错法。"""
    d = x.df
    mid = x.shift('_tp', 1)
    d['_cr_up'] = np.maximum(d['_high_hfq'] - mid, 0.0)
    d['_cr_dn'] = np.maximum(mid - d['_low_hfq'], 0.0)
    return 100.0 * x.rsum('_cr_up', 26) / x.rsum('_cr_dn', 26).replace(0.0, np.nan)


def _mass(x):
    """梅斯线：EMA1 = EMA(H−L,9)；EMA2 = EMA(EMA1,9)；MASS = Σ₂₅(EMA1/EMA2)。"""
    d = x.df
    if '_hl' not in d.columns:
        d['_hl'] = d['_high_hfq'] - d['_low_hfq']
    e1 = x.ema('_hl', 9)
    e2 = x._memo('mass_e2', '_hl', 9, lambda: x._ema_impl(e1, 9))
    d['_mass_r'] = e1 / e2.replace(0.0, np.nan)
    return x.rsum('_mass_r', 25)


def _psy12(x):
    d = x.df
    d['_up1'] = (x.col('_ret') > 0).astype('float64')
    return 100.0 * x.rsum('_up1', 12) / 12.0


register(
    [Spec('cci%d' % n, '%d日顺势指标' % n, 'osc',
          'TP=(H+L+C)/3；CCI = (TP − MA(TP,%d)) / (0.015 × MAD(TP,%d))' % (n, n),
          '价格偏离自身均值的程度，常用 ±100 当超买超卖线。'
          '🔴 分母是平均【绝对】偏差不是标准差 —— 换成标准差数值会整体偏小'
          '约两成，而那是个看着完全正常的错',
          '无量纲', ('high', 'low', 'close_hfq', 'hfq_factor'), n + 5,
          (lambda n: lambda x: _cci(x, n))(n))
     for n in _CCI_N] +

    [Spec('bias%d' % n, '%d日乖离率' % n, 'osc',
          '(C − MA(C,%d)) / MA(C,%d)' % (n, n),
          '现价偏离均线多远。🔴 这里是【小数】（0.05 = 高出 5%%）；'
          'assay/indicators.py 的 bias 是【百分数】，两者差 100 倍',
          '小数(0.05=5%)', ('close_hfq',), n,
          (lambda n: lambda x: (x.C - x.ma('close_hfq', n)) / x.ma('close_hfq', n))(n))
     for n in _BIAS_N] +

    [Spec('roc%d' % n, '%d日变动速率（Price Rate of Change）' % n, 'osc',
          'C / C₋%d − 1' % n,
          '%d 个交易日的涨跌幅，最直接的动量。🔴 量纲是小数不是百分数' % n,
          '小数(0.05=5%)', ('close_hfq',), n + 1,
          (lambda n: lambda x: x.C / x.shift('close_hfq', n) - 1.0)(n))
     for n in _ROC_N] +

    [
        Spec('atr6', '6日均幅指标', 'osc',
             'TR = max(H−L, |H−PC|, |L−PC|)；ATR = Wilder(TR, 6)',
             '波动【幅度】，不指示方向，常用来定止损距离。'
             '🔴 取 Wilder 递推（α=1/6）不是简单均值 —— 与 '
             'assay/indicators.py 同口径；两者能差 10%% 以上，'
             '而都像正常数字',
             '元', ('high', 'low', 'preclose', 'hfq_factor'), 6 * 4,
             lambda x: x.wilder('_tr', 6)),
        Spec('atr14', '14日均幅指标', 'osc',
             'TR = max(H−L, |H−PC|, |L−PC|)；ATR = Wilder(TR, 14)',
             '同 atr6，窗口 14 天（Wilder 的原始参数）',
             '元', ('high', 'low', 'preclose', 'hfq_factor'), 14 * 4,
             lambda x: x.wilder('_tr', 14)),

        Spec('aroon_up', 'Aroon指标上轨', 'osc',
             '100 × (25 − 距窗口内最高价出现日的天数) / 25　（窗口取 26 根）',
             '距上一次创新高过去了多久：100 = 今天就是最高。'
             '★ 窗口取 n+1 = 26 根才能让取值真正落在 0~100 —— '
             '只取 25 根的话最小值是 4，永远到不了 0',
             '0~100', ('high', 'hfq_factor'), 26,
             lambda x: 100.0 * (25.0 - x.since_max('_high_hfq', 26)) / 25.0),
        Spec('aroon_dn', 'Aroon指标下轨', 'osc',
             '100 × (25 − 距窗口内最低价出现日的天数) / 25　（窗口取 26 根）',
             '距上一次创新低过去了多久，口径同上轨',
             '0~100', ('low', 'hfq_factor'), 26,
             lambda x: 100.0 * (25.0 - x.since_min('_low_hfq', 26)) / 25.0),

        Spec('ar26', '人气指标', 'osc',
             'AR(26) = 100 × Σ₂₆(H − O) / Σ₂₆(O − L)',
             '开盘价上下的力量对比：高说明买气旺。分母为 0 时给空值',
             '无量纲', ('high', 'low', 'open', 'hfq_factor'), 26, _ar26),
        Spec('br26', '意愿指标', 'osc',
             'BR(26) = 100 × Σ₂₆ max(H − PC, 0) / Σ₂₆ max(PC − L, 0)',
             '相对【昨收】的力量对比（AR 相对的是今开）',
             '无量纲', ('high', 'low', 'preclose', 'hfq_factor'), 26, _br26),
        Spec('arbr', 'ARBR', 'osc', 'AR(26) − BR(26)',
             '两个指标的差。★ 它们分母不同（今开 vs 昨收），差值反映的是'
             '"跳空"那部分的力量',
             '无量纲', ('high', 'low', 'open', 'preclose', 'hfq_factor'), 26,
             lambda x: _ar26(x) - _br26(x)),
        Spec('cr26', 'CR指标', 'osc',
             'MID = 前一日的 (H+L+C)/3；CR(26) = 100 × Σ₂₆ max(H − MID₋₁, 0) '
             '/ Σ₂₆ max(MID₋₁ − L, 0)',
             '以【前一日】典型价为中枢的力量对比。'
             '🔴 中枢是前一日的 —— 用当日 TP 是最常见的抄错法，'
             '而算出来的数照样在正常范围里',
             '无量纲', ('high', 'low', 'close_hfq', 'hfq_factor'), 27, _cr26),

        Spec('mass', '梅斯线', 'osc',
             'EMA1 = EMA(H−L, 9)；EMA2 = EMA(EMA1, 9)；MASS = Σ₂₅ (EMA1 / EMA2)',
             '看振幅的"扩张/收缩"：它不看方向，只看高低价区间在变宽还是变窄。'
             '常用来提示趋势反转前的鼓包',
             '无量纲', ('high', 'low', 'hfq_factor'), 9 * 4 + 25, _mass),

        Spec('bull_power', '多头力道', 'osc', 'H − EMA(C, 13)',
             '当日最高价超出 13 日 EMA 多少 —— 多头把价格推高的能力。'
             '🔴 量纲是【元】，跨股票不可直接比大小',
             '元', ('high', 'close_hfq', 'hfq_factor'), 13 * 4,
             lambda x: x.col('_high_hfq') - x.ema('close_hfq', 13)),
        Spec('bear_power', '空头力道', 'osc', 'L − EMA(C, 13)',
             '当日最低价低于 13 日 EMA 多少（通常为负）',
             '元', ('low', 'close_hfq', 'hfq_factor'), 13 * 4,
             lambda x: x.col('_low_hfq') - x.ema('close_hfq', 13)),

        Spec('wvad', '威廉变异离散量', 'osc',
             'WVAD_日 = (C − O) / (H − L) × V；WVAD = Σ₂₄ WVAD_日',
             '按"收盘在当日区间里的位置"给成交量加权再累计 24 天。'
             '🔴 一字板时 H == L，分母为 0 —— 那不是边角料，A 股每天几十只。'
             '这里给【空值】而不是 ±inf（inf 是合法 float，不报错，'
             '却会毁掉之后任何均值与排序）',
             '股', ('high', 'low', 'open', 'close_hfq', 'hfq_factor',
                    'volume_shares'), 24,
             lambda x: x.rsum(_wvad_day(x), 24)),
        Spec('wvad_ma6', '因子WVAD的6日均值', 'osc', 'MA(WVAD_日, 6)',
             '★ 用的是【单日】值不是 24 日累计值', '股',
             ('high', 'low', 'open', 'close_hfq', 'hfq_factor',
              'volume_shares'), 6,
             lambda x: x.ma(_wvad_day(x), 6)),

        Spec('psy12', '心理线指标', 'osc',
             'PSY(12) = 100 × count(R > 0, 12) / 12',
             '过去 12 天里有几天是涨的，折成百分数。'
             '★ 平盘日算"不涨"（`R > 0` 是严格大于）',
             '0~100', ('close_hfq',), 13, _psy12),
    ]
)
