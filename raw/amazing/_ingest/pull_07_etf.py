#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""⑦ ETF 数据（手册 3.5.11）—— 申赎清单 / 基金份额 / 基金净值 / 收盘 IOPV。

    python pull_07_etf.py

| 表 | 接口 | 是否有历史 |
|---|---|---|
| `etf_pcf_info` | `get_etf_pcf`（返回值第 1 个） | 🔴 **没有，只有当天** |
| `etf_pcf_constituent` | `get_etf_pcf`（返回值第 2 个） | 🔴 **没有，只有当天** |
| `fund_share` | `get_fund_share` | 有（按变动日期） |
| `fund_nav` | `get_fund_nav` | 有（按变动日期） |
| `fund_iopv` | `get_fund_iopv` | 有（按变动日期） |

## 🔴 申赎清单只有"每日最新"，拉不到历史

手册 3.5.11.1 的标题就是「ETF**每日最新**申赎数据」，入参只有 `code_list`
—— 没有 `begin_date`/`end_date`，也没有 `local_path`。所以**这两张表天生只有
今天这一份**，想要历史只能每天跑一次攒起来。

本脚本因此给它们加了 `ASOF_DATE` 列并把分片 id 打上日期
（`part-20260904-0000.parquet`）—— 不这么做的话每天重跑会**覆盖**昨天那份，
而"覆盖了"这件事**不报错**，只是那张表永远只有一天。
★ 附带的好处：它天然幂等 —— 同一天重跑写的是同一个文件。

## 🔴 一次调用返回两个东西，别调两遍

`get_etf_pcf` 返回 `(etf_pcf_info, etf_pcf_constituent)`：前者是 dataframe
（index=ETF 代码），后者是 `dict[ETF代码] -> dataframe`（成分股清单）。
两张表分开落盘，但**共用一次调用的结果**（`_PCF` 缓存）——
分别调一次等于把请求量翻倍，而限流是这条链上唯一的真风险。

## 三张历史表的日期都写着"变动日期"

`fund_share` / `fund_nav` / `fund_iopv` 的入参表都写 `begin_date` = 变动日期。
但 `fund_iopv` 的字段里是 `PRICE_DATE`（日期）、`fund_nav` 里既有 `ANN_DATE`
（公告日）又有 `PRICE_DATE`（估值日）—— 🔴 **过滤用的那个日期与你想要的
那个日期可能不是同一个**。做 as-of 时按字段自己的语义来，别按参数名。
"""
import os
import sys

# 从任何目录都能跑：`python datalake/raw/amazing/_ingest/pull_0X.py`
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import amazing_common as ac

_PCF = {}


def specs(args, cfg):
    import time as _t
    today = int(_t.strftime('%Y%m%d'))
    start = args.start or ac.MARKET_START
    end = args.end or today
    etfs = ac.universe(args, cfg, 'EXTRA_ETF', ac.MARKET_START, args.end) \
        if cfg else ['DRYRUN%06d' % i for i in range(900)]

    # ------------------------------------------------ 申赎清单（当天快照）
    def pcf(code_list):
        """一次调用喂两张表。

        🔴 缓存必须**留住全部分片**，不能只留最近一片。两张表是**先跑完
        第一张的所有分片、再跑第二张**（`run_table` 是逐表的），只留最近
        一片的话第二张表每片都要重新调一次 —— 请求量正好翻倍，
        **而这不报错**，只是慢一倍、也多一倍被限流的机会。
        代价是内存：900 只 ETF 的成分清单约 900 × 几百行 × 9 列（几十 MB），
        换掉一倍请求量是划算的。
        """
        # 🔴 缓存的键是**代码清单本身**，不是分片 id。用分片 id 做键时，
        #    同一个进程里换过 `--chunk` 会让"第 0 片"指向不同的代码集 ——
        #    键相同、内容不同，于是取回来的是**别的分片的数据**。
        #    自证里那条 ⑱ 就是这么暴露出来的（先跑了 chunk=5、再跑 chunk=3）。
        key = '|'.join(map(str, code_list))
        if key not in _PCF:
            a = ac.api(cfg, args.sdk_cache)
            _PCF[key] = a['base'].get_etf_pcf(code_list)
        return _PCF[key]

    def fetch_pcf_info(code_list):
        res = pcf(code_list)
        info = res[0] if isinstance(res, (tuple, list)) else res
        if info is None or len(info) == 0:
            return None
        info = info.copy()
        info.index.name = 'code'
        info = info.reset_index()
        info.insert(0, 'ASOF_DATE', int(today))
        return info

    def fetch_pcf_cons(code_list):
        res = pcf(code_list)
        if not isinstance(res, (tuple, list)) or len(res) < 2:
            return None
        cons = res[1]
        if not isinstance(cons, dict):
            return cons
        # dict[ETF] -> 成分表：ETF 代码要显式带上（成分表里只有成分股的代码）
        out = {}
        for etf, df in cons.items():
            if df is None or len(df) == 0:
                continue
            d = df.copy()
            d.insert(0, 'ETF_CODE', etf)
            d.insert(0, 'ASOF_DATE', int(today))
            out[etf] = d
        return out

    # 分片 id 带上日期：每天跑一次就多一个文件，不会覆盖昨天那份
    size_pcf = ac.chunk_size(args, 300)     # = QueryPara.req_etf_pdf_len
    pcf_shards = [('%d-%s' % (today, sid), kw)
                  for sid, kw in ac.shards_codes(etfs, size_pcf)]

    out = [
        dict(
            name='etf_pcf_info',
            api='BaseData.get_etf_pcf(code_list)  → 返回值[0]',
            params='沪深 ETF %d 只（当天快照）' % len(etfs),
            date_sem='无日期参数 —— **只有当天**',
            shard_by='每 %d 只 ETF 一片，分片 id 带日期（每天跑一次就多一份）' % size_pcf,
            shards=pcf_shards,
            fetch=fetch_pcf_info,
            add_fields={'ASOF_DATE': ('int', '本脚本落盘时标的日期。🔴 这张表没有历史，'
                                              '不标日期的话每天重跑会覆盖昨天那份'),
                        'code': ('str', 'ETF 代码（原返回值的 index）')},
            note="""
- 🔴 **没有历史**：手册标题就是"每日最新"，入参只有 `code_list`。
  要历史就得每天跑（分片 id 带日期，所以同一天重跑幂等、隔天不覆盖）。
- 字段里 `creation`/`redemption`/`creation_redemption_switch` 是申赎开关，
  ★ 注意手册标了**"仅深圳有效"/"仅上海有效"**的那批：沪深两市的字段可用性
  不一样，拿深市字段去判沪市 ETF 会得到一个空值而不是错误。
- `estimate_cash_component`（预估现金差额）/ `cash_component`（前一日现金差额）
  是申赎定价的关键，别混。
- ★ 手册的字段表里还列了 `net_creation_limit_per_user` 等几个，
  **wheel 的 `columns_list` 里没有** —— 说明这版 SDK 不返回它们。
""".strip()),
        dict(
            name='etf_pcf_constituent',
            api='BaseData.get_etf_pcf(code_list)  → 返回值[1]',
            params='同上（与 etf_pcf_info **共用同一次调用**）',
            date_sem='无日期参数 —— **只有当天**',
            shard_by='同 etf_pcf_info',
            shards=pcf_shards,
            fetch=fetch_pcf_cons,
            add_fields={'ASOF_DATE': ('int', '本脚本落盘时标的日期（这张表没有历史）'),
                        'ETF_CODE': ('str', '所属 ETF 代码。🔴 原返回值里 ETF 代码只在 '
                                            'dict 的 key 上，成分表内部只有成分股代码')},
            note="""
- `dict[ETF代码] -> 成分股清单`。🔴 ETF 代码**只在 dict 的 key 上**，
  成分表里那个 `underlying_security_id` 是"拟合指数"不是 ETF 自己 ——
  不显式加 `ETF_CODE` 就再也分不清哪几行属于哪只 ETF。
- `substitute_flag` 现金替代标志 / `premium_ratio` 溢价比例 /
  `creation_cash_substitute` 申购替代金额 —— 这几个决定"这只成分能不能用现金替代"。
- 与 `etf_pcf_info` **共用一次 `get_etf_pcf` 调用**（`_PCF` 缓存整轮都留着）：
  分别调等于请求量翻倍。🔴 只有**同一个进程里两张表一起跑**时才共用；
  单独 `--only etf_pcf_constituent` 会自己调一遍（那是对的，不是浪费）。
""".strip()),
    ]

    # ------------------------------------------------ 三张历史表
    for tbl, meth, cn, note in (
        ('fund_share', 'get_fund_share', 'ETF 基金份额', """
- `FUND_SHARE` / `TOTAL_SHARE` / `FLOAT_SHARE` 的单位都是**万份**（不是份）。
- 🔴 `IS_CONSOLIDATED_DATA` 0=非合并 1=合并 2=合并但该代码不实际交易。
  不筛就会把同一只基金的场内份额与合并份额一起加，**而结果看着像个大数**。
- 按**变动日期**过滤：份额只在申赎/分拆时变，不是日频。
"""),
        ('fund_nav', 'get_fund_nav', 'ETF 基金净值', """
- 三个"净值"要分清：`UNIT_NAV` 单位净值 / `ACCUM_NAV` 累计净值 /
  `ADJ_UNIT_NAV` 复权单位净值。算收益率用**复权**那个
  （`NAV_ADJ_FACTOR` 是它的因子）。
- 两个日期：`ANN_DATE` 公告日、`PRICE_DATE` 估值日。做 PIT 用公告日。
- `INNER_CODE`（场内代码）/ `OUTER_CODE`（场外代码）—— 同一只基金两个身份，
  接进本地时要归一，否则会变成两只票。
"""),
        ('fund_iopv', 'get_fund_iopv', 'ETF 每日收盘 IOPV', """
- 只有三列：`MARKET_CODE` / `PRICE_DATE` / `IOPV_NAV`（IOPV 收盘净值）。
- ★ IOPV 是盘中参考净值，**收盘 IOPV ≠ 当日单位净值**（一个是实时估算、
  一个是估值结果）。算折溢价用它对当日收盘价，别拿它当净值用。
"""),
    ):
        size = ac.chunk_size(args, 200)

        def make(meth=meth):
            def fetch(code_list, begin_date, end_date):
                a = ac.api(cfg, args.sdk_cache)
                return getattr(a['info'], meth)(
                    code_list, local_path=a['sdk_path'], is_local=False,
                    begin_date=int(begin_date), end_date=int(end_date))
            return fetch

        out.append(dict(
            name=tbl,
            api='InfoData.%s(code_list, begin_date, end_date)' % meth,
            params='%s；沪深 ETF %d 只；%s ~ %s' % (cn, len(etfs), start, end),
            date_sem='**变动日期**（手册 3.5.11.x 的入参表）—— 注意与字段里的 '
                     '`PRICE_DATE`/`ANN_DATE` 不一定是同一个日期',
            shard_by='年 × 每 %d 只 ETF' % size,
            shards=ac.shards_codes_years(etfs, size, start, end),
            fetch=make(),
            note=note.strip()))
    return out


if __name__ == '__main__':
    ac.main_for(specs, '⑦ ETF 数据')
