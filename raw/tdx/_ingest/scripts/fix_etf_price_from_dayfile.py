#!/usr/bin/env python3
"""ETF 价格按【通达信 .day 正本】重写 —— 取代 fix_etf_price_scale.py 的 ×10 补丁。

## 为什么不是 ×10

2026-09-17 定案：`tdx2db` 有两条取数路径，坏的是每天那条。

    init （引导历史）  vipdoc/*.day                      ÷1000  ✅ 2019~2025 精度完好
    cron （每日增量）  products/data/data/g4day/*.zip    ETF 按 ÷10000 再舍到 3 位 ❌

所以 `1107`（= 真值 1.107 × 1000）进库变成 `0.111`：**量级小 10 倍、而且第 3 位
小数被舍掉了**。`fix_etf_price_scale.py` 的 ×10 只把 `0.111` 抬回 `1.11` ——
它补在错误的层上，那一位永远回不来。

实测后果（ETF「与前一交易日收盘完全相同」的占比）：

    1~4 月 3~4%   ->   6 月 18.7%   ->   9 月 30.9%
    红利低波那几只 40%+          对照：指数 0.19% / 股票 2.44%

红利低波 ETF 单价 1.1 元，一分钱 = 0.9%，而它日内波动 0.2~0.5% ——
于是**连着好几天报同一个收盘价**，页面上看着像"数据没同步"。

## 正本是好的，所以这条链取正本

`.day` 文件的编码**从来没变过**（sz159525：05-22 = 1079、05-25 = 1077，连续），
第 3 位一直在。所以做法是绕开 cron 那条解析，直接读 `.day`：

    open/high/low/close = 记录里的 int / 1000

## 只下 61 MB，不是 500 MB

两个 zip 合计 500 MB，但 **ETF 成员在里面是连续的**（sz 1850 只、sh 2114 只
各占一整段）。先用 HTTP Range 取中央目录（~1.2 MB），算出 ETF 的字节区间，
再各发 **1 个 Range 请求**取那一段 —— 实测 sz 32.4 MB + sh 28.5 MB。

## 三道自证（任一不过就拒绝写入）

🔴🔴 **判据是「和正本比」，不是「坏成什么样」。**
  最近 `WINDOW_DAYS` 个交易日**无条件**逐行核对（对坏法不作任何假设）；
  塌陷探测（数"有没有第 3 位小数"）只负责把区间**往回扩**，一次性回补历史。
  —— 第一版只有塌陷探测，而那检测的是**那一次事故的指纹**：它成立的前提是
  ×10 补丁跑过、值被舍成两位。那个补丁已经退役，于是 cron 新写进来的是
  `0.111`（÷10、**仍带 3 位小数**），探测器会判成"无事可做"，
  价格就小 10 倍留在库里（实测构造验过）。
  同理 `--since` 也不写死日期：写死 `2026-05-25` 的话，换个日子再发生就报
  "异常 0"（那正是上一版 `load_tdx_kline.py` 守卫失效的原因）。

| | 判据 |
|---|---|
| 除数对不对 | 拿**塌陷起点之前**的行对数：正本 ÷1000 必须与库里已有值逐位相同（那段是 init 灌的、已知正确）。对不上 = 除数或对齐错了 |
| 对齐对不对 | 同一 (symbol,date) 的 `amount` 必须与库里相同（amount 一直是对的，不参与修正）—— 它证明我们改的是**同一行** |
| 修好没有 | **主判据：还有几行与正本不符（必须 0）** —— 它与坏法无关，÷10 / 舍位 / 乱码都抓得住。"有第 3 位小数"的占比只在真检测到塌陷时才查，它只对"被舍成两位"那一种坏法有意义 |

## 幂等

写的是正本值，重复跑收敛到同一结果（第二次 0 行待修）。
⚠️ 与 `fix_etf_price_scale.py` **不能同时接在链上**：那个脚本会把已经正确的
   价格再 ×10。它已从 `sync_daily.sh` 摘掉，文件保留只为留住这段历史。

用法:
  python3 fix_etf_price_from_dayfile.py --db ./tdx.db --dry-run
  python3 fix_etf_price_from_dayfile.py --db ./tdx.db
"""
from __future__ import annotations

import argparse
import hashlib
import os
import struct
import sys
from email.utils import parsedate_to_datetime
import datetime
import urllib.request
import zlib

import duckdb

VIPDOC = 'https://www.tdx.com.cn/products/data/data/vipdoc/%slday.zip'
MARKETS = ('sh', 'sz')
DIVISOR = 1000.0          # ETF/基金在 .day 里是价格 ×1000
REC = 32                  # date open high low close amount(f4) volume reserved
UA = {'User-Agent': 'Mozilla/5.0'}

# 塌陷判据：一天里 ETF 收盘价"有第 3 位小数"的占比低于它就算塌了
COLLAPSE_PCT = 20.0
# 修完之后至少要回到这个水平（历年实测 55~81%）
RECOVER_PCT = 30.0

# 🔴 每次都【无条件】重查最近这么多个交易日 —— 与"塌陷探测"是两回事，见下。
WINDOW_DAYS = 30


def _say(*a):
    print(*a, flush=True)


# ---------------------------------------------------------------- 远端 zip

def _get(url, a, b):
    r = urllib.request.Request(url, headers=dict(UA, Range='bytes=%d-%d' % (a, b)))
    resp = urllib.request.urlopen(r, timeout=180)
    if resp.status != 206:
        raise RuntimeError('服务端不支持 Range（status=%s）' % resp.status)
    return resp.read()


def _head(url):
    r = urllib.request.Request(url, headers=UA, method='HEAD')
    h = urllib.request.urlopen(r, timeout=60).headers
    return int(h['Content-Length']), h.get('Last-Modified', '')


def _zip_day(lm):
    """zip 的 Last-Modified（GMT）-> 它最多可能带到哪一天的 bar（北京日期）。

    整包是收盘后重打的，所以"打包时刻所在的北京日"就是它能覆盖的最后一天。
    实测 2026-09-16：sh 09:25 GMT / sz 09:55 GMT = 北京 17:25 / 17:55。
    解析不了就返回 None —— **不猜**，退回下载后的权威判定。
    """
    try:
        return (parsedate_to_datetime(lm) + datetime.timedelta(hours=8)).date()
    except Exception:                                           # noqa: BLE001
        return None


def precheck_lag(zip_day, db_max):
    """HEAD 预检：正本【明显】还没放出 db_max 那天 -> True（这一轮别下那 61MB）。

    🔴 只做**保守否定**，等号必须放行 —— 整包就是当天收盘后重打的，
      `zip_day == db_max` 正是正常情形。写成 `<=` 的话**每天都判落后、
      整条链永远跑不完**，而它不报错，日志里只有一句"先不往下走"。
    ★ 反过来 `zip_day >= db_max` **不等于**"里面真有那天"（整包可能次日
      早上重打却不含新数据），所以下载解包之后仍然有权威判定（`lagging`）。
      预检是省流量的，不是判据。
    ★ `zip_day` 解析不出来时一律放行（不猜）。
    """
    return bool(zip_day and db_max and zip_day < db_max)


def _central_dir(url, total):
    """-> {成员名: (method, csize, usize, 本地头偏移)}"""
    back = min(3_000_000, total)
    tail = _get(url, total - back, total - 1)
    i = tail.rfind(b'PK\x05\x06')
    if i < 0:
        raise RuntimeError('找不到 zip 的 EOCD')
    cd_size, cd_off = struct.unpack('<II', tail[i + 12:i + 20])
    start = cd_off - (total - back)
    cd = tail[start:start + cd_size] if start >= 0 else _get(url, cd_off, cd_off + cd_size - 1)
    out, p = {}, 0
    while p + 46 <= len(cd) and cd[p:p + 4] == b'PK\x01\x02':
        method, = struct.unpack('<H', cd[p + 10:p + 12])
        csize, usize = struct.unpack('<II', cd[p + 20:p + 28])
        nlen, elen, clen = struct.unpack('<HHH', cd[p + 28:p + 34])
        lho, = struct.unpack('<I', cd[p + 42:p + 46])
        out[cd[p + 46:p + 46 + nlen].decode('latin1')] = (method, csize, usize, lho)
        p += 46 + nlen + elen + clen
    return out


def _fetch_members(url, entries, cache=None, tag=''):
    """entries: [(名字, (method, csize, usize, lho))] -> {名字: 原始字节}

    成员按偏移排序后并成【连续区间】一次取回 —— ETF 在 vipdoc 里是挨着的，
    实测各市场只需 1 个请求。逐个成员发请求的话是 2000 次往返（几分钟，
    而且那是在给对方加压）。
    """
    ent = sorted(entries, key=lambda e: e[1][3])
    lo = ent[0][1][3]
    hi = max(e[1][3] + 30 + 512 + e[1][1] for e in ent)
    ck = None
    if cache:
        ck = os.path.join(cache, '%s_%s_%d_%d.bin' % (tag, hashlib.md5(url.encode()).hexdigest()[:8], lo, hi))
        if os.path.isfile(ck):
            blob = open(ck, 'rb').read()
            _say('    [cache] %s %.1f MB' % (tag, len(blob) / 1e6))
            return _slice(blob, lo, ent)
    _say('    取 %s 字节 %d~%d（%.1f MB，1 个 Range 请求）' % (tag, lo, hi, (hi - lo) / 1e6))
    blob = _get(url, lo, hi)
    if cache:
        os.makedirs(cache, exist_ok=True)
        open(ck, 'wb').write(blob)
    return _slice(blob, lo, ent)


def _slice(blob, base, ent):
    out = {}
    for name, (method, csize, usize, lho) in ent:
        p = lho - base
        if p < 0 or p + 30 > len(blob) or blob[p:p + 4] != b'PK\x03\x04':
            continue
        nlen, elen = struct.unpack('<HH', blob[p + 26:p + 30])
        q = p + 30 + nlen + elen
        data = blob[q:q + csize]
        if len(data) < csize:
            continue
        raw = zlib.decompressobj(-15).decompress(data) if method == 8 else data
        if len(raw) != usize:
            continue
        out[name] = raw
    return out


# ---------------------------------------------------------------- 判据

def collapse_start(con):
    """算出精度塌陷从哪天开始 —— 检测现象，不写死日期。"""
    rows = con.execute("""
        SELECT k.date,
               100.0 * sum(CASE WHEN k.close <> round(k.close, 2) THEN 1 ELSE 0 END) / count(*) AS pct
        FROM raw_kline_daily k
        JOIN raw_symbol_class sc ON sc.symbol = k.symbol AND sc.class = 'etf'
        GROUP BY 1 ORDER BY 1
    """).fetchall()
    last_ok = None
    for d, pct in rows:
        if pct is not None and pct > COLLAPSE_PCT:
            last_ok = d
    if last_ok is None:
        return None, None
    after = [d for d, _ in rows if d > last_ok]
    return (after[0] if after else None), last_ok


def _nth_last_date(con, n):
    """库里 ETF 的倒数第 n 个交易日 —— 滚动窗口的起点。"""
    r = con.execute("""
        SELECT min(d) FROM (
          SELECT DISTINCT k.date d FROM raw_kline_daily k
          JOIN raw_symbol_class sc ON sc.symbol = k.symbol AND sc.class = 'etf'
          ORDER BY d DESC LIMIT %d)""" % int(n)).fetchone()
    return r[0] if r else None


def _dates_before(con, since, n):
    """since 之前最近 n 个 ETF 交易日（自证①的对照窗口）。

    🔴 对照窗口必须落在【这次不修的那一段】上 —— 取到待修区间里的话，
    坏行会混进"已知正确"的基准里，自证就成了自己证自己。
    """
    rows = con.execute("""
        SELECT DISTINCT k.date FROM raw_kline_daily k
        JOIN raw_symbol_class sc ON sc.symbol = k.symbol AND sc.class = 'etf'
        WHERE k.date < DATE '%s' ORDER BY k.date DESC LIMIT %d
    """ % (since, int(n))).fetchall()
    return set(int(str(r[0]).replace('-', '')) for r in rows)


def _pct_3dp(con, since=None):
    w = "AND k.date >= DATE '%s'" % since if since else ''
    r = con.execute("""
        SELECT count(*),
               100.0 * sum(CASE WHEN k.close <> round(k.close, 2) THEN 1 ELSE 0 END) / nullif(count(*), 0)
        FROM raw_kline_daily k
        JOIN raw_symbol_class sc ON sc.symbol = k.symbol AND sc.class = 'etf'
        WHERE 1 = 1 %s""" % w).fetchone()
    return r[0], (r[1] or 0.0)


def plan_since(con, cli_since=None):
    """这次要与正本逐行核对的起点 -> (since, detected, last_ok)。

    🔴🔴 **滚动窗口是主判据，塌陷探测只是把区间往回扩。**
    第一版只有塌陷探测（数"有没有第 3 位小数"），而那检测的是
    **那一次事故的指纹**：它成立的前提是 ×10 补丁跑过、值被舍成两位。
    那个补丁已经退役，于是 cron 新写进来的是 `0.111`（÷10、**仍带 3 位
    小数**）—— 探测器会判成"无事可做"，价格就小 10 倍留在库里。

    所以最近 WINDOW_DAYS 个交易日**无条件**比对（对坏法不作任何假设，
    没见过的坏法也照样抓），塌陷探测只负责一次性回补历史。
    """
    recent = _nth_last_date(con, WINDOW_DAYS)
    detected, last_ok = collapse_start(con)
    if cli_since:
        return cli_since, detected, last_ok
    cands = [x for x in (recent, detected) if x is not None]
    return (min(cands) if cands else None), detected, last_ok


# ---------------------------------------------------------------- 主流程

def main(argv=None):
    ap = argparse.ArgumentParser(description='ETF 价格按 .day 正本重写')
    ap.add_argument('--db', required=True)
    ap.add_argument('--since', help='从哪天起修（默认按现象自动判定）')
    ap.add_argument('--dry-run', action='store_true')
    ap.add_argument('--cache', help='把取回的 zip 片段缓存到这个目录（调试用）')
    args = ap.parse_args(argv)

    con = duckdb.connect(args.db, read_only=bool(args.dry_run))

    # ---- 起点 = 滚动窗口 ∪ 塌陷区间 ----
    #
    # 🔴🔴 判据是【和正本比】，不是"坏成什么样"。
    #   第一版只靠 `collapse_start`（数"有没有第 3 位小数"），那检测的是
    #   **那一次事故的指纹** —— 它成立的前提是 ×10 补丁跑过、值被舍成两位。
    #   而那个补丁已经退役，于是明天 cron 写进来的是 `0.111`（÷10、**仍有
    #   3 位小数**），探测器会判成"无事可做"，ETF 价格就小 10 倍留在库里。
    #   —— 与 `load_tdx_kline.py` 那个写死日期的守卫是同一种错法的变体：
    #   **检测现象，不要检测那一次事故长什么样。**
    #
    # 所以：最近 WINDOW_DAYS 个交易日**无条件**与正本逐行比（覆盖任何坏法，
    # 包括还没见过的）；`collapse_start` 只负责把区间**往回扩**，用来一次性
    # 回补历史。两者取并集。
    since, detected, last_ok = plan_since(con, args.since)
    if since is None:
        _say('❌ 库里一个 ETF 交易日都没有 —— 空结果一律当失败')
        return 1
    if args.since:
        _say('起点（命令行指定）: %s' % since)
    else:
        _say('起点 %s ＝ 滚动窗口最近 %d 个交易日%s'
             % (since, WINDOW_DAYS,
                ('，并往回扩到塌陷起点 %s（最后一个正常交易日 %s）' % (detected, last_ok))
                if detected and detected < _nth_last_date(con, WINDOW_DAYS)
                else '（未检测到更早的塌陷）'))
    since_int = int(str(since).replace('-', ''))

    # 自证①的对照窗口：待修区间【之前】那 120 个交易日（这次不动它们）
    ref_set = _dates_before(con, since, 120)

    n_before, pct_before = _pct_3dp(con, since)
    _say('待查区间 ETF 行 %d，其中有第 3 位小数的占 %.2f%%' % (n_before, pct_before))

    etf = [r[0] for r in con.execute(
        "SELECT symbol FROM raw_symbol_class WHERE class='etf'").fetchall()]
    _say('ETF %d 只' % len(etf))

    db_max = con.execute("""
        SELECT max(k.date) FROM raw_kline_daily k
        JOIN raw_symbol_class sc ON sc.symbol = k.symbol AND sc.class = 'etf'""").fetchone()[0]

    # ---- HEAD 预检：正本明显还没放出今天，就别下那 61 MB ----
    #
    # 🔴 `sync_daily.sh` 的轮询 16:00 就开始，而 vipdoc 整包约 17:25~17:55
    #   才带上当天的 bar。中间那十来个点位每次都会走到这里 —— 不预检的话
    #   每轮白下 61 MB（一天能到几百 MB），而**限流是这条链上唯一的风险**
    #   （同「绝不能每分钟给每只都打一次 trends2」那条）。
    #   一次 HEAD 就能判掉：打包时刻所在的北京日 < 库里最新交易日 -> 不可能有。
    # ★ 预检只做**保守**的否定：`zip_day >= db_max` 不等于"里面真有那天"
    #   （整包可能在次日早上重打却不含新数据），所以下载解包之后**仍然**有
    #   那道权威判定（`lagging`）。预检是省流量的，不是判据。
    heads = {}
    for mk in MARKETS:
        url = VIPDOC % mk
        total, lm = _head(url)
        zd = _zip_day(lm)
        heads[mk] = (total, lm)
        _say('  %s  %.0f MB  Last-Modified %s%s'
             % (url.rsplit('/', 1)[-1], total / 1e6, lm,
                '  -> 最多到 %s' % zd if zd else '  -> 解析不了，跳过预检'))
        if precheck_lag(zd, db_max):
            return _lag_stop(db_max, zd, pre=True)

    # ---- 取正本（每个市场一个 Range 请求，成员只解一遍）----
    truth, ref, missing = {}, [], []
    for mk in MARKETS:
        url = VIPDOC % mk
        total, lm = heads[mk]
        cd = _central_dir(url, total)
        want = [(s + '.day', cd[s + '.day']) for s in etf
                if s.startswith(mk) and s + '.day' in cd]
        missing += [s for s in etf if s.startswith(mk) and s + '.day' not in cd]
        if not want:
            continue
        for name, raw in _fetch_members(url, want, cache=args.cache, tag=mk).items():
            sym = name[:-4]
            for k in range(len(raw) // REC):
                d, o, h, l, c, amt, _v, _r = struct.unpack('<iiiiifii', raw[k * REC:(k + 1) * REC])
                if d >= since_int:
                    truth[(sym, d)] = (o / DIVISOR, h / DIVISOR, l / DIVISOR,
                                       c / DIVISOR, float(amt))
                elif d in ref_set:
                    ref.append((sym, str(d), c / DIVISOR))
    _say('正本取到 %d 行（%d 只在 zip 里找不到：退市/新代码）' % (len(truth), len(missing)))
    if not truth:
        _say('❌ 一行都没取到 —— 空结果一律当失败')
        return 1

    # 🔴 正本比库里【落后】时必须说出来并拒绝放行。
    #   vipdoc 的整包实测约 17:25~17:55（北京时）才带上当天的 bar，而
    #   `sync_daily.sh` 的轮询窗口从 16:00 就开始 —— 早班那几轮 tdx 的增量
    #   已经有今天了、正本还没有。这时**今天那批行修不了**（JOIN 不上，
    #   `diff`/`left` 都是 0），脚本会一路报"✅ 完成"，然后 4/8 体检拦下整条链
    #   ——「报错必须指向真正的原因」。
    #   ★ 退出码非 0 -> 5~8 跳过 -> 面板不前进 -> `is_stale` 判"该跑"
    #     -> 下一个 10 分钟的点位自动重试，等正本放出来就过了。
    truth_max = max(d for _s, d in truth)
    lagging = db_max is not None and int(str(db_max).replace('-', '')) > truth_max

    # ---- 自证①：除数 ----
    if not _verify_divisor(con, ref):
        _say('❌ 除数自证没过 —— 拒绝写入')
        return 1

    con.execute('CREATE OR REPLACE TEMP TABLE _truth('
                'symbol VARCHAR, date DATE, o DOUBLE, h DOUBLE, l DOUBLE, c DOUBLE, amt DOUBLE)')
    con.executemany("INSERT INTO _truth VALUES (?, strptime(?, '%Y%m%d')::DATE, ?, ?, ?, ?, ?)",
                    [(s, str(d), v[0], v[1], v[2], v[3], v[4]) for (s, d), v in truth.items()])

    # ---- 自证②：对齐（amount 不参与修正，它证明改的是同一行）----
    algn = con.execute("""
        SELECT count(*), sum(CASE WHEN abs(k.amount - t.amt) <= 0.51 + abs(k.amount) * 1e-6
                                  THEN 1 ELSE 0 END)
        FROM raw_kline_daily k JOIN _truth t ON t.symbol = k.symbol AND t.date = k.date
    """).fetchone()
    if not algn[0]:
        _say('❌ 正本与库里一行都对不上（symbol/date 对齐错了）')
        return 1
    r_align = 100.0 * (algn[1] or 0) / algn[0]
    _say('自证②对齐：共同行 %d，amount 吻合 %.2f%%' % (algn[0], r_align))
    if r_align < 99.0:
        _say('❌ amount 吻合率过低 —— 说明比的不是同一行，拒绝写入')
        return 1

    diff = con.execute("""
        SELECT count(*) FROM raw_kline_daily k JOIN _truth t
          ON t.symbol = k.symbol AND t.date = k.date
        WHERE k.close <> t.c OR k.open <> t.o OR k.high <> t.h OR k.low <> t.l
    """).fetchone()[0]
    _say('待修行 %d' % diff)
    if diff == 0:
        if lagging:
            _say('✓ 已核对的那些与正本一致')
            return _lag_stop(db_max, truth_max)
        _say('✓ 已与正本一致，跳过')
        return 0

    if args.dry_run:
        for r in con.execute("""
            SELECT k.symbol, k.date, k.close, t.c FROM raw_kline_daily k JOIN _truth t
              ON t.symbol = k.symbol AND t.date = k.date
            WHERE k.close <> t.c ORDER BY k.date DESC, k.symbol LIMIT 5""").fetchall():
            _say('    %s %s  %s -> %s' % r)
        _say('[DRY-RUN] 不写库')
        return 0

    # ---- 写 ----
    con.execute('BEGIN')
    con.execute("""
        UPDATE raw_kline_daily k SET open = t.o, high = t.h, low = t.l, close = t.c
        FROM _truth t WHERE t.symbol = k.symbol AND t.date = k.date
    """)
    # raw_basic_daily 的 close/preclose/change_pct/amplitude 是 kline 的派生量，
    # 必须一起重算 —— 只改 kline 的话会出现"K 线与涨跌幅对不上"，而它不报错。
    con.execute("""
        CREATE OR REPLACE TEMP TABLE _basic AS
        WITH k AS (
          SELECT k.symbol, k.date, k.high, k.low, k.close,
                 lag(k.close) OVER (PARTITION BY k.symbol ORDER BY k.date) AS pc
          FROM raw_kline_daily k
          JOIN raw_symbol_class sc ON sc.symbol = k.symbol AND sc.class = 'etf'
        )
        SELECT symbol, date, close, pc AS preclose,
               round(CASE WHEN pc > 0 THEN (close / pc - 1) * 100 END, 2) AS change_pct,
               round(CASE WHEN pc > 0 THEN (high - low) / pc * 100 END, 2) AS amplitude
        FROM k WHERE date >= (SELECT min(date) FROM _truth)
    """)
    con.execute("""
        UPDATE raw_basic_daily b
           SET close = n.close, preclose = n.preclose,
               change_pct = n.change_pct, amplitude = n.amplitude
        FROM _basic n WHERE n.symbol = b.symbol AND n.date = b.date AND n.preclose IS NOT NULL
    """)
    con.execute('COMMIT')

    # ---- 自证③：修好没有 ----
    n_after, pct_after = _pct_3dp(con, since)
    _say('修完：ETF 行 %d，有第 3 位小数的占 %.2f%%（修前 %.2f%%）'
         % (n_after, pct_after, pct_before))
    left = con.execute("""
        SELECT count(*) FROM raw_kline_daily k JOIN _truth t
          ON t.symbol = k.symbol AND t.date = k.date WHERE k.close <> t.c""").fetchone()[0]
    _say('仍与正本不符的行: %d' % left)
    # 🔴 主判据是 `left`（还有几行与正本不符）—— 它**与坏法无关**，
    #   ÷10、舍位、乱码都一样抓得住。精度占比只在"确实检测到塌陷"时才查，
    #   因为它只对"被舍成两位"那一种坏法有意义。
    if left:
        _say('❌ 自证③没过：仍有 %d 行与正本不符' % left)
        return 1
    if detected and pct_after < RECOVER_PCT:
        _say('❌ 自证③没过：精度没回到正常量级（%.2f%% < %.2f%%）'
             % (pct_after, RECOVER_PCT))
        return 1
    if lagging:
        return _lag_stop(db_max, truth_max)
    _say('✅ 完成')
    return 0


def _lag_stop(db_max, truth_max, pre=False):
    t = str(truth_max)
    t = '%s-%s-%s' % (t[:4], t[4:6], t[6:]) if len(t) == 8 else t
    _say('⏸ 正本还没放出 %s 的数据（vipdoc 最新到 %s%s）—— 今天那批 ETF 行'
         '**没有被核对过**，先不往下走。'
         % (db_max, t, '，按 Last-Modified 预检' if pre else ''))
    _say('   vipdoc 整包实测约 17:25~17:55（北京时）更新；轮询窗口到 20:00，'
         '下一个点位会自动重试。%s' % ('（本轮没下载正本）' if pre else ''))
    return 3


def _verify_divisor(con, ref):
    """自证①：塌陷起点【之前】那一段，正本 ÷1000 必须与库里已有值逐位相同。

    那一段是 init 从同样的 .day 灌进去的、已知正确 —— 所以它同时证明了
    「÷1000」这个除数、记录布局、以及 symbol/date 的对齐都是对的。
    """
    if not ref:
        _say('自证①：对照区间取不到行 —— 空结果一律当失败')
        return False
    con.execute('CREATE OR REPLACE TEMP TABLE _ref(symbol VARCHAR, date DATE, c DOUBLE)')
    con.executemany("INSERT INTO _ref VALUES (?, strptime(?, '%Y%m%d')::DATE, ?)", ref)
    tot, hit = con.execute("""
        SELECT count(*), sum(CASE WHEN abs(k.close - f.c) < 1e-9 THEN 1 ELSE 0 END)
        FROM raw_kline_daily k JOIN _ref f ON f.symbol = k.symbol AND f.date = k.date
    """).fetchone()
    if not tot:
        _say('自证①：对照区间与库里无交集 —— 空结果一律当失败')
        return False
    pct = 100.0 * (hit or 0) / tot
    _say('自证①除数：对照区间 %d 行，正本÷%d 与库里逐位相同 %.2f%%'
         % (tot, int(DIVISOR), pct))
    return pct >= 99.0


if __name__ == '__main__':
    sys.exit(main())
