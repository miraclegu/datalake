# -*- coding: utf-8 -*-
"""取一块股票的全历史（面板列 + as-of 过的财务列）—— **唯一正本**。

## 🔴 为什么要单独一个文件

`build_factor_daily`（落盘）与 `factor_try`（试跑）都要这一步，而它们
原本各写了一份。加财务族之后**两份同时坏，坏法还一样**：

    Spec 的 `deps` 里出现了 `b_total_assets` 这类 as-of 贴上来的列，
    而它们不在面板里 —— 直接 SELECT 报 "没有这一列"。

`build_factor_daily` 那份当时顺手修了（与面板列求交 + 接 as-of），
`factor_try` 与 `tools/check_vs_indicators` 那两份**没人想起来** ——
直到把守卫接进 selftest 才发现（同「两处实现必然分叉」）。

所以现在只有这一份，三个调用方都用它。
"""
from . import all_specs
from . import fin


def _panel_cols(con, panel):
    return set(d[0] for d in con.execute(
        "SELECT * FROM read_parquet('%s') LIMIT 0" % panel).description)


def select_cols(con, panel):
    """要从**面板**读哪几列：各 Spec 的 `deps` 并集 ∩ 面板实有列。

    ★ 不写死清单 —— 写死的话加一个用到新列的因子会静默读不到
      （更糟的是某些路径下拿到全 NaN）。
    🔴 求交这一步就是上面说的那个坑：财务列由 as-of 自带，不能进这个 SELECT。
    """
    have = _panel_cols(con, panel)
    need = {'jq_code', 'date'}
    for s in all_specs():
        need |= {c for c in s.deps if c in have}
    return sorted(need)


def chunk_df(con, panel, root, codes=None, where=None):
    """一块股票（或给定筛选）的**全历史** + 财务列 -> DataFrame。

    🔴 **总是**贴财务列：实测两年全市场 340 万行 0.1 秒，几乎免费。
      做成"有财务因子才贴"要多一个开关，而开关忘了开的表现是
      那一批因子整列为空 —— 不报错。
    🔴 **全历史**，不许按年截断：EMA 递推永远记着起点、长期停牌股的窗口
      跨过缺口，截断会让值与落盘的那份不同（见 build_factor_daily 的 docstring）。
    """
    w = []
    if codes:
        w.append('jq_code IN (%s)' % ', '.join("'%s'" % c for c in codes))
    if where:
        w.append(where)
    sub = ("SELECT %s FROM read_parquet('%s')%s"
           % (', '.join(select_cols(con, panel)), panel,
              (' WHERE ' + ' AND '.join(w)) if w else ''))
    return con.execute(fin.asof_sql(root, sub)).df()
