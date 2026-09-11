#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""两条腿的新鲜度 —— 同步脚本与 Web 看板共用的**单一事实来源**。

    python3 datalake/build/sync_status.py                    # 打印
    python3 datalake/build/sync_status.py --write <path>     # 顺便落 JSON
    python3 datalake/build/sync_status.py --json             # 只输出 JSON

## 为什么单独一个文件

新鲜度判据写两遍必然漂移（脚本说"没问题"、页面说"落后 3 天"）。
这里是唯一实现，`sync_daily.sh` 和 `assay/server.py` 都调它。

## 为什么这件事必须可见

数据字典 4-按陷阱索引.md 的 **E-0「数据新鲜度错配」**：tdx 行情与聚宽财务
是**两条独立链**。行情天天新、财务停在三个月前时，回测照跑、报告看着完全
正常，而选股用的是过期财报。实测代价：2026 中报只覆盖 55.6% 时，froec
同日选股与聚宽只对上 6/10；补完中报后升到 9/10。
"""
import argparse
import datetime
import json
import os

import duckdb

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TDX = os.path.join(os.path.dirname(ROOT), 'tdx2db', 'tdx.db')
CAL = os.path.join(os.path.dirname(ROOT), 'assay', 'live', 'trade_calendar.json')

# (键, 人读名, 属于哪条腿, SQL)
CHECKS = [
    ('tdx_kline', 'tdx.db 日线', 'A',
     'SELECT max(date) FROM t.raw_kline_daily'),
    ('panel', '面板 mart/panel_daily', 'A',
     "SELECT max(date) FROM read_parquet('{R}/mart/panel_daily/panel_*.parquet')"),
    ('beta', 'std/beta_daily', 'A',
     "SELECT max(date) FROM read_parquet('{R}/std/beta_daily.parquet')"),
    ('fin_indicator', '财务指标 pub_date', 'B',
     "SELECT max(pub_date) FROM read_parquet('{R}/std/fin_indicator_q.parquet')"),
    ('dividend', '分红 board_plan_pub_date', 'B',
     "SELECT max(board_plan_pub_date) FROM read_parquet('{R}/std/dividend.parquet')"),
    ('share_change', '股本变动 pub_date', 'B',
     "SELECT max(pub_date) FROM read_parquet('{R}/std/share_change.parquet')"),
    # ★ 三大报表此前【完全没被监控】—— 实测卡在 2026-08-24 而指标已到 08-31，
    #   差 5 个交易日，而没有任何地方会说出来。
    #   两条实盘策略（红利指数增强 / froec_traded）不用这三张表（只用
    #   t_indicator / t_dividend / t_beta / t_universe），但 红利价值.py 与
    #   build_jqfactor_q.py 用，所以滞后仍然会静默影响那条线。
    ('fin_income', '利润表 pub_date', 'B',
     "SELECT max(pub_date) FROM read_parquet('{R}/raw/jq/financials/income.parquet')"),
    ('fin_balance', '资产表 pub_date', 'B',
     "SELECT max(pub_date) FROM read_parquet('{R}/raw/jq/financials/balance.parquet')"),
    ('fin_cashflow', '现金流量表 pub_date', 'B',
     "SELECT max(pub_date) FROM read_parquet('{R}/raw/jq/financials/cashflow.parquet')"),
    ('fin_forcast', '业绩预告 pub_date', 'B',
     "SELECT max(pub_date) FROM read_parquet('{R}/raw/jq/stk_fin_forcast.parquet')"),
]


def _cal():
    try:
        with open(CAL, encoding='utf-8') as f:
            d = json.load(f)
        return [datetime.date.fromisoformat(x) for x in d['days']], d
    except Exception:                                       # noqa: BLE001
        return [], {}


def collect():
    con = duckdb.connect()
    try:
        con.execute("ATTACH '%s' AS t (READ_ONLY)" % TDX)
    except Exception:                                       # noqa: BLE001
        pass
    days, cmeta = _cal()
    today = datetime.date.today()
    # 最近一个【已收盘】交易日：今天若是交易日且已过 15:30 才算上今天
    past = [d for d in days if d < today]
    now = datetime.datetime.now()
    if today in set(days) and (now.hour, now.minute) >= (15, 30):
        past.append(today)
    expect = past[-1] if past else None

    items = []
    for key, name, leg, sql in CHECKS:
        try:
            v = con.execute(sql.format(R=ROOT)).fetchone()[0]
            # ★ 各表的日期列类型不一：parquet 里可能是 DATE/TIMESTAMP，
            #   也可能是 VARCHAR（stk_fin_forcast 全列 str）。都归一成 date。
            if hasattr(v, 'date'):
                v = v.date()
            elif isinstance(v, str):
                v = datetime.date.fromisoformat(v[:10]) if v[:4].isdigit() else None
        except Exception as e:                              # noqa: BLE001
            items.append({'key': key, 'name': name, 'leg': leg, 'max': None,
                          'lag_days': None, 'error': str(e)[:120]})
            continue
        # ★ 只有 A 腿（行情）才有"落后 N 个交易日"这个概念：行情每个交易日
        #   必然有新数据，缺了就是同步没跑。B 腿（财务/分红/股本）是
        #   **事件驱动**的 —— 没有公司发公告的日子本来就不该有新 pub_date，
        #   按交易日算落后是【必然误报】。B 腿只报"距今几天"。
        lag = None
        if leg == 'A' and v and expect:
            lag = len([d for d in days if v < d <= expect])
        row = {'key': key, 'name': name, 'leg': leg,
               'max': v.isoformat() if v else None, 'lag_days': lag}
        if leg == 'B' and v:
            row['days_since'] = (today - v).days
        items.append(row)

    a = [i for i in items if i['leg'] == 'A' and i['lag_days'] is not None]
    b = [i for i in items if i['leg'] == 'B' and i['lag_days'] is not None]
    # ★ B 腿的"落后"不按交易日算 —— 财务是【事件驱动】的，没有公告的日子
    #   本来就不该有新 pub_date。所以看的是"距今自然日"，只在明显偏大时告警。
    bmax = max((datetime.date.fromisoformat(i['max']) for i in b), default=None)
    return {
        'checked_at': now.replace(microsecond=0).isoformat(),
        'expect_trade_day': expect.isoformat() if expect else None,
        'calendar': {'source': cmeta.get('source'), 'max': cmeta.get('max'),
                     'authoritative_until': cmeta.get('authoritative_until')},
        'items': items,
        'leg_a_lag': max((i['lag_days'] for i in a), default=None),
        'leg_b_days_since': (today - bmax).days if bmax else None,
        'extract': _extract_info(today),
        'mismatch': None,
    }


def _extract_info(today):
    """读 `_manifest/jq_extract.json` —— 「**上一次从聚宽抽数**是什么时候」。

    🔴 **为什么必须单独一维**：B 腿是**事件驱动**的，没公告的日子 `pub_date`
      不前进。于是「距今 11 天」这一个数**分不出两种情况**：
        ① 你昨天刚导，只是这 11 天里没有新公告   <- 正常
        ② 你两周没导了                          <- 要去导
      用户原话：「实际上财务数据我昨天已经导入了最新的，但是上面的最新时间
      不会更新，显得我好像没有更新一下。」
    ★ 这个时点**只有抽取端知道**（聚宽研究环境跑脚本时的 now），本地无法
      反推 —— 所以由 extract 脚本写进包、merge 时落到这里。
    ★ 老包没有这个文件 -> 返回 None，页面显示「未知（旧格式包）」，
      **不要猜**（比如拿文件 mtime 当抽取时刻 —— 那是下载/解压时刻）。
    """
    p = os.path.join(ROOT, '_manifest', 'jq_extract.json')
    if not os.path.exists(p):
        return None
    try:
        with open(p, encoding='utf-8') as f:
            d = json.load(f)
    except Exception:                                       # noqa: BLE001
        return None
    out = {'extracted_at': d.get('extracted_at'),
           'extract_date': d.get('extract_date'),
           'merged_at': d.get('merged_at'),
           'data_max_date': d.get('data_max_date'),
           'since': d.get('since'), 'tar': d.get('tar'),
           'recovered': d.get('recovered')}
    ed = (d.get('extract_date') or (d.get('extracted_at') or '')[:10])
    if ed:
        try:
            out['days_since_extract'] = (
                today - datetime.date.fromisoformat(ed)).days
        except ValueError:
            pass
    return out


def _fmt(st):
    out = []
    out.append('  应到交易日 %s   日历来源 %s（权威到 %s）'
               % (st['expect_trade_day'], st['calendar'].get('source'),
                  st['calendar'].get('authoritative_until')))
    for i in st['items']:
        if i.get('error'):
            out.append('  %-1s %-26s ❌ %s' % (i['leg'], i['name'], i['error']))
            continue
        if i['leg'] == 'A':
            lag = i['lag_days']
            tag = '✅' if lag == 0 else ('⚠ 落后 %d 个交易日' % lag if lag else '?')
        else:
            ds = i.get('days_since')
            tag = ('距今 %d 天' % ds) if ds is not None else '?'
            if ds is not None and ds > 21:
                tag = '🔴 ' + tag
        out.append('  %-1s %-26s %s  %s' % (i['leg'], i['name'], i['max'], tag))
    la, bd = st['leg_a_lag'], st['leg_b_days_since']
    if la:
        out.append('  🔴 A 腿（行情）落后 %d 个交易日 —— 同步没跑成功' % la)
    if bd is not None and bd > 21:
        out.append('  🔴 B 腿（聚宽财务）距今 %d 天没更新。行情天天新、财务过期时，'
                   '回测照跑、报告看着正常，而选股用的是旧财报（数据字典 E-0）。'
                   '去聚宽研究环境跑 raw/jq/_ingest/extract_jq_increment.py' % bd)
    elif bd is not None:
        out.append('  B 腿（聚宽财务）距今 %d 天，正常（财务是事件驱动，'
                   '没公告就没有新 pub_date）' % bd)
    # 🔴 **「上次抽取」要单独说一行。** 只看数据内容的话，「距今 11 天」
    #   分不出「昨天刚导、只是没新公告」和「两周没导」—— 而这两件事
    #   要做的动作完全相反。
    ex = st.get('extract')
    if ex is None:
        out.append('  上次抽取 未知（包里没有 _manifest.json —— 旧格式包；'
                   '下次导出会带上）')
    else:
        ds = ex.get('days_since_extract')
        tag = '' if ds is None else (
            '（今天）' if ds == 0 else '（%d 天前）' % ds)
        # ★ 老包只恢复得出**日期**、没有时刻 —— 那就只写日期。
        #   占位成 '?' 是噪声：它看着像"读出来是空的"，而其实是这个字段
        #   当时根本没记。
        when = ex.get('extracted_at') or ex.get('extract_date') or '未知'
        line = '  上次抽取 %s%s' % (when, tag)
        if ex.get('data_max_date'):
            line += '   数据切到 pub_date %s' % ex['data_max_date']
        if ex.get('since'):
            line += '   SINCE=%s' % ex['since']
        out.append(line)
        if ex.get('recovered'):
            out.append('    （%s）' % ex['recovered'])
        if ex.get('merged_at'):
            out.append('  本地合并 %s%s'
                       % (ex['merged_at'],
                          '   包 ' + ex['tar'] if ex.get('tar') else ''))
        # ★ 告警判据换成**抽取时点**，而不是数据内容 —— 那才是"该不该去导"。
        if ds is not None and ds > 14:
            out.append('  🔴 已经 %d 天没从聚宽抽数了 —— 去研究环境跑 '
                       'raw/jq/_ingest/extract_jq_increment.py' % ds)
    return '\n'.join(out)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--write', default=None, help='把状态 JSON 落到这个路径')
    ap.add_argument('--json', action='store_true')
    ap.add_argument('--log', default=None, help='记进状态里的本次日志路径')
    a = ap.parse_args()
    st = collect()
    if a.log:
        st['last_log'] = a.log
    if a.write:
        os.makedirs(os.path.dirname(a.write), exist_ok=True)
        tmp = a.write + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(st, f, ensure_ascii=False, indent=1)
        os.replace(tmp, a.write)
    print(json.dumps(st, ensure_ascii=False, indent=1) if a.json else _fmt(st))


if __name__ == '__main__':
    main()
