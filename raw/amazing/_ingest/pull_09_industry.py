#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""⑨ 行业指数数据（手册 3.5.13）—— 基本信息 / 成分股 / 成分股日权重 / 日行情。

    python pull_09_industry.py
    # 后三张表的代码列表来自第一张，所以第一张失败时后面会**明确报错退出**，
    # 而不是拿空列表跑出一堆 0 行

| 表 | 接口 | 说明 |
|---|---|---|
| `industry_base_info` | `get_industry_base_info` | 行业指数清单（**后三张表的输入**） |
| `industry_constituent` | `get_industry_constituent` | 成分股，**带 INDATE/OUTDATE** |
| `industry_weight` | `get_industry_weight` | 成分股日权重 |
| `industry_daily` | `get_industry_daily` | 行业指数**日行情真实点位** |

## 🔴 这四张表有依赖关系，且手册明写只能这么来

3.5.13.2/3/4 的 `code_list` 都写着「**仅从 `get_industry_base_info` 取到的
指数代码**」。所以本脚本先跑 `industry_base_info`，把 `INDEX_CODE` 存进
`_ingest/state/industry_codes.json`，后三张表读它。
★ 拿股票代码或申万代码去传是能跑通的 —— **只是全部返回空**，
而那看着像"这个行业没有成分"。

## 🔴 `LEVEL_TYPE` / `LEVEL1~3_NAME`：手册没写这是哪一套分类

全文搜"申万"零命中。字段只有 `LEVEL_TYPE`（指数类别）+ `LEVEL1_NAME` /
`LEVEL2_NAME` / `LEVEL3_NAME`。**接进本项目之前必须实测对数**：
如果不是申万一级，那红利策略的行业中性化与板块页的口径就变了
—— 见 `raw/amazing/README.md`「接进来之前必须对的三样数」。

## ★ `industry_daily` 补上本项目一条明写的缺口

`assay/CLAUDE.md` 里写着「涨幅是成分**等权平均**…**本地没有板块指数点位**」。
这张表给的是真实点位：`OPEN/HIGH/LOW/CLOSE/PRE_CLOSE/AMOUNT/VOLUME/PB/PE/
TOTAL_CAP/A_FLOAT_CAP`。有了它，板块页的涨幅可以从"等权近似"换成真实指数。
🔴 但**换之前要知道两者不一样**：等权 ≠ 市值加权指数，历史上的板块榜会重排。

## 🔴 `industry_daily` 的批量上限是 2（不是 50、不是 200）

`QueryPara.req_industry_daily_len = 2` —— SDK 内部每次只发 2 个代码。
所以这张表的请求数 = 指数数 × 年数 / 2，是九个脚本里**请求最密的一张**。
本脚本每片给 2 个代码 × 1 年，并建议配 `--sleep 0.2` 跑（怕限流）。
"""
import io
import json
import os
import sys

# 从任何目录都能跑：`python datalake/raw/amazing/_ingest/pull_0X.py`
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import amazing_common as ac

def codes_cache():
    """行业指数代码清单的缓存路径。

    ★ 写成函数而不是模块级常量：模块级会在 **import 的那一刻**把
      `ac.STATE_DIR` 的值抄走，之后谁改了 STATE_DIR（自证就要改，
      为的是别把测试数据写进真 state 目录）都不会跟着变 ——
      **这类"名字绑早了"的错不报错**，只是文件落在别处。
      与 CLAUDE.md 里 `serve.py` 拆分踩到的"名字遮蔽"是同一类。
    """
    return os.path.join(ac.STATE_DIR, 'industry_codes.json')


def industry_codes(args, cfg):
    """行业指数代码清单：先读缓存，没有就现取（并存下来）。

    🔴 取不到就**抛错退出**，不返回空列表 —— 返回空的话后三张表会
    "成功地"跑出 0 片、0 行，报告上一片绿，而那正是最难发现的坏。
    """
    if os.path.exists(codes_cache()) and not args.force:
        with io.open(codes_cache(), encoding='utf-8') as f:
            return json.load(f)['codes']
    if not cfg:
        return ['DRYRUN%06d' % i for i in range(120)]
    a = ac.api(cfg, args.sdk_cache)
    res = a['info'].get_industry_base_info(local_path=a['sdk_path'], is_local=False)
    df, _ = ac.to_long(res, 'industry_base_info')
    if df is None or 'INDEX_CODE' not in df.columns:
        raise SystemExit(
            'get_industry_base_info 没给出 INDEX_CODE —— 后三张表没法取。\n'
            '先单独跑 `python pull_09_industry.py --only industry_base_info` 看看它返回什么。\n'
            '（这个接口的 is_local 语义是"只看本地"，第一次必须 False，本脚本已经传了 False。）')
    codes = sorted(set(str(c) for c in df['INDEX_CODE'].dropna().tolist()))
    ac.ensure_dir(ac.STATE_DIR)
    with io.open(codes_cache(), 'w', encoding='utf-8') as f:
        f.write(json.dumps({'asof': ac.now(), 'codes': codes}, ensure_ascii=False))
    ac.say('行业指数 %d 个（已存 state/industry_codes.json）' % len(codes))
    return codes


def specs(args, cfg):
    import time as _t
    today = int(_t.strftime('%Y%m%d'))
    start = args.start or ac.MARKET_START
    end = args.end or today

    def fetch_base():
        a = ac.api(cfg, args.sdk_cache)
        return a['info'].get_industry_base_info(local_path=a['sdk_path'], is_local=False)

    out = [dict(
        name='industry_base_info',
        api='InfoData.get_industry_base_info(local_path, is_local=False)',
        params='无 code_list',
        date_sem='无日期参数',
        shard_by='一片',
        shards=[('all', {})],
        fetch=fetch_base,
        note="""
- 🔴 `is_local` **必须 False**（同 `index_constituent`：这个接口的 True 是
  "只看本地"，第一次跑传 True 拿到的是空，**而这不报错**）。
- 🔴 **这张表是后三张表的输入**（手册 3.5.13.2/3/4 都写"仅从
  get_industry_base_info 取到的指数代码"）。落盘后代码清单存进
  `_ingest/state/industry_codes.json` 供后三张复用。
- 🔴 `LEVEL_TYPE` / `LEVEL1_NAME` / `LEVEL2_NAME` / `LEVEL3_NAME`
  —— **手册没写这是哪一套分类**（全文搜"申万"零命中）。
  接进本项目之前必须与申万一级对数：不是申万的话红利策略的行业中性化
  与板块页的口径就变了。
- `IS_PUB` 是否发布、`CHANGE_REASON` 变动原因。
""".strip())]

    # 只是列任务时不必去取代码清单
    if args.only and 'industry_base_info' in [x.strip() for x in args.only.split(',')] \
            and len([x for x in args.only.split(',') if x.strip()]) == 1:
        return out

    codes = industry_codes(args, cfg)

    def fetch_cons(code_list):
        a = ac.api(cfg, args.sdk_cache)
        return a['info'].get_industry_constituent(
            code_list, local_path=a['sdk_path'], is_local=False)

    def fetch_weight(code_list, begin_date, end_date):
        a = ac.api(cfg, args.sdk_cache)
        return a['info'].get_industry_weight(
            code_list, local_path=a['sdk_path'], is_local=False,
            begin_date=int(begin_date), end_date=int(end_date))

    def fetch_daily(code_list, begin_date, end_date):
        a = ac.api(cfg, args.sdk_cache)
        return a['info'].get_industry_daily(
            code_list, local_path=a['sdk_path'], is_local=False,
            begin_date=int(begin_date), end_date=int(end_date))

    out += [
        dict(
            name='industry_constituent',
            api='InfoData.get_industry_constituent(code_list, local_path, is_local=False)',
            params='行业指数 %d 个' % len(codes),
            date_sem='无日期参数 —— 用 `INDATE`/`OUTDATE` 表达区间',
            shard_by='每 %d 个指数一片' % ac.chunk_size(args, 200),
            shards=ac.shards_codes(codes, ac.chunk_size(args, 200)),
            fetch=fetch_cons,
            note="""
- **带 `INDATE`（纳入日）/ `OUTDATE`（剔除日）** —— 这是它比本项目现有做法
  强的地方：本项目现在是从 285,781 条 as-of 快照**推**出 8,748 条区间。
- 🔴 与 `index_constituent` 同一条纪律：手册说剔除日期会被最新数据改写，
  所以**每次全量重取、不要增量合并**（增量会留下永不剔除的僵尸成分）。
- 🔴 `is_local` 必须 False。
""".strip()),
        dict(
            name='industry_weight',
            api='InfoData.get_industry_weight(code_list, begin_date, end_date)',
            params='行业指数 %d 个；%s ~ %s' % (len(codes), start, end),
            date_sem='**交易日期**（手册 3.5.13.3）',
            shard_by='年 × 每 %d 个指数（SDK 内部上限 50）' % ac.chunk_size(args, 50),
            shards=ac.shards_codes_years(codes, ac.chunk_size(args, 50), start, end),
            fetch=fetch_weight,
            note="""
- 四列：`INDEX_CODE` / `CON_CODE` / `TRADE_DATE` / `WEIGHT`。日频权重。
- 量不小：指数数 × 成分数 × 交易日。按年分片。
""".strip()),
        dict(
            name='industry_daily',
            api='InfoData.get_industry_daily(code_list, begin_date, end_date)',
            params='行业指数 %d 个；%s ~ %s' % (len(codes), start, end),
            date_sem='**交易日期**（手册 3.5.13.4）',
            shard_by='年 × 每 %d 个指数（🔴 SDK 内部上限就是 2：'
                     'QueryPara.req_industry_daily_len）' % ac.chunk_size(args, 2),
            shards=ac.shards_codes_years(codes, ac.chunk_size(args, 2), start, end),
            fetch=fetch_daily,
            note="""
- **行业指数的真实点位**：`OPEN/HIGH/LOW/CLOSE/PRE_CLOSE` + `AMOUNT`（元）
  / `VOLUME`（股）+ `PB`/`PE` + `TOTAL_CAP`/`A_FLOAT_CAP`（**万元**）。
- ★ 它补上本项目明写的一条缺口（`assay/CLAUDE.md`：「本地没有板块指数点位」，
  板块涨幅现在是成分**等权平均**）。🔴 换成真实指数之前要知道
  **等权 ≠ 市值加权**，历史上的板块榜会重排 —— 那不是 bug。
- 🔴 `TOTAL_CAP` / `A_FLOAT_CAP` 的单位是**万元**，`AMOUNT` 是元。
  混着算市值加权会差 1 万倍，**而结果看着仍然像个权重**。
- 🔴 **这张表请求最密**：SDK 内部一次只发 2 个代码
  （`req_industry_daily_len=2`）。请求数 = 指数数 × 年数 / 2。
  怕限流就配 `--sleep 0.2`。
""".strip()),
    ]
    return out


if __name__ == '__main__':
    ac.main_for(specs, '⑨ 行业指数数据')
