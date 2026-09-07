#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""② 历史日 K（手册 3.5.4.2 `query_kline`）—— 股票 / 指数 / ETF / 可转债。

    python pull_02_kline_day.py                      # 股票+指数+ETF
    python pull_02_kline_day.py --kinds stock        # 只要股票
    python pull_02_kline_day.py --kinds stock,index,etf,kzz
    python pull_02_kline_day.py --start 20240101     # 只补最近

落成一张表 `kline_day`，列是 `code / kline_time / open / high / low / close /
volume / amount`（附录 4.2.6 K线 Kline）。

## 🔴 六个坑

1. **`period` 必须显式传日线。** `query_kline` 的默认值是
   `MDDatatype.k1KLine`（**1 分钟线**）—— 不传就取到分钟线，
   一天几百根，看着像"数据特别多"而不是像出错。
   本脚本传 `ad.constant.Period.day.value`（照手册示例用 `.value`）。
2. **起点 2013**（手册 §2.2）。早于它不报错，**返回空** —— 那看着像
   "这只票那时候还没上市"。本项目 tdx 那份 1990 起的**不能扔**，
   它的用途收窄为做样本外检验（2006-2007 大牛 + 2008 崩盘的样本
   全在 2013 之前，而 froec 那条 edge 正是被这段样本外否证的）。
3. **按 (年 × 500 代码) 分片。** SDK 内部就是按 500 只 / 1500 天切的
   （`QueryPara.req_kline_len=500`、`req_kline_day_date_len=1500`），
   但**一次要 13 年全量**会把几千万行攒在内存里，而且断了没有断点。
4. **`volume` 的单位是股，`amount` 是元** —— 这正是它比 tdx 那份强的地方：
   本项目在 tdx 的 volume 上栽过"少乘 100"（修了 74,952 行）。
   接进来时**不要再乘 100**，也不要拿 mart 层的口径来套。
5. **K 线是不复权的原始成交价。** 复权要自己乘因子
   （`pull_01_basic.py` 的 `backward_factor`）。
   🔴 区间涨幅一律用后复权 —— 不复权跨除权日有**假跌幅**
   （实测 601088 近 250 日：后复权 +30.9% / 不复权 +24.8%）。
6. **集合竞价与前推算法**（附录 4.3.1）：日线的开盘集合竞价成交量含在当日；
   分钟线里 9:30 那根算的是 9:30:00.000~9:30:59.999。本脚本只取日线，
   但接分钟线时这条会决定"9:30 那根到底是哪一分钟"。
"""
import os
import sys

# 从任何目录都能跑：`python datalake/raw/amazing/_ingest/pull_0X.py`
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import amazing_common as ac

KINDS = {
    # 别名 -> (security_type, 说明)
    'stock': ('EXTRA_STOCK_A', '沪深北 A 股（含已退市）'),
    'index': ('EXTRA_INDEX_A_SH_SZ', '沪深指数'),
    'etf': ('EXTRA_ETF', '沪深 ETF'),
    'kzz': ('EXTRA_KZZ', '沪深可转债'),
}
DEFAULT_KINDS = 'stock,index,etf'


def day_period(a):
    """日线的 period 值。

    🔴 `Period` **不在** `AmazingData` 的顶层导出里（顶层只有 login/logout/
    BaseData/InfoData/MarketData…），要从 `ad.constant` 或
    `AmazingData.utils.constant` 拿 —— 直接 `ad.Period` 是 AttributeError。
    """
    try:
        return a['ad'].constant.Period.day.value
    except AttributeError:
        from AmazingData.utils.constant import Period
        return Period.day.value


def specs(args, cfg):
    start = args.start or ac.MARKET_START
    end = args.end or (int(ac.api(cfg, args.sdk_cache)['calendar'][-1]) if cfg else 20261231)
    kinds = [k.strip() for k in (getattr(args, 'kinds', None) or DEFAULT_KINDS).split(',')
             if k.strip()]
    bad = [k for k in kinds if k not in KINDS]
    if bad:
        raise SystemExit('--kinds 里有不认识的：%s，可选 %s' % (bad, list(KINDS)))

    def fetch(code_list, begin_date, end_date):
        a = ac.api(cfg, args.sdk_cache)
        return a['market'].query_kline(code_list, begin_date=int(begin_date),
                                       end_date=int(end_date), period=day_period(a))

    shards, params = [], []
    for k in kinds:
        st, desc = KINDS[k]
        codes = ac.universe(args, cfg, st, start, end) if cfg \
            else ['DRYRUN%06d' % i for i in range(5000 if k == 'stock' else 800)]
        params.append('%s=%d 只（%s）' % (k, len(codes), desc))
        # 分片 id 里带品种：`part-stock-2019-0003.parquet` 一眼看出缺哪一块
        shards += ac.shards_codes_years(codes, 500, start, end, tag='%s-' % k)

    return [dict(
        name='kline_day',
        api='MarketData.query_kline(code_list, begin_date, end_date, period=Period.day.value)',
        params='%s ~ %s；%s' % (start, end, '；'.join(params)),
        date_sem='**交易日**（8 位整型，如 20240101）',
        shard_by='品种 × 年 × 每 500 个代码（= QueryPara.req_kline_len）',
        shards=shards,
        fetch=fetch,
        note="""
- 一次 `query_kline` 返回 `dict[code] -> DataFrame`，**index 是日期**，
  column 是 Kline 的八个字段。`code` 列里带市场后缀（`600000.SH`）。
- 🔴 `period` 的默认值是**1 分钟线**，必须显式传日线，见本文件头。
- 🔴 `volume` 单位是**股**、`amount` 是**元**。本项目在 tdx 的 volume 上
  栽过"少乘 100"（74,952 行），接这份时不要再套那套修正。
- K 线是**不复权**的原始成交价（`open` 就是当日竞价成交价）。
  要跨除权日比涨幅必须自己乘后复权因子。
- 起点 2013（手册 §2.2）；再早**不报错、返回空**。
""".strip())]


if __name__ == '__main__':
    import argparse
    ap = argparse.ArgumentParser(description='② 历史日 K')
    ac.add_common_args(ap)
    ap.add_argument('--retry-failed', action='store_true')
    ap.add_argument('--kinds', default=DEFAULT_KINDS,
                    help='要取哪些品种：%s（逗号分隔）' % ','.join(KINDS))
    ac.run_with_args(specs, ap.parse_args(), '② 历史日 K')
