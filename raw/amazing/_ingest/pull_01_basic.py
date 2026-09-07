#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""① 基础数据（手册 3.5.2）—— 交易日历 / 代码表 / 证券基础信息 / 历史证券状态 / 复权因子。

    python pull_01_basic.py                 # 全拉，断点续跑
    python pull_01_basic.py --only stock_basic
    python pull_01_basic.py --dry-run       # 只看有多少片

| 表 | 接口 | 是什么 |
|---|---|---|
| `trading_calendar` | `get_calendar` | 各市场交易日（1990 起，**含未来已公布的**） |
| `hist_code_list` | `get_hist_code_list` | 区间内**存在过**的代码，含已退市 |
| `code_info` | `get_code_info` | 每日最新证券信息（**只有今天，没有历史**） |
| `stock_basic` | `get_stock_basic` | 中英文名 / 上市日 / 退市日 / 板块 / 上市状态 |
| `history_stock_status` | `get_history_stock_status` | **日频**涨跌停价 / ST / 停牌 / 除权除息 |
| `backward_factor` | `get_backward_factor` | 后复权因子（宽表 → 落盘前 melt） |
| `adj_factor` | `get_adj_factor` | 单次复权因子（同上） |
| `bj_code_mapping` | `get_bj_code_mapping` | 北交所新旧代码对照 |

## 🔴 三件必须知道的事

1. **`code_info` 没有历史。** 手册写「交易日早上 9 点前更新当日最新」——
   它是快照，**今天跑就只有今天**。所以落盘时加一列 `ASOF_DATE`，
   否则明天再跑一次就分不出哪份是哪天的；也别指望用它回溯涨跌停价
   —— 那要用 `history_stock_status`。
2. **复权因子是宽表**（index=交易日，column=代码）。直落 parquet 会让
   每个分片一个 schema（列名就是那一批的股票代码），整表读不起来。
   所以 melt 成 `(date, code, factor)` 长表 —— 见 `amazing_common.melt_wide`。
3. **`history_stock_status` 就是本项目要的那张表**：涨跌停价 / ST / 停牌
   现在是三处自己算/自己拼的（涨跌停按规则算 + 自校验、ST 靠
   `public_status` 字符串、停牌是单独一张稀疏表）。接进来之前
   **必须与面板自算的涨跌停价对数**（本项目 `limit_rule_ok` 99.97%，
   漏的 0.03% 是新股首日 / 退市整理期）—— 见 `raw/amazing/README.md`
   「接进来之前必须对的三样数」。
"""
import os
import sys

# 从任何目录都能跑：`python datalake/raw/amazing/_ingest/pull_0X.py`
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import amazing_common as ac

# 代码表要取哪几类。EXTRA_STOCK_A 含北交所；EXTRA_STOCK_A_SH_SZ 只有沪深
# —— 手册所有财务/股东类接口的示例都用后者（"支持沪深 A 的代码列表"），
# 所以后面几个脚本用它，这里两个都存一份，便于对差集。
CODE_TYPES = ['EXTRA_STOCK_A', 'EXTRA_STOCK_A_SH_SZ', 'SH_A', 'SZ_A', 'BJ_A',
              'EXTRA_INDEX_A_SH_SZ', 'EXTRA_INDEX_A', 'EXTRA_ETF', 'EXTRA_KZZ']
# 交易日历要取哪几个市场（附录 4.1.4）
CALENDAR_MARKETS = ['SH', 'SZ', 'BJ', 'CFE']


def specs(args, cfg):
    start = args.start or ac.MARKET_START
    out = []

    # ---------------------------------------------------------- 交易日历
    def fetch_calendar(market):
        import pandas as pd
        a = ac.api(cfg, args.sdk_cache)
        days = a['base'].get_calendar(market=market)
        return pd.DataFrame({'date': [int(d) for d in days],
                             'market': market})

    out.append(dict(
        name='trading_calendar',
        api='BaseData.get_calendar(data_type, market)',
        params='market ∈ %s' % CALENDAR_MARKETS,
        shard_by='一个市场一片',
        shards=[(m, {'market': m}) for m in CALENDAR_MARKETS],
        fetch=fetch_calendar,
        note="""
- 返回 `List[int]`（yyyymmdd），SDK 内部是拿 `start_date=19900101` 去问的。
- ★ **本地也能算交易日历**（`tdx.db` 的 `raw_holidays`：交易日 = 工作日 − 休市日，
  与 `std/trading_calendar.parquet` 逐日对数一致 5746 天）。所以这张表的用途是
  **交叉校验**，不是取代 —— 两边不一致时要先查清楚是哪边错。
- 🔴 各市场的日历**不一样**（中金所 CFE 与沪深不同），所以按市场分片、
  落盘带 `market` 列。混成一列会让"某天是不是交易日"这件事失去主体。
""".strip()))

    # ---------------------------------------------------------- 历史代码表
    def fetch_hist_codes(security_type):
        import pandas as pd
        a = ac.api(cfg, args.sdk_cache)
        end = args.end or a['calendar'][-1]
        codes = a['base'].get_hist_code_list(
            security_type=security_type, start_date=int(start),
            end_date=int(end), local_path=a['sdk_path'])
        return pd.DataFrame({'code': [str(c) for c in codes],
                             'security_type': security_type,
                             'start_date': int(start), 'end_date': int(end)})

    out.append(dict(
        name='hist_code_list',
        api='BaseData.get_hist_code_list(security_type, start_date, end_date, local_path)',
        params='security_type ∈ %s；区间 %s ~ 今天' % (CODE_TYPES, start),
        shard_by='一个 security_type 一片',
        shards=[(t, {'security_type': t}) for t in CODE_TYPES],
        fetch=fetch_hist_codes,
        note="""
- 返回的是 `List[str]`，**没有纳入/剔除日期** —— SDK 内部拿到了
  `ENTRY_DATE`/`REMOVE_DATE`，但对外只给代码。要区间信息只能用
  `stock_basic` 的 `LISTDATE`/`DELISTDATE`。
- 🔴 **这张表是所有按代码取数的表的输入**，而它**每天都在变**（新股/退市）。
  所以 `amazing_common.universe()` 把它**冻结**成
  `_ingest/state/universe_*.json`，九个脚本共用同一份 ——
  不冻结的话不同的表覆盖的代码集不同，join 出来的面板会缺行，**而这不报错**。
- ★ 用它而不是 `get_code_list`：后者只有**今天在市**的，用它取历史 =
  幸存者偏差（退市股整段消失）。
""".strip()))

    # ---------------------------------------------------------- 每日最新证券信息
    def fetch_code_info(security_type):
        a = ac.api(cfg, args.sdk_cache)
        df = a['base'].get_code_info(security_type=security_type)
        if df is None or len(df) == 0:
            return None
        df = df.copy()
        df.index.name = 'code'
        df = df.reset_index()
        df.insert(0, 'ASOF_DATE', int(a['calendar'][-1]))
        df.insert(1, 'SECURITY_TYPE', security_type)
        return df

    out.append(dict(
        name='code_info',
        api='BaseData.get_code_info(security_type)',
        params='security_type ∈ %s' % CODE_TYPES,
        shard_by='一个 security_type 一片',
        shards=[(t, {'security_type': t}) for t in CODE_TYPES],
        fetch=fetch_code_info,
        add_fields={'ASOF_DATE': ('int', '本脚本落盘时标的日期（= 交易日历最后一天）'),
                    'SECURITY_TYPE': ('str', '本脚本落盘时标的 security_type')},
        note="""
- 🔴 **这是快照，不是历史**（手册：交易日早上 9 点前更新当日最新）。
  落盘加了 `ASOF_DATE` 列 —— 不加的话隔天再跑一次就分不出哪行是哪天的，
  而两天的涨停价长得一模一样。
- ★ 实测 pyc 的列元组里有 `list_day`，**手册的输出说明里没列它**。
  这也是"列名以 wheel 为准、手册只提供说明"的旁证。
- 要历史的涨跌停/ST/停牌，用 `history_stock_status`，不要拿这张表凑。
""".strip()))

    # ---------------------------------------------------------- 证券基础信息
    codes_a = ac.universe(args, cfg, 'EXTRA_STOCK_A', start, args.end) if cfg \
        else ['DRYRUN%06d' % i for i in range(6000)]

    def fetch_stock_basic(code_list):
        a = ac.api(cfg, args.sdk_cache)
        return a['info'].get_stock_basic(code_list)

    out.append(dict(
        name='stock_basic',
        api='InfoData.get_stock_basic(code_list)',
        params='沪深北全部 A 股（含已退市）%d 个' % len(codes_a),
        shard_by='每 %d 个代码一片（SDK 内部上限 200：QueryPara.req_stock_basic_len）'
                 % ac.chunk_size(args, 200),
        shards=ac.shards_codes(codes_a, ac.chunk_size(args, 200)),
        fetch=fetch_stock_basic,
        note="""
- **含已退市标的**（`IS_LISTED` 1=上市交易 / 3=终止上市），这是它比
  本项目现有源强的地方之一：退市股的上市/退市日一次给全。
- 这个接口**没有** `local_path` / `is_local` / `begin_date` / `end_date`
  —— 只有 `code_list`。所以只能按代码分片。
""".strip()))

    # ---------------------------------------------------------- 历史证券信息（日频）
    def fetch_hss(code_list, begin_date, end_date):
        a = ac.api(cfg, args.sdk_cache)
        return a['info'].get_history_stock_status(
            code_list, local_path=a['sdk_path'], is_local=False,
            begin_date=int(begin_date), end_date=int(end_date))

    end = args.end or (int(ac.api(cfg, args.sdk_cache)['calendar'][-1]) if cfg else 20261231)
    out.append(dict(
        name='history_stock_status',
        api='InfoData.get_history_stock_status(code_list, begin_date, end_date)',
        params='%s ~ %s，沪深北 A 股' % (start, end),
        date_sem='**交易日**（手册 3.5.2.10）',
        shard_by='年 × 每 %d 个代码' % ac.chunk_size(args, 200),
        shards=ac.shards_codes_years(codes_a, ac.chunk_size(args, 200), start, end),
        fetch=fetch_hss,
        note="""
- 日频，一只票一天一行：`PRECLOSE` / `HIGH_LIMITED` / `LOW_LIMITED` /
  `PRICE_HIGH_LMT_RATE` / `PRICE_LOW_LMT_RATE` / `IS_ST_SEC` / `IS_SUSP_SEC`
  / `IS_WD_SEC`（除息）/ `IS_XR_SEC`（除权）。
- 🔴 **量最大的一张基础表**：6000 只 × 13 年 × 250 天 ≈ 2000 万行。
  所以按 (年 × 代码块) 切成几百片（每片多少个代码见上面「分片方式」）——
  一次全要会把内存和响应都撑爆，
  而失败之后没有断点。
- 🔴 接进本项目之前**必须对数**：拿它的 `HIGH_LIMITED` 与面板自算的涨跌停价
  逐日比。本项目自算的命中率 99.97%，漏的是"新股首日 +44%""退市整理期首日
  无限制"这类特例 —— 正好用它验，也正好用它替掉那段规则代码。
- 起点受 2013 限制（手册 §2.2）。**早于 2013 不会报错，只会返回空**，
  那看着像"这只票那时候没上市"。
""".strip()))

    # ---------------------------------------------------------- 复权因子（两种）
    for tbl, meth, what in (('backward_factor', 'get_backward_factor', '后复权因子'),
                            ('adj_factor', 'get_adj_factor', '单次复权因子')):
        def make(meth=meth):
            def fetch(code_list):
                a = ac.api(cfg, args.sdk_cache)
                return getattr(a['base'], meth)(
                    code_list, local_path=a['sdk_path'], is_local=False)
            return fetch

        out.append(dict(
            name=tbl,
            api='BaseData.%s(code_list, local_path, is_local=False)' % meth,
            params='沪深北 A 股 %d 个（is_local=False 强制走服务端）' % len(codes_a),
            shard_by='每 %d 个代码一片（SDK 内部上限 300：QueryPara.req_%s_len）'
                     % (ac.chunk_size(args, 300), tbl),
            wide=True, value_name='factor',
            shards=ac.shards_codes(codes_a, ac.chunk_size(args, 300)),
            fetch=make(),
            note="""
- 返回**宽表**：index=交易日，column=证券代码。落盘前 melt 成
  `(date, code, factor)` 长表 —— 直落宽表会让每个分片一个 schema
  （列名是那一批的代码），**单个文件读得起来、整表读不起来**，
  所以问题只在合并时才暴露。
- 🔴 这个接口**没有** `begin_date`/`end_date`，只有参数组 1
  （`local_path`+`is_local`），所以它**一定会往 local_path 写 HDF5**
  —— 缺 `tables` 包时报的是 pandas 的 ImportError，看着与网络无关。
- 传 `is_local=False` 是刻意的：True 时"本地有就用本地"，
  于是**永远拿不到最新的因子**，而这不报错。
- ★ %s。本项目现在用的是 tdx 的复权因子（实测只有 300114 一只错）——
  接进来的价值是**交叉校验**，两边不一致的那几只才是要查的。
""".strip() % what))

    # ---------------------------------------------------------- 北交所代码对照
    def fetch_bj():
        a = ac.api(cfg, args.sdk_cache)
        return a['info'].get_bj_code_mapping(local_path=a['sdk_path'], is_local=False)

    out.append(dict(
        name='bj_code_mapping',
        api='InfoData.get_bj_code_mapping(local_path, is_local=False)',
        params='无入参',
        shard_by='一片',
        shards=[('all', {})],
        fetch=fetch_bj,
        note="""
- 北交所存量公司的**新旧代码对照**（`OLD_CODE` / `NEW_CODE` / `SECURITY_NAME`）。
- 🔴 有它才能把 2021 年前的新三板精选层代码接到现在的北交所代码上。
  不对照的话同一家公司在历史与现在是两只票，**而这不报错**，
  只是那一段历史凭空消失。
""".strip()))

    return out


if __name__ == '__main__':
    ac.main_for(specs, '① 基础数据')
