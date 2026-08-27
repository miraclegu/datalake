#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""FROEC-PB-CAP-HL 的本地回测（取数逻辑完全本地化）。

用法
    python3 datalake/build/backtest_froec.py --start 2019-01-01 --end 2026-06-30

⚠️ 复现聚宽策略时踩到的 4 个口径坑（全部由 v0b 真实交易记录反推验证）
  1. 选股用【前一交易日】数据，过滤用【当日】数据
     聚宽 get_fundamentals(q, date=yesterday) 取前一交易日；而
     filter_limitup_stock 用 get_current_data() 取当日。混用两个日期。
  2. indicator.eps 是【单季】不是累计
     603029 在 2018 累计 EPS: Q1 +0.0095 -> H1 -0.07 -> Q3 -0.05
     单季 Q3 = -0.05-(-0.07) = +0.02 > 0 -> JQ 放行；用累计 -0.05 会误剔。
  3. indicator.roe 也是【单季】—— FROEC 的 increase 公式本来就是单季口径
  4. 股本变动生效时点 tdx 比聚宽晚 1 个交易日
     603536 在 2018-12-31 解禁，流通比 0.25->0.3587；
     tdx 在 2019-01-02 才生效，聚宽更早 -> 边界日选股会差一只。此项无法在本地消除。

v0b（纯小市值）复现命中率 9/10 = 90%，唯一差异即第 4 项。

交易假设（与 JQ 侧对齐）
    周频（每周第一个交易日）、10 只等权、开盘价成交
    滑点 0.0015 双边（PriceRelatedSlippage 语义：买 +0.075%、卖 -0.075%）
    佣金双边万三（最低 5 元）、卖出印花税千一
"""
import argparse
import os

import duckdb

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PANEL = "read_parquet('%s/mart/panel_daily/panel_*.parquet')" % ROOT
FINQ  = "read_parquet('%s/std/fin_quarterly.parquet')" % ROOT
IND   = "read_parquet('%s/std/fin_indicator_q.parquet')" % ROOT   # 聚宽权威单季指标

import os as _os
STOCK_NUM   = 10
CANDIDATE   = 10          # FROEC 原版：get_stock_list()[:10]，先截再过滤、不补位
LISTED_DAYS = 250
SLIP        = float(_os.environ.get('SLIP', 0.0015))   # 双边
COMM        = float(_os.environ.get('COMM', 0.0003))   # 单边佣金
TAX         = float(_os.environ.get('TAX', 0.001))     # 卖出印花税
HOLD_LU     = _os.environ.get('HOLD_LU') == '1'   # 涨停留仓
DELIST_HAIRCUT = float(_os.environ.get('DELIST_HAIRCUT', 0))  # 退市清算折价
DELIST_DAYS = 3           # 连续 N 个【交易日】面板无行 -> 判定退市并强制清算（日频后放宽，避开偶发缺行）
LIMIT_DAYS  = 20          # JQ g.limit_days：黑名单回看窗口
# 股息红利差别化个税（财税[2015]101号）：持股 <=1月 按 20%，1月~1年 按 10%，>1年 免征。
# 征收方式是【派息时不扣、卖出时补缴】，券商从资金账户扣收 —— 所以按持有期在卖出点结算。
# 后复权价隐含「分红全额免税再投资」，不扣这笔税就是系统性高估。
# 默认【开启】：聚宽确实扣红利税（已知事实），扣了才是对标口径。
# DIV_TAX=0 只用于量化这笔税本身的影响。
DIV_TAX     = _os.environ.get('DIV_TAX', '1') == '1'
DIV_T1, DIV_R1 = 30,  0.20      # <=30 天
DIV_T2, DIV_R2 = 365, 0.10      # 31~365 天；>365 天免征
ALLOW_LD_SELL = _os.environ.get('ALLOW_LD_SELL') == '1'  # =1 恢复旧行为(跌停也能卖)，用于量化影响
DUMP_POS    = _os.environ.get('DUMP_POS')   # 导出逐日持仓，用于与新引擎(assay)对账
LU_STRICT   = _os.environ.get('LU_STRICT') == '1'   # 涨停卖出代理 B（见 check_limit_up 处）
REBAL       = _os.environ.get('REBAL') == '1'     # 每周等权重置(卖出全部再等权买入)；JQ 原版不再平衡
EXCL_IND = ('钢铁I', '煤炭I', '石油石化I', '采掘I', '银行I', '非银金融I',
            '金融服务I', '交运设备I', '交通运输I', '传媒I', '环保I')


def select_v0b(con, sel_date, trade_date):
    """v0b（纯小市值）：无任何因子，仅预过滤 + eps>0 + 流通市值升序取 15。
    用于与【区间完全已知】的 JQ 结果对标（2019-01-01~2026-06-30 年化 38.98%）。"""
    rows = con.execute("""
        SELECT jq_code FROM {P} WHERE date = DATE '{sd}'
          AND is_st = 0 AND listed_days > 375 AND list_date IS NOT NULL
          AND symbol NOT LIKE 'sh68%' AND eps_q > 0 AND floatmv > 0
        ORDER BY floatmv ASC LIMIT 15""".format(P=PANEL, sd=sel_date)).fetchall()
    cand = [r[0] for r in rows]
    if not cand:
        return []
    q = "','".join(cand)
    ok = {r[0] for r in con.execute("""
        SELECT jq_code FROM {P} WHERE date = DATE '{td}' AND jq_code IN ('{q}')
          AND NOT is_open_limit_up AND NOT is_open_limit_down
        """.format(P=PANEL, td=trade_date, q=q)).fetchall()}
    return [x for x in cand if x in ok][:STOCK_NUM]


def select(con, sel_date, trade_date):
    """FROEC 选股：sel_date 用于取数（前一交易日），trade_date 用于过滤（当日）"""
    ind = "','".join(EXCL_IND)
    rows = con.execute("""
    WITH q AS (   -- 每只票在 sel_date 已公告的最近 5 期单季 ROE
      -- ★ 用聚宽权威 indicator.roe，不再本地推算。实测其分母是
      --   【平均净资产 (期初+期末)/2】：257,525 条比对，平均 87.35% /
      --   期末 41.22% / 期初 40.08%。本地原先用期末，命中率因此卡在 72.1%。
      SELECT code, roe AS roe_q,
             row_number() OVER (PARTITION BY code ORDER BY report_date DESC) rn
      FROM {IND} WHERE pub_date <= DATE '{sd}' AND roe IS NOT NULL
    ), roec AS (
      SELECT code,
             4*max(CASE WHEN rn=1 THEN roe_q END)
             - max(CASE WHEN rn=2 THEN roe_q END) - max(CASE WHEN rn=3 THEN roe_q END)
             - max(CASE WHEN rn=4 THEN roe_q END) - max(CASE WHEN rn=5 THEN roe_q END)
             AS increase
      FROM q WHERE rn <= 5 GROUP BY 1 HAVING count(*) = 5
    ), base AS (          -- 预过滤 + PB/eps 条件（用 sel_date）
      SELECT p.jq_code, p.symbol, p.floatmv, p.pb, p.sw_l1_name
      FROM {PANEL} p
      WHERE p.date = DATE '{sd}'
        AND p.is_st = 0 AND p.listed_days > {ld} AND p.list_date IS NOT NULL
        AND p.symbol NOT LIKE 'sh68%'
        AND p.eps_q > 0 AND p.pb > 0 AND p.floatmv > 0
    ), pb_half AS (       -- PB 升序取最便宜 50%
      -- ★ 与 JQ 逐字一致：`list(df.code)[:int(0.5*len(df.code))]` 是【向下取整截断】。
      --   原用 ntile(2)=1，DuckDB 把余数分给前桶（等价 ceil(n/2)），会多取一只，
      --   进而推移下游 ROE 十分位的边界 —— 实测未命中的 14 笔全部压在
      --   分位 0.100~0.105 的擦边带上。
      SELECT * FROM (SELECT *, row_number() OVER (ORDER BY pb ASC) rn,
                            count(*) OVER () AS n FROM base)
      WHERE rn <= floor(0.5 * n)
    ), roe_top AS (       -- increase 降序取前 10%（同为向下取整截断）
      SELECT * EXCLUDE (rn2, n2) FROM (
        SELECT b.* EXCLUDE (rn, n),
               row_number() OVER (ORDER BY r.increase DESC) rn2,
               count(*) OVER () AS n2
        FROM pb_half b JOIN roec r ON r.code = b.jq_code
      ) WHERE rn2 <= floor(0.1 * n2)
    )
    SELECT jq_code FROM roe_top
    WHERE sw_l1_name IS NULL OR sw_l1_name NOT IN ('{ind}')
    ORDER BY floatmv ASC LIMIT {cand}
    """.format(IND=IND, PANEL=PANEL, sd=sel_date, ld=LISTED_DAYS,
               cand=CANDIDATE, ind=ind)).fetchall()
    cand = [r[0] for r in rows]
    if not cand:
        return []
    q = "','".join(cand)
    ok = {r[0] for r in con.execute("""
        SELECT jq_code FROM {P} WHERE date = DATE '{td}' AND jq_code IN ('{q}')
          AND NOT is_open_limit_up AND NOT is_open_limit_down
        """.format(P=PANEL, td=trade_date, q=q)).fetchall()}
    return [x for x in cand if x in ok][:STOCK_NUM]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--start', default='2019-01-01')
    ap.add_argument('--end', default='2026-06-30')
    ap.add_argument('--cash', type=float, default=1e6)
    ap.add_argument('--mode', default='froec', choices=('froec', 'v0b'))
    a = ap.parse_args()
    con = duckdb.connect(os.path.join(ROOT, 'lake.db'), read_only=True)

    # 物化价格切片并按 date 排序 —— zone map 让「按日取数」变成小范围扫描，
    # 否则日频循环要对 8.5M 行的 parquet 全扫 1800+ 次。
    con.execute("""CREATE TEMP TABLE px AS
        SELECT date, jq_code,
               round(open * hfq_factor, 4) AS o,   -- 后复权开盘价（成交/记账）
               open                        AS o_raw,   -- 真实开盘价（整手约束用）
               hfq_factor                  AS fac,
               close_hfq                   AS c,   -- 后复权收盘价（估值/尾盘成交）
               is_limit_up                 AS islu,
               -- 全天封死 = 最低价也在涨停价上；否则说明盘中被打开过
               (low >= limit_up - 0.005)   AS sealed,
               is_open_limit_down          AS old,   -- 开盘跌停：市价卖单无人接
               is_limit_down               AS cld
        FROM {P} WHERE date BETWEEN DATE '{s}' AND DATE '{e}'
        ORDER BY date""".format(P=PANEL, s=a.start, e=a.end))

    days = [r[0] for r in con.execute(
        "SELECT DISTINCT date FROM px ORDER BY 1").fetchall()]
    # ★ 回测首日也必须能调仓。选股要用【前一交易日】的数据，而那一天在
    #   区间之外，若拿不到就会静默跳过首次建仓 —— 实测 FROEC 因此空仓躲过了
    #   2016-01-04(-8.99%) 与 01-07(-9.21%) 的熔断，凭空多出十几个点。
    day0 = con.execute(
        "SELECT max(date) FROM %s WHERE date < DATE '%s'" % (PANEL, days[0])).fetchone()[0]
    if day0 is None:
        print('  ⚠ 区间起点之前无交易日，首次建仓仍会跳过')
    reb, seen = set(), set()
    for d in days:
        k = d.isocalendar()[:2]
        if k not in seen:
            seen.add(k); reb.add(d)
    print('模式 %s | 区间 %s ~ %s | 交易日 %d | 调仓 %d 次'
          % (a.mode, days[0], days[-1], len(days), len(reb)))

    # 权威退市日。2200-01-01 是「未退市」哨兵值，必须剔除，
    # 否则任何日期比较都会把在市股票判成已退市。
    DELIST = {r[0]: r[1] for r in con.execute(
        "SELECT code, delist_date::DATE FROM read_parquet('%s/std/security_universe.parquet') "
        "WHERE delist_date IS NOT NULL AND delist_date < DATE '2100-01-01'" % ROOT).fetchall()}
    print('  权威退市日 %d 只' % len(DELIST))

    # 现金分红除权事件。★ bonus_ratio_rmb 实测是【每 10 股派息】——
    #   用复权因子在除权日的跳变反推，30,693 个纯现金分红事件比值精确为 1.0。
    #   送股/转增不产生现金，不在此列。
    DIV = {}
    for _c, _d, _dps in con.execute(
            "SELECT code, a_xr_date::DATE, bonus_ratio_rmb / 10.0 "
            "FROM read_parquet('%s/std/dividend.parquet') "
            "WHERE plan_progress = '实施方案' AND a_xr_date IS NOT NULL "
            "AND bonus_ratio_rmb > 0" % ROOT).fetchall():
        DIV[(_c, _d)] = DIV.get((_c, _d), 0.0) + _dps
    print('  现金分红除权事件 %s 条' % format(len(DIV), ','))

    sel_fn = select_v0b if a.mode == 'v0b' else select
    cash, pos, last = a.cash, {}, {}
    stale, delisted, lu_sold = {}, [], 0
    hist_hold, prev_islu = [], {}
    curve, nsel, diag, fills, _hold = [], [], [], [], []
    tlog, entry, gapped = [], {}, {}
    div_gross, prev_fac, div_tax_paid = {}, {}, [0.0]

    def sell(code, d, price, slip=True):
        """★ 唯一的卖出出口。三个触发时点（9:30 调仓 / 14:00 涨停打开 /
        退市清算）都必须走这里，只是传入的成交价不同。

        为什么必须收敛成一个函数：卖出有 6 件副作用（平仓、滑点、佣金、印花税、
        红利税结算、交易日志与状态清理）。之前三处各写一遍，结果 tlog 只挂在
        其中一处 —— 直接导致「78 笔 x 3.10% 推不出 +56%」那次诊断本身是错的，
        白查了一轮。副作用分散是这类 bug 的病根，不是风格问题。
        """
        nonlocal cash
        sh = pos.pop(code)
        px_eff = price * (1 - SLIP / 2) if slip else price
        amt = sh * px_eff
        # 印花税(卖出千一) + 佣金(万三，最低 5 元/笔)。
        # JQ 的 min_commission=5 是【按笔】生效、买卖都算；
        # 原实现只在买入侧加了 min-5，卖出侧漏了 —— 小额仓位会低估成本。
        fee = amt * TAX + max(amt * COMM, 5)
        # 红利税：持有期内已收现金分红的毛额，按差别化税率在【卖出时】补缴
        g = div_gross.pop(code, 0.0)
        dtax = 0.0
        if g and DIV_TAX:
            e = entry.get(code)
            held = (d - e[0]).days if e else 0
            rate = DIV_R1 if held <= DIV_T1 else (DIV_R2 if held <= DIV_T2 else 0.0)
            dtax = g * rate
            div_tax_paid[0] += dtax
        cash += amt - fee - dtax
        e = entry.pop(code, None)
        if e:
            tlog.append((e[0], d, code, px_eff / e[1] - 1, gapped.pop(code, 0)))
        gapped.pop(code, None)
        prev_fac.pop(code, None)
        stale.pop(code, None)
        return amt

    for i, d in enumerate(days):
        prev = days[i - 1] if i > 0 else day0

        # ---------- 9:05 prepare_stock_list ----------
        hist_hold.append(set(pos))
        if len(hist_hold) > LIMIT_DAYS:
            hist_hold = hist_hold[-LIMIT_DAYS:]
        not_buy_again = set().union(*hist_hold) if hist_hold else set()
        # 昨收涨停的【持仓】票：调仓时不卖，留到 14:00 再看是否打开
        hll = {s for s in pos if prev_islu.get(s)}
        bought_today = set()   # T+1：当日买入不可卖出

        # 当日行情（只取持仓票，避免全表扫）
        need_px = set(pos)
        px = {}
        if need_px:
            q = "','".join(need_px)
            px = {r[0]: (r[1], r[2], r[3], r[4], r[5]) for r in con.execute(
                "SELECT jq_code, o, c, islu, old, fac FROM px WHERE date = DATE '%s' "
                "AND jq_code IN ('%s')" % (d, q)).fetchall()}
            for c_, v_ in px.items():
                if v_[1] is not None:
                    last[c_] = v_[1]

        # ---------- 现金分红：累计毛额，税在卖出时结算 ----------
        # 真实股数 = sh(后复权记账单位) x factor。除权日 factor 会跳，
        # 所以必须用【前一日】的 factor，用当日的会低估股数、少算分红。
        for code in pos:
            dps = DIV.get((code, d))
            if dps:
                f = prev_fac.get(code)
                if f:
                    div_gross[code] = div_gross.get(code, 0.0) + pos[code] * f * dps
        for code in pos:
            if code in px and px[code][4]:
                prev_fac[code] = px[code][4]

        # ---------- 退市清算 ----------
        # 判据必须是权威 delist_date，不能拿「面板无行」当代理。
        # 曾用「连续 3 个交易日无行 = 退市」，结果把【普通停牌】全判成退市：
        # 000546 / 002606 / 300317 / 300125 / 600367 等 19 只，面板末日都是最新
        # 交易日、delist_date 是哨兵 2200-01-01（根本没退市），却被按停牌前的
        # 陈价强行卖掉、复牌后再没买回。停牌应【继续持有】，退市才该清算。
        # 真退市的票，面板是覆盖退市整理期的（末 30 日 -43%~-93% 的暴跌都在
        # 数据里），所以按最后已知价清算无需额外折价 —— 崩盘已经计入。
        for code in list(pos):
            if code in px:
                continue
            gapped[code] = gapped.get(code, 0) + 1
            dl = DELIST.get(code)
            if dl is not None and d >= dl:
                # 退市清算：无滑点（不是真成交，是按最后已知价出清）
                sell(code, d, last.get(code, 0) * (1 - DELIST_HAIRCUT), slip=False)
                delisted.append((d, code))

        # ---------- 9:30 weekly_adjustment ----------
        if d in reb and prev is not None:
            tgt = sel_fn(con, prev, d)
            # 20 日黑名单：最近 20 日持有过 且 最近 20 日涨停过 -> 不再买入
            if tgt:
                lo = days[max(0, i - LIMIT_DAYS)]
                qt = "','".join(tgt)
                recent_lu = {r[0] for r in con.execute(
                    "SELECT DISTINCT jq_code FROM px WHERE date > DATE '%s' "
                    "AND date <= DATE '%s' AND jq_code IN ('%s') AND islu"
                    % (lo, prev, qt)).fetchall()}
                black = not_buy_again & recent_lu
                tgt = [x for x in tgt if x not in black]
            nsel.append(len(tgt))

            # 目标票的当日开盘价
            tpx = {}
            if tgt:
                qt = "','".join(tgt)
                # 必须连当日【收盘价】一起取：新买入的票此前没持有过，
                # last 里没有它的价格，只取开盘价会让当晚市值按 0 计。
                tpx = {r[0]: (r[1], r[2], r[3], r[4], r[5]) for r in con.execute(
                    "SELECT jq_code, o, c, islu, o_raw, fac FROM px WHERE date = DATE '%s' "
                    "AND jq_code IN ('%s')" % (d, qt)).fetchall()}

            for code in list(pos):
                # ★ 一字跌停无人接盘，市价卖单成交不了。JQ 的 close_position
                #   显式检查 order.filled == order.amount，失败即保留持仓。
                #   本地原先无条件卖出 —— 方向是乐观的：跌停票通常继续跌，
                #   卖掉就躲过了后续损失。
                if code in bought_today:      # T+1
                    continue
                if (REBAL or (code not in tgt and code not in hll)) and code in px \
                        and px[code][0] and not (px[code][3] and not ALLOW_LD_SELL):
                    sell(code, d, px[code][0])          # 9:30 按开盘价
            need = [c for c in tgt if c not in pos and (tpx.get(c) or (None,))[0]]
            need = need[:max(0, len(tgt) - len(pos))]
            if need:
                per = cash / len(need)
                for code in need:
                    # ★「100 股一手」约束的是【真实股数】，不是后复权单位。
                    #   曾用 int(per/后复权价/100)*100，等价于要求后复权价 < per/100，
                    #   于是老股票（复权因子 20~30、后复权价上百元）被整只静默跳过，
                    #   钱滞留成现金、下周挤进更少的票 —— 2016 年出现过 25% 的畸形单仓。
                    #   本金越小越严重（10 万本金时几乎所有高因子老股都买不进）。
                    raw = tpx[code][3] * (1 + SLIP / 2)      # 真实成交价
                    fac = tpx[code][4] or 1.0
                    lots = int(per / (raw * 100)) if raw > 0 else 0
                    sh = lots * 100 / fac                    # 换算回后复权记账单位
                    pr = tpx[code][0] * (1 + SLIP / 2)
                    if sh > 0:
                        cost = lots * 100 * raw
                        cash -= cost + max(cost * COMM, 5)
                        pos[code] = sh
                        prev_fac[code] = fac
                        bought_today.add(code)
                        px[code] = tpx[code]
                        entry[code] = (d, pr); gapped[code] = 0
                        if _os.environ.get('DIAG2'):
                            fills.append((d, code, cost))
                        if tpx[code][1]:
                            last[code] = tpx[code][1]

        # ---------- 14:00 check_limit_up ----------
        # 昨日涨停的持仓，今日尾盘若已打开 -> 卖出；仍封住 -> 继续持有。
        # 日线只有收盘价，用「今日是否仍收在涨停价」代替 14:00 快照。
        if hll:
            fresh = {}
            q = "','".join(hll)
            fresh = {r[0]: (r[1], r[2], r[3], r[4]) for r in con.execute(
                "SELECT jq_code, c, islu, sealed, cld FROM px WHERE date = DATE '%s' "
                "AND jq_code IN ('%s')" % (d, q)).fetchall()}
            for code in hll:
                if code not in pos or code not in fresh or code in bought_today:
                    continue   # T+1：当日买入不可卖出
                cpx, islu, sealed, cld = fresh[code]
                if cld and not ALLOW_LD_SELL:   # 收盘跌停，卖不出去
                    continue
                # 两种日线代理：
                #   A(默认) 收盘未封涨停才卖 —— 盘中打开又回封的票会留着
                #   B(LU_STRICT=1) 盘中被打开过就卖 —— 更接近 JQ 的 14:00 快照
                open_today = (not sealed) if LU_STRICT else (not islu)
                if open_today and cpx:
                    sell(code, d, cpx)                  # 14:00 用当日收盘价代理
                    lu_sold += 1

        # ---------- 收盘：记权益 + 记今日涨停状态 ----------
        mv = sum(sh * (px[c][1] if (c in px and px[c][1]) else last.get(c, 0))
                 for c, sh in pos.items())
        curve.append((d, cash + mv))
        if DUMP_POS:
            for _c, _sh in pos.items():
                _hold.append((d, _c, _sh))
        if _os.environ.get('DIAG4') and len(curve) > 1:
            _r = curve[-1][1] / curve[-2][1] - 1
            if abs(_r) > 0.05:
                _tot = cash + mv
                _det = sorted(((sh * (px[c][1] if (c in px and px[c][1]) else last.get(c, 0)) / _tot, c)
                               for c, sh in pos.items()), reverse=True)[:4]
                print('JUMP\t%s\t%+.2f%%\t权益 %s\t现金%.0f%%\t%s'
                      % (d, 100 * _r, format(int(_tot), ','), 100 * cash / _tot,
                         ' '.join('%s:%.0f%%' % (c, 100 * w) for w, c in _det)))
        diag.append((d, len(pos), cash, mv))
        prev_islu = {c: (px[c][2] if c in px else False) for c in pos}

    import math
    tot = curve[-1][1] / a.cash
    yrs = (curve[-1][0] - curve[0][0]).days / 365.25
    ann = tot ** (1 / yrs) - 1
    peak, mdd = 0, 0
    for _, v in curve:
        peak = max(peak, v); mdd = max(mdd, 1 - v / peak)
    print()
    print('=' * 60)
    print('  期末权益   %14s' % format(int(curve[-1][1]), ','))
    print('  累计收益   %13.2f%%' % ((tot - 1) * 100))
    print('  年化收益   %13.2f%%' % (ann * 100))
    print('  最大回撤   %13.2f%%  (日频)' % (mdd * 100))
    print('  年数       %13.2f' % yrs)
    print('  平均选中   %13.2f 只 (调仓 %d 次)' % (sum(nsel) / max(len(nsel), 1), len(reb)))
    print('  涨停打开卖 %13d 笔' % lu_sold)
    print('  退市清算   %13d 笔' % len(delisted))
    print('  红利税     %14s  (%s)'
          % (format(int(div_tax_paid[0]), ','), '已计' if DIV_TAX else '未计 DIV_TAX=0'))
    if _os.environ.get('DIAG2'):
        for _d, _c in delisted:
            print('EVENT\t%s\t%s' % (_d, _c))
    print('=' * 60)
    if _os.environ.get('DIAG3') and tlog:
        import collections as _c
        agg = _c.defaultdict(lambda: [0, 0.0, 0, 0.0])
        for e, x, code, r, g in tlog:
            i = 2 if g > 0 else 0
            agg[e.year][i] += 1; agg[e.year][i+1] += r
        print()
        print('%-6s %9s %10s %8s %10s' % ('入场年','无停牌笔','平均收益','停牌笔','平均收益'))
        for k in sorted(agg):
            n0, s0, n1, s1 = agg[k]
            print('%-6s %9d %9.2f%% %8d %9.2f%%'
                  % (k, n0, 100*s0/max(n0,1), n1, 100*s1/max(n1,1)))
    if _os.environ.get('DIAG2') and fills:
        # 单笔委托金额 / 当日成交额。聚宽默认 order_volume_ratio=0.25，
        # 超过这个比例的部分不会成交 —— 本地是 100% 全额按开盘价成交。
        con.execute("CREATE TEMP TABLE _f(d DATE, code VARCHAR, cost DOUBLE)")
        con.executemany("INSERT INTO _f VALUES (?,?,?)", fills)
        print()
        print(con.execute("""
          SELECT year(f.d) AS 年, count(*) AS 笔数,
            round(median(f.cost/nullif(p.amount,0)),4)              AS 委托占成交额_中位,
            round(quantile_cont(f.cost/nullif(p.amount,0),0.9),4)   AS P90,
            round(100.0*avg((f.cost/nullif(p.amount,0) > 0.25)::INT),1) AS 超25pct占比,
            round(avg(f.cost)) AS 平均委托额
          FROM _f f JOIN read_parquet('%s/mart/panel_daily/panel_*.parquet') p
            ON p.jq_code=f.code AND p.date=f.d
          GROUP BY 1 ORDER BY 1""" % ROOT).df().to_string(index=False))
    if _os.environ.get('DIAG'):
        import collections
        by = collections.OrderedDict()
        for d, npos, csh, mv in diag:
            by.setdefault(d.year, []).append((npos, csh / max(csh + mv, 1), csh + mv))
        print('\n%-6s %7s %8s %14s %10s' % ('年', '均持仓', '均现金%', '年末权益', '年收益%'))
        pe = a.cash
        for y, rows in by.items():
            eq = rows[-1][2]
            print('%-6s %7.1f %7.1f%% %14s %9.1f%%' % (
                y, sum(r[0] for r in rows) / len(rows),
                100 * sum(r[1] for r in rows) / len(rows),
                format(int(eq), ','), 100 * (eq / pe - 1)))
            pe = eq
    if DUMP_POS:
        import pandas as pd
        pd.DataFrame(_hold, columns=['date', 'code', 'shares']).to_parquet(
            DUMP_POS, index=False)
        print('  逐日持仓已导出 %s (%d 行)' % (DUMP_POS, len(_hold)))
    con.close()


if __name__ == '__main__':
    main()
