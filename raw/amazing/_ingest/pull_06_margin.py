#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""⑥ 融资融券数据（手册 3.5.8）—— 成交汇总 / 交易明细。

    python pull_06_margin.py

| 表 | 接口 | 粒度 |
|---|---|---|
| `margin_summary` | `get_margin_summary` | **一个交易所一天一行**（没有 code_list） |
| `margin_detail` | `get_margin_detail` | 一只票一天一行 |

## 🔴 `margin_summary` 有一列手册里没写：`EXCHANGE`

wheel 的 `columns_list` 里是
`TRADE_DATE, EXCHANGE, SUM_BORROW_MONEY_BALANCE, …`，而手册 3.5.8.1 的字段表
**只列了 7 个、漏了 `EXCHANGE`**。这一列很要紧：汇总是**按交易所**一天一行
（沪/深/北各一条）。不知道它存在的话会把"融资余额"当成全市场的一个数用
—— **而这不报错，只是小了一半**。要全市场就得 `GROUP BY TRADE_DATE` 求和。

（这也是本仓库"列名以 wheel 的 pyc 为准、手册只提供中文说明"的由来，
见 `tools/parse_manual_fields.py`。）

## 另外两件事

- `margin_summary` **没有 `code_list` 参数**，只有 `local_path`/`is_local` +
  `begin_date`/`end_date`。所以它只能按**时间**分片，不能按代码分片。
- `SUM_SALES_OF_BORROWED_SEC`（融券卖出量）的类型是 **int**、单位是
  "股,份,手" —— 🔴 同一列混了三种单位（股票是股、基金是份、债券是手），
  跨品种加总没有意义。金额类的都是元。
"""
import os
import sys

# 从任何目录都能跑：`python datalake/raw/amazing/_ingest/pull_0X.py`
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import amazing_common as ac


def specs(args, cfg):
    import time as _t
    today = int(_t.strftime('%Y%m%d'))
    start = args.start or ac.MARKET_START      # 融资融券是行情类，起点受 2013 限制
    end = args.end or today
    codes = ac.universe(args, cfg, 'EXTRA_STOCK_A_SH_SZ', ac.MARKET_START, args.end) \
        if cfg else ['DRYRUN%06d' % i for i in range(5000)]

    def fetch_summary(begin_date, end_date):
        a = ac.api(cfg, args.sdk_cache)
        return a['info'].get_margin_summary(
            local_path=a['sdk_path'], is_local=False,
            begin_date=int(begin_date), end_date=int(end_date))

    def fetch_detail(code_list, begin_date, end_date):
        a = ac.api(cfg, args.sdk_cache)
        return a['info'].get_margin_detail(
            code_list, local_path=a['sdk_path'], is_local=False,
            begin_date=int(begin_date), end_date=int(end_date))

    size = ac.chunk_size(args, 200)     # = QueryPara.req_margin_detail_len
    return [
        dict(
            name='margin_summary',
            api='InfoData.get_margin_summary(begin_date, end_date)  # 没有 code_list',
            params='%s ~ %s，全市场（沪/深/北各一行）' % (start, end),
            date_sem='**交易日**（手册 3.5.8.1）',
            shard_by='一年一片（这个接口没有代码维度，只能按时间切）',
            shards=[('%d' % y, {'begin_date': b, 'end_date': e})
                    for (y, b, e) in ac.years_between(start, end)],
            fetch=fetch_summary,
            note="""
- 🔴 **按交易所一天一行**，靠 `EXCHANGE` 区分（这一列**手册的字段表里没有**，
  是从 wheel 的 `columns_list` 里发现的）。要全市场口径必须
  `GROUP BY TRADE_DATE` 求和 —— 直接取一行会少一半。
- 这个接口**没有 `code_list`**，所以只能按时间分片。
- `SUM_SALES_OF_BORROWED_SEC` 单位是"股,份,手"（跨品种不可加总），
  其余金额列都是元。
""".strip()),
        dict(
            name='margin_detail',
            api='InfoData.get_margin_detail(code_list, begin_date, end_date)',
            params='沪深 A 股 %d 只；%s ~ %s' % (len(codes), start, end),
            date_sem='**交易日**（手册 3.5.8.2）',
            shard_by='年 × 每 %d 个代码' % size,
            shards=ac.shards_codes_years(codes, size, start, end),
            fetch=fetch_detail,
            note="""
- 逐票日频：融资余额/买入额/偿还额、融券余额/卖出量/偿还量、融资融券余额。
- 🔴 **只有两融标的才有数据**，非标的票返回空 —— 空不是失败
  （manifest 里记成 `rows: 0`，不是 `failed`）。所以某一片"0 行"是正常的，
  而**整年 0 行**才是要查的。
- `SEC_LENDING_BALANCE`（融券余额，元）与 `SEC_LENDING_BALANCE_VOL`
  （融券余量，股）是两列 —— 名字只差一个后缀，混用会差一个价格的量级。
- 量级：6000 只 × 13 年 × 250 天，但非标的为空，实际远小于日 K。
""".strip()),
    ]


if __name__ == '__main__':
    ac.main_for(specs, '⑥ 融资融券数据')
