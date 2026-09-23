# -*- coding: utf-8 -*-
"""因子注册表 —— 自建因子池的【唯一正本】。

一个因子 = 一条 `Spec`：编号 / 中文名 / 族 / 公式 / 说明 / 单位 / 依赖列 /
预热根数 / 可信度 / 算法，**全在一处声明**。目录表、因子值、将来的接口与
页面都照它生成 —— 加一个因子只改一处。

这条纪律是 `assay/indicators.py` 那一轮的直接套用：

    改造前 MACD/KDJ 的公式写死在三处，加一个指标要改三处，
    而漏掉画的那处**不报错** —— 只是选中它之后副图一片空白

## 🔴 `tier` 必须跟着因子一路走到页面上

`因子实现规格.md` 把 278 个因子分了四档可信度，而**只写在 md 里等于没写**
（同「靠人记得跑的步骤 = 迟早不跑」）。所以它是 Spec 的字段、进目录表：

    exact  ✅✅ 逐只实测定案，与聚宽精确一致
    std    ✅  行业标准 / 会计定义，无歧义；与聚宽**大概率**一致但未逐个验证
    own    ⚠️  **自建定义** —— 数据齐、聚宽口径未知。算出来是个有意义的因子，
               **但不是聚宽那个**

🔴 `own` 那 42 个是本模块最大的误用风险：名字与聚宽清单一字不差，口径却是
我们自己定的。不标的话，拿本模块的 IC 去跟 `factors.xlsx` 的 IC 比就是
**在比两个不同的量**，而它不报错。

## 🔴 口径与 `assay/indicators.py` 同源，不另立一套

MA / EMA / MACD / BOLL 那几个 `assay/indicators.py` 里**已经有了**，而且
口径是刻意选过的（EMA 用 SMA 起步、BOLL 的 σ 用**总体**标准差）。这里是
**同一个公式的另一种形状**：那边单只票 × 时间序列（看盘现算），这里全市场
截面 × 落盘（研究用）—— 形状不同，不能合并（同「`perf.bench_curves` 与
`symbols.alt_panel` 都算 `close × factor` 但形状不同，硬并的等价性风险
大于收益」那条）。

**但口径必须一致**，所以 `tools/check_vs_indicators.py` 拿两边逐值对数，
不一致就是有一边错了 —— 「看着差不多」不是判据。

## 🔴 `warm` 有下限 120 根

`indicators.py` 那条：RSI/ATR/EMA 这类递推**永远记着起点**，多喂 24 根
末尾的值就会差几厘。所以增量计算时无条件多读 `max(warm, WARM_FLOOR)` 根
—— 少读**不报错**，只是那几天的值悄悄变了。
"""
import numpy as np
import pandas as pd


WARM_FLOOR = 120

TIERS = {
    'exact': '✅✅ 逐只实测定案',
    'std':   '✅ 行业标准/会计定义，无歧义',
    'own':   '⚠️ 自建定义 —— 不是聚宽那个因子',
}

GROUPS = {
    'ma':   '均线 / 趋势',
    'pos':  '价格位置 / 回归',
    'dist': '收益分布 / 波动',
    'osc':  '摆动 / 超买超卖',
    'vol':  '量能 / 资金流',
    'turn': '换手率',
}


class Spec(object):
    """一个因子。

    id       我们的**稳定编号**（落盘列名、接口 key 都用它）。🔴 一旦发布不改 ——
             改了等于换了一个因子，而历史值还在那儿（同「账户 id 不可改」）。
    name_cn  `factors.xlsx` 里的原名 —— 目录表靠它对回原清单
    group    见 GROUPS
    formula  怎么算的（给人读的一行）
    desc     干什么用的。★ 与 formula **分两个字段**：揉成一段话的话，
             要么公式被埋在叙述里、要么叙述被公式挤没
    unit     🔴 比率一律写 '小数(0.05=5%)' —— 标错量纲页面会显示 "0.05%"，
             而那看着像个正常的小数字
    deps     依赖哪几列面板原始列（增量时只读这些）
    warm     要多少根预热（真正用的是 max(warm, WARM_FLOOR)）
    calc     (Ctx) -> pd.Series，与 Ctx.df 同索引
    tier     exact / std / own
    note     要让人看见的话（稀疏字段、口径取舍…）
    src_dup  True = `factors.xlsx` 里这个中文名出现过不止一次，
             **光看名字分不出是哪一个**，落地前要拿到聚宽的 factor code
    """

    def __init__(self, id, name_cn, group, formula, desc, unit, deps, warm,
                 calc, tier='std', note='', src_dup=False):
        assert group in GROUPS, '未知因子族: %s' % group
        assert tier in TIERS, '未知可信度: %s' % tier
        # 🔴 **这几个字段是数据不是 markdown。** 它们会进目录表、接口、
        #   将来还会进页面的 title 属性 —— 而属性里连 <b> 都用不了，
        #   `**强调**` 会原样显示成一串星号（本项目为此踩过三次）。
        #   要强调就用【】。这里**直接拒**，不是写在文档里提醒。
        for _f, _v in (('formula', formula), ('desc', desc),
                       ('unit', unit), ('note', note), ('name_cn', name_cn)):
            assert '**' not in (_v or ''), (
                '%s 的 %s 里有 markdown 星号（页面上会原样显示），用【】：%r'
                % (id, _f, _v))
        self.id, self.name_cn, self.group = id, name_cn, group
        self.formula, self.desc, self.unit = formula, desc, unit
        self.deps, self.warm, self.calc = tuple(deps), int(warm), calc
        self.tier, self.note, self.src_dup = tier, note, bool(src_dup)

    @property
    def warm_eff(self):
        return max(self.warm, WARM_FLOOR)

    def row(self):
        """目录表的一行。**目录表照它生成，不手工维护**。"""
        return {
            'factor_id': self.id,
            'name_cn': self.name_cn,
            'group_key': self.group,
            'group_cn': GROUPS[self.group],
            'formula': self.formula,
            'desc': self.desc,
            'unit': self.unit,
            'tier': self.tier,
            'tier_cn': TIERS[self.tier],
            'deps': ','.join(self.deps),
            'warm': self.warm,
            'warm_eff': self.warm_eff,
            'note': self.note,
            'src_dup': self.src_dup,
        }


# ======================= 共享中间量：算一次，多处用 =======================
class Ctx(object):
    """面板 + 带缓存的窗口算子。

    🔴 **`ma20` 与布林上下轨共用同一次 `MA(C,20)`** —— 各算各的话不只是慢，
      更是两份实现（同「一件事只许有一份实现」）。缓存键是 (算子, 列, 窗口)。

    ★ 全部按 `jq_code` 分组、按 date 排序之后再做滚动 —— 不分组的话窗口会
      **跨到上一只票**上去，而它不报错，只是每只票头 N 行是错的。
    """

    #: 构造时就派生出来的列。🔴 **必须是真列不是 Series** ——
    #: 缓存键是 (算子, 列名, 窗口)，而 Series 不可哈希；传 Series 进来会
    #: 直接 TypeError（那还算好的），或者绕过缓存把同一个窗口算好几遍。
    DERIVED = ('_ret', '_high_hfq', '_low_hfq', '_open_hfq', '_pre_hfq',
              '_tp', '_tr')

    def __init__(self, df):
        # 🔴 排序是正确性的前提，不是"顺手" —— 滚动窗口按行序走，
        #   不排的话窗口会跨到上一只票上去，而它不报错。
        self.df = df.sort_values(['jq_code', 'date'], kind='mergesort') \
                    .reset_index(drop=True)
        self._g = self.df.groupby('jq_code', sort=False)
        self._c = {}
        d = self.df
        # 日收益：🔴 必须**分组** pct_change —— 不分组的话每只票的第一天会拿
        #   上一只票的最后一天当昨收，算出一个巨大的假收益（而它不报错）。
        d['_ret'] = self._g['close_hfq'].pct_change()
        # 高/低价的后复权：面板里 high/low 是**不复权**的，只有 close 有
        #   `close_hfq`。忘了乘因子的话，跨除权日会算出假的区间位置。
        # 🔴 面板里 open/high/low/preclose 是【不复权】的，只有 close 有
        #   `close_hfq`。忘了乘因子的话，跨除权日会算出假的波幅与假的位置
        #   —— 而它不报错（数还在合理范围内）。`因子实现规格.md` 里
        #   ATR 那几条写的就是 `high/low/preclose × hfq_factor`。
        if 'high' in d.columns and 'hfq_factor' in d.columns:
            f = d['hfq_factor']
            d['_high_hfq'] = d['high'] * f
            d['_low_hfq'] = d['low'] * f
            if 'open' in d.columns:
                d['_open_hfq'] = d['open'] * f
            if 'preclose' in d.columns:
                d['_pre_hfq'] = d['preclose'] * f
            # 典型价 TP 与真实波幅 TR：CCI / MFI / ATR / 资金流都要它们，
            # 各算一遍就是四份实现。
            d['_tp'] = (d['_high_hfq'] + d['_low_hfq'] + d['close_hfq']) / 3.0
            if '_pre_hfq' in d.columns:
                hl = d['_high_hfq'] - d['_low_hfq']
                d['_tr'] = np.maximum(hl, np.maximum(
                    (d['_high_hfq'] - d['_pre_hfq']).abs(),
                    (d['_low_hfq'] - d['_pre_hfq']).abs()))

    # ---- 原始列 ----
    def col(self, name):
        return self.df[name]

    @property
    def C(self):
        return self.df['close_hfq']

    def _memo(self, op, col, n, fn):
        k = (op, col, n)
        if k not in self._c:
            self._c[k] = fn()
        return self._c[k]

    def _by(self, s):
        return s.groupby(self.df['jq_code'], sort=False)

    # ---- 窗口算子 ----
    def ma(self, col, n):
        return self._memo('ma', col, n, lambda: self._flat(self._roll(col, n).mean()))

    def std_pop(self, col, n):
        """🔴 **总体**标准差（除 N），与 `assay/indicators.py` 的 BOLL 同口径。
        用样本标准差（除 N−1）算出来的通道会系统性偏宽，而它不报错。"""
        return self._memo('stdp', col, n, lambda: self._flat(self._roll(col, n).std(ddof=0)))

    def std_samp(self, col, n):
        return self._memo('stds', col, n, lambda: self._flat(self._roll(col, n).std(ddof=1)))

    def var_samp(self, col, n):
        return self._memo('vars', col, n, lambda: self._flat(self._roll(col, n).var(ddof=1)))

    def skew(self, col, n):
        return self._memo('skew', col, n, lambda: self._flat(self._roll(col, n).skew()))

    def kurt(self, col, n):
        return self._memo('kurt', col, n, lambda: self._flat(self._roll(col, n).kurt()))

    def rmax(self, col, n):
        return self._memo('rmax', col, n, lambda: self._flat(self._roll(col, n).max()))

    def rmin(self, col, n):
        return self._memo('rmin', col, n, lambda: self._flat(self._roll(col, n).min()))

    def _roll(self, col, n):
        # 🔴 只收**列名**。收 Series 的话缓存键不可哈希，而更隐蔽的是
        #   "同一个中间量被算好几遍"——那不报错，只是慢。
        assert isinstance(col, str), '窗口算子只收列名，收到 %r' % type(col)
        return self._by(self.df[col]).rolling(n, min_periods=n)

    def _flat(self, s):
        """把 `groupby().rolling()` 聚合出来的 (组, 原索引) 双层索引拍平。

        🔴 **`reindex` 不能省。** 拍平之后行序取决于组的出现顺序，
          恰好与 `self.df` 一致是因为它按 jq_code 排过序 —— 而"恰好一致"
          不是判据：哪天换个分组方式，因子值就会**整体错位到别的票上**，
          而它不报错（数还是那些数，只是贴错了行）。
        """
        return s.reset_index(level=0, drop=True).reindex(self.df.index)

    def rsum(self, col, n):
        return self._memo('rsum', col, n, lambda: self._flat(self._roll(col, n).sum()))

    def shift(self, col, k):
        """滞后 k 个交易日。🔴 必须**分组** —— 不分组的话每只票开头 k 行会
        拿上一只票的尾巴当自己的历史，而它不报错。"""
        return self._memo('shift', col, k,
                          lambda: self._by(self.df[col]).shift(k))

    def cumsum(self, col):
        """从上市起累计（PVT / OBV 这类）。分组同上。"""
        return self._memo('cumsum', col, 0,
                          lambda: self._by(self.df[col]).cumsum())

    def _win(self, col, n, fn):
        """按组做滑动窗口，**向量化**求值。

        🔴 `rolling().apply(..., raw=False)` 会给每个窗口建一个 Series ——
          实测 4 个 CCI 在 166 万行上花了 **209 秒，占全部因子耗时的 92%%**。
          换成 `sliding_window_view` 之后同样的活是秒级。
        ★ **按组切片**做，不是对整列做：整列的窗口矩阵是 (m, n)，
          m=460 万、n=88 时就是 3 GB —— 会把内存吃光（同「16.3M × 200
          float64 = 26.1 GB > 24 GB」那次）。逐组做的话每块只有几百行。
        ★ 窗口里有 NaN 一律给 NaN，与 `rolling(min_periods=n)` 同语义 ——
          不统一的话同一个因子两条路径给出不同的头部，而它不报错。
        """
        arr = self.df[col].to_numpy(dtype='float64')
        code = self.df['jq_code'].to_numpy()
        out = np.full(len(arr), np.nan)
        # 组边界：df 已按 jq_code 排过序（Ctx.__init__ 的前提）
        starts = np.flatnonzero(np.r_[True, code[1:] != code[:-1]])
        for a, b in zip(starts, np.r_[starts[1:], len(arr)]):
            seg = arr[a:b]
            if len(seg) < n:
                continue
            w = np.lib.stride_tricks.sliding_window_view(seg, n)
            v = np.asarray(fn(w), dtype='float64')
            v[np.isnan(w).any(axis=1)] = np.nan
            out[a + n - 1:b] = v
        return pd.Series(out, index=self.df.index)

    def mad(self, col, n):
        """平均【绝对】偏差：mean(|x − MA(x,n)|)，窗口内对**当期均值**取。

        🔴 不是标准差 —— CCI 的分母就是它，换成标准差数值会整体偏小约 20%，
          而那是个"看着完全正常"的错（同 assay/indicators.py 那条）。
        """
        return self._memo('mad', col, n, lambda: self._win(
            col, n, lambda w: np.abs(w - w.mean(axis=1, keepdims=True)).mean(axis=1)))

    def wilder(self, col, n):
        """Wilder 平滑：首值取前 n 个的均值，之后 (prev×(n−1) + x)/n。

        ★ 它等价于 `ewm(alpha=1/n)` + SMA 起步 —— 与 EMA(span=n) 的
          alpha=2/(n+1) **不是一回事**。ATR / RSI 用的是这个；
          混用的话数值能差 10% 以上，而两个都像正常数字
          （抄 assay/indicators.py 的 `_wilder`）。
        """
        return self._memo('wilder', col, n,
                          lambda: self._ema_impl(col, n, alpha=1.0 / n))

    def since_max(self, col, n):
        """窗口内【最高值出现在几天前】（0 = 就是今天）。Aroon 用它。"""
        return self._memo('smax', col, n, lambda: self._win(
            col, n, lambda w: (n - 1 - np.argmax(w, axis=1)).astype('float64')))

    def since_min(self, col, n):
        return self._memo('smin', col, n, lambda: self._win(
            col, n, lambda w: (n - 1 - np.argmin(w, axis=1)).astype('float64')))

    def ema(self, col, n):
        """EMA，**首值用 SMA 起步**（不是直接拿首值）。

        🔴 口径抄 `assay/indicators.py._sma_seed_ema` —— 那是本项目已经定了的
          （"少一点起步偏差"）。换成"首值起步"会让前几十根系统性偏离，
          **而它不报错**，只是数悄悄变了。
        ★ 做法：把前 n−1 个置 NaN、第 n−1 个换成前 n 个的均值，再让
          `ewm(adjust=False)` 从第一个非 NaN 起步 —— 与逐点递推等价（有对数）。
        """
        return self._memo('ema', col, n, lambda: self._ema_impl(col, n))

    def _ema_impl(self, col, n, alpha=None):
        # ★ 这一个**允许收 Series**：MACD 的 DEA 是对 DIF 再做一次 EMA，
        #   而 DIF 是中间量、不是面板列。缓存由调用方用列名做键。
        # 🔴 种子要放在**第一个算得出 SMA 的位置**，不能按行序放第 n−1 行 ——
        #   DIF 前面有二十几行 NaN（EMA(26) 还没起步），按行序放的话种子落在
        #   NaN 区里，整条 DEA 跟着错位。**而它不报错**，只是 MACD 的数
        #   悄悄偏掉；是与 `assay/indicators.py` 逐值对数才抓出来的。
        s = self.df[col] if isinstance(col, str) else col
        g = self.df['jq_code']
        seed = self._flat(s.groupby(g, sort=False)
                           .rolling(n, min_periods=n).mean())
        sv = seed.notna()
        c = sv.astype('int64').groupby(g, sort=False).cumsum()
        first, after = sv & (c == 1), c > 1
        x = pd.Series(np.nan, index=s.index, dtype='float64')
        x[first] = seed[first]
        x[after] = s[after]
        return self._flat(x.groupby(g, sort=False).apply(
            lambda t: (t.ewm(alpha=alpha, adjust=False, ignore_na=False).mean()
                       if alpha else
                       t.ewm(span=n, adjust=False, ignore_na=False).mean())))

    def slope(self, col, n):
        """窗口内对 (序号 t, 值) 做 OLS 的**斜率**。

        ★ t 取 0..n−1 是常数，所以 `slope = Σ(t−t̄)(y−ȳ) / Σ(t−t̄)²`
          的分母是常数、分子可以用 `Σ t·y − t̄·Σy` 算 —— 不必逐窗口回归。
        """
        def _f():
            t = np.arange(n, dtype='float64')
            tbar = t.mean()
            den = ((t - tbar) ** 2).sum()
            return self._win(col, n, lambda w: (w @ t - tbar * w.sum(axis=1)) / den)
        return self._memo('slope', col, n, _f)


# ============================== 注册表 ==============================
FACTORS = []


def register(specs):
    seen = {s.id for s in FACTORS}
    for s in specs:
        # 🔴 编号撞车要**响亮失败** —— 静默覆盖的话后注册的那个会顶掉前一个，
        #   而目录表照样列得整整齐齐（同「保护分支不该静默跳过」）。
        assert s.id not in seen, '因子编号重复: %s' % s.id
        seen.add(s.id)
        FACTORS.append(s)


def _load():
    from . import ma, pos, dist, turn, vol, osc      # noqa: F401  注册副作用
    return FACTORS


def all_specs():
    if not FACTORS:
        _load()
    return list(FACTORS)


def by_id(fid):
    return {s.id: s for s in all_specs()}.get(fid)
