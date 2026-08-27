"""L1(笔均收益) vs L2(组合模拟) 排序一致性检验.

受控实验: 完全沿用 trades 表里的成交腿(买/卖日期与价格由 panel 给定),
只加上 L1 缺失的四件事: 仓位上限 / 资金复利 / 逐日盯市 / 交易成本.
因此排名差异只能来自这四者, 与入场出场逻辑无关.
"""
import sys, time
import numpy as np
import duckdb

DB = "/Users/guhao/finacial/tdx2db/tdx.db"
MAX_POS = 10
INIT_CAP = 1_000_000.0
SEEDS = [11, 22, 33, 44, 55, 66, 77, 88]
# A股现实成本: 佣金 0.02%/边 + 印花税 0.05%(仅卖) + 滑点 5bp/边
BUY_COST = 0.0002 + 0.0005
SELL_COST = 0.0002 + 0.0005 + 0.0005
PERIODS = {"full": ("2010-01-01", "2026-06-16"), "oos2021": ("2021-01-01", "2026-06-16")}

def log(m):
    print(f"[{time.strftime('%H:%M:%S')}] {m}", flush=True)

con = duckdb.connect(DB, read_only=True)

log("加载交易日历与价格矩阵…")
dates = np.array([r[0] for r in con.execute(
    "select distinct date from stock_hfq where date>='2010-01-01' order by 1").fetchall()])
n_days = len(dates)
didx = {d: i for i, d in enumerate(dates)}
syms = [r[0] for r in con.execute(
    "select distinct symbol from stock_hfq where date>='2010-01-01' order by 1").fetchall()]
sidx = {s: i for i, s in enumerate(syms)}
n_sym = len(syms)
log(f"  日历 {n_days} 天 ({dates[0]}..{dates[-1]}), 标的 {n_sym}")

close_mat = np.full((n_sym, n_days), np.nan, dtype=np.float32)
CH = 4_000_000
off = 0
while True:
    rows = con.execute(
        "select symbol, date, close from stock_hfq where date>='2010-01-01' "
        f"order by symbol, date limit {CH} offset {off}").fetchall()
    if not rows:
        break
    si = np.fromiter((sidx[r[0]] for r in rows), dtype=np.int32, count=len(rows))
    di = np.fromiter((didx[r[1]] for r in rows), dtype=np.int32, count=len(rows))
    cl = np.fromiter((r[2] for r in rows), dtype=np.float32, count=len(rows))
    close_mat[si, di] = cl
    off += len(rows)
    log(f"  价格矩阵 {off:,} 行")
log("价格矩阵就绪")

STRATS = [r[0] for r in con.execute(
    "select strategy_code from trades group by 1 having count(*)>=100000 "
    "order by avg(profit_pct) desc").fetchall()]
L1 = {r[0]: r[1] for r in con.execute(
    "select strategy_code, avg(profit_pct) from trades group by 1").fetchall()}
# L1b: 坑位时间加权(每持仓日收益) —— 便宜的修正版, 检验它能否恢复排序能力
L1B = {r[0]: r[1] for r in con.execute(
    "select strategy_code, avg(profit_pct/greatest(holding_days,1)) from trades "
    "where sell_price>0 group by 1").fetchall()}
# L1c: 按月先算截面均值再跨月等权
L1C = {r[0]: r[1] for r in con.execute(
    "select strategy_code, avg(m) from (select strategy_code, date_trunc('month',buy_date) mm,"
    " avg(profit_pct) m from trades where sell_price>0 group by 1,2) group by 1").fetchall()}


def simulate(sym, bd, sd, bp, sp, day_ptr, t0, t1, rng, cost=True):
    """单次组合模拟, 返回 (equity_curve, n_executed)."""
    bc = BUY_COST if cost else 0.0
    sc = SELL_COST if cost else 0.0
    cash = INIT_CAP
    pos = {}                      # sym_id -> [shares, sell_day, sell_price, last_price]
    exits = {}                    # sell_day -> [sym_id, ...]
    eq = np.empty(t1 - t0, dtype=np.float64)
    n_exec = 0
    for t in range(t0, t1):
        for s in exits.pop(t, ()):
            p = pos.pop(s, None)
            if p is not None:
                cash += p[0] * p[2] * (1.0 - sc)
        mtm = 0.0
        for s, p in pos.items():
            c = close_mat[s, t]
            if not np.isnan(c):
                p[3] = float(c)
            mtm += p[0] * p[3]
        equity = cash + mtm
        slots = MAX_POS - len(pos)
        if slots > 0:
            lo, hi = day_ptr[t], day_ptr[t + 1]
            if hi > lo:
                cand = np.arange(lo, hi)
                cand = cand[~np.isin(sym[cand], list(pos.keys()))] if pos else cand
                if len(cand):
                    if len(cand) > slots:
                        cand = rng.choice(cand, size=slots, replace=False)
                    alloc = equity / MAX_POS
                    for i in cand:
                        spend = min(alloc, cash)
                        if spend < 1000.0:
                            continue
                        price = bp[i] * (1.0 + bc)
                        if price <= 0:
                            continue
                        sh = spend / price
                        cash -= spend
                        s = int(sym[i])
                        pos[s] = [sh, int(sd[i]), float(sp[i]), float(bp[i])]
                        exits.setdefault(int(sd[i]), []).append(s)
                        n_exec += 1
        for s in exits.pop(t, ()) if t in exits else ():
            p = pos.pop(s, None)
            if p is not None:
                cash += p[0] * p[2] * (1.0 - sc)
        mtm = 0.0
        for s, p in pos.items():
            c = close_mat[s, t]
            mtm += p[0] * (float(c) if not np.isnan(c) else p[3])
        eq[t - t0] = cash + mtm
    return eq, n_exec


def metrics(eq):
    r = np.diff(eq) / eq[:-1]
    n = len(eq)
    cagr = (eq[-1] / eq[0]) ** (252.0 / n) - 1.0
    sd = r.std(ddof=1)
    sharpe = (r.mean() / sd * np.sqrt(252)) if sd > 0 else 0.0
    dd = (eq / np.maximum.accumulate(eq) - 1.0).min()
    return cagr, sharpe, dd


results = {}
for k, code in enumerate(STRATS, 1):
    log(f"[{k}/{len(STRATS)}] {code}")
    rows = con.execute(
        "select stock_code, buy_date, sell_date, buy_price, sell_price from trades "
        f"where strategy_code=? and sell_price is not null and sell_price>0 "
        "and buy_date>='2010-01-01' order by buy_date", [code]).fetchall()
    n = len(rows)
    sym = np.fromiter((sidx.get(r[0], -1) for r in rows), dtype=np.int32, count=n)
    bd = np.fromiter((didx.get(r[1], -1) for r in rows), dtype=np.int32, count=n)
    # sell_date 有 ~10% 落在非交易日(panel 按自然日算退出), 顺延到下一交易日; 超出日历末尾则钉在末日
    _sd_raw = np.array([r[2] for r in rows], dtype=object)
    sd = np.searchsorted(dates, _sd_raw, side='left').astype(np.int32)
    sd = np.minimum(sd, n_days - 1)
    bp = np.fromiter((r[3] for r in rows), dtype=np.float64, count=n)
    sp = np.fromiter((r[4] for r in rows), dtype=np.float64, count=n)
    ok = (sym >= 0) & (bd >= 0) & (bp > 0)
    sym, bd, sd, bp, sp = sym[ok], bd[ok], sd[ok], bp[ok], sp[ok]
    sd = np.maximum(sd, bd)
    day_ptr = np.searchsorted(bd, np.arange(n_days + 1))
    log(f"    可用成交腿 {len(bd):,} / {n:,}")

    ent = {}
    for pname, (d0, d1) in PERIODS.items():
        t0 = int(np.searchsorted(dates, np.datetime64(d0).astype(object)))
        t1 = int(np.searchsorted(dates, np.datetime64(d1).astype(object), side="right"))
        accs = []
        for s in SEEDS:
            eq, ne = simulate(sym, bd, sd, bp, sp, day_ptr, t0, t1, np.random.default_rng(s))
            accs.append((*metrics(eq), ne))
        A = np.array(accs)
        avail = int(((bd >= t0) & (bd < t1)).sum())
        ent[pname] = dict(cagr=A[:, 0].mean(), cagr_sd=A[:, 0].std(),
                          sharpe=A[:, 1].mean(), mdd=A[:, 2].mean(),
                          n_exec=A[:, 3].mean(), n_avail=avail)
        log(f"    {pname}: CAGR {A[:,0].mean()*100:+.2f}% (±{A[:,0].std()*100:.2f}) "
            f"Sharpe {A[:,1].mean():.2f} MaxDD {A[:,2].mean()*100:.1f}% "
            f"执行 {A[:,3].mean():.0f}/{avail:,} ({100*A[:,3].mean()/max(avail,1):.2f}%)")
    # 零成本对照 (只测容量+时间+复利的影响)
    t0 = int(np.searchsorted(dates, np.datetime64("2010-01-01").astype(object)))
    t1 = n_days
    z = [metrics(simulate(sym, bd, sd, bp, sp, day_ptr, t0, t1,
                          np.random.default_rng(s), cost=False)[0])[0] for s in SEEDS[:4]]
    ent["full_nocost_cagr"] = float(np.mean(z))
    results[code] = ent

np.save("/private/tmp/claude-501/-Users-guhao-finacial/fd6317a3-850c-492e-be9e-07f85228bf86/scratchpad/l1_vs_l2_results.npy",
        results, allow_pickle=True)


def spearman(a, b):
    def rk(v):
        o = np.argsort(-np.asarray(v))
        r = np.empty(len(v)); r[o] = np.arange(len(v)); return r
    x, y = rk(a), rk(b)
    n = len(x)
    return 1 - 6 * ((x - y) ** 2).sum() / (n * (n * n - 1))


codes = list(results)
l1 = [L1[c] for c in codes]
print("\n" + "=" * 118)
print(f"{'策略':<52}{'L1笔均%':>9}{'L2 CAGR':>10}{'L2无成本':>10}{'Sharpe':>8}{'MaxDD':>8}{'2021后CAGR':>11}{'执行率':>8}")
print("=" * 118)
for c in sorted(codes, key=lambda c: -L1[c]):
    f, o = results[c]["full"], results[c]["oos2021"]
    print(f"{c[:51]:<52}{L1[c]:>9.3f}{f['cagr']*100:>9.2f}%{results[c]['full_nocost_cagr']*100:>9.2f}%"
          f"{f['sharpe']:>8.2f}{f['mdd']*100:>7.1f}%{o['cagr']*100:>10.2f}%"
          f"{100*f['n_exec']/max(f['n_avail'],1):>7.2f}%")
print("=" * 118)
print(f"\nSpearman  L1 vs L2(full)        = {spearman(l1,[results[c]['full']['cagr'] for c in codes]):+.3f}")
print(f"Spearman  L1 vs L2(full,无成本)  = {spearman(l1,[results[c]['full_nocost_cagr'] for c in codes]):+.3f}")
print(f"Spearman  L1 vs L2(2021后)       = {spearman(l1,[results[c]['oos2021']['cagr'] for c in codes]):+.3f}")
print(f"Spearman  L2(full) vs L2(2021后) = {spearman([results[c]['full']['cagr'] for c in codes],[results[c]['oos2021']['cagr'] for c in codes]):+.3f}")
l2f = [results[c]['full']['cagr'] for c in codes]
l2o = [results[c]['oos2021']['cagr'] for c in codes]
print(f"Spearman  L2(full) vs L2(2021后) = {spearman(l2f,l2o):+.3f}")
print("\n--- 修正版 L1 指标能否恢复排序能力 ---")
print(f"Spearman  L1b(每持仓日收益) vs L2(full) = {spearman([L1B[c] for c in codes], l2f):+.3f}")
print(f"Spearman  L1b(每持仓日收益) vs L2(2021)  = {spearman([L1B[c] for c in codes], l2o):+.3f}")
print(f"Spearman  L1c(按月等权)     vs L2(full) = {spearman([L1C[c] for c in codes], l2f):+.3f}")
print(f"Spearman  L1c(按月等权)     vs L2(2021)  = {spearman([L1C[c] for c in codes], l2o):+.3f}")
print("\n--- 各策略三种 L1 指标 ---")
print(f"{'策略':<52}{'L1笔均%':>9}{'L1b每日%':>10}{'L1c月等权%':>11}{'L2 CAGR%':>10}")
for c in sorted(codes, key=lambda c: -L1[c]):
    print(f"{c[:51]:<52}{L1[c]:>9.3f}{L1B[c]:>10.4f}{L1C[c]:>11.3f}{results[c]['full']['cagr']*100:>10.2f}")
