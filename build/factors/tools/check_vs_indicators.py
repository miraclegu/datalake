# -*- coding: utf-8 -*-
"""因子口径对数：与 `assay/indicators.py` 逐值比 —— 15 项。

    python3 datalake/build/factors/tools/check_vs_indicators.py

## 为什么要有这个脚本

MA / EMA / MACD / BOLL / BBI 在 `assay/indicators.py` 里**已经有一份实现**，
而且口径是刻意选过的（EMA 用 SMA 起步、BOLL 的 σ 用**总体**标准差）。
因子模块是**同一个公式的另一种形状**（那边单票时间序列、这边全市场截面落盘），
形状不同不能合并 —— 但**口径必须一致**，否则「同一只票在看盘页和因子表里
对不上」，而它不报错。

「记得手工核对一遍」不是判据（同「靠人记得跑的步骤 = 迟早不跑」），所以写成脚本。

## 🔴 判据是【相对误差】，不是绝对差 —— 这一条试错了三次

    ① 拿 `indicators.compute()` 的输出比      -> 它经过 `_r3`（舍到 3 位）。
       而 close_hfq 是 2 位小数、÷20 必然落在 .xx5 上：实测 2831 个 ma20 里
       **1413 个（50%）恰好在舍入边界上**，1e-12 的浮点差就把第 3 位翻面。
       于是 ma20/ma60/ma120 报"2034 处不等"，而两边数值其实完全一样。
       -> 要比的是**未舍入的内部函数**，`_r3` 是显示不是口径。
    ② 绝对阈值 `max|差| < 1e-9`               -> 600519 后复权价 ~18000，
       float64 在这个量级上的累计噪声就有 2.7e-9 -> BOLL 被误判成分叉。
    ③ **相对误差 < 1e-10**                    -> 与量级无关。

★ 而反向自证说明这个阈值有多宽松：真把 EMA 换成"首值起步"、把 BOLL 的 σ
  换成样本标准差，max|差| 是 **6.6 / 68.9** —— 比阈值大十个数量级。
  所以它既不会误报，也绝不会漏报。
"""
import os
import sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
BUILD = os.path.dirname(os.path.dirname(HERE))          # datalake/build
DL = os.path.dirname(BUILD)                             # datalake
REPO = os.path.dirname(DL)                              # 工作区根
sys.path.insert(0, BUILD)
sys.path.insert(0, os.path.join(REPO, 'assay'))

from factors import Ctx, all_specs                       # noqa: E402
from assay import indicators as ind                      # noqa: E402

CODES = ('601857.XSHG', '000001.XSHE', '600519.XSHG')    # 低价 / 中价 / 高价各一
SINCE = '2015-01-01'
RTOL = 1e-10
FIDS = ['ma5', 'ma10', 'ma20', 'ma60', 'ma120',
        'ema5', 'ema10', 'ema12', 'ema20', 'ema26', 'ema120',
        'boll_up', 'boll_dn', 'macd', 'bbi',
        'cci10', 'cci15', 'cci20', 'cci88',
        'bias5', 'bias10', 'bias20', 'bias60',
        'atr6', 'atr14']

#: 这几个要 high/low/close（后复权）而不只是 close
NEED_HLC = {'cci10', 'cci15', 'cci20', 'cci88', 'atr6', 'atr14'}
#: 🔴 BIAS 两边差 **100 倍**：因子取小数、indicators 取百分数（见 osc.py 的
#:   那张分歧表）。对数时把我的值 ×100 —— 这是**声明的**口径差，
#:   不是分叉；混着比的话会报一个 99% 的假分叉。
SCALE = {'bias5': 100.0, 'bias10': 100.0, 'bias20': 100.0, 'bias60': 100.0}


def ref(cl, fid):
    """参照值 —— 一律走 `indicators.py` 的**内部**函数（不经 `_r3`）。"""
    if fid.startswith('ma') and fid[2:].isdigit():
        return ind._ma(cl, int(fid[2:]))
    if fid.startswith('ema'):
        return ind._sma_seed_ema(cl, int(fid[3:]))
    if fid in ('boll_up', 'boll_dn'):
        n, o = 20, []
        for i in range(len(cl)):
            if i < n - 1:
                o.append(None)
                continue
            seg = cl[i - n + 1:i + 1]
            mu = sum(seg) / n
            sd = (sum((v - mu) ** 2 for v in seg) / n) ** 0.5   # 总体
            o.append(mu + 2 * sd if fid == 'boll_up' else mu - 2 * sd)
        return o
    if fid == 'macd':
        ef, es = ind._sma_seed_ema(cl, 12), ind._sma_seed_ema(cl, 26)
        dif = [(ef[i] - es[i]) if (ef[i] is not None and es[i] is not None)
               else None for i in range(len(cl))]
        got = [v for v in dif if v is not None]
        dr = ind._sma_seed_ema(got, 9)
        off = len(cl) - len(got)
        dea = [(dr[i - off] if i >= off else None) for i in range(len(cl))]
        return [2 * (dif[i] - dea[i])
                if (dif[i] is not None and dea[i] is not None) else None
                for i in range(len(cl))]
    if fid.startswith('bias'):
        # 🔴 `_bias_calc` 里有 `round(..., 3)` —— 照抄它的**公式**但不舍入。
        #   MA 仍然用 ind._ma，口径还是那边的（要证的是口径不是算术）。
        n = int(fid[4:])
        ma = ind._ma(cl, n)
        return [((cl[i] / ma[i] - 1) * 100) if ma[i] else None
                for i in range(len(cl))]
    if fid == 'bbi':
        ms = [ind._ma(cl, k) for k in (3, 6, 12, 24)]
        return [(sum(m[i] for m in ms) / 4.0)
                if all(m[i] is not None for m in ms) else None
                for i in range(len(cl))]
    raise KeyError(fid)


def ref_hlc(bars, tr, fid):
    """要 H/L/C 的那几个。`bars` 已是后复权。"""
    if fid.startswith('cci'):
        # 🔴 `_cci_calc` 里有 `round(..., 2)`，舍入上界 5e-3 —— 拿它的输出比
        #   会报一个 5.00e-03 的假分叉（这个坑本轮是**第三次**踩：
        #   `_r3` / `round(·,2)` / `round(·,3)`）。照抄公式、不舍入。
        n = int(fid[3:])
        tp = [(b['high'] + b['low'] + b['close']) / 3.0 for b in bars]
        ma = ind._ma(tp, n)
        o = []
        for i in range(len(bars)):
            if ma[i] is None:
                o.append(None)
                continue
            seg = tp[i - n + 1:i + 1]
            md = sum(abs(v - ma[i]) for v in seg) / n
            o.append((tp[i] - ma[i]) / (0.015 * md) if md else None)
        return o
    if fid.startswith('atr'):
        # 🔴 **喂我自己的 TR**，而不是让 indicators 从 bars 重算 ——
        #   这一条要证的是「Wilder 平滑口径一致」，而 TR 的定义两边本来
        #   就有一处【已声明】的差别：indicators 拿前一根的 close 当 PC，
        #   因子按规格文档用 `preclose × hfq_factor`。实测最大差 0.005，
        #   那正是 CLAUDE.md 记过的「权威 close_hfq vs 推算 close_bfq×factor
        #   各自舍入」（<0.004 那条），**不是除权问题**（833 处差异里只有
        #   3 处落在除权日）。所以它单独用 `tr_gap` 那条判据管，
        #   不混进平滑口径这条里 —— 混着比的话两个问题都说不清。
        return ind._wilder(tr, int(fid[3:]))
    raise KeyError(fid)


def main():
    import duckdb
    from factors import load
    g = os.path.join(DL, 'mart', 'panel_daily', 'panel_*.parquet')
    # ★ 走取数正本 —— 自己拼 SELECT 的话，加一族因子就会与落盘那条路分叉
    #   （这个文件就这么坏过一次）。
    df = load.chunk_df(duckdb.connect(':memory:'), g, DL, CODES,
                       where="date >= DATE '%s'" % SINCE)
    x = Ctx(df)
    # 🔴 **只算这一条用例要比的那几个**，不是 all_specs()。
    #   加了财务族之后 `all_specs()` 里有 64 条要 `b_*` / `t_*` 列，
    #   而这里的 Ctx 只喂了面板列 —— 全算会 NameError 直接崩。
    #   ⚠ 这个 bug 是加财务因子那一轮引入的，而**直到把守卫接进 selftest
    #     才被发现**（上次跑它是在加财务因子之前）。正是
    #     「靠人记得跑的步骤 = 迟早不跑」。
    want = set(FIDS)
    V = {s.id: np.asarray(s.calc(x), dtype='float64')
         for s in all_specs() if s.id in want}
    miss = want - set(V)
    assert not miss, '注册表里没有这几个因子，对数清单过期了: %s' % sorted(miss)
    D = x.df
    print('样本 %d 行 / %d 只 / %s 起' % (len(D), D.jq_code.nunique(), SINCE))
    print('判据：与 indicators.py 内部函数逐值比，**相对**误差 < %g\n' % RTOL)
    print('%-9s %8s %11s %11s %s' % ('因子', '可比点', 'max|差|', '相对', '判定'))
    bad = []
    for fid in FIDS:
        md = scale = 0.0
        npts = 0
        for c in CODES:
            m = (D['jq_code'] == c).values
            cl = [float(v) for v in D.loc[m, 'close_hfq']]
            if fid in NEED_HLC:
                bars = [{'high': float(h), 'low': float(lo), 'close': float(cc)}
                        for h, lo, cc in zip(D.loc[m, '_high_hfq'],
                                             D.loc[m, '_low_hfq'],
                                             D.loc[m, 'close_hfq'])]
                tr = [float(v) if np.isfinite(v) else None
                      for v in D.loc[m, '_tr']]
                r = ref_hlc(bars, tr, fid)
            else:
                r = ref(cl, fid)
            mine = V[fid][m] * SCALE.get(fid, 1.0)
            for a, b in zip(r, mine):
                if a is None or not np.isfinite(b):
                    continue
                npts += 1
                md = max(md, abs(float(a) - float(b)))
                scale = max(scale, abs(float(a)))
        rel = md / scale if scale else 0.0
        ok = rel < RTOL and npts > 1000
        if not ok:
            bad.append(fid)
        print('%-9s %8d %11.2e %11.2e %s'
              % (fid, npts, md, rel, '✓' if ok else '🔴 分叉'))

    # 🔴 反向自证：判据抓不抓得住真分叉。没有这一段的话，"全绿"可能只是
    #   因为阈值宽到什么都拦不住（同「断言要能被变异打红」）。
    c = CODES[2]
    m = (D['jq_code'] == c).values
    cl = [float(v) for v in D.loc[m, 'close_hfq']]
    import pandas as pd
    d1 = max(abs(a - b) for a, b in zip(
        ind._sma_seed_ema(cl, 12),
        pd.Series(cl).ewm(span=12, adjust=False).mean().tolist()) if a is not None)
    d2 = 0.0
    for i in range(19, len(cl)):
        seg = cl[i - 19:i + 1]
        mu = sum(seg) / 20
        d2 = max(d2, abs(2 * ((sum((v - mu) ** 2 for v in seg) / 20) ** 0.5)
                         - 2 * ((sum((v - mu) ** 2 for v in seg) / 19) ** 0.5)))
    # 🔴 TR 的 PC 定义差：必须小于一个最小价位变动（0.01），否则就不是
    #   舍入而是真分叉了。
    tg = 0.0
    for c in CODES:
        m = (D['jq_code'] == c).values
        bars = [{'high': float(h), 'low': float(lo), 'close': float(cc)}
                for h, lo, cc in zip(D.loc[m, '_high_hfq'],
                                     D.loc[m, '_low_hfq'], D.loc[m, 'close_hfq'])]
        ti = ind._tr(bars)
        for a, b in zip(ti, D.loc[m, '_tr']):
            if a is not None and np.isfinite(b):
                tg = max(tg, abs(float(a) - float(b)))
    print('\nTR 的 PC 定义差（前一根 close vs preclose×factor）: max %.4f  -> %s'
          % (tg, '✓ 小于一个最小价位变动(0.01)，是已知的舍入差'
             if tg < 0.01 else '🔴 超出舍入量级，要查'))
    if tg >= 0.01:
        bad.append('tr_gap')

    print('\n反向自证（真分叉必须远大于阈值）：')
    print('  EMA 改"首值起步"      max|差| = %.3e' % d1)
    print('  BOLL σ 改样本(除N−1)  max|差| = %.3e' % d2)
    ok2 = d1 > 1e-3 and d2 > 1e-3
    print('  -> %s' % ('✓ 判据抓得住（比阈值大十个数量级）' if ok2 else '🔴 判据空转'))

    # ★ 条数**自己算**，不写死 —— 写死的话下次加因子忘了改，
    #   报告串会说"15 项全过"而其实跑了 25 项（同「数字写死的话下次再拆
    #   就得手改，而忘了改的表现是报告串在说谎」）。
    print('\n总判定: %s' % ('✓ %d 项与 indicators.py 口径一致' % len(FIDS)
                          if not bad and ok2 else '🔴 %s' % (bad or '自证失败')))
    return 0 if (not bad and ok2) else 1


if __name__ == '__main__':
    raise SystemExit(main())
