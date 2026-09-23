# -*- coding: utf-8 -*-
"""价格位置 / 回归（8）—— 全部 ✅。

公式出处：`因子实现规格.md` 的「价格位置 / 回归」一节。
"""
from . import Spec, register


_DIV = [(20, 'price_div_ma20', '当前股价除以过去一个月股价均值再减1', '一个月'),
        (60, 'price_div_ma60', '当前股价除以过去三个月股价均值再减1', '三个月'),
        (250, 'price_div_ma250', '当前股价除以过去一年股价均值再减1', '一年')]

_SLOPE = [6, 12, 24]

register(
    [Spec(fid, cn, 'pos',
          'C / MA(C, %d) − 1' % n,
          '现价相对过去%s均价的偏离。正 = 站在均线上方。'
          '🔴 量纲是【小数】（0.05 = 高出 5%%），不是百分数' % who,
          '小数(0.05=5%)', ('close_hfq',), n,
          (lambda n: lambda x: x.C / x.ma('close_hfq', n) - 1.0)(n))
     for n, fid, cn, who in _DIV] +

    [Spec('slope%d' % n, '%d日收盘价格与日期线性回归系数' % n, 'pos',
          '窗口内对 (序号 t, C) 做 OLS，取【斜率】（t = 0..%d）' % (n - 1),
          '最近 %d 天的价格趋势强度：斜率为正说明在涨，绝对值说明涨得多陡。'
          '🔴 它的量纲是【元/天】，所以【不同价位的票之间不可直接比大小】 —— '
          '要横向比得先除以价格或做横截面标准化' % n,
          '元/天', ('close_hfq',), n,
          (lambda n: lambda x: x.slope('close_hfq', n))(n))
     for n in _SLOPE] +

    [
        Spec('close_bfq', '不复权价格因子', 'pos',
             'close_bfq（原始盘面价，【不复权】）',
             '券商软件上看到的那个价。🔴 【不复权】是它的定义，不是疏忽 —— '
             '"股价高低"这个概念本身就该用盘面价；换成后复权的话，'
             '一只分红多年的票会显示成几百块',
             '元', ('close_bfq',), 1,
             lambda x: x.col('close_bfq')),

        Spec('pos_1y', '当前价格处于过去1年股价的位置', 'pos',
             '(C − MIN(L,250)) / (MAX(H,250) − MIN(L,250))',
             '现价在过去一年区间里的分位，0 = 一年最低、1 = 一年最高。'
             '⚠ 分子用收盘、分母用最高/最低，所以理论上可能略微越界（停牌'
             '复牌当天），【不做钳制】 —— 钳了就看不出数据异常了',
             '小数(0~1)', ('close_hfq', 'high', 'low', 'hfq_factor'), 250,
             lambda x: ((x.C - x.rmin('_low_hfq', 250))
                        / (x.rmax('_high_hfq', 250) - x.rmin('_low_hfq', 250)))),
    ]
)
