#!/usr/bin/env python3
"""把 tdx 的 ETF 行情搭成一个【assay 引擎能直接吃】的迷你 lake。

    python3 datalake/build/build_etf_lake.py
    python3 assay/run.py <策略> --datalake /Users/guhao/finacial/datalake/etf_lake ...

## 为什么要单独一个 lake，而不是把 ETF 塞进主面板

主面板 `mart/panel_daily/` 是**(date, 股票)** 的宽表，0 行 ETF。把 ETF 混进去会改变
"一天有多少只票"的语义 —— 全市场统计、涨跌家数、分位切点全部受影响，**而那不报错**
（同「分钟数据不许进 mart/」那条）。所以 ETF 走**平行的 lake**：目录结构与主 lake
完全一致，`run.py --datalake` 指过来即可，引擎一行没改。

## 结构

    etf_lake/
      mart/panel_daily/panel_etf.parquet   ETF 日线，schema 与主面板的必需列一致
      std/security_universe.parquet        ETF 上市/退市（引擎只用 delist_date）
      std/dividend.parquet                 **空表**，见下
      std/etf_master.parquet               代码/名称/赛道/首末交易日（策略选池用）
      raw/tdx/kline/                       -> 软链到主 lake（基准指数点位）

## 🔴 分红表【故意留空】

引擎的分红模型是「后复权价 + 派息日按比例把股数折成现金」，两者相抵、数学上精确
（见 broker.py 那段推导）。ETF 这边给空表 = **总回报口径：分红已含在后复权价里、
视同再投**。代价是拿不到"现金分红"这条线，也不计红利税 —— ETF 分红少且税制与个股
不同，这个近似是刻意的，不是漏了。

## 🔴 涨跌停：ETF 按 ±10% 算

场内基金的涨跌幅限制是 10%（与主板个股同档）。`limit_ok=TRUE` 让 broker 正常拦
"涨停买不进/跌停卖不掉"。★ 货币 ETF、部分债券 ETF 实际无涨跌停，这里会给它们
一个用不上的边界 —— 但那类标的日波动远小于 10%，判定不会被触发。

## 🔴 ETF 价格：源头有个修正步，链条上必须有那一步

`tdx2db` 有两条取数路径，坏的是每天那条：`init` 读 `vipdoc/*.day`（÷1000，对），
`cron` 读 `g4day`（÷10000 **再舍到 3 位**）—— 于是 ETF 价格既小 10 倍、
**第 3 位小数又被舍掉**。修法是 `tdx2db/scripts/fix_etf_price_from_dayfile.py`
（直接取 `.day` 正本，并重算 basic_daily 的 preclose/change_pct）。

🔴 **原来那个 `fix_etf_price_scale.py` 的 ×10 补在错误的层上**：量级抬回来了，
那一位永远回不来，表现是"ETF 好几天报同一个收盘价"而不报错（2026-09-17 定案，
已退役）。

**2026-09-13 修过一次链条问题**：那个脚本存在很久，但**从来没接进任何一条链**，
一直靠人记得手动跑。2026-08-31 之后没人跑了，于是 09-01 ~ 09-11 的 18,909 行
ETF 价格全部偏小 10 倍（1566 只真 ETF，占 88.5%），而下游**一路静默**：
datalake 如实复制、面板如实构建、ETF 回测直接给出 -90% 的假暴跌。
现在它是 `sync_daily.sh` 的 **3/8** 步，后面紧跟 **4/8 更新体检**当守卫。

★ 本脚本仍然**每次重建都现算集体异动日**（>=50 只单日 |收益|>50%）并落进
  `_manifest.json` 的 `mass_break_days` —— 那是最后一道网：上游链条万一又
  漏跑，这里至少会把"哪天不能用"说出来，而不是让回测拿着坏数据跑。
"""
import json
import os
import shutil

import duckdb

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, 'etf_lake')
TDX = os.path.join(ROOT, 'raw', 'tdx', '_ingest', 'tdx.db')
START = '2015-01-01'          # 留足动量窗口的预热（策略最长看 240 日）

# 🔴🔴 真 ETF 的代码段。**这一条必须按段精确写，不能只看前 4 位。**
#
# AlphaMiner 的判据是「代码段 ∪ 名称含 ETF，再按名称剔除 LOF/联接/分级」。
# 照搬会漏 —— 因为**名称过滤对已退市的产品完全失效**：
#   实测 `raw_symbol_name` 里 sz150xxx（分级基金子份额）**一行都没有**
#   （307 只全部无名）。tdx 的名称表是 type-1 只存当前状态，而分级基金
#   2020~2021 已全部折算退市 —— 于是 `name` 取到 NULL，
#   `NOT_ETF_NAME.search('')` 不命中，它们就**全数混进池子**。
#
# 第一版我写成 ('sh51','sh56','sh58','sz15')，后果实测可见：
#   · 288 只分级基金进池（2 倍杠杆 + 强制折算，价格行为极端）
#   · sh580xxx 是 2005~2007 的【权证】，也被 'sh58' 捞了进来
#   · 双动量默认参数跑出 年化 -3.49% / 回撤 72.46%，持仓里赫然有 150153
#
# 按段逐一核对过的结论（tdx 实测）：
#   收：sh510~518 / sh52x / sh53x / sh56x / sh588 / sh589 / sz158 / sz159
#   弃：sh519(场外基金) sh50x(LOF) sh580~587(权证,无名、2007 前就摘牌)
#       sz150(分级,307只全无名) sz16x(LOF)
CODE_OK = """(
     (substr(symbol, 1, 3) = 'sh5')                       -- 沪市场内基金全段
  OR (substr(symbol, 1, 4) IN ('sz15', 'sz16'))           -- 深市 ETF(15x) + LOF(16x)
)"""
# 🔴 **lake 存【原始全集】，筛选规则留给策略。**
#
# 这一版刻意放宽到「沪市 5 开头 + 深市 15/16 开头」—— 那是 P1 策略
# (`commands/trend/core/qmt_etf_pit_t1open.py` 的聚宽版) 的 `filter_fund_code`
# 口径，含 LOF、含已退市的分级基金。3917 只。
#
# 上一版按 AlphaMiner 的 taxonomy 把 LOF/分级/权证挡在 lake 层（2285 只），
# 那是**把策略的规则烧进了数据层** —— 换一个口径更宽的策略就没法跑了。
# 现在改成：
#     lake  = 原始全集（只做"这是不是场内基金代码段"这一层客观判断）
#     策略  = 自己在 SQL 里写自己的标的池规则
# 与 CLAUDE.md 那条「feed 管从哪取、策略管阈值/筛选」一致。
#
# ★ 各策略实际用的口径：
#     etf_trend_momentum / etf_dual_momentum -> AlphaMiner taxonomy
#         （`_etf_core.py` 的 UNIV_OK：sh510~518/52x/53x/56x/588/589 + sz158/159）
#     etf_p1_rotation -> P1 自己的 filter_fund_code + is_pool_excluded
#
# 🔴 分级基金(sz150xxx，307 只全部无当前名称)与 sh580xxx(2005~07 权证)现在会
#   进 lake —— 策略侧必须自己挡住。P1 的 `is_pool_excluded` 正是按
#   `num.startswith('150')` 与「名称取不到即剔除」两条挡的。


def jq(sym_expr):
    """tdx symbol -> 聚宽代码。sh510300 -> 510300.XSHG"""
    return ("substr(%s, 3) || CASE WHEN substr(%s, 1, 2) = 'sh'"
            " THEN '.XSHG' ELSE '.XSHE' END" % (sym_expr, sym_expr))


def main():
    for d in ('mart/panel_daily', 'std', 'raw/tdx'):
        p = os.path.join(OUT, d)
        if not os.path.isdir(p):
            os.makedirs(p)
    # 基准指数点位软链到主 lake —— 不复制，省 60MB 且永远跟着主 lake 更新
    link = os.path.join(OUT, 'raw', 'tdx', 'kline')
    if not os.path.islink(link) and not os.path.exists(link):
        os.symlink(os.path.join(ROOT, 'raw', 'tdx', 'kline'), link)

    con = duckdb.connect()
    con.execute("ATTACH '%s' AS tdx (READ_ONLY)" % TDX)

    # ---------------- 证券全集（引擎只读 delist_date）----------------
    uni = os.path.join(OUT, 'std', 'security_universe.parquet')
    con.execute("""
    COPY (
      WITH k AS (
        SELECT symbol, min(date) f, max(date) l, count(*) nd
        FROM tdx.raw_kline_daily
        WHERE %s GROUP BY symbol
      ), mx AS (SELECT max(date) m FROM tdx.raw_kline_daily)
      SELECT %s AS code, symbol,
             NULL::VARCHAR AS display_name_current,
             f::TIMESTAMP AS list_date,
             -- 🔴 "最后一根 K 线距今 > 30 天" 才算退市。用"有没有今天的行"判会
             --   把停牌误判成退市，而那会让引擎在期末强制清算。
             CASE WHEN l < mx.m - INTERVAL 30 DAY THEN l::TIMESTAMP END AS delist_date,
             'etf' AS sec_type, nd AS n_days
      FROM k, mx
    ) TO '%s' (FORMAT PARQUET, COMPRESSION ZSTD)
    """ % (CODE_OK, jq('k.symbol'), uni))
    nu, nd = con.execute(
        "SELECT count(*), sum(CASE WHEN delist_date IS NOT NULL THEN 1 ELSE 0 END)"
        " FROM read_parquet('%s')" % uni).fetchone()
    print('全集  %d 只（其中已停止交易 %d 只 —— 保留它们才没有幸存者偏差）' % (nu, nd))

    # ---------------- 主标的表（策略选池用：名称 + 赛道）----------------
    master = os.path.join(OUT, 'std', 'etf_master.parquet')
    con.execute("""
    COPY (
      SELECT u.code, u.symbol, n.name, u.list_date, u.delist_date, u.n_days
      FROM read_parquet('%s') u
      LEFT JOIN tdx.raw_symbol_name n ON n.symbol = u.symbol
    ) TO '%s' (FORMAT PARQUET, COMPRESSION ZSTD)
    """ % (uni, master))

    # ---------------- 名称表（回测报告/看板显示用）----------------
    # 🔴 看板与 `runs.py` 的名称一律读 `<root>/std/security_name.parquet`
    #   （`assay/srv/runs.py` 的 `_name_table`）。ETF lake 缺这张表时，
    #   回测结果里只有代码没有名字 —— 不报错，只是读的人认不出买了什么。
    #
    # 两个源合并，**JQ 优先**：
    #   JQ  `std/security_universe_fund.parquet` 的 display_name —— 干净，
    #        且**含已退市基金**（tdx 的名称表是 type-1，退市的就没了）
    #   tdx `raw_symbol_name` —— 补 JQ 没有的那些；★ 它是**截断**的
    #        （实测 `港股通金融ETF鹏\ufffd`，最后是半个字），所以只当兜底
    #
    # ⚠️ **这是"当前名称"，不是 PIT 名称历史。** 主 lake 的股票名称表有真正的
    #   更名区间（14265 行 / 5670 只），而 ETF 这边只有今天这一份：
    #   `symbol_name` 的 PIT 快照 2026-09-01 才开始（11 天），撑不起历史。
    #   所以这里把今天的名字铺到全历史（valid_from=上市日、valid_to=NULL）。
    #   ETF 更名不频繁，代价可接受；但**不要拿它当"当时叫什么"的证据**。
    name_p = os.path.join(OUT, 'std', 'security_name.parquet')
    con.execute("""
    COPY (
      SELECT u.code,
             COALESCE(j.display_name, m.name) AS name,
             u.list_date                      AS valid_from,
             NULL::TIMESTAMP                  AS valid_to
      FROM read_parquet('%s') u
      LEFT JOIN read_parquet('%s/std/security_universe_fund.parquet') j
             ON j.code = u.code
      LEFT JOIN read_parquet('%s') m ON m.code = u.code
      WHERE COALESCE(j.display_name, m.name) IS NOT NULL
    ) TO '%s' (FORMAT PARQUET, COMPRESSION ZSTD)
    """ % (uni, ROOT, master, name_p))
    nn = con.execute("SELECT count(*) FROM read_parquet('%s')" % name_p).fetchone()[0]
    print('名称  %d/%d 只有名字 (%.1f%%)；其余多是已退市且两个源都没收录'
          % (nn, nu, 100.0 * nn / nu))

    # 🔴 **顺序不能改**：面板要 LEFT JOIN 全集(取 list_date)与名称表，
    #   所以那两张必须先落盘。第一版把面板放在最前，结果 SQL 引用了
    #   还不存在的 parquet —— 好在 DuckDB 当场报错，不是静默空列。
    # ---------------- 面板 ----------------
    # 🔴 open/high/low 必须是**不复权**：`_BAR_COLS` 自己会乘 hfq_factor。
    #   直接塞后复权价进去等于乘两次，而那不报错、只是价格全错。
    panel = os.path.join(OUT, 'mart', 'panel_daily', 'panel_etf.parquet')
    con.execute("""
    COPY (
      WITH k AS (
        SELECT symbol, date, open, high, low, close, volume, amount,
               lag(close) OVER (PARTITION BY symbol ORDER BY date) AS preclose
        FROM tdx.raw_kline_daily
        WHERE %s AND date >= DATE '%s'
      ),
      f AS (SELECT symbol, date, hfq_factor FROM tdx.v_etf_hfq)
      SELECT
        %s AS jq_code,
        k.symbol,
        k.date,
        k.open, k.high, k.low, k.close,
        round(k.close * f.hfq_factor, 6) AS close_hfq,
        f.hfq_factor,
        k.preclose,
        k.volume, k.amount,
        -- ETF 涨跌停 ±10%%（场内基金与主板个股同档）
        round(k.preclose * 1.1, 3) AS limit_up,
        round(k.preclose * 0.9, 3) AS limit_down,
        (k.close  >= round(k.preclose * 1.1, 3) - 0.0005) AS is_limit_up,
        (k.close  <= round(k.preclose * 0.9, 3) + 0.0005) AS is_limit_down,
        (k.open   >= round(k.preclose * 1.1, 3) - 0.0005) AS is_open_limit_up,
        (k.open   <= round(k.preclose * 0.9, 3) + 0.0005) AS is_open_limit_down,
        -- 首日无 preclose -> 涨跌停算不准，标 false 让 broker 跳过判定
        -- 🔴 列名是 `limit_rule_ok`，不是 `limit_ok` —— 后者是 `Bar` 里的字段名。
        --   凭印象写成 limit_ok 时 DuckDB 当场报「找不到列」（还好它报错），
        --   照主面板的真实列名抄才对（同「凭印象假设接口形状之前先确认」那条）。
        (k.preclose IS NOT NULL AND k.preclose > 0) AS limit_rule_ok,
        round((k.close / nullif(k.preclose, 0) - 1) * 100, 4) AS change_pct,
        -- ---- 以下是给【个股浮层 / 个股页】用的展示列 ----
        -- 🔴 `assay/stock.py` 的 `profile()` 是 `SELECT <58 个列名>`，
        --   **缺任何一列 SQL 直接报错**，浮层就是一片空白（而"点了没反应"
        --   是最难查的那种坏）。所以这里把那份列清单补齐：
        --     · 算得出来的照算（close_bfq / amplitude / volume_shares / 涨跌停档）
        --     · **算不出来的给 NULL，不给 0** —— ETF 没有 PB/ROE/申万行业，
        --       填 0 会在页面上显示成一个看着正常的数字（同"留空 ≠ 填 0"那条）
        k.close                                        AS close_bfq,
        round((k.high - k.low) / nullif(k.preclose,0) * 100, 4) AS amplitude,
        k.volume / 100.0                               AS volume_shares,
        10.0                                           AS limit_pct,
        n.name                                         AS sec_name,
        u.list_date                                    AS list_date,
        NULL::DOUBLE AS turnover,  NULL::DOUBLE AS floatmv, NULL::DOUBLE AS totalmv,
        NULL::DOUBLE AS pb, NULL::DOUBLE AS pe_ttm, NULL::DOUBLE AS ps_ttm,
        NULL::DOUBLE AS roe_ttm, NULL::DOUBLE AS peg, NULL::DOUBLE AS np_ttm,
        NULL::DOUBLE AS rev_ttm, NULL::DOUBLE AS rev_yoy, NULL::DOUBLE AS np_yoy,
        NULL::DOUBLE AS np_q, NULL::DOUBLE AS rev_q, NULL::DOUBLE AS np_q_yoy,
        NULL::DOUBLE AS rev_q_yoy, NULL::DOUBLE AS eps_q, NULL::DOUBLE AS roe_q,
        NULL::DOUBLE AS eps_basic, NULL::DOUBLE AS roe_parent, NULL::DOUBLE AS bps,
        NULL::DOUBLE AS revenue, NULL::DOUBLE AS net_profit_parent,
        NULL::DOUBLE AS adjusted_profit_q,
        NULL::DATE AS fin_report_date, NULL::DATE AS fin_pub_date,
        NULL::VARCHAR AS sw_l1_code, NULL::VARCHAR AS sw_l1_name,
        FALSE AS is_st, FALSE AS is_risk_warned,
        '正常上市'::VARCHAR AS public_status,
        date_diff('day', u.list_date::DATE, k.date)    AS listed_days,
        FALSE AS in_hs300, FALSE AS in_zz500, FALSE AS in_zz1000,
        FALSE AS in_sz50, FALSE AS in_cyb, FALSE AS in_kc50, FALSE AS in_zz800
      FROM k
      JOIN f ON f.symbol = k.symbol AND f.date = k.date
      LEFT JOIN read_parquet('%s') u ON u.symbol = k.symbol
      LEFT JOIN read_parquet('%s')  n ON n.code   = u.code
      WHERE k.close > 0 AND k.preclose IS NOT NULL
    ) TO '%s' (FORMAT PARQUET, COMPRESSION ZSTD)
    """ % (CODE_OK, START, jq('k.symbol'), uni, name_p, panel))
    n, nc, d0, d1 = con.execute(
        "SELECT count(*), count(DISTINCT jq_code), min(date)::VARCHAR, max(date)::VARCHAR"
        " FROM read_parquet('%s')" % panel).fetchone()
    print('面板  %s 行 / %d 只 / %s ~ %s' % ('{:,}'.format(n), nc, d0, d1))

    # ---------------- 分红：空表（见文件头）----------------
    div = os.path.join(OUT, 'std', 'dividend.parquet')
    con.execute("""
    COPY (SELECT ''::VARCHAR AS code, NULL::TIMESTAMP AS a_xr_date,
                 0.0::DOUBLE AS bonus_ratio_rmb, ''::VARCHAR AS plan_progress
          WHERE FALSE)
    TO '%s' (FORMAT PARQUET, COMPRESSION ZSTD)""" % div)

    # ---------------- 现算数据损坏边界 ----------------
    # 判据不写死日期：同一类 bug 2026-05-25 出过一次、修好后 2026-09-01 又来。
    bad = con.execute("""
      WITH k AS (
        SELECT date, jq_code, close_hfq,
               lag(close_hfq) OVER (PARTITION BY jq_code ORDER BY date) pc
        FROM read_parquet('%s'))
      SELECT date::VARCHAR d, count(*) n FROM k
      WHERE pc > 0 AND abs(close_hfq / pc - 1) > 0.5
      GROUP BY 1 HAVING count(*) >= 50 ORDER BY 1
    """ % panel).fetchall()
    safe_end = d1
    if bad:
        safe_end = con.execute(
            "SELECT max(date)::VARCHAR FROM read_parquet('%s') WHERE date < DATE '%s'"
            % (panel, bad[0][0])).fetchone()[0]
        print('🔴 检测到 %d 个【集体异动日】（>=50 只单日 |收益|>50%%，几乎必是坏数据）：'
              % len(bad))
        for d, cnt in bad[:5]:
            print('     %s  %d 只' % (d, cnt))
        print('   -> 建议回测 end 截到 %s' % safe_end)
    mf = os.path.join(OUT, '_manifest.json')
    with open(mf, 'w') as f:
        json.dump({'built_at': __import__('datetime').datetime.now().isoformat(timespec='seconds'),
                   'panel_rows': n, 'n_etf': nc, 'data_start': d0, 'data_end': d1,
                   'safe_end': safe_end,
                   'mass_break_days': [{'date': d, 'n': c} for d, c in bad]}, f,
                  ensure_ascii=False, indent=1)
    print('lake -> %s   （回测传 --datalake %s）' % (OUT, OUT))
    return safe_end


if __name__ == '__main__':
    main()
