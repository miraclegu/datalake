#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""⑤ 股东权益数据（手册 3.5.7）—— 分红 / 配股。

    python pull_05_equity.py

| 表 | 接口 | `begin_date`/`end_date` | 用途 |
|---|---|---|---|
| `dividend` | `get_dividend` | **公告日期** | 股息率、红利策略、除权除息 |
| `right_issue` | `get_right_issue` | **公告日期** | 配股（会摊薄股本） |

## 🔴 分红这张表本项目最在意，三条口径必须照抄不许自创

本项目的 `assay/alerts.py`（买点清单）与红利策略已经把分红口径定过案，
接这份数据时**必须与那套口径对齐**，否则同一只票会算出两个股息率：

1. **按 `DIV_PROGRESS` 过滤进度**（附录 4.1.10：1 董事会预案 / 2 股东大会通过
   / 3 实施 / 4 未通过 / 12 停止实施 / 17 股东提议 / 19 董事会预案预披露）。
   🔴 同一次分红会**多次公告**（预案→股东大会→实施），
   不去重就会把一次分红算成三次 —— 本项目的判据是同一
   `(code, report_date, bonus_type)` **留流程最靠后那条**。
2. **每股派息用税前**（`DVD_PER_SHARE_PRE_TAX_CASH`）。税后那列
   （`DVD_PER_SHARE_AFTER_TAX_CASH`）随持有人身份变，不能当口径。
3. **默认口径是 `fy`（最近一个完整会计年度合计）**，不是近 365 天。
   🔴 `r365` 在**半年派**的公司上会漏中期分红（2024 年后大量银行/央企改半年派）：
   实测海尔智家 1.1607 vs 0.8915（少 26%）、国电电力 0.241 vs 0.141（少 41%）
   —— **而它不报错**，只是目标价算高、那一行永远不会触发。
   这张表里对应的是按 `REPORT_PERIOD`（分红年度）归拢。

## 关键日期有四个，不要混

`DATE_EQY_RECORD` 股权登记日 / `DATE_EX` 除权除息日 / `DATE_DVD_PAYOUT` 派息日
/ `LISTINGDATE_OF_DVD_SHR` 红股上市日。做 PIT 用 `ANN_DATE`（公告日）；
算复权用 `DATE_EX`；算"钱什么时候到账"用派息日。
★ 本项目 `live/` 的现金流水里分红到账走 `cashflows.jsonl`，用的是派息日。
"""
import os
import sys

# 从任何目录都能跑：`python datalake/raw/amazing/_ingest/pull_0X.py`
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import amazing_common as ac

TABLES = [
    ('dividend', 'get_dividend', '分红数据', """
- 🔴 **一次分红多条记录**：`DIV_PROGRESS` 是进度码（预案/股东大会/实施…），
  `IS_CHANGED` 标记方案是否变更过。按 `(code, REPORT_PERIOD, 进度)` 去重、
  留最靠后那条 —— 与本项目 `alerts.suggest_div` 同一判据。
- 🔴 每股派息取**税前** `DVD_PER_SHARE_PRE_TAX_CASH`；单位是元/股
  （不是每 10 股 —— 东财那份 `bonus_ratio_rmb` 才是每 10 股，除 10 才可比。
  两个源混用时这一步错了会让分红大 10 倍，**而它不报错**）。
- `DVD_PER_SHARE_STK` 每股送转、`DIV_BONUSRATE` 每股送股比例、
  `DIV_CONVERSEDRATE` 每股转增比例 —— 送转不是现金分红，别加进股息里。
- `DIV_BASESHARE` 基准股本的单位是**万股**。
- 四个日期：股权登记日 / 除权除息日 / 派息日 / 红股上市日，用途见文件头。
"""),
    ('right_issue', 'get_right_issue', '配股数据', """
- 配股会**摊薄股本**，所以它和分红一样要进复权因子的计算。
- `PROGRESS` 是配股进度码（附录 4.1.11，26 种，比分红那套多得多：
  证监会核准/发审委通过/提交注册…）。🔴 同样是**一次配股多条记录**，
  不筛进度会把一次配股数成十几次。
- `RATIO_DENOMINATOR` / 配股比例这类字段要连着看才知道"每 10 股配几股"。
"""),
]


def specs(args, cfg):
    import time as _t
    today = int(_t.strftime('%Y%m%d'))
    start = args.start or ac.INFO_START
    # 分红/配股按**公告日**过滤，公告日不会在未来，所以终点取今天
    end = args.end or today
    codes = ac.universe(args, cfg, 'EXTRA_STOCK_A_SH_SZ', ac.MARKET_START, args.end) \
        if cfg else ['DRYRUN%06d' % i for i in range(5000)]
    size = ac.chunk_size(args, 200)      # = QueryPara.req_dividend_len / req_right_issue_len

    out = []
    for tbl, meth, cn, note in TABLES:
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
            params='%s；沪深 A 股 %d 只；公告日 %s ~ %s' % (cn, len(codes), start, end),
            date_sem='**公告日期**（手册 3.5.7.x）',
            shard_by='每 %d 个代码一片' % size,
            shards=ac.shards_codes(codes, size,
                                   extra={'begin_date': start, 'end_date': end}),
            fetch=make(),
            note=note.strip()))
    return out


if __name__ == '__main__':
    ac.main_for(specs, '⑤ 股东权益数据')
