#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""⑧ 交易所指数数据（手册 3.5.12）—— 指数成分股 / 成分股日权重。

    python pull_08_index.py

| 表 | 接口 | 覆盖范围 |
|---|---|---|
| `index_constituent` | `get_index_constituent` | 常用指数约 600 多只，**带纳入/剔除日** |
| `index_weight` | `get_index_weight` | 🔴 **只有 5 个指数** |

## 🔴 `index_weight` 只支持 5 个指数，手册明写

上证 50 `000016.SH` / 沪深 300 `000300.SH` / 中证 500 `000905.SH` /
中证 800 `000906.SH` / 中证 1000 `000852.SH`。
传别的指数**不报错，只是返回空** —— 那看着像"这个指数没有权重数据"。
所以本脚本把这 5 个**写死在代码里**（而不是拿指数代码表去循环）：
写死的话加一个指数是显式改代码，循环的话会安静地跑出 600 个空片。

## 🔴 `index_constituent` / `industry_constituent` 的 `is_local` 语义与别处**相反**

手册 3.5.12.1 原话：「默认为 True，**仅从本地获取，不从服务器获取数据**；
False，仅从服务器获取，不从本地获取数据；因为原始数据的剔除日期会根据最新
数据修改，所以**第一次运行 `is_local` 需要设置成 False**」。

别处的 `is_local=True` 是"本地有就用本地、没有就去服务器"；这里的 True 是
**只看本地**。第一次跑传 True 会拿到**空**（本地什么都没有），
**而这不报错**。所以本脚本这两张表恒传 `is_local=False`。
★ 它也因此**没有 `begin_date`/`end_date`**，一次给全历史区间
（`INDATE`/`OUTDATE`），并且**一定会往 `local_path` 写 HDF5** —— 需要 `tables` 包。

## ★ 带纳入/剔除日 = 天然的 type-2 区间

本项目现在是从 285,781 条 as-of 快照**推**出 8,748 条区间（`index_member_asof`）。
这份直接给 `INDATE` / `OUTDATE`（未剔除时 `OUTDATE` 是 nan）。
🔴 但手册那句"剔除日期会根据最新数据修改"意味着**这张表不是 append-only**：
同一条成分记录的 `OUTDATE` 会被后来的数据改写。所以每次全量重取、
不要做增量合并 —— 增量合并会留下一批**永远不会被剔除**的僵尸成分，
而那不报错，只是回测里那几只票一直在指数里。
"""
import os
import sys

# 从任何目录都能跑：`python datalake/raw/amazing/_ingest/pull_0X.py`
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import amazing_common as ac

# 🔴 手册 3.5.12.2 明写只支持这 5 个。写死在这里而不是循环指数代码表 ——
#    见文件头。
WEIGHT_INDEXES = ['000016.SH', '000300.SH', '000905.SH', '000906.SH', '000852.SH']
WEIGHT_NAMES = {'000016.SH': '上证50', '000300.SH': '沪深300', '000905.SH': '中证500',
                '000906.SH': '中证800', '000852.SH': '中证1000'}


def specs(args, cfg):
    import time as _t
    today = int(_t.strftime('%Y%m%d'))
    start = args.start or ac.MARKET_START
    end = args.end or today
    idx = ac.universe(args, cfg, 'EXTRA_INDEX_A_SH_SZ', ac.MARKET_START, args.end) \
        if cfg else ['DRYRUN%06d' % i for i in range(600)]

    def fetch_cons(code_list):
        a = ac.api(cfg, args.sdk_cache)
        # is_local=False 是**必须**的，见文件头
        return a['info'].get_index_constituent(
            code_list, local_path=a['sdk_path'], is_local=False)

    def fetch_weight(code_list, begin_date, end_date):
        a = ac.api(cfg, args.sdk_cache)
        return a['info'].get_index_weight(
            code_list, local_path=a['sdk_path'], is_local=False,
            begin_date=int(begin_date), end_date=int(end_date))

    size_c = ac.chunk_size(args, 200)   # = QueryPara.req_index_constituent_len
    return [
        dict(
            name='index_constituent',
            api='InfoData.get_index_constituent(code_list, local_path, is_local=False)',
            params='沪深指数 %d 只（手册：仅支持常用指数约 600 多只，'
                   '不支持的**返回空而不报错**）' % len(idx),
            date_sem='无日期参数 —— 一次给全历史，用 `INDATE`/`OUTDATE` 表达区间',
            shard_by='每 %d 个指数一片' % size_c,
            shards=ac.shards_codes(idx, size_c),
            fetch=fetch_cons,
            note="""
- 五列：`INDEX_CODE` / `CON_CODE` / `INDATE`（纳入日）/ `OUTDATE`（剔除日，
  未剔除时 nan）/ `INDEX_NAME`。**天然的 type-2 区间**。
- 🔴 `is_local` **必须传 False**：这个接口的 True 是"只看本地"，
  第一次跑传 True 会拿到空的，**而这不报错**（手册 3.5.12.1 原话见文件头）。
- 🔴 **每次全量重取，不要增量合并**：手册说"剔除日期会根据最新数据修改"，
  也就是同一条记录的 `OUTDATE` 会被改写。增量合并会留下永远不被剔除的
  僵尸成分，而那不报错。
- 🔴 手册说"仅支持常用指数，**无返回数据则不支持**"—— 所以某一片全空是
  正常的（manifest 记 0 行不是 failed）；但如果连 000300.SH 都空，
  那就是真出问题了。
""".strip()),
        dict(
            name='index_weight',
            api='InfoData.get_index_weight(code_list, begin_date, end_date)',
            params='%s；%s ~ %s' % (
                '、'.join('%s(%s)' % (c, WEIGHT_NAMES[c]) for c in WEIGHT_INDEXES),
                start, end),
            date_sem='**变动日期**（手册 3.5.12.2）；字段里的 `TRADE_DATE` 是生效日期',
            shard_by='指数 × 年（每片一个指数一年；SDK 内部批量上限是 50）',
            shards=[('%s-%d' % (c.replace('.', ''), y),
                     {'code_list': [c], 'begin_date': b, 'end_date': e})
                    for c in WEIGHT_INDEXES
                    for (y, b, e) in ac.years_between(start, end)],
            fetch=fetch_weight,
            note="""
- 🔴 **只有 5 个指数有权重**（上证50/沪深300/中证500/中证800/中证1000）。
  传别的指数返回空而不报错。
- 字段：`WEIGHT`（权重 %）/ `WEIGHT_FACTOR`（权重因子）/ `CALC_SHARE`
  （计算用股本）/ `FREE_SHARE_RATIO`（自由流通比例 %，备注写"归档后"）/
  `TOTAL_SHARE` / `CLOSE`。
- ★ 这是本项目现在**完全没有**的东西（只有成分、没有权重）。有了权重才能
  做真正的基准跟踪与 Brinson 归因 —— 等权近似在大盘股占比高的年份误差很大。
- 🔴 `WEIGHT` 与 `WEIGHT_FACTOR` 是两个东西：前者是百分比（加总≈100），
  后者是编制方用的因子。当权重用错了那个，组合权重加总不到 1
  —— **而它不报错**，只是所有归因结果按比例偏。
""".strip()),
    ]


if __name__ == '__main__':
    ac.main_for(specs, '⑧ 交易所指数数据')
