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

## 环境约束（踩过）

聚宽研究环境是 **Python 3.6 + 老 pandas，没装 pyarrow / fastparquet**：
  · 不能 `df.to_parquet(...)` —— 抛 ImportError，本脚本一律存 `.csv.gz`
  · 不用命名聚合（`df.agg(x=('a','sum'))` 那套）
  · f-string 可用，但 walrus / dataclass 之类 3.8+ 语法不行

**跑完请执行文末的清理 cell**，否则 Out[n] 会一直占内存。
"""
import datetime
import json
import os
import tarfile

import pandas as pd
from jqdata import *

# ============================== 改这两处 ====================================
# 事件类表按 pub_date >= SINCE 抽。★ 要比本地最新 pub_date 早几天 ——
# 留重叠是刻意的：合并按自然键去重，重叠不会重复，而缺口会静默丢数据。
# 本地当前最新（2026-09-01 查）：指标/分红 2026-08-31、三大报表 2026-08-24。
SINCE = '2026-08-20'
QUARTERS = ['2026q2', '2026q1']   # 财务类重抽这几个报告期（顺带捡回重述）
# ============================================================================

OUT = '/home/jquser/jq_increment'
CHUNK = 500               # 单次 run_query 的股票数。沿用既有脚本的实测值
PAGE = 3000               # get_fundamentals 分页

os.makedirs(OUT, exist_ok=True)
_saved = []
_stats = {}          # {表名: {rows, date_col, max_date, min_date}}


def _save(name, df):
    """存 csv.gz，【不用 to_parquet】。

    [!] 聚宽研究环境是 Python 3.6 + 老 pandas，【没装 pyarrow / fastparquet】——
        df.to_parquet 会抛 ImportError: Unable to find a usable engine。
        既有的 extract_jq_indicator_q.py / round3.py 早就写着这条，我第一版没照做。
        gzip 后体积与 parquet 同量级，本地 duckdb 的 read_csv_auto 直接能读。
    """
    if df is None or len(df) == 0:
        print('  [空] %s' % name)
        _stats[name] = {'rows': 0}
        return
    p = os.path.join(OUT, name + '.csv.gz')
    df.to_csv(p, index=False, compression='gzip', encoding='utf-8')
    _saved.append(p)
    # 🔴 **记下这张表【实际抽到】的最大日期。**
    #   为什么需要：B 腿（财务）是**事件驱动**的 —— 没公告的日子 pub_date
    #   本来就不前进。于是 `sync_status` 只看数据内容时，昨天刚导完也显示
    #   「距今 11 天」，看着像**没更新**（用户原话：「显得我好像没有更新」）。
    #   ★ 「我什么时候导的」与「数据内容到哪天」是**两件事**，必须分开记。
    #     前者只有抽取端知道（本地无法反推），所以要在包里带出来。
    d = {'rows': int(len(df))}
    for col in ('pub_date', 'board_plan_pub_date', 'change_date', 'end_date'):
        if col in df.columns:
            try:
                s = df[col].dropna().astype(str)
                if len(s):
                    d['date_col'] = col
                    d['max_date'] = str(s.max())[:10]
                    d['min_date'] = str(s.min())[:10]
            except Exception:                      # noqa: BLE001
                pass
            break
    _stats[name] = d
    print('  [OK] %-26s %7d 行  %.1f MB%s' % (
        name, len(df), os.path.getsize(p) / 1e6,
        ('  最大 %s=%s' % (d['date_col'], d['max_date'])) if 'max_date' in d else ''))


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
# ★ 三大报表【不做行级增量】，而是按报告期年份【整年重抽、整文件替换】。
#
# 理由：全量抽取（extract_jq_financials.py）本来就是按 report_date 年份分片存
#   income_YYYY.csv.gz / balance_YYYY.csv.gz / cashflow_YYYY.csv.gz，
#   而 load_jq_financials.py 是 glob 所有年份文件重建 parquet。
#   所以只要把当年那几个文件换掉、重跑 loader，就是完整刷新 —— 不需要
#   任何合并逻辑，也就没有「增量缺列 / 类型错位 / 去重键选错」这些风险。
#
# 🔴 第一版踩的两个坑，都很静默：
#   1. 用 get_fundamentals(query(income)) 抽 —— 与 finance.STK_* 的 schema
#      不同（缺 company_id / a_code / pub_date 等），合并时报缺列
#   2. 改用 finance.STK_* 后**漏了 `report_type == 0`** —— 那会把母公司报表
#      一起拉进来，行数翻倍、合并/母公司口径混在一张表里。
#      全量抽取里有这个过滤（见 extract_jq_financials.fetch_year），
#      增量必须一模一样，否则两部分数据口径不同而**没有任何报错**。
FIN_YEARS = [2026, 2025]      # 重抽这几个【报告期年份】，顺带捡回重述
FIN_PAGE = 3000


def grab_financials():
    """按报告期年份整年重抽，文件名与全量一致 —— 合并时整文件替换。"""
    jobs = [('income',   finance.STK_INCOME_STATEMENT,   'report_date'),
            ('balance',  finance.STK_BALANCE_SHEET,      'report_date'),
            ('cashflow', finance.STK_CASHFLOW_STATEMENT, 'report_date')]
    for name, tbl, dcol in jobs:
        for year in FIN_YEARS:
            lo, hi = '%d-01-01' % year, '%d-12-31' % year
            frames, last_id, n_page = [], -1, 0
            try:
                while True:
                    d = getattr(tbl, dcol)
                    df = finance.run_query(query(tbl).filter(
                        d >= lo, d <= hi, tbl.id > last_id,
                        tbl.report_type == 0        # ★ 只要合并报表，与全量一致
                    ).order_by(tbl.id).limit(FIN_PAGE))
                    if len(df) == 0:
                        break
                    frames.append(df)
                    last_id = int(df['id'].max())
                    n_page += 1
                    if len(df) < FIN_PAGE:
                        break
                    if n_page > 200:
                        raise RuntimeError('%s %d 分页超 200 页' % (name, year))
            except Exception as e:                              # noqa: BLE001
                print('  [!] %s_%d 抽取失败: %s' % (name, year, str(e)[:80]))
                continue
            out = pd.concat(frames, ignore_index=True) if frames else None
            _save('%s_%d' % (name, year), out)


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


# ------------------------------------------------------- 3.5 未来交易日历
def grab_calendar():
    """未来交易日 —— 实盘模块（assay/live.py）判断"下一个交易日是哪天"要用。

    ★ 为什么必须从聚宽抽而不是本地推：本地 `PanelFeed.trading_days` 来自
      **面板**（有行情的日子），永远不含未来；而春节/国庆的休市安排
      **无法从星期推出**。实盘模块拿不到就直接报错，不猜 ——
      猜错一天 = 该调仓的日子不提示，或者休市日发一堆单。

    get_all_trade_days() 返回当年全部交易日（含未来），是聚宽的权威日历。
    """
    days = [str(d)[:10] for d in get_all_trade_days()]
    today = str(datetime.date.today())
    fut = [d for d in days if d >= today]
    obj = {'days': days, 'source': 'jq.get_all_trade_days',
           'updated': today, 'n_future': len(fut),
           'max': days[-1] if days else None}
    p = os.path.join(OUT, 'trade_calendar.json')
    with open(p, 'w') as f:
        json.dump(obj, f)
    _saved.append(p)
    print('  trade_calendar.json  共 %d 天，未来 %d 天，最远 %s'
          % (len(days), len(fut), obj['max']))


def _manifest():
    """把「这一次抽取」的元信息写进包里 —— 本地无法反推，只有抽取端知道。

    🔴 **为什么必须有它**：B 腿（财务）是**事件驱动**的，没公告的日子
      `pub_date` 本来就不前进。于是 `build/sync_status.py` 只看数据内容时，
      昨天刚导完也显示「距今 11 天」—— 看着像没更新。
      「**我什么时候导的**」与「**数据内容到哪天**」是两件事：
        · 后者在本地查得到（std/*.parquet 的 max(pub_date)）
        · 前者**只有抽取端知道** —— 所以要在包里带出来
    ★ `extracted_at` 用**聚宽服务器**的时间（研究环境跑这个脚本时的 now），
      那就是"这份数据是什么时候从 JQ 拿的"。本地打包/解包时间不算 ——
      包可能放几天才导。
    ★ 同时记 `SINCE` 与每张表的 `max_date`：这样本地能判「这次抽取的窗口
      是否覆盖了本地缺口」，而不是只能看行数。
    """
    obj = {
        'extracted_at': datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'extract_date': str(datetime.date.today()),
        'since': SINCE,
        'quarters': QUARTERS,
        'tables': _stats,
        # ★ 全局最大 pub_date —— 这就是"聚宽那边的数据切到哪天"
        'data_max_date': max(
            [v['max_date'] for v in _stats.values() if v.get('max_date')]
            or ['']) or None,
    }
    p = os.path.join(OUT, '_manifest.json')
    with open(p, 'w') as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)
    _saved.append(p)
    print()
    print('  _manifest.json  抽取于 %s   数据最新 pub_date %s   SINCE=%s'
          % (obj['extracted_at'], obj['data_max_date'], SINCE))
    return obj


# ---------------------------------------------------------------- 4 打包
def pack():
    mf = _manifest()
    ts = str(datetime.date.today()).replace('-', '')
    tar = os.path.join(OUT, 'jq_increment_%s.tar' % ts)
    with tarfile.open(tar, 'w') as t:
        for p in _saved:
            t.add(p, arcname=os.path.basename(p))
    print()
    print('=' * 70)
    print('打包完成: %s  (%.1f MB)' % (tar, os.path.getsize(tar) / 1e6))
    print('  抽取时刻 %s（聚宽服务器时间）' % mf['extracted_at'])
    print('  数据最新 pub_date %s' % mf['data_max_date'])
    print('下载它，然后在本地跑：')
    print('    python3 datalake/build/merge_jq_increment.py <tar路径>')
    print('=' * 70)


print('=' * 70)
print('聚宽增量补数   SINCE=%s   报告期=%s' % (SINCE, QUARTERS))
print('=' * 70)
print('[1/4] 指标 indicator')
grab_indicator()
print('[2/4] 三大报表')
grab_financials()
print('[3/4] 事件类（分红/名称/状态/预告/股本）')
grab_events()
print('[4/4] 未来交易日历')
grab_calendar()
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
