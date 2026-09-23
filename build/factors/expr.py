# -*- coding: utf-8 -*-
"""表达式因子：一个字符串就是一个因子。

    from .expr import expr
    Spec('vup20', '20日涨日成交量占比', 'vol',
         'rsum(where(R>0,V,0),20)/rsum(V,20)',      # <- 公式（给人读）
         '…', '小数(0~1)', deps_of(E), warm_of(E),
         expr('rsum(where(R>0,V,0),20)/rsum(V,20)'))  # <- 实现（同一个字符串）

## 🔴 为什么要有这一层：让「试跑」与「落盘」不可能分叉

`factor_try.py` 要能跑任意表达式，族文件里的 Spec 要能落盘 ——
两边各写一个求值器的话，"试的时候是这个数、进面板变成另一个"迟早发生，
**而两边都不报错**。所以求值器只有这一份，两边都 import 它。

★ 副产品：用 `expr()` 写的因子，`formula` 字段与 `calc` **是同一个字符串**
  —— 公式与实现在结构上就分叉不了（同「每档只存你填的那个，另一个永远现算」）。

## ⚠ 受限 `eval`，与 `query.py` 的三层防护【不是一回事】

`query.py` 要防的是"从看板上把 mart/ 写坏"（语句白名单 + 关键字黑名单 +
子进程超时）。这里的输入是**族文件里的字面量**或你自己敲的命令行，
不接任何外部输入 —— 命名空间限制只为了**手滑时报错指得到原因**
（写错列名当场告诉你有哪些列），不是安全边界。别把两者的分工记反。
"""
import ast

import numpy as np

#: 短名 = `因子实现规格.md` 里的记号 —— 这样"试出来的公式"与文档里写的
#: 是同一套写法，粘进 Spec 的 `formula` 不用翻译。
ALIAS = {
    'C': 'close_hfq', 'O': '_open_hfq', 'H': '_high_hfq', 'L': '_low_hfq',
    'PC': '_pre_hfq', 'Cb': 'close_bfq', 'V': 'volume_shares', 'A': 'amount',
    'TO': 'turnover', 'R': '_ret', 'TP': '_tp', 'TR': '_tr', 'F': 'hfq_factor',
}

#: 派生列 -> 它真正依赖的**面板原始列**。
#: 🔴 `deps` 要给原始列不是派生列 —— 建面板时按 `deps` 决定读哪几列，
#:   给派生名的话那几列根本读不进来，而 `Ctx` 建派生列时静默少一列。
SRC = {
    '_ret': ('close_hfq',),
    '_high_hfq': ('high', 'hfq_factor'),
    '_low_hfq': ('low', 'hfq_factor'),
    '_open_hfq': ('open', 'hfq_factor'),
    '_pre_hfq': ('preclose', 'hfq_factor'),
    '_tp': ('high', 'low', 'close_hfq', 'hfq_factor'),
    '_tr': ('high', 'low', 'preclose', 'hfq_factor'),
}

#: 窗口算子 -> Ctx 上的方法名
OPS = {
    'ma': 'ma', 'ema': 'ema', 'wilder': 'wilder',
    'std': 'std_samp', 'stdp': 'std_pop', 'var': 'var_samp',
    'skew': 'skew', 'kurt': 'kurt', 'rsum': 'rsum',
    'rmax': 'rmax', 'rmin': 'rmin', 'mad': 'mad',
    'shift': 'shift', 'cumsum': 'cumsum', 'slope': 'slope',
    'since_max': 'since_max', 'since_min': 'since_min',
}

#: 逐点函数（不涉及窗口）
PT = {'abs': np.abs, 'log': np.log, 'sqrt': np.sqrt, 'sign': np.sign,
      'exp': np.exp, 'maximum': np.maximum, 'minimum': np.minimum,
      'where': np.where, 'clip': np.clip,
      # 🔴 `z(x)` = 缺失按 0 —— **只用在【求和项】上**。
      #   资产负债表的明细行（应付债券、长期借款…）在公司没有这项业务时
      #   本来就是空的，直接相加会让 NULL 传染掉整个和：实测「净债务」
      #   非空率因此只有 **17.7%**（四个明细行的交集）。
      #   ⚠ 反过来**不许用在分母或比率的主项上** —— 那里的空是"不知道"，
      #     按 0 算会把"没披露"变成一个看着正常的数（同「拿不到分红那一格
      #     标查不到，不猜一个数」）。
      'z': lambda v: np.nan_to_num(np.asarray(v, dtype='float64'), nan=0.0)}


class _Env(object):
    """把 Ctx 的算子包一层，使它们**也能收中间 Series**。

    🔴 `Ctx` 的算子只收列名 —— 那是为了缓存键可哈希（收 Series 的话
      `_memo` 直接 TypeError，更隐蔽的是同一个中间量被算好几遍）。
      而表达式里写 `ema(ma(C,5), 9)` 是很自然的事，所以这里把中间 Series
      **物化成一个临时列**再传进去，并按对象缓存不重复建列。
    """

    def __init__(self, ctx):
        self.x = ctx
        self._n = 0
        self._seen = {}

    def _name(self, v):
        if isinstance(v, str):
            return ALIAS.get(v, v)
        k = id(v)
        if k not in self._seen:
            self._n += 1
            c = '_expr%d' % self._n
            self.x.df[c] = np.asarray(v, dtype='float64')
            self._seen[k] = c
        return self._seen[k]

    def ns(self):
        g = {'__builtins__': {}}
        # 🔴 **先放列、后放算子** —— 反过来的话，哪天有个列叫 `ma`
        #   就会把算子顶掉，而表达式照样"跑得通"、只是算的是那一列。
        for c in self.x.df.columns:
            if not c.startswith('_expr'):
                g[c] = self.x.col(c)
        for short, real in ALIAS.items():
            g[short] = self.x.col(real) if real in self.x.df.columns else None
        for name, meth in OPS.items():
            g[name] = self._wrap(getattr(self.x, meth))
        g.update(PT)
        g['np'] = np
        return g

    @staticmethod
    def _wrap(f):
        def call(col, *a):
            return f(_ENV_CUR[-1]._name(col), *a)
        return call


_ENV_CUR = []


def evaluate(ctx, e):
    """在 `ctx` 上求值一个表达式 -> Series（与 ctx.df 同索引）。"""
    env = _Env(ctx)
    _ENV_CUR.append(env)
    try:
        v = eval(e, env.ns())                               # noqa: S307  见 docstring
    except NameError as ex:
        raise NameError('%s —— 可用的列: %s；算子: %s'
                        % (ex, ' '.join(sorted(ALIAS)), ' '.join(sorted(OPS))))
    finally:
        _ENV_CUR.pop()
    if np.isscalar(v):
        raise ValueError('表达式算出来是个标量 %r —— 它不随票/日期变，'
                         '多半是列名写错了' % v)
    return v


def expr(e):
    """表达式 -> Spec 的 `calc`。★ 与 `factor_try` 走**同一个**求值器。"""
    def calc(x):
        return evaluate(x, e)
    calc.__doc__ = e
    calc.expr = e
    return calc


def deps_of(e):
    """从表达式里推出它依赖哪几个**面板原始列**。

    ★ 自动推比手写可靠：手写漏一列的表现是建面板时那一列没读进来，
      而 `Ctx` 建派生列时**静默跳过**（`if 'high' in d.columns`），
      于是那个因子整列是空的 —— 不报错。
    """
    out = set()
    for n in ast.walk(ast.parse(e, mode='eval')):
        if not isinstance(n, ast.Name) or n.id in OPS or n.id in PT:
            continue
        if n.id in ALIAS:
            out |= set(SRC.get(ALIAS[n.id], (ALIAS[n.id],)))
        elif n.id.startswith(('b_', 't_', 'c_')) or n.id in ('totalmv', 'floatmv'):
            # 财务列（as-of 贴上来的）与市值列按原名走。
            # ★ 建面板时 `_cols()` 只把**面板真有的**列拿去 SELECT，
            #   财务列由 as-of 自带 —— 混在一起 SELECT 会直接报"没有这一列"。
            out.add(n.id)
    return tuple(sorted(out))


def warm_of(e, floor=1):
    """从表达式里取**最大的窗口参数**当预热的建议值。

    ⚠ 只是**建议**：递推类（ema / wilder）真正要的预热远大于 n
      —— 本族的约定是 `n * 4`，而 `Spec.warm_eff` 还有 120 根的下限。
      所以这里给出的数要**自己再想一遍**，别直接照抄
      （同「段落取自源码里的标记，是排版不是语义保证」）。
    """
    w = floor
    t = ast.parse(e, mode='eval')
    for n in ast.walk(t):
        if (isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                and n.func.id in OPS):
            rec = n.func.id in ('ema', 'wilder')
            for a in n.args[1:]:
                if isinstance(a, ast.Constant) and isinstance(a.value, int):
                    w = max(w, a.value * (4 if rec else 1))
    return w
