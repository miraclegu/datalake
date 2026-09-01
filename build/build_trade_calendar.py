#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""生成【含未来】的交易日历 -> assay/live/trade_calendar.json。

    python3 datalake/build/build_trade_calendar.py

## 为什么本地能推出未来交易日

`tdx.db` 有 `raw_holidays` 表（休市日清单，1991-01-01 ~ 2030-10-07，680 行）。
交易日 = 工作日 − 休市日。

★ 这条规则【已对数验证】：按它生成 2003-01-02 ~ 2026-08-31 的日历，与
  `std/trading_calendar.parquet`（从行情反推的权威日历，5745 天）
  **逐日完全一致，0 漏 0 多**。所以外推到 2030 可信。

## 为什么不用聚宽的 get_all_trade_days

用得着才用。tdx 这张表本地就有、覆盖到 2030、且能对数验证 —— 而聚宽那条
路要人进研究环境、导出、下载、合并四步。
（`extract_jq_increment.py` 里的 `grab_calendar` 保留着当交叉校验用。）

## 消费方

`assay/live.py` 判断"下一个交易日是哪天"才知道今天要不要调仓。
`PanelFeed.trading_days` 来自**面板**（有行情的日子），永远不含未来。
拿不到日历 live 就响亮报错 —— 所以这个脚本要进每日同步链。
"""
import datetime
import json
import os

import duckdb

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TDX = os.path.join(os.path.dirname(ROOT), 'tdx2db', 'tdx.db')
TRUTH = os.path.join(ROOT, 'std', 'trading_calendar.parquet')
OUT = os.path.join(os.path.dirname(ROOT), 'assay', 'live', 'trade_calendar.json')
START = datetime.date(2003, 1, 2)


def main():
    con = duckdb.connect()
    con.execute("ATTACH '%s' AS tdx (READ_ONLY)" % TDX)
    hol = {r[0] for r in con.execute('SELECT date FROM tdx.raw_holidays').fetchall()}
    hmax = max(hol)
    truth = [r[0] for r in con.execute(
        "SELECT date FROM read_parquet('%s') ORDER BY date" % TRUTH).fetchall()]
    if not truth:
        raise SystemExit('权威日历为空：%s' % TRUTH)
    lo, hi = truth[0], truth[-1]

    def gen(a, b):
        out, d = [], a
        while d <= b:
            if d.weekday() < 5 and d not in hol:
                out.append(d)
            d += datetime.timedelta(days=1)
        return out

    # ★ 每次都重跑对数，不是一次性验证 —— raw_holidays 是 type-1 覆盖写的表，
    #   上游改了口径而我们照用，会静默给出错的调仓日。
    chk = gen(max(lo, START), hi)
    ts, gs = set(truth), set(chk)
    if ts != gs:
        raise SystemExit(
            '❌ 对数失败，拒绝写出：\n'
            '   权威有而生成无（漏判休市）%d 天: %s\n'
            '   生成有而权威无（多判交易）%d 天: %s\n'
            '   —— 规则「工作日 − raw_holidays」不再成立，先查 tdx.raw_holidays'
            % (len(ts - gs), sorted(ts - gs)[:10],
               len(gs - ts), sorted(gs - ts)[:10]))

    days = sorted(ts | set(gen(hi + datetime.timedelta(days=1), hmax)))
    fut = [d for d in days if d > hi]
    obj = {
        'days': [d.isoformat() for d in days],
        'source': 'tdx.raw_holidays',
        'updated': datetime.date.today().isoformat(),
        'authoritative_until': hi.isoformat(),
        'max': days[-1].isoformat(),
        'n_future': len(fut),
        'verified': ('工作日−raw_holidays 与 std/trading_calendar.parquet '
                     '逐日一致 %d 天（%s ~ %s），0 漏 0 多' % (len(truth), lo, hi)),
    }
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    tmp = OUT + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(obj, f, ensure_ascii=False)
    os.replace(tmp, OUT)
    print('✅ 对数通过：%d 天与权威日历逐日一致（%s ~ %s）' % (len(truth), lo, hi))
    print('   写出 %s' % os.path.relpath(OUT, os.path.dirname(ROOT)))
    print('   共 %d 天，未来 %d 天，最远 %s' % (len(days), len(fut), obj['max']))


if __name__ == '__main__':
    main()
