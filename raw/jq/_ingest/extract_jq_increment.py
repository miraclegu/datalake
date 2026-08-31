"""聚宽 → 增量补数（在研究环境跑）。把整个文件粘进一个 cell。

## 为什么要这个脚本

`tdx.db` 补行情和聚宽补财务是【两条独立链路】。补了行情不代表财务也新——
2026-09-01 实测踩过：行情到 08-31，而聚宽财务抽取停在 08-26，
2026 中报只覆盖 55.6%（2892/5206），导致 froec 的 `eps > 0` 过滤
在本地用 Q1（庄园牧场 -0.1611 被剔除）、在聚宽用中报（保留）——
同一基准日选股只有 6/10 重合，看着像逻辑差异，其实是数据不新。

## 设计

**不做"按日期切增量"**，因为 `get_fundamentals(statDate=)` 一次就返回
全市场该报告期的值 —— 所以财务类的增量 = **重抽最近 N 个报告期**，
既简单又能顺带捡回【财报重述】。事件类（分红/名称/状态/股本）才按 pub_date 切。

产出一个 tar，下载后在本地跑：
    python3 datalake/build/merge_jq_increment.py <下载的 tar>
它会按各表的自然键【合并去重】进 raw/jq/，再提示你跑哪些 loader。
—— 必须合并：load_jq_indicator_q 是 `COPY (SELECT ... FROM raw) TO std`，
   raw 只放增量的话历史会被整段抹掉。

**跑完请执行文末的清理 cell**，否则 Out[n] 会一直占内存。
"""
import datetime
import os
import tarfile

import pandas as pd
from jqdata import *

# ============================== 改这两处 ====================================
SINCE = '2026-08-20'      # 事件类表按 pub_date >= SINCE 抽。留几天重叠，合并会去重
QUARTERS = ['2026q2', '2026q1']   # 财务类重抽这几个报告期（顺带捡回重述）
# ============================================================================

OUT = '/home/jquser/jq_increment'
CHUNK = 500               # 单次 run_query 的股票数。沿用既有脚本的实测值
PAGE = 3000               # get_fundamentals 分页

os.makedirs(OUT, exist_ok=True)
_saved = []


def _save(name, df):
    if df is None or len(df) == 0:
        print('  [空] %s' % name)
        return
    p = os.path.join(OUT, name + '.parquet')
    df.to_parquet(p, index=False)
    _saved.append(p)
    print('  [OK] %-26s %7d 行  %.1f MB' % (name, len(df), os.path.getsize(p) / 1e6))


def _chunks(xs, n):
    for i in range(0, len(xs), n):
        yield xs[i:i + n]


# ---------------------------------------------------------------- 1 指标
def grab_indicator():
    """get_fundamentals(query(indicator), statDate=) —— 与策略同源同口径。

    [!] 按 statDate 抽，不按 date 抽：一次拿全市场该报告期的值。
        分页用 limit/offset，实测 offset 可用（既有脚本已验证）。
    """
    acc = []
    for q in QUARTERS:
        off, got = 0, 0
        while True:
            df = get_fundamentals(query(indicator).limit(PAGE).offset(off), statDate=q)
            if df is None or len(df) == 0:
                break
            acc.append(df)
            got += len(df)
            off += PAGE
            if len(df) < PAGE:
                break
        print('    %s -> %d 行' % (q, got))
    _save('fundamentals_indicator_q', pd.concat(acc, ignore_index=True) if acc else None)


# ---------------------------------------------------------------- 2 三大报表
def grab_financials():
    """三大报表也按 statDate 重抽最近两期。"""
    for name, tbl in (('income', income), ('balance', balance),
                      ('cash_flow', cash_flow)):
        acc = []
        for q in QUARTERS:
            off = 0
            while True:
                df = get_fundamentals(query(tbl).limit(PAGE).offset(off), statDate=q)
                if df is None or len(df) == 0:
                    break
                acc.append(df)
                off += PAGE
                if len(df) < PAGE:
                    break
        _save('fin_%s' % name, pd.concat(acc, ignore_index=True) if acc else None)


# ---------------------------------------------------------------- 3 事件类
def grab_events():
    """按 pub_date >= SINCE 抽。这几张是 finance.run_query，要自己分批。"""
    codes = list(get_all_securities('stock').index)
    jobs = [
        ('stk_xr_xd',        finance.STK_XR_XD,        'board_plan_pub_date'),
        ('dim_name_history', finance.STK_NAME_HISTORY, 'pub_date'),
        ('dim_status_change', finance.STK_STATUS_CHANGE, 'pub_date'),
        ('stk_fin_forcast',  finance.STK_FIN_FORCAST,  'pub_date'),
        ('share_change',     finance.STK_CAPITAL_CHANGE, 'pub_date'),
    ]
    for name, tbl, datefield in jobs:
        acc = []
        try:
            for part in _chunks(codes, CHUNK):
                df = finance.run_query(query(tbl).filter(
                    tbl.code.in_(part), getattr(tbl, datefield) >= SINCE))
                if df is not None and len(df):
                    acc.append(df)
        except Exception as e:                                  # noqa: BLE001
            print('  [!] %s 抽取失败: %s' % (name, str(e)[:80]))
            continue
        _save(name, pd.concat(acc, ignore_index=True) if acc else None)


# ---------------------------------------------------------------- 4 打包
def pack():
    ts = str(datetime.date.today()).replace('-', '')
    tar = os.path.join(OUT, 'jq_increment_%s.tar' % ts)
    with tarfile.open(tar, 'w') as t:
        for p in _saved:
            t.add(p, arcname=os.path.basename(p))
    print()
    print('=' * 70)
    print('打包完成: %s  (%.1f MB)' % (tar, os.path.getsize(tar) / 1e6))
    print('下载它，然后在本地跑：')
    print('    python3 datalake/build/merge_jq_increment.py <tar路径>')
    print('=' * 70)


print('=' * 70)
print('聚宽增量补数   SINCE=%s   报告期=%s' % (SINCE, QUARTERS))
print('=' * 70)
print('[1/3] 指标 indicator')
grab_indicator()
print('[2/3] 三大报表')
grab_financials()
print('[3/3] 事件类（分红/名称/状态/预告/股本）')
grab_events()
pack()

# ============================== 清理 cell ===================================
# 跑完把下面这段单独执行一次，否则 Out[n] 会一直占内存：
#
#   import gc
#   for _v in ['acc', 'df', '_saved']:
#       if _v in dir():
#           del globals()[_v]
#   gc.collect()
#   %reset -f out
# ============================================================================
