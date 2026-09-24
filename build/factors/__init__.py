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

#: 🔴🔴 **单位表就是「能不能做横截面排序」的判据** —— 每个单位显式声明。
#:
#: 判的不是"有没有量纲"，是**这个量的横截面大小由什么标度决定**：
#:
#:   元(价格)  股价与它的派生量。标度是**股本与拆股史** —— 任意。后复权再乘
#:             一个 `hfq_factor`（实测跨票 1.00~5899.9），于是 `ma20` 的横截面
#:             排序 = 排「股价 × 上市以来分红拆细」（与不复权股价秩相关
#:             +0.536、与复权因子 +0.421 —— 两个混淆项就是它的全部内容）。
#:   股        成交量水平。标度是**股本** —— 任意（大盘股天然成交几亿股）。
#:   元/天     回归斜率。20 元的票涨 1% 是 0.2 元/天，2000 元的票是 20 元/天。
#:   ──────────────────────────────────────────────────────────────
#:   元(金额)  市值 / 成交金额 / 资金流 / 财务绝对额。**元本身就是全市场的
#:             共同标度**，"10 亿 vs 1 亿"是真实差别 —— 可比。
#:
#: 🔴 **2026-09-24 修：原来是 `('元','股','元/天')` 一刀切，误判了 35 个。**
#:   被误判的正是【规模 / 流动性 / 财务绝对额】—— 其中 `mv` / `mv_float` /
#:   `a_ma6` / `a_std6` / `mf_sum20`，而本仓库 froec 整个策略就是**按流通市值
#:   升序取 10**、ETF-P1 的池子就是**成交额前 40**：广场上默认看不见它们，
#:   等于把自己最核心的那两个因子藏了起来。
#:   ★ **自相矛盾的铁证（不用测数据就成立）**：`ln_mv = log(totalmv)` 判为
#:     可比、`mv = totalmv` 判为不可比 —— 而 log 是**严格单调**，广场算的是
#:     **秩相关**，两者是**同一个排序**。实测 13 项里 **6 项逐位相同**
#:     （上涨占比 / 高分位超额 / 两端换手 / 天数 / 调仓次数），其余 7 项差在
#:     **1e-9 ~ 3e-6** —— 不是"完全相同"，而是 float64 把两个极近的
#:     `totalmv` 的 log 压成相等、在并列处翻了几下。
#:     ⚠ 这个数我第一次是照 6 位小数的显示读的，说成"七项逐位相同"——
#:       **没量过的数不许拿出来当对照**（同那条纪律）。逐位量过才是上面这些。
#:     ★ 结论不受影响：它们是同一个量的**单调变换**，判成两档就是自相矛盾。
#:   ★ 那条老发现**本身没错**，错在按单位外推：`元` 同时装着"价格水平"与
#:     "经济规模"两种东西。而「IC 符号与 factors.xlsx 对不上的 19 个全部落在
#:     这几种单位里」证明的是这条判据**召回率高**，不是**精确率高**
#:     （同「判据比要证的事宽」那条）。
#: 🔴 **未登记的单位直接拒绝注册** —— 新因子写错单位会**响亮失败**，
#:   而不是静默归进"可比"那一档（同「保护分支不该静默跳过」）。
UNITS = {
    # 标度是个股自有的（股本 / 拆股史 / 股价）-> 横截面排序排的是量纲
    '元(价格)': False,
    '股': False,
    '元/天': False,
    # 元是全市场的共同标度；比率 / 无量纲天然可比
    '元(金额)': True,
    '对数元': True,
    '无量纲': True,
    '倍': True,
    '百分数': True,
    '小数': True,
    '小数²': True,
    '0~100': True,
    '小数(0~1)': True,
    '小数(0.01=1%)': True,
    '小数(0.05=5%)': True,
}

#: 仍然导出这个名字（外部有引用），但它现在是**从单位表推出来的**，
#: 不再是手写的一串 —— 两处各写一份迟早分叉。
ABS_UNITS = tuple(u for u, ok in UNITS.items() if not ok)

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
    'lev':  '偿债 / 杠杆 / 结构',
    'val':  '市值 / 估值',
    'ttm':  'TTM 金额',
    'cfq':  '现金流质量',
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
        assert unit in UNITS, (
            '未登记的单位 %r（因子 %s）—— 单位表就是「横截面能不能排序」\n'
            '的判据，新单位必须去 UNITS 里显式声明可不可比。\n'
            '已登记：%s' % (unit, id, ', '.join(sorted(UNITS))))
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
    def xs_comparable(self):
        """这个因子的值**能不能跨股票直接比大小**（= 能不能做横截面排序）。

        判据就是单位 —— 见 `UNITS` 那张表（每个单位显式声明可不可比）。
        做成属性而不是在两处各判一次：目录表与评价层都读它，分叉的话会
        出现「目录说可比、评价说不可比」。
        """
        return UNITS[self.unit]

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
            'xs_comparable': self.xs_comparable,
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
    from . import fin_lev, fin_ttm                  # noqa: F401  财务族
    return FACTORS


def all_specs():
    if not FACTORS:
        _load()
    return list(FACTORS)


def by_id(fid):
    return {s.id: s for s in all_specs()}.get(fid)


# ======================= 算不出来的那些 =======================
"""🔴🔴 「有哪些因子」的另一半：**没有 Spec 的那些，也要有名有姓有原因**。

`factors.xlsx` 有 278 个不同的名字，注册表实现了其中 161 个。剩下 117 个
此前**在广场上根本不存在** —— 而「查不到」与「本来就算不出来」在屏幕上
长得一模一样（同「删了要留痕，否则"空"与"本来就没有"分不出来」那条）。
于是人只能去翻 md，或者以为是页面坏了。

所以这份清单与 `FACTORS` 是**同一份名单的两半**，正本都在这个文件里：

    MISSING  名字 -> 为什么算不出来
    covered_by(names)  两半的并集必须【正好】覆盖原清单，且不许重叠

🔴 **那条覆盖自证才是这份清单不会烂掉的原因。** 手工维护的清单迟早与代码
分叉：实现了一个却忘了从这儿删 -> 广场上同时列在两边；`factors.xlsx` 加了
一行 -> 它静默地哪一边都不在。两种都不报错。所以 `build_factor_catalog.py`
在**写盘之前**跑这条自证，不过就退出码 2 拒绝写出
（同 `load_tdx_gbbq.py` 拿除权公式复算因子跳变那条）。

⚠ **原因分两类，页面上别混**：`no_data` / `calib_failed` / `no_spec` /
`ambiguous` 是**做不了或要先定案**；`todo` 是**可复现、只是还没写**。
混成一句"暂时无法计算"的话，65 个只差有人去写的因子会被读成"这东西算不了"。

★ 逐条的推导在 `datalake/docs/因子清单-可复现性.md`（285 行逐条定案）——
  那份是散文与证据，**这份是正本**：改这里，回那边加一行
  （同「新结论先写进代码注释 / loader docstring，再回到索引加一行」）。
"""

#: 原因 -> (页面上的短标签, 为什么, 是不是【做不了】)
#: 🔴 `blocked=False` 的那档要说清"不是做不了" —— 见上面那条 ⚠。
MISS_REASONS = {
    'no_data': (
        '本地没有这类数据源',
        '需卖方分析师一致预期（consensus）。本地只有 raw/jq/stk_fin_forcast'
        '（上市公司自己发的业绩预告）—— 区间不是点估计、只在触发披露阈值时才有、'
        '更新频率与覆盖率都不同，拿它顶替是【有值但对不上】，比没有值更危险。',
        True),
    'calib_failed': (
        '照字面算【实测对不上】',
        '数据齐、公式也写得出来，但照字面实现的值与聚宽同名因子对不上 —— '
        '已经逐只探针否证过，不是没试过。推导见 datalake/docs/jqfactor-口径.md。',
        True),
    'no_spec': (
        '合成因子，规格未公开',
        'Barra/CNE5 式的合成因子：一个因子 = 若干描述子按权重合成 -> winsorize -> '
        '标准化 -> 对市值与行业正交化。这四件事聚宽都没公开完整规格'
        '（描述子清单 / 权重 / 缩尾分位 / 哪版申万行业）。'
        '🔴 数据侧全有，所以任选一套规格【算得出数】—— 而那个数与聚宽不可比，'
        '且它不报错（同「对数对得上 ≠ 移植忠实」）。',
        True),
    'ambiguous': (
        '名称对不到唯一公式',
        '光看中文名分不出算哪个量。要么拿到聚宽的因子定义文档，'
        '要么照探针实测定案（打印 get_factor_values 的值 + 本地候选公式逐只比对）'
        '—— 不要凭字面猜。',
        True),
    'sparse': (
        '依赖字段覆盖稀疏',
        '依赖的财务字段非空率很低，而【NULL 不等于 0】：当 0 用会把"没披露"'
        '算成"没有这项"，算出来是一批看着正常的错值。口径要先定案（补充口径 / '
        '限定行业 / 改报告期粒度），定案之前不落盘。',
        True),
    'const_undef': (
        '缺一个要先定的常数',
        '公式与数据都有，但结果取决于一个本地还没定下来的常数 —— '
        '先定案并写进注释，再落盘。',
        True),
    'todo': (
        '还没写进注册表',
        '🔴 这一档【不是算不出来】：数据齐、公式确定（属于「因子清单-可复现性」'
        '里的 A 类），只是注册表里还没有这一条 Spec。写一条就会自动出现在广场上。',
        False),
}

#: 名字 -> (原因, 逐条细节)。细节留空 = 原因那句话已经说全了。
#: 🔴 顺序按【原因】分组，不按原清单行号 —— 这份清单是给人读"为什么"的，
#:   而对回原清单靠 src_row（目录表那一列），不靠这里的顺序。
MISSING = (
    # ---- ❌ 本地没有这类数据源（4）—— 不是"还没做"，是做不了 ----
    ('预期短期盈利增长率', 'no_data'),
    ('预期长期盈利增长率', 'no_data'),
    ('盈利预期因子', 'no_data'),
    ('预期市盈率', 'no_data'),

    # ---- ❌ 照字面实现【实测对不上】（3）—— 已否证，不是没试过 ----
    ('5年营业收入增长率', 'calib_failed',
     '聚宽 sales_growth。Barra SGRO 式斜率÷均值 / 5年CAGR / 首末比 / (新−旧)/(新+旧) / 离线网格穷举 300 组 —— 全 0/6'),
    ('5年盈利增长率', 'calib_failed',
     '聚宽 earnings_growth，同上 0/6；与 CAGR5 皮尔逊 +0.92 而秩相关只有 +0.49 —— 同源但被某种非单调变换处理过'),
    ('营业收入增长率', 'calib_failed',
     '聚宽 operating_revenue_growth_rate，TTM 营收同比只中 4/6；operating_revenue 与 total_operating_revenue 都试过'),

    # ---- ⚠️ 合成风格因子：缺的是规格不是数据（31） ----
    ('中等市值因子', 'no_spec'),
    ('价值因子', 'no_spec'),
    ('分红因子', 'no_spec'),
    ('分红收益率因子', 'no_spec'),
    ('动量因子', 'no_spec'),
    ('市值因子', 'no_spec'),
    ('市值立方因子', 'no_spec'),
    ('市值规模因子', 'no_spec'),
    ('市净率因子', 'no_spec'),
    ('市场波动率因子', 'no_spec'),
    ('情绪因子', 'no_spec'),
    ('成长因子', 'no_spec'),
    ('投资能力因子', 'no_spec'),
    ('收益因子', 'no_spec'),
    ('杠杆因子', 'no_spec'),
    ('残余波动率因子', 'no_spec'),
    ('残差历史波动率', 'no_spec'),
    ('残差波动因子', 'no_spec'),
    ('波动率因子', 'no_spec'),
    ('流动性因子', 'no_spec'),
    ('盈利变动率因子', 'no_spec'),
    ('盈利能力因子', 'no_spec'),
    ('盈利质量因子', 'no_spec'),
    ('相对动量因子', 'no_spec'),
    ('相对强弱', 'no_spec'),
    ('规模因子', 'no_spec'),
    ('财务杠杆因子', 'no_spec'),
    ('质量因子', 'no_spec'),
    ('长期反转因子', 'no_spec'),
    ('长期成长因子', 'no_spec'),
    ('非线性市值因子', 'no_spec'),

    # ---- ⚠️ 名称无法唯一映射到公式（8） ----
    ('财务杠杆指数', 'ambiguous',
     '"指数"含义未定'),
    ('营业收入指数', 'ambiguous',
     '"指数"含义未定（同比/环比/相对均值）'),
    ('收益离差', 'ambiguous',
     '名称无法唯一映射到公式（收益标准差 / CMRA / 预期离散度都讲得通）'),
    ('毛利率指数', 'ambiguous',
     '名称无法唯一映射到公式'),
    ('销售管理费用指数', 'ambiguous',
     '名称无法唯一映射到公式'),
    ('应收账款指数', 'ambiguous',
     '名称无法唯一映射到公式'),
    ('盈利能力稳定性', 'ambiguous',
     '窗口与统计量未定（几期 ROE 的 std/变异系数）'),
    ('最大盈利水平', 'ambiguous',
     '名称无法唯一映射到公式'),

    # ---- ⚠️ 依赖字段覆盖稀疏，NULL 不等于 0（3） ----
    ('净利息费用', 'sparse',
     'income.interest_expense 非空率只有 1.18%、interest_income 3.57% —— 那是金融企业专用行（696 家）；非金融要从 financial_expense(98.2%) 推，两套口径先定案'),
    ('息税折旧摊销前利润', 'sparse',
     '折旧摊销只在中报/年报披露（6 月 99.6% / 12 月 99.6%，而 3 月 21.1% / 9 月 25.5%）—— 季频上只能 as-of 到最近一个半年度，不是季度粒度'),
    ('对联营和合营公司投资收益/利润总额', 'sparse',
     'invest_income_associates 非空率 49.38% —— 当 0 用会把「没披露」当成「没有投资收益」'),

    # ---- ⚠️ 公式与数据都有，缺一个要先定下来的常数（3） ----
    ('20日夏普比率', 'const_undef',
     'assay 本地没有无风险利率序列 —— 用 0 还是 0.04/252 算出来不是一个数。公式与数据都有，缺的是这个常数先定下来并写进注释'),
    ('60日夏普比率', 'const_undef',
     'assay 本地没有无风险利率序列 —— 用 0 还是 0.04/252 算出来不是一个数。公式与数据都有，缺的是这个常数先定下来并写进注释'),
    ('120日夏普比率', 'const_undef',
     'assay 本地没有无风险利率序列 —— 用 0 还是 0.04/252 算出来不是一个数。公式与数据都有，缺的是这个常数先定下来并写进注释'),

    # ---- ⏳ 可复现（数据齐 + 公式确定），只是注册表里还没有这一条（65） ----
    ('应收账款周转天数', 'todo'),
    ('扣除非经常损益后的净利润/净利润', 'todo'),
    ('营业周期', 'todo'),
    ('财务费用与营业总收入之比', 'todo'),
    ('存货周转天数', 'todo'),
    ('应付账款周转天数', 'todo'),
    ('管理费用与营业总收入之比', 'todo'),
    ('RAW BETA', 'todo'),
    ('BETA', 'todo'),
    ('每股资本公积金', 'todo'),
    ('股东权益周转率', 'todo'),
    ('总资产增长率', 'todo'),
    ('营业费用与营业总收入之比', 'todo'),
    ('销售成本率', 'todo'),
    ('净资产增长率', 'todo'),
    ('毛利率增长', 'todo'),
    ('销售毛利率', 'todo'),
    ('营业外收支利润净额/利润总额', 'todo'),
    ('每股现金流量净额，根据当时日期来获取最近变更日的总股本', 'todo'),
    ('经营资产周转率TTM', 'todo'),
    ('经营活动净收益/利润总额', 'todo'),
    ('每股净资产', 'todo'),
    ('净利润增长率', 'todo'),
    ('归属母公司股东的净利润增长率', 'todo'),
    ('资产回报率TTM', 'todo'),
    ('利润总额增长率', 'todo'),
    ('净利润与营业总收入之比', 'todo'),
    ('销售净利率', 'todo'),
    ('营业利润增长率', 'todo'),
    ('长期资产回报率TTM', 'todo'),
    ('总资产周转率', 'todo'),
    ('总资产报酬率', 'todo'),
    ('销售利润率TTM', 'todo'),
    ('成本费用利润率', 'todo'),
    ('营业利润与营业总收入之比', 'todo'),
    ('营业利润率', 'todo'),
    ('销售税金率', 'todo'),
    ('权益回报率TTM', 'todo'),
    ('投资资本回报率TTM', 'todo'),
    ('经营资产回报率TTM', 'todo'),
    ('固定资产周转率', 'todo'),
    ('存货周转率', 'todo'),
    ('每股营业利润', 'todo'),
    ('每股收益TTM', 'todo'),
    ('每股营业利润TTM', 'todo'),
    ('经营活动净收益', 'todo'),
    ('息税前利润', 'todo'),
    ('每股盈余公积金', 'todo'),
    ('长期权益回报率TTM', 'todo'),
    ('流动资产周转率TTM', 'todo'),
    ('每股营业总收入', 'todo'),
    ('每股营业收入', 'todo'),
    ('每股未分配利润', 'todo'),
    ('每股现金及现金等价物余额', 'todo'),
    ('每股留存收益', 'todo'),
    ('每股营业总收入TTM', 'todo'),
    ('每股营业收入TTM', 'todo'),
    ('非经常性损益', 'todo'),
    ('筹资活动产生的现金流量净额增长率', 'todo'),
    ('经营活动产生的现金流量净额增长率', 'todo'),
    ('应付账款周转率', 'todo'),
    ('应收账款周转率', 'todo'),
    ('长期毛利率增长', 'todo'),
    ('每股经营活动产生的现金流量净额', 'todo'),
    ('1减去 过去一个月收益率排名与股票总数的比值', 'todo'),
)


def _miss_rows():
    rows = []
    for t in MISSING:
        name, why = t[0], t[1]
        detail = t[2] if len(t) > 2 else ''
        assert why in MISS_REASONS, '未知原因: %s（%s）' % (why, name)
        # 🔴 同 Spec 那几个字段：这些字会进目录表、接口、页面的 title 属性，
        #   而属性里连 <b> 都用不了，markdown 星号会原样显示成一串星号。
        for _f, _v in (('name_cn', name), ('detail', detail)):
            assert '**' not in (_v or ''), (
                '%s 的 %s 里有 markdown 星号（页面上会原样显示），用【】：%r'
                % (name, _f, _v))
        label, why_cn, blocked = MISS_REASONS[why]
        rows.append({'name_cn': name, 'reason_key': why, 'reason_cn': label,
                     'reason_why': why_cn, 'detail': detail, 'blocked': blocked})
    return rows


def all_missing():
    """算不出来的那些，每条带原因。与 `all_specs()` 是同一份名单的两半。"""
    seen = set()
    for r in _miss_rows():
        # 名字重复要响亮失败：页面按名字列，重了就是同一条列两遍
        assert r['name_cn'] not in seen, '清单里重复: %s' % r['name_cn']
        seen.add(r['name_cn'])
    return _miss_rows()


def all_reasons():
    """全量原因表（含【一条因子都没有】的那些档）。

    🔴 页面上"手工加一条"的原因下拉照它渲染 —— 照"清单里出现过的 key"
      去拼的话，某一档被实现光了它就**静默从下拉里消失**，
      而那不报错（同「存的是隐藏哪些、不是显示哪些」那条）。
    """
    return [{'key': k, 'label': lab, 'why': why, 'blocked': bool(blk)}
            for k, (lab, why, blk) in MISS_REASONS.items()]


def covered_by(names):
    """🔴 覆盖自证：`names`（原清单的全部名字）必须正好被两半分完。

    返回 `(ok, 说明)`。三种烂法各报各的，**不许合并成一句"对不上"** ——
    报错要指得到原因（同「报错必须指向真正的原因」）：

        both    实现了却还留在 MISSING 里 -> 广场上同时列在两边
        neither 原清单加了一行，而它哪一半都不在 -> 静默消失
        extra   MISSING 里有原清单没有的名字 -> 多半是名字抄错了一个字
    """
    impl = {s.name_cn for s in all_specs()}
    miss = {r['name_cn'] for r in all_missing()}
    names = list(names)
    ns = set(names)
    both = sorted(impl & miss)
    neither = [n for n in names if n not in impl and n not in miss]
    extra = sorted(miss - ns)
    if both or neither or extra:
        part = []
        if both:
            part.append('%d 个既实现了又列在 MISSING 里（实现之后要从 MISSING '
                        '删掉）：%s' % (len(both), '、'.join(both[:6])))
        if neither:
            part.append('%d 个哪一半都不在（原清单加了行？给它一条 Spec 或一条 '
                        'MISSING）：%s' % (len(neither), '、'.join(neither[:6])))
        if extra:
            part.append('%d 个 MISSING 里有而原清单里没有（名字抄错了？）：%s'
                        % (len(extra), '、'.join(extra[:6])))
        return False, '；'.join(part)
    return True, '实现 %d + 算不出来 %d = 原清单 %d 个名字，不重不漏' % (
        len(impl), len(miss), len(ns))
