#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""④ 股东股本数据（手册 3.5.6）—— 十大股东 / 股东户数 / 股本结构 / 股权冻结质押 / 限售解禁。

    python pull_04_holder.py
    python pull_04_holder.py --only equity_restricted

| 表 | 接口 | `begin_date`/`end_date` 的含义 |
|---|---|---|
| `share_holder` | `get_share_holder` | **到期日期**（报告期末） |
| `holder_num` | `get_holder_num` | **股东户数统计的截止日期** |
| `equity_structure` | `get_equity_structure` | **变动日期** |
| `equity_pledge_freeze` | `get_equity_pledge_freeze` | **公告日期** |
| `equity_restricted` | `get_equity_restricted` | **解禁日期** |

## 🔴 五张表的日期含义各不相同 —— 这是本脚本最容易错的地方

同一个参数名 `begin_date`，五张表分别是"到期日 / 统计截止日 / 变动日 /
公告日 / 解禁日"（逐条抄自手册 3.5.6.1~3.5.6.5 的入参表）。拿同一个区间
套五张表能跑通、不报错，**但取回来的集合不是你以为的那个**。

最要紧的一条：**`equity_restricted` 按【解禁日期】过滤，而解禁日在未来。**
用 `end_date=今天` 会把**未来的解禁计划整段丢掉** —— 而限售解禁这件事的
全部价值就在"未来哪天有多少股要出来"。所以这张表的终点写到 20991231。
（它的起点也可以比 2013 早：解禁日不是行情，不受 §2.2 那条限制。）
"""
import os
import sys

# 从任何目录都能跑：`python datalake/raw/amazing/_ingest/pull_0X.py`
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import amazing_common as ac

TABLES = [
    # (表名, 方法, 中文, 日期含义, 起点, 终点, 每片代码数, 备注)
    ('share_holder', 'get_share_holder', '十大股东', '到期日期（报告期末）',
     'info', 'today', 200, """
- `HOLDER_TYPE` 10=十大股东 / 20=流通股前十大股东 —— 🔴 **两套榜混在一张表里**，
  不筛就会把同一期的股东数出成 20 个，而"前十大持股比例合计"会直接翻倍。
- `HOLDER_HOLDER_CATEGORY` 1=个人 2=公司；`HOLDER_SHARECATEGORYNAME` 是股份类型
  （`HOLDER_TYPE=20` 时全是 'A Float Holder'）。
- `QTY_NUM` 是持股量序号（第几大股东）。
"""),
    ('holder_num', 'get_holder_num', '股东户数', '股东户数统计的截止日期',
     'info', 'today', 200, """
- 🔴 `HOLDER_NUM` 是 **A 股股东户数**，`HOLDER_TOTAL_NUM` 是
  **A+B+H+境外股的总户数** —— 名字长的那个是"总"。搞反了不报错，
  只是"户均持股"算出来偏一截。
- `ANN_DT`（注意不是 ANN_DATE）是公告日，`HOLDER_ENDDATE` 是统计截止日。
  做 PIT 要用公告日。
"""),
    ('equity_structure', 'get_equity_structure', '股本结构', '变动日期',
     'info', 'today', 200, """
- 54 列，把股本拆成流通/非流通、国有/法人/自然人、限售/非限售各档。
  本项目现在只用 `TOT_SHARE` / `FLOAT_A_SHARE` 两个 —— 其余在这里全有。
- 🔴 按**变动日期**过滤：股本只在变动时有记录（不是日频）。
  要"某天的股本"必须自己按变动日 as-of 前向填充，
  直接 join 交易日会大面积缺行。
- `SHR_CATEGORY_CODE` / `SHARE_CHANGE_REASON` 说明这次变动是什么原因。
"""),
    ('equity_pledge_freeze', 'get_equity_pledge_freeze', '股权冻结/质押', '公告日期',
     'info', 'today', 200, """
- 一次冻结/质押一行：`FRO_SHARES`（本次冻结/质押股数）、
  `FRO_SHR_TO_TOTAL_HOLDING_RATIO`（占所持股比例）、`FROZEN_INSTITUTION`。
- 🔴 `TOTAL_HOLDING_SHR` 的单位是**万股**（手册备注"持股总数（万股）"），
  而 `FRO_SHARES` 是股。两列相除会差 1 万倍，**而结果看着像个比例**。
- `IS_EQUITY_PLEDGE_REPO` 标记是不是股票质押式回购。
"""),
    ('equity_restricted', 'get_equity_restricted', '限售股解禁', '解禁日期',
     'info', 'future', 200, """
- 🔴 **终点必须伸到未来**（本脚本用 20991231）。按解禁日期过滤，而解禁日在未来
  —— `end_date=今天` 会把未来的解禁计划整段丢掉，**而这不报错**，
  只是那张表看着"最近没有解禁"。
- `SHARE_LST_IS_ANN` 0=预测值 / 1=实际公布值 —— 🔴 未来的那些是**预测**，
  当成确定值用会得到一个看着正常的错数。
- `SHARE_LST_MARKET_VALUE` = `SHARE_LST` × `CLOSE_PRICE`（前日收盘价），
  所以未来行的市值是按**今天的价**估的，会随下次取数变化。
"""),
]


def specs(args, cfg):
    import time as _t
    today = int(_t.strftime('%Y%m%d'))
    codes = ac.universe(args, cfg, 'EXTRA_STOCK_A_SH_SZ', ac.MARKET_START, args.end) \
        if cfg else ['DRYRUN%06d' % i for i in range(5000)]
    out = []
    for tbl, meth, cn, sem, s0, e0, size, note in TABLES:
        start = args.start or (ac.INFO_START if s0 == 'info' else ac.MARKET_START)
        end = args.end or (ac.FUTURE_END if e0 == 'future' else today)
        size = ac.chunk_size(args, size)

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
            params='%s；沪深 A 股 %d 只；%s ~ %s' % (cn, len(codes), start, end),
            date_sem='**%s**（手册 3.5.6.x 的入参表）' % sem,
            shard_by='每 %d 个代码一片' % size,
            shards=ac.shards_codes(codes, size,
                                   extra={'begin_date': start, 'end_date': end}),
            fetch=make(),
            note=note.strip()))
    return out


if __name__ == '__main__':
    ac.main_for(specs, '④ 股东股本数据')
