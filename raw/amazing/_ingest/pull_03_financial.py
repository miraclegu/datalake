#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""③ 财务数据（手册 3.5.5）—— 资产负债表 / 现金流量表 / 利润表 / 业绩快报 / 业绩预告。

    python pull_03_financial.py
    python pull_03_financial.py --only balance_sheet --chunk 20   # 老超时就把片切小

| 表 | 接口 | 列数 |
|---|---|---|
| `balance_sheet` | `get_balance_sheet` | 179 |
| `cash_flow` | `get_cash_flow` | 120 |
| `income` | `get_income` | 111 |
| `profit_express` | `get_profit_express` | 33（业绩快报） |
| `profit_notice` | `get_profit_notice` | 15（业绩预告） |

## 🔴 `STATEMENT_TYPE` 有 26+ 种，不指定就是混合口径

同一个 `(code, REPORTING_PERIOD)` 会有**多行**，靠 `STATEMENT_TYPE` 区分
（附录 4.1.9 列了 40 多个码）。**取数时接口不让你指定**，所以 raw 层
原样全存，口径在 `std` 层选。常用的四个：

| 码 | 含义 | 用途 |
|---|---|---|
| `1` | 合并报表 | 默认口径 |
| `2` | 合并报表(单季度) | **官方算好的单季**（= 本期 − 上一季），不用自己减 |
| `4` | 合并报表(调整) | 本年度公布的上年同期数 |
| `5` | **合并报表(更正前)** | 出更正公告后，原记录改成"更正前"，更正后的另存一条 |

★ `5` 正对上本项目那条已知缺陷（README：财报重述被压平，信息滞后中位数
47 天）—— **它保留了版本**，而本地把同一 `(code, end_date)` 的多次公告压平了。
🔴 但反过来：**不筛 `STATEMENT_TYPE` 就直接 join 会一行变多行**，
而那不报错，只是市值、ROE 这些除法全被放大了几倍。

## 🔴 PIT：`ANN_DATE` 与 `ACTUAL_ANN_DATE` 是两个东西

- `ANN_DATE` = 公告日期（首次披露该事件的日期）
- `ACTUAL_ANN_DATE` = **实际数据来源公告的日期**（更正发生公告的日期）

做 as-of 过滤要用能表达"这一行在那天是否可见"的那个。**差一天就是未来函数**
—— 接进来之前必须与聚宽的 `pub_date` 逐条对数（见 `raw/amazing/README.md`
「接进来之前必须对的三样数」）。

## begin_date / end_date 在这里是【报告期】，不是公告日

手册 3.5.5.1 明写"报告期"。所以本脚本的默认区间是 `19901231 ~ 今年年末`
—— 用交易日区间去卡会把老报告期整段漏掉，**而这不报错**。
"""
import os
import sys

# 从任何目录都能跑：`python datalake/raw/amazing/_ingest/pull_0X.py`
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import amazing_common as ac

# 三张大表列多（179/120/111），一片给少一点；快报/预告列少，可以多给
BIG = ('balance_sheet', 'cash_flow', 'income')
DEFAULT_CHUNK_BIG = 25
DEFAULT_CHUNK_SMALL = 200

TABLES = [
    ('balance_sheet', 'get_balance_sheet', '资产负债表'),
    ('cash_flow', 'get_cash_flow', '现金流量表'),
    ('income', 'get_income', '利润表'),
    ('profit_express', 'get_profit_express', '业绩快报'),
    ('profit_notice', 'get_profit_notice', '业绩预告'),
]

NOTE_COMMON = """
- `begin_date` / `end_date` 在这个接口里是**报告期**（手册 3.5.5.x），
  不是公告日、不是交易日。默认 %s ~ %s。
- 返回 `dict[code] -> DataFrame`（手册在某几节写的是 dataframe，实测 pyc 里
  是 dict；本脚本两种都接，见 `amazing_common.to_long`）。
- 🔴 **同一 `(code, REPORTING_PERIOD)` 有多行**，靠 `STATEMENT_TYPE` 区分
  （26+ 种，附录 4.1.9）。raw 层原样全存；用之前必须先筛口径，
  否则 join 会一行变多行，**而这不报错**。
- 🔴 PIT 用 `ACTUAL_ANN_DATE`（实际公告日）而不是 `ANN_DATE`（首次披露日）；
  两者差一天就是未来函数。接进来之前与聚宽 `pub_date` 对数。
- `REPORT_TYPE` 是报告期名称（1=3月 2=6月 3=9月 4=12月，附录 4.1.8）。
"""


def specs(args, cfg):
    start = args.start or ac.INFO_START
    # 报告期不会是未来（业绩预告的报告期也是当年年末），所以终点取今年年末
    import time as _t
    end = args.end or int('%d1231' % _t.localtime().tm_year)
    codes = ac.universe(args, cfg, 'EXTRA_STOCK_A_SH_SZ', ac.MARKET_START, args.end) \
        if cfg else ['DRYRUN%06d' % i for i in range(5000)]

    out = []
    for tbl, meth, cn in TABLES:
        size = ac.chunk_size(args, DEFAULT_CHUNK_BIG if tbl in BIG else DEFAULT_CHUNK_SMALL)

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
            params='%s；沪深 A 股 %d 只；报告期 %s ~ %s' % (cn, len(codes), start, end),
            date_sem='**报告期**（手册 3.5.5.x）',
            shard_by='每 %d 个代码一片（列多的表切小；`--chunk` 可再调）' % size,
            shards=ac.shards_codes(codes, size,
                                   extra={'begin_date': start, 'end_date': end}),
            fetch=make(),
            note=(NOTE_COMMON % (start, end)).strip() + extra_note(tbl)))
    return out


def extra_note(tbl):
    return {
        'balance_sheet': """
- `COMP_TYPE_CODE` 决定该看哪些字段：1=非金融 2=银行 3=保险 4=证券。
  🔴 银行/保险/证券的科目与非金融完全不同（`DEPOSIT_TAKING` /
  `LIFE_INSUR_RESV` / `ACT_TRADING_SEC`），不分公司类型直接汇总
  "总资产"是能算出数的，**只是那个数没有意义**。
- `CAP_STOCK`（股本）/ `TOT_SHARE` 的备注写"金额（元），公布值"。
""",
        'cash_flow': """
- 有 `FREE_CASH_FLOW`（自由现金流）这种**加工过的**字段，不是原始报表科目
  —— 用它之前先确认口径（手册没写公式）。
- `IS_CALCULATION` 标记该行是不是算出来的。
""",
        'income': """
- 🔴 `ADJ_PREV_YEAR_LOSS_GAIN` 这一列**手册的字段表里没有**（wheel 的
  `columns_list` 里有，说明接口会返回它）—— 见 `field_docs.py` 里的标注。
- `TOT_OPERA_COST` 与 `TOT_OPERA_COST2` 两列并存，含义要自己对数区分。
""",
        'profit_express': """
- 业绩快报：正式财报之前先出的那一份。字段是 `float64` 型的汇总指标
  （总资产/净利润/营业总收入/EPS/ROE + 一堆同比增长率）。
- ★ 它与正式报表**同一个报告期会各有一行**（在不同的表里）。做 PIT 时
  快报先可见、正式报表后可见 —— 这正是本项目"信息滞后中位数 47 天"
  那条缺陷的来源。
""",
        'profit_notice': """
- 业绩预告：只有区间（`NET_PROFIT_MAX` / `NET_PROFIT_MIN` 之类）和类型描述，
  **不是确定值**。拿它当净利润用会得到一个"看着正常"的错值。
- `FIRST_ANN_DATE` 是首次预告日 —— 预告会改，一个报告期可能有多条。
""",
    }.get(tbl, '')


if __name__ == '__main__':
    ac.main_for(specs, '③ 财务数据')
