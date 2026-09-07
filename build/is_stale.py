#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""本地行情数据齐了吗 —— 给 `sync_daily.sh --if-stale` 当判据。

    python3 datalake/build/is_stale.py     # 打印原因，用退出码回答

    退出码 0  已齐，不用跑
            1  该跑
            2  现在不该跑（非交易日 / 还没收盘）—— 与 0 一样"什么都不做"，
               但原因不同，日志里要分得开

## 为什么要这个：把「几点跑」换成「齐没齐」

原来 launchd 写死 18:10。但通达信什么时候放出当天数据是**它说了算**的，
写死时间就有两种坏法：定早了抓不到（而 tdx2db cron 不会因此报错，
只是库里没有当天的行）、定晚了白等两小时。
所以改成 **16:00 起每 10 分钟问一次「齐没齐」，不齐就试着抓**。
判据落在"数据现在是什么状态"上，而不是"到点没到点"
（同 launchd 那条：判据永远是现在的状态，不是记录）。

## 三道判据

1. **今天是交易日吗** —— 不是就什么都不做。
2. 🔴 **过了收盘吗**（默认 15:00）—— 盘中 tdx 可能给出当天**不完整**的 bar，
   抓了会写进库、再被面板构建吃进去，而**那不报错**：表现是当天的
   成交量/最高最低价偏小，隔天才被下一次全量覆盖修正。
3. **今天的行数够吗** —— 判据是"不少于上一交易日的 95%"，
   ★ 不用固定阈值：新股上市与退市会让全市场只数天天微变（本机实测
     最近 20 个交易日在 5202~5208 之间波动），固定值迟早过时，
     而过时的表现是"永远判成不齐、每 10 分钟白跑一次 cron"。
   ★ 也不能只判"有没有今天的行"：抓到一半（几百只）时也算"有"，
     那时跑面板构建就把半截数据固化进去了。
"""
import argparse
import datetime
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))   # datalake
TDX_DB = os.path.join(ROOT, 'raw', 'tdx', '_ingest', 'tdx.db')
PANEL = os.path.join(ROOT, 'mart', 'panel_daily')
CLOSE_AFTER = '15:00'          # 收盘（含集合竞价收尾）之后才认当天数据
MIN_RATIO = 0.95               # 今天的只数至少是上一交易日的这个比例


def _say(*a):
    print(*a, flush=True)


def _open_tdx():
    """打开 tdx.db（只读）。打不开就是上一轮正在抓 —— 这一轮别叠着跑。

    ★ 放在**最前面**：`tdx2db cron` 持有写锁时我们既判不了也抓不了，
      而叠着跑两个 cron 只会互相等锁。
    """
    import duckdb
    if not os.path.isfile(TDX_DB):
        return None, 'tdx.db 不存在（先跑 --bootstrap）'
    try:
        return duckdb.connect(TDX_DB, read_only=True), None
    except Exception as e:                                      # noqa: BLE001
        return None, 'tdx.db 打不开：%s' % str(e)[:90]


def _is_trading_day(d, con):
    """交易日 = 工作日 − 休市日（tdx.raw_holidays，覆盖 1991~2030）。

    🔴 **不能用 `std/trading_calendar.parquet`** —— 它只到**最后一个有数据
      的交易日**（本机 2026-09-04），而这里要判断的正是"今天"。
      用它的话每天都判成"不是交易日"，于是这个轮询**永远不干活**，
      而日志里只有一行「今天不是交易日」—— 不报错。
      （CLAUDE.md 里那条：交易日历本地就能算，raw_holidays 是唯一含未来的源。）
    """
    if d.weekday() >= 5:
        return False
    try:
        n = con.execute("SELECT count(*) FROM raw_holidays WHERE date = DATE '%s'"
                        % d).fetchone()[0]
        return n == 0
    except Exception:                                           # noqa: BLE001
        # 表名/结构变了：宁可判成交易日（多跑一次），不要静默不干活
        return True


def main():
    ap = argparse.ArgumentParser(description='本地行情数据齐了吗')
    ap.add_argument('--today', help='假装今天是这一天（测试用）')
    ap.add_argument('--after', default=CLOSE_AFTER,
                    help='几点之后才认当天数据（默认 %s）' % CLOSE_AFTER)
    a = ap.parse_args()
    now = datetime.datetime.now()
    today = (datetime.date.fromisoformat(a.today) if a.today
             else now.date())
    import duckdb
    con = duckdb.connect()

    # ---- 判据 0：tdx.db 能打开吗（锁着 = 上一轮在抓）----
    d, err = _open_tdx()
    if d is None:
        if '不存在' in (err or ''):
            _say('🔴 %s —— 该跑' % err)
            return 1
        _say('%s —— 上一轮可能正在抓，这一轮跳过' % err)
        return 2

    # ---- 判据 1：今天是交易日吗 ----
    if not _is_trading_day(today, d):
        d.close()
        _say('今天 %s 不是交易日 —— 什么都不做' % today)
        return 2

    # ---- 判据 2：过了收盘吗 ----
    hh, mm = (int(x) for x in a.after.split(':'))
    if not a.today and (now.hour, now.minute) < (hh, mm):
        d.close()
        _say('现在 %s，还没到 %s —— 不抓（盘中的 bar 是不完整的，'
             '抓了会把半截数据写进库，而那不报错）'
             % (now.strftime('%H:%M'), a.after))
        return 2

    # ---- 判据 3：今天的行数够吗 ----
    try:
        S = ("symbol IN (SELECT symbol FROM raw_symbol_class "
             "WHERE class='stock') AND symbol NOT LIKE 'sh90%' "
             "AND symbol NOT LIKE 'sz20%'")
        n_today = d.execute(
            "SELECT count(*) FROM raw_kline_daily WHERE %s AND date=DATE '%s'"
            % (S, today)).fetchone()[0]
        prev = d.execute(
            "SELECT max(date) FROM raw_kline_daily WHERE %s AND date < DATE '%s'"
            % (S, today)).fetchone()[0]
        n_prev = d.execute(
            "SELECT count(*) FROM raw_kline_daily WHERE %s AND date=DATE '%s'"
            % (S, prev)).fetchone()[0] if prev else 0
    finally:
        d.close()
    need = int(n_prev * MIN_RATIO)
    if n_prev and n_today < need:
        _say('tdx.db 今天 %s 只有 %d 只（上一交易日 %s 有 %d 只，'
             '要 ≥ %d = 95%%）—— 该跑'
             % (today, n_today, prev, n_prev, need))
        return 1
    if not n_prev:
        _say('🔴 连上一交易日的数据都没有 —— 该跑')
        return 1

    # ---- 行情齐了，再看面板跟上了没 ----
    import glob
    fs = sorted(glob.glob(os.path.join(PANEL, 'panel_*.parquet')))
    if not fs:
        _say('行情齐了（%d 只），但面板不存在 —— 该跑' % n_today)
        return 1
    pmax = con.execute(
        "SELECT max(date) FROM read_parquet('%s')"
        % os.path.join(PANEL, 'panel_%d.parquet' % today.year)).fetchone()[0]
    if str(pmax) != str(today):
        _say('行情齐了（%d 只），但面板只到 %s —— 该跑'
             % (n_today, pmax))
        return 1
    # ---- 判据 4：🔴 今天的 PIT 快照抓过吗 ----
    #   这条与"行情齐不齐"**无关** —— daily_snapshot 抓的是通达信的
    #   名称/分类/板块成分，那是 **type-1 覆盖写、漏一天永久丢失**的。
    #   只判行情的话：通达信某天一直不放行情 -> 轮询永远判成"该跑"但
    #   cron 抓不到 -> 看着在重试，而 snapshot 那步其实每次都跑到了；
    #   反过来若判据写成"行情齐就算齐"，行情齐了却在 snapshot 之前退出，
    #   当天的名称/分类就永久丢了。所以它必须是**独立的一条**。
    snap = os.path.join(ROOT, 'raw', 'tdx', 'snapshots', 'manifest.csv')
    if not os.path.isfile(snap):
        _say('行情与面板都齐了，但快照台账不存在 —— 该跑')
        return 1
    try:
        got = con.execute(
            "SELECT count(*) FROM read_csv_auto('%s') WHERE snap_date = '%s'"
            % (snap, today)).fetchone()[0]
    except Exception as e:                                      # noqa: BLE001
        _say('快照台账读不了（%s）—— 该跑（宁可多跑一次）' % str(e)[:70])
        return 1
    if not got:
        _say('行情与面板都齐了，但**今天的 PIT 快照还没抓**'
             '（漏一天永久丢失）—— 该跑')
        return 1
    _say('✅ 已齐：tdx.db 与面板都到 %s（%d 只，上一交易日 %d 只），'
         '今天的 PIT 快照有 %d 条' % (today, n_today, n_prev, got))
    return 0


if __name__ == '__main__':
    sys.exit(main() or 0)
