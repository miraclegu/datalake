#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""构建 mart/panel_daily —— 回测的唯一入口。

设计原则（见 docs/requirements/datalake-3layer-plan.md 第一节）
    以 (date, jq_code) 为主键的宽面板，**每一列都是 as-of-date 正确的**。
    回测代码只读这张表，不做任何 join —— 因为所有 PIT bug 都藏在 join 里。

⚠️ 财务口径（实测，不是凭印象）
    fin_core / fin_ratio 来自 finance.STK_INCOME_STATEMENT，是【累计】口径（年初至今）。
      平安银行 2017 归母净利：Q1 6.21e9 -> H1 1.26e10 -> Q3 1.92e10 -> 年报 2.32e10
      四期全和 6.11e10 ≈ 2.6 倍年报 -> 累计确认。
    注意与另一个接口不同：get_fundamentals(statDate=...) 是【单季】口径
      （平安银行 2017 四个单季 net_profit 相加 == 年报，精确相等）。
    两个接口口径相反，算 TTM 时用错就全废。

    累计口径下：
      TTM(t)  = 累计(t) + 上年年报 - 上年同期累计     （年报本身即 TTM）
      同比     = 累计(t) / 上年同期累计 - 1            （直接可比，无需 TTM）
      单季(t)  = 累计(t) - 累计(同年上一期)            （Q1 的单季 == 累计）

    ⚠️ 【聚宽 indicator.eps 是单季，不是累计】—— 本地复现 JQ 选股时踩到的坑：
      603029 在 2018 的累计 EPS: Q1 +0.0095 -> H1 -0.07 -> Q3 -0.05
      单季 Q3 = -0.05 - (-0.07) = +0.02 > 0  -> JQ 的 eps>0 过滤【放行】
      而用累计 eps_basic = -0.05 < 0        -> 本地【剔除】，选股就差了一只
      所以复现 JQ 的 indicator.eps 必须用 eps_q（单季），不是 eps_basic（累计）。

    单季推导已交叉验证：平安银行 2017 从累计减出 62.140/63.400/65.990/40.360 亿，
    与 get_fundamentals 直接给的单季【精确一致】。
    连续性：98.06% 的报告期可算单季（267,730/273,018）。不可算的是
      「年内首期不是 Q1」5,075 期 + 「报告期跳跃(Q1->Q3 等)」213 期 -> 置 NULL。
    ⚠️ 连续性必须比【季度序号】，不能用日期减法
      （2017-06-30 减 3 个月 = 2017-03-30，与 2017-03-31 对不上，会误判不连续）。

    分层原则：raw 只存【累计】（保真，可从源重建）；std 落【单季】（好用）；
    mart 面板同时提供累计/TTM/同比/单季 —— 各司其职，不是同层放冗余字段。
    ⚠️ roe_parent / eps 等【累计比率】跨报告期不可横截面比较
      （Q1 的 ROE 天然只有年报的 1/4），做选股请用 roe_ttm。

as-of 语义（每一处都显式写出，少一个就是未来函数）
    财务   ASOF on pub_date，且先剔除「旧报告期的事后重述」
           （按 pub_date 排序取 running max(report_date)，只留 report_date=max 的行）
    行业   区间 join [valid_from, valid_to)
    ST     ASOF on GREATEST(valid_from, known_from)
           —— 状态必须【已生效】且【已知道】，两者取晚的那个
    成分   季度快照（88 个，2005-03-31 起）。快照日的选择必须【全局】：先 ASOF 出当日适用的 as_of，再按 (code, as_of)
           精确 join。若按 code 各自 ASOF，退出成分的票会一直保留最后一次"是成分"
           的状态（只进不出）—— 实测会让沪深300 变成 316 只、中证1000 变成 2097 只。
    上市   list_date/delist_date 静态，直接算 listed_days

范围
    仅 class='stock' 且 match_type='matched'，**再排除 B 股**（sh90*/sz20*）：
      · 聚宽 PIT 维度里无上市日（list_date 全空），也无财务
      · hfq_factor 有 49,519 行 <= 0（占 B 股 9%），preclose 有 26 行为负
      · 对 A 股策略无用，留着只污染统计与断言
    板块指数、ETF、指数均不入面板

涨跌停价规则（每一条都用实际 high/low 反验过）
    科创板 sh68*                      ±20%   2019-07-22 开板即 20%
    创业板 sz30*  date >= 2020-08-24  ±20%   注册制改革（ST 亦 20%）
    创业板 sz30*  ST                  ±5%
    创业板 sz30*                      ±10%
    主板   ST     date >= 2026-07-06  ±10%   <- 实证发现，见下
    主板   ST                         ±5%
    主板                              ±10%
    价格四舍五入到分: round(preclose*(1±pct), 2)

    2026-07-06 那条是从数据反推的，不是查规则查到的:
      2026-01-01~07-03  用 5% 规则越界 0.018% (3/16,934)
      2026-07-06 起     用 5% 规则越界 13.801% (732/5,304) -> 用 10% 越界 0.000%
    证据极强（10% 零越界），但具体监管依据未核实。

    limit_rule_ok = 当日实际 high/low 未越界 -> 规则对该行可信。
    新股上市前几日无涨跌幅限制（创业板/科创板前 5 日、主板 2023-04-10 后前 5 日），
    这类行 limit_rule_ok=false，因此不会产生假的涨停标记 —— 用数据兜底而不是
    枚举历次新股规则变更，更稳健。

用法
    python3 datalake/build/build_panel_daily.py --year 2024      # 单年，调试用
    python3 datalake/build/build_panel_daily.py                  # 全量
    python3 datalake/build/build_panel_daily.py --verify         # 只校验已有产物
"""
import argparse
import os
import sys

import duckdb

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB   = os.path.join(ROOT, 'lake.db')
OUT  = os.path.join(ROOT, 'mart', 'panel_daily')
OUT_PAUSED = os.path.join(ROOT, 'mart', 'paused_daily')
START_YEAR, END_YEAR = 2003, 2026

INDEXES = {'000300.XSHG': 'in_hs300', '000905.XSHG': 'in_zz500',
           '000852.XSHG': 'in_zz1000', '000016.XSHG': 'in_sz50',
           '399006.XSHE': 'in_cyb', '000688.XSHG': 'in_kc50',
           '000906.XSHG': 'in_zz800'}
# 两个用途必须用两个常量，不能共用一个：
#   ST_STATUS   -> 涨跌停【幅度】规则：ST/*ST 是 5%（2026-07-06 前）
#   RISK_STATUS -> is_st【风险标记】：还要包含「进入退市整理期」
# 实测 002711(欧浦退) 在退市整理期 limit_pct = 0.1、每天 -10%，
# 所以退市整理期不能按 5% 算 —— 共用一个常量的话，修好 is_st 就会把幅度改错。
# ★★ ASOF JOIN 的右表键必须【唯一】，否则整个面板构建是不确定的。
#    实测：同一份源数据三次重建，FROEC 年化得到 41.31% / 40.73% / 42.52%，跨度 1.8pp。
#    根因：fin_quarterly 有 8.617%、fin_indicator_q 有 8.775% 的行与另一行共享
#    (code, pub_date) —— 补披露/延迟披露会在同一天公布多个报告期
#    （000338 在 2007-06-28 一天披露 6 个报告期；300201 在 2022-12-13 披露 Q1/Q2/Q3）。
#    键并列时 ASOF 取哪一行不确定；而剔重述用的窗口 ORDER BY pub_date 在并列时
#    行序任意，运行中最大值也跟着变。
#    修法：ASOF 右表先按 (code, pub_date) 去重，同日多期取【最新那期】。
#    这不只是「消除噪声」—— 不确定的构建会让每一次对比都带上不可归因的误差。
#
# ★★★ 上面这个去重【只能加在 ASOF 右表上】，不能加在推导链上。
#    2026-08-27 实测：它原先加在 _fin（推导链源头），后果是静默丢年报 ——
#      · A 股公司常在 4 月【同一天】披露年报和一季报：L0 里 24,032 个
#        (code, pub_date) 同日多期分组，97.4% 含年报，主形态 [3,12] 有 23,398 组。
#        「同日多期取最新那期」= 留一季报、丢年报。
#      · 而 np_ttm 的公式是「本期累计 + 【上年年报】 - 上年同期累计」。
#        实测：上年年报缺失的 72,160 行 Q1/Q2/Q3，np_ttm NULL 率 100.0%；
#        上年年报存在的 131,203 行只有 5.98%。
#      · 级联到面板：pe_ttm NULL 从 36.5%(2016) 恶化到 60.8%(2026)。
#      · 级联到策略：`pe_ttm BETWEEN 5 AND 50` 遇 NULL 得 NULL -> 行被丢，
#        不报错不告警。2019 年有 169,527 行(19.2%) 属于「本地 NULL 而
#        JQ pe_ratio 落在 5~50」，即策略本该看见却从未看见的股票。
#        用 JQ 2019 valuation 对拍红利价值的选股：JQ 每期都比本地多选 2~3 只，
#        9 个 JQ 独有的入选票里 6 个正是 np_ttm=NULL。
#    所以：**推导表要完整（含全部报告期），去重只属于 as-of 那一步。**
#    现在 _fin 不去重、不做单调过滤；ASOF 用单独的 _fin3_asof。

# 成交量单位陷阱：tdx 原始 volume 字段的值是【股数 × 100】。
#   交叉验证：volume/100 算换手率得 0.4731，面板 turnover 列 0.473107，精确吻合；
#   茅台 2024-06-28 volume/100 = 3,858,202 股，与 amount/close 推得的 3,884,878 股
#   相符（差 0.7% 来自收盘价 vs 均价）。日成交 200~400 万股正是茅台的量级。
#   既不是「股」也不是「手」（若是手则股数应为 volume×100）。
#   最可能是采集侧对一个本已是「股」的字段又做了一次「手->股」的 x100 转换。
#
#   处理方式遵循本项目既有原则（见 build_canonical_views.py）：
#   **把语义写进列名，让人不必记住这些坑**。mart 层【只暴露正确的列】：
#     volume_shares = 股数
#   原始值不在 mart 层重复保留 —— 可追溯性由 raw/tdx/kline 承担，
#   那才是「按采集原样保存」该待的层。清洗层再带一份错误列就违背了分层的意义。
#   光加注释不够 —— 有人写 where="volume > 1e7" 做流动性过滤就会错 100 倍；
#   改名后旧查询会直接报错，而不是静默算错 100 倍。
ST_STATUS = ("'ST'", "'*ST'")
RISK_STATUS = ("'ST'", "'*ST'", "'进入退市整理期'")

# listed_days < 0（上市日之前就有 K 线）的【已知】案例白名单。
# 用白名单而不是放宽阈值 —— 这样任何【新】案例都会被断言抓到。
#   600018.XSHG 上港集团: 聚宽 list_date=2006-10-26（上港集团上市日），
#     但 tdx 有 2003-01-02 起的 K 线 —— 那是「上港集箱」时期。
#     2006 年上港集团换股吸并上港集箱整体上市，沿用了 600018 这个代码。
#     两边数据都对，是同一代码指向不同实体。862 行 / 0.005%。
#   注意 tdx 的 stock_merge_history 只记北交所代码变更(bj430→bj920)，
#     不含这类换股吸并，所以无法系统识别，只能白名单。
KNOWN_PRELIST_KLINE = {'600018.XSHG'}

# 主板 ST 涨跌幅由 5% 改 10% 的生效日（实证反推，见文件头）
ST_MAIN_10PCT_FROM = '2026-07-06'

# 涨跌停幅度（顺序敏感：具体在前）。{st} 填 ST 状态列表，{st10} 填切换日
LIMIT_PCT_TMPL = (
    "CASE"
    " WHEN c.symbol LIKE 'sh68%' THEN 0.20"
    " WHEN c.symbol LIKE 'sz30%' AND c.date >= DATE '2020-08-24' THEN 0.20"
    " WHEN c.symbol LIKE 'sz30%' AND s.public_status IN ({st}) THEN 0.05"
    " WHEN c.symbol LIKE 'sz30%' THEN 0.10"
    " WHEN s.public_status IN ({st}) AND c.date >= DATE '{st10}' THEN 0.10"
    " WHEN s.public_status IN ({st}) THEN 0.05"
    " ELSE 0.10 END")

FAILURES = []
def check(cond, msg):
    print(('  ✓ ' if cond else '  ✗ ') + msg)
    if not cond:
        FAILURES.append(msg)


def prepare(con):
    """建临时视图：as-of 逻辑集中在这里，只写一次"""
    # 财务：剔除「旧报告期的事后重述」——按 pub_date 推进取 running max(report_date)
    # ★★★ 这里【不能】做 (code, pub_date) 去重，也不能做 report_date 单调过滤。
    #     两者都会丢年报，而 np_ttm 的公式恰恰需要「上年年报」——见文件头
    #     「同日披露年报+一季报」那节。去重只属于 ASOF 右表（见下方 _fin3_asof）。
    #     fin_core(定期报告) 的 (code, report_date) 实测唯一（0 个重复组），
    #     所以这里不去重不会让下游 join 扇出。
    con.execute("""
    CREATE OR REPLACE TEMP VIEW _fin AS
    SELECT c.*, f.roe_parent, f.bps, f.total_assets, f.equities
    FROM fin_core c
    LEFT JOIN fin_ratio f ON f.code=c.code AND f.report_date=c.report_date
    """)
    # 累计口径 -> TTM 与同比。年报本身即 TTM；其余用「本期累计 + 上年年报 - 上年同期累计」
    con.execute("""
    CREATE OR REPLACE TEMP VIEW _fin2 AS
    SELECT f.*,
      CASE WHEN month(f.report_date)=12 THEN f.net_profit_parent
           ELSE f.net_profit_parent + a.net_profit_parent - p.net_profit_parent END AS np_ttm,
      CASE WHEN month(f.report_date)=12 THEN f.revenue
           ELSE f.revenue + a.revenue - p.revenue END AS rev_ttm,
      CASE WHEN p.revenue > 0 THEN f.revenue / p.revenue - 1 END AS rev_yoy,
      CASE WHEN p.net_profit_parent > 0
           THEN f.net_profit_parent / p.net_profit_parent - 1 END AS np_yoy
    FROM _fin f
    LEFT JOIN _fin a ON a.code=f.code
         AND a.report_date = make_date(year(f.report_date)-1, 12, 31)
    LEFT JOIN _fin p ON p.code=f.code
         AND p.report_date = f.report_date - INTERVAL 1 YEAR
    """)
    # 单季：累计(t) - 累计(同年上一期)。Q1 的单季即累计。
    # 连续性比【季度序号】而非日期减法（见文件头）。不连续则置 NULL。
    con.execute("""
    CREATE OR REPLACE TEMP VIEW _finq AS
    WITH z AS (
      SELECT f.*,
        lag(f.net_profit_parent) OVER w AS _pnp,
        lag(f.revenue)           OVER w AS _prev,
        lag(f.report_date)       OVER w AS _prd,
        lag(f.eps_basic)         OVER w AS _peps
      FROM _fin2 f
      WINDOW w AS (PARTITION BY f.code, year(f.report_date) ORDER BY f.report_date)
    ), y AS (
      SELECT z.*,
        (quarter(report_date) = 1
         OR quarter(report_date) - quarter(_prd) = 1) AS q_ok
      FROM z
    )
    SELECT y.*,
      CASE WHEN NOT q_ok THEN NULL
           WHEN quarter(report_date)=1 THEN net_profit_parent
           ELSE net_profit_parent - _pnp END AS np_q,
      CASE WHEN NOT q_ok THEN NULL
           WHEN quarter(report_date)=1 THEN revenue
           ELSE revenue - _prev END AS rev_q,
      CASE WHEN NOT q_ok THEN NULL
           WHEN quarter(report_date)=1 THEN eps_basic
           ELSE eps_basic - _peps END AS eps_q
    FROM y
    """)
    # 单季同比：本期单季 / 上年同期单季 - 1（比累计同比更敏感）
    con.execute("""
    CREATE OR REPLACE TEMP VIEW _fin3 AS
    SELECT a.* EXCLUDE (_pnp, _prev, _prd, _peps),
      CASE WHEN b.np_q  > 0 THEN a.np_q  / b.np_q  - 1 END AS np_q_yoy,
      CASE WHEN b.rev_q > 0 THEN a.rev_q / b.rev_q - 1 END AS rev_q_yoy
    FROM _finq a
    LEFT JOIN _finq b ON b.code=a.code
         AND b.report_date = a.report_date - INTERVAL 1 YEAR
    """)
    # 聚宽权威单季指标。剔除「同一 code 的旧报告期事后重述」——
    # 与 _fin 用同一套规则：按公告日滚动取最大报告期，早于它的都是重述。
    con.execute("""
    CREATE OR REPLACE TEMP VIEW _ind AS
    -- 见文件头「ASOF JOIN 的右表键必须唯一」
    WITH d AS (
      SELECT * EXCLUDE (_rn) FROM (
        SELECT i.*, row_number() OVER (PARTITION BY i.code, i.pub_date
                      ORDER BY i.report_date DESC) AS _rn
        FROM read_parquet('%s') i
      ) WHERE _rn = 1
    )
    SELECT * EXCLUDE (_max_rd) FROM (
      SELECT d.*, max(d.report_date) OVER (
               PARTITION BY d.code ORDER BY d.pub_date
               ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) AS _max_rd
      FROM d
    ) WHERE report_date = _max_rd
    """ % os.path.join(ROOT, 'std', 'fin_indicator_q.parquet'))

    con.execute("""
    CREATE OR REPLACE TEMP VIEW _shr AS
    -- ★★ 流通股本的【双键 as-of】：可见性看 pub_date，有效性看 change_date。
    --   缺一个键就错：tdx 的 floatmv 等价于只看 change_date，于是在 2015-12-31
    --   就用上了 2016-03-31 才披露的年报口径 —— 这是 look-ahead，不是数据错。
    --   实证 300317.XSHE：
    --     change_date 2015-10-16 转增     pub_date 2015-10-10  流通 16744.3595万
    --     change_date 2015-12-31 定期报告 pub_date 2016-03-31  流通  9317.7268万
    --   2015-12-31 决策时市场只能知道 16744.3595万（×24.64 = 41.26亿），
    --   聚宽开了 avoid_future_data 给的正是 41.258亿；tdx 给 9317.7268万（-44%）。
    --   小市值策略按流通市值【升序】取最小 N 只，被低估的票被系统性推到前面，
    --   是选择性偏差而非随机噪声 —— 所以这一项必须 PIT 正确。
    --
    --   定增【不】立刻增加流通股本，share_change 已严格建模锁定期：
    --     增发新股上市 → 只增 share_total，新股全额进 share_limited
    --     限售股份上市 / 激励股份解禁 → share_total 不变，share_trade_total 才增
    --   恒等式 share_total = share_trade_total + share_limited 每行自洽。
    --
    --   arg_max 取「已可见行中 change_date 最大者」；排序键带 pub_date 破平局，
    --   保证同一 change_date 的后续修订版本只在其 pub_date 之后才生效。
    SELECT code, pub_date, float_sh, total_sh FROM (
      SELECT code, pub_date,
             arg_max(fs, ord) OVER win AS float_sh,
             arg_max(ts, ord) OVER win AS total_sh,
             row_number() OVER (PARTITION BY code, pub_date
                 ORDER BY change_date DESC) AS _rn
      FROM (
        SELECT code, change_date, pub_date,
               -- ★ 流通【A股】= share_trade_total - B股 - H股。
               --   share_trade_total 是「全部无限售流通股」，含 B/H。
               --   聚宽 circulating_market_cap 只算 A 股，实测四例精确吻合：
               --     600054 黄山旅游 含B 64.602亿 / 扣B 27.770亿 = JQ 27.770亿
               --     000756 新华制药 含H 61.326亿 / 扣H 41.211亿 = JQ 41.211亿
               --     002705/300317 无B/H，扣不扣都等于 JQ
               --   321 个代码有 B/H 股，其中 15.99% 的行 B/H 字段为 NULL ——
               --   必须【前向结转】，直接 COALESCE(...,0) 会把这些行的流通股
               --   算大一倍多。只前向不后向：B股发行之前确实没有 B 股，
               --   后向填充会把未来才存在的 B 股倒推到发行前（look-ahead）。
               greatest(share_trade_total - bf_b - bf_h, 0) * 1e4 AS fs,
               share_total * 1e4 AS ts,
               {'c': change_date, 'p': pub_date} AS ord
        FROM (
          SELECT * EXCLUDE (share_trade_total),
                 -- ★★ 「定期报告」行的 share_trade_total 会【回退】到某个已被
                 --   解禁事件超越的旧值 —— 源数据问题，实测 603536.XSHG：
                 --     2018-06-13 限售股份上市  流通 6025.41万
                 --     2018-06-30 定期报告      流通 4200.00万  <- 回退
                 --     2018-12-31 定期报告      流通 6025.41万  <- 又回来
                 --   双键 as-of 按 change_date 取最大，就会在 2018-06~12 期间
                 --   取到那个错的 4200万，把流通市值算成 3.46 亿（真值 4.97 亿），
                 --   于是它在「流通市值升序取最小 N 只」里被顶到第 1 名 ——
                 --   聚宽同期根本没买它。这是 v0b 对标残差的成因之一。
                 --   规模：全表 7.74% 的行「流通降而总股本未降」，其中 77% 是定期报告。
                 -- 护栏：定期报告【不得】把流通股压到低于最近一个【事件行】的值。
                 --   只管定期报告 —— 回购 / 承诺限售 是事件行，仍可正常下调流通股。
                 CASE WHEN change_reason = '定期报告' AND ev_f IS NOT NULL
                      THEN greatest(share_trade_total, ev_f)
                      ELSE share_trade_total END AS share_trade_total
          FROM (
            SELECT *,
                   COALESCE(share_b, last_value(share_b IGNORE NULLS) OVER w, 0) AS bf_b,
                   COALESCE(share_h, last_value(share_h IGNORE NULLS) OVER w, 0) AS bf_h,
                   last_value(CASE WHEN change_reason <> '定期报告'
                                   THEN share_trade_total END IGNORE NULLS) OVER w AS ev_f
            FROM read_parquet('{shr}')
            WHERE share_trade_total IS NOT NULL AND share_trade_total > 0
              AND pub_date IS NOT NULL
            WINDOW w AS (PARTITION BY code ORDER BY change_date, pub_date
                ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW)
          )
        )
      )
      WINDOW win AS (PARTITION BY code ORDER BY pub_date
          ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW)
    ) WHERE _rn = 1
    """.replace('{shr}', os.path.join(ROOT, 'std', 'share_change.parquet')))

    # ASOF 专用右表：键必须唯一，否则整个面板构建不确定（实测跨度 1.8pp）。
    # 只在这里去重 —— 推导链（np_ttm 需要上年年报）用的是完整的 _fin3。
    # 同日多期取【最新那期】，这对 as-of「当日已知的最新财报」正是想要的语义。
    con.execute("""
    CREATE OR REPLACE TEMP VIEW _fin3_asof AS
    WITH d AS (
      SELECT * EXCLUDE (_rn) FROM (
        SELECT t.*, row_number() OVER (PARTITION BY t.code, t.pub_date
                      ORDER BY t.report_date DESC) AS _rn
        FROM _fin3 t
      ) WHERE _rn = 1
    )
    -- 剔除「旧报告期的事后重述」：按 pub_date 推进取 running max(report_date)。
    -- 注：loader 已实测「定期报告」自身没有多版本（275,229 组里 0 组），
    -- 所以这一步当前是 no-op；保留以防将来放宽 source 过滤。
    SELECT * EXCLUDE (_max_rd) FROM (
      SELECT d.*, max(d.report_date) OVER (PARTITION BY d.code ORDER BY d.pub_date
               ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) AS _max_rd
      FROM d
    ) WHERE report_date = _max_rd
    """)

    # 落到 std 层供其它消费方使用（面板之外也能用单季）
    # ⚠️ 这份是【完整】的：含全部报告期（年报在内），因此 (code, pub_date) 可能重复
    #    （年报与一季报同日披露，23,398 组）。消费方做 as-of 时必须自行按
    #    (code, pub_date) 取最新 report_date —— 不能假设键唯一。
    std_out = os.path.join(ROOT, 'std', 'fin_quarterly.parquet')
    con.execute("""COPY (SELECT code, report_date, pub_date, q_ok,
                          net_profit_parent AS np_cum, revenue AS rev_cum,
                          np_q, rev_q, eps_q, np_q_yoy, rev_q_yoy, np_ttm, rev_ttm,
                          equities, bps
                   FROM _fin3 ORDER BY code, report_date)
                   TO '%s' (FORMAT parquet, COMPRESSION zstd)""" % std_out)
    n = con.execute("SELECT count(*) FROM read_parquet('%s')" % std_out).fetchone()[0]
    print('  std/fin_quarterly.parquet: %s 行' % format(n, ','))
    # ST：状态须【已生效】且【已知道】——取两者较晚者作为生效点
    con.execute("""
    CREATE OR REPLACE TEMP VIEW _status AS
    -- 同上：eff_from 并列时 ASOF 取哪行不确定，先去重（同日多条取最后一条状态）
    SELECT * EXCLUDE (_rn) FROM (
      SELECT code, greatest(valid_from, known_from) AS eff_from, public_status, name,
             row_number() OVER (PARTITION BY code, greatest(valid_from, known_from)
                        ORDER BY valid_from DESC, known_from DESC) AS _rn
      FROM security_status
    ) WHERE _rn = 1
    """)
    # 指数成分：月度快照 → 每个 (code, as_of) 一行宽表
    cols = ',\n'.join(
        "max(CASE WHEN index_code='%s' THEN 1 ELSE 0 END) AS %s" % (k, v)
        for k, v in INDEXES.items())
    con.execute("""
    CREATE OR REPLACE TEMP VIEW _idx AS
    SELECT stock_code AS code, as_of, %s
    FROM index_member_asof GROUP BY 1, 2
    """ % cols)


def build_year(con, y):
    idx_sel = ',\n           '.join(
        'COALESCE(x.%s, 0)::TINYINT AS %s' % (v, v) for v in INDEXES.values())
    sql = """
    WITH base AS (
        -- 只取能对上聚宽 PIT 维度的 A 股；跨年多取一天用于算 ret_1d
        SELECT k.date, m.jq_code, k.symbol,
               k.close AS close_bfq, k.open, k.high, k.low,
               k.volume, k.amount, k.hfq_factor,
               round(k.close * k.hfq_factor, 2) AS close_hfq
        FROM kline_bfq k
        JOIN code_map m ON m.tdx_symbol = k.symbol
        WHERE m.class='stock' AND m.match_type='matched'
          AND k.symbol NOT LIKE 'sh90%' AND k.symbol NOT LIKE 'sz20%'   -- 排除 B 股
          AND k.date >= DATE '{y}-01-01' - INTERVAL 12 DAY
          AND k.date <  DATE '{y1}-01-01'
    ), px AS (
        SELECT *, lag(close_hfq) OVER (PARTITION BY jq_code ORDER BY date) AS prev_hfq
        FROM base
    ), cur AS (
        SELECT * FROM px WHERE date >= DATE '{y}-01-01'
    )
    SELECT
        c.date, c.jq_code, c.symbol,
        c.close_bfq, c.open, c.high, c.low,
        c.volume / 100 AS volume_shares,   -- 单位见文件头；原始值只留在 raw 层
        c.amount,
        c.close_hfq, c.hfq_factor,
        CASE WHEN c.prev_hfq > 0 THEN c.close_hfq / c.prev_hfq - 1 END AS ret_1d,
        b.preclose, b.change_pct, b.amplitude, b.turnover,
        -- ★ 市值为 0 是【缺失值伪装成极值】，不是真值：300114.XSHE 有真实价格
        --   与成交量却 floatmv=totalmv=0（tdx 股本数据缺失）。按市值【升序】选股时
        --   0 永远排第一 —— 实测它从 2016 年起 2056 个交易日一直霸占 v0b 候选池首位，
        --   每次调仓挤掉一只真票，使选股命中率从 94.9% 掉到 66.2%。
        --   置 NULL 让它被比较运算自然剔除，而不是伪装成"最小市值"。
        --   根治要等 P1-b 采 valuation.circulating_market_cap。
        -- ★ floatmv 优先用 share_change 重建（见 prepare 里 _shr 的实证），
        --   取不到时回落到 tdx 值并置 0 为 NULL（0 是缺失伪装成极值：
        --   300114.XSHE 有真实价量却 floatmv=0，按市值升序会永远排第一，
        --   实测它从 2016 起 2056 个交易日霸占 v0b 候选池首位）。
        COALESCE(sh.float_sh * c.close_bfq, nullif(b.floatmv, 0)) AS floatmv,
        nullif(b.floatmv, 0) AS floatmv_tdx,   -- 原值留痕，便于审计
        nullif(b.totalmv, 0) AS totalmv,
        -- 财务（as-of，可审计）
        f.report_date AS fin_report_date, f.pub_date AS fin_pub_date,
        f.revenue, f.net_profit_parent, f.eps_basic,
        f.roe_parent, f.bps, f.total_assets, f.equities,
        -- TTM 与同比（累计口径推导，见文件头）
        f.np_ttm, f.rev_ttm, f.rev_yoy, f.np_yoy,
        -- 单季。★ eps_q / roe_q 用【聚宽权威值】(get_fundamentals(indicator))，
        --   与策略里 get_history_fundamentals(indicator.roe/eps) 同源同口径。
        --   本地推算版保留为 *_derived，只作对账与降级兜底，**不要拿来选股**。
        --   实测：indicator.roe 的分母是【平均净资产 (期初+期末)/2】——
        --   257,525 条比对，平均净资产 87.35% / 期末 41.22% / 期初 40.08%。
        --   本地原先用期末，所以 FROEC 选股命中率卡在 72.1%。
        f.np_q, f.rev_q, f.np_q_yoy, f.rev_q_yoy,
        ai.eps AS eps_q,
        ai.roe AS roe_q,
        ai.adjusted_profit AS adjusted_profit_q,
        ai.report_date AS ind_report_date, ai.pub_date AS ind_pub_date,
        f.eps_q AS eps_q_derived,
        CASE WHEN f.equities > 0 AND f.np_q IS NOT NULL
             THEN f.np_q / f.equities END AS roe_q_derived,
        -- 估值（分母用【当日可见】的财报 -> 真 PIT，比现成的 pb/pe 更严格）
        CASE WHEN f.equities > 0 THEN b.totalmv / f.equities END AS pb,
        CASE WHEN f.np_ttm  > 0 THEN b.totalmv / f.np_ttm  END AS pe_ttm,
        CASE WHEN f.rev_ttm > 0 THEN b.totalmv / f.rev_ttm END AS ps_ttm,
        CASE WHEN f.equities > 0 AND f.np_ttm IS NOT NULL
             THEN f.np_ttm / f.equities END AS roe_ttm,
        CASE WHEN f.np_ttm > 0 AND f.np_yoy > 0
             THEN (b.totalmv / f.np_ttm) / (f.np_yoy * 100) END AS peg,
        -- 维度（as-of）
        i.sw_l1_code, i.sw_l1_name,
        CASE WHEN s.public_status IN ({risk}) THEN 1 ELSE 0 END::TINYINT AS is_st,
        nmh.name AS sec_name,   -- 见下：名称必须取自 security_name
        -- 复刻聚宽 filter_st_stock 的【四条】检查，不只是 is_st：
        --   not is_st and 'ST' not in name and '*' not in name and '退' not in name
        -- 实测 149 只「进入退市整理期」的票 is_st 原为 0，会被小市值策略买入 ——
        -- 它们价格已崩塌、流通市值极小，恰好排在市值升序最前，暴露最大化。
        (s.public_status IN ({risk})
         OR nmh.name LIKE '%ST%' OR nmh.name LIKE '%*%' OR nmh.name LIKE '%退%')
            AS is_risk_warned,
        s.public_status,
        u.list_date,
        date_diff('day', u.list_date, c.date) AS listed_days,
        -- 涨跌停（规则见文件头；limit_rule_ok=false 时不要用这几列判涨停）
        {lim} AS limit_pct,
        round(b.preclose * (1 + ({lim})), 2) AS limit_up,
        round(b.preclose * (1 - ({lim})), 2) AS limit_down,
        (b.preclose > 0
         AND c.high <= round(b.preclose * (1 + ({lim})), 2) + 0.005
         AND c.low  >= round(b.preclose * (1 - ({lim})), 2) - 0.005) AS limit_rule_ok,
        -- 直接可用的判据：is_open_limit_up 就是「开盘即封、买不进」
        (b.preclose > 0 AND abs(c.close_bfq - round(b.preclose*(1+({lim})),2)) < 0.005
         AND c.high <= round(b.preclose*(1+({lim})),2) + 0.005) AS is_limit_up,
        (b.preclose > 0 AND abs(c.close_bfq - round(b.preclose*(1-({lim})),2)) < 0.005
         AND c.low  >= round(b.preclose*(1-({lim})),2) - 0.005) AS is_limit_down,
        (b.preclose > 0 AND abs(c.open - round(b.preclose*(1+({lim})),2)) < 0.005
         AND c.high <= round(b.preclose*(1+({lim})),2) + 0.005) AS is_open_limit_up,
        (b.preclose > 0 AND abs(c.open - round(b.preclose*(1-({lim})),2)) < 0.005
         AND c.low  >= round(b.preclose*(1-({lim})),2) - 0.005) AS is_open_limit_down,
        -- 指数成分（as-of）
        {idx}
    FROM cur c
    LEFT JOIN basic_daily b ON b.symbol=c.symbol AND b.date=c.date
    -- 流通股本（双键 as-of：可见性 pub_date + 有效性 change_date）
    --   条件必须是 pub_date，用 change_date 会引入 look-ahead（见 _shr 注释）
    ASOF LEFT JOIN _shr    sh ON sh.code=c.jq_code AND sh.pub_date <= c.date
    ASOF LEFT JOIN _fin3_asof f ON f.code=c.jq_code AND f.pub_date  <= c.date
    -- 聚宽权威单季指标（eps/roe/扣非），按公告日 as-of，与 _fin3 同样是 PIT
    ASOF LEFT JOIN _ind    ai ON ai.code=c.jq_code AND ai.pub_date <= c.date
    ASOF LEFT JOIN _status s ON s.code=c.jq_code AND s.eff_from  <= c.date
    -- 先全局定位当日适用的快照日，再精确 join（见文件头「成分」说明）
    ASOF LEFT JOIN (SELECT DISTINCT as_of FROM index_member_asof) sn
         ON sn.as_of <= c.date
    LEFT JOIN _idx x ON x.code=c.jq_code AND x.as_of=sn.as_of
    -- ★ 名称取自 security_name（专用名称历史表），不能用 security_status.name。
    --   security_status 只在【状态】变化时才有新行，公司改名而状态不变时
    --   它的 name 字段就停在旧值 —— 实测 2024-06-28 有 888/5088 行（17.4%）
    --   与权威名称不符（688109 品茗股份->品茗科技、600929 湖南盐业->雪天盐业，
    --   全是「正常上市」故无状态变更行）。
    --   这不只影响显示：is_risk_warned 的名称检查也用它 ——
    --   实测 60 行 / 2 只股票会因过期名称被漏判风险。
    LEFT JOIN security_name nmh ON nmh.code=c.jq_code
         AND nmh.valid_from <= c.date
         AND (nmh.valid_to IS NULL OR nmh.valid_to > c.date)
    LEFT JOIN security_industry i ON i.code=c.jq_code
         AND i.valid_from <= c.date AND (i.valid_to IS NULL OR i.valid_to > c.date)
    LEFT JOIN security_universe u ON u.code=c.jq_code
    ORDER BY c.jq_code, c.date
    """.format(y=y, y1=y + 1, st=','.join(ST_STATUS), risk=','.join(RISK_STATUS), idx=idx_sel,
               lim=LIMIT_PCT_TMPL.format(st=','.join(ST_STATUS),
                                         st10=ST_MAIN_10PCT_FROM))
    out = os.path.join(OUT, 'panel_%d.parquet' % y)
    con.execute("COPY (%s) TO '%s' (FORMAT parquet, COMPRESSION zstd)" % (sql, out))
    n = con.execute("SELECT count(*) FROM read_parquet('%s')" % out).fetchone()[0]
    mb = os.path.getsize(out) / 1048576.0
    print('  %d  %10s 行  %7.1f MB' % (y, format(n, ','), mb))
    return n


def build_paused(con):
    """停牌表：交易日历里有、面板里无 = 全天停牌。

    为什么单独一张表而不是面板里的一列：停牌的票【本来就没有行】，
    列放不进去。选股时面板已天然正确（停牌票不在里面选不到）；
    但持仓时需要知道手里的票停牌了（卖不出），那就查这张表。

    只覆盖【全天停牌】。盘中临时停牌（有成交、只交易半天）在日线上看不出来，
    不在此表中 —— 那种情况实盘仍能买卖，对选股无影响。
    """
    print('\n' + '=' * 72)
    print('构建 mart/paused_daily')
    print('=' * 72)
    P = "read_parquet('%s/panel_*.parquet')" % OUT
    out = os.path.join(OUT_PAUSED, 'paused.parquet')
    con.execute("""
    COPY (
      WITH expect AS (   -- 在市期间 x 交易日 = 本应有行情的 (code, date)
        SELECT u.code AS jq_code, t.date
        FROM security_universe u
        CROSS JOIN trading_calendar t
        WHERE u.list_date IS NOT NULL
          AND t.date >= greatest(u.list_date, DATE '2003-01-01')
          AND t.date <= least(coalesce(u.delist_date, DATE '2200-01-01'), DATE '2026-08-24')
          AND u.code IN (SELECT DISTINCT jq_code FROM %s)
      )
      SELECT e.date, e.jq_code
      FROM expect e
      LEFT JOIN %s p ON p.jq_code=e.jq_code AND p.date=e.date
      WHERE p.jq_code IS NULL
      ORDER BY e.date, e.jq_code
    ) TO '%s' (FORMAT parquet, COMPRESSION zstd)""" % (P, P, out))
    n = con.execute("SELECT count(*) FROM read_parquet('%s')" % out).fetchone()[0]
    print('  %s 行  %.1f MB' % (format(n, ','), os.path.getsize(out) / 1048576.0))

    # 与已知历史事件对账：2015-07-08~10 千股停牌
    print('  停牌最多的 5 天:')
    for r in con.execute("""SELECT date, count(*) c FROM read_parquet('%s')
        GROUP BY 1 ORDER BY 2 DESC LIMIT 5""" % out).fetchall():
        print('    %s  %s 只' % (r[0], format(r[1], ',')))
    return n


def verify(con):
    print('\n' + '=' * 72); print('校验'); print('=' * 72)
    P = "read_parquet('%s/panel_*.parquet')" % OUT
    n = con.execute('SELECT count(*) FROM %s' % P).fetchone()[0]
    print('  面板总行数: %s' % format(n, ','))

    # 1) 行数应等于 kline 里 matched 股票的行数
    k = con.execute("""SELECT count(*) FROM kline_bfq k JOIN code_map m
        ON m.tdx_symbol=k.symbol WHERE m.class='stock' AND m.match_type='matched'
          AND k.symbol NOT LIKE 'sh90%' AND k.symbol NOT LIKE 'sz20%'
          AND k.date >= DATE '2003-01-01'""").fetchone()[0]
    check(n == k, '行数与 kline(stock,matched,非B股) 一致 (%s vs %s)'
          % (format(n, ','), format(k, ',')))

    # 2) 主键唯一
    d = con.execute('SELECT count(*) FROM (SELECT date, jq_code FROM %s '
                    'GROUP BY 1,2 HAVING count(*)>1)' % P).fetchone()[0]
    check(d == 0, '主键 (date, jq_code) 唯一 (重复 %d)' % d)
    if d:
        # 主键重复曾出现过一次且不复现（3 次重建中 1 次、1 行）。
        # 与其盲追不可复现的个例，先保证【下次发生时能被诊断】：
        # 把重复行整行打出来，而不是只报一个计数。
        print('    ↳ 重复主键明细（最多 10 组）:')
        dup = con.execute('SELECT date, jq_code, count(*) n FROM %s '
                          'GROUP BY 1,2 HAVING count(*)>1 ORDER BY n DESC LIMIT 10' % P).df()
        print('\n'.join('      ' + x for x in dup.to_string(index=False).split('\n')))

    # 3) 🔴 PIT 核心：财务公告日绝不晚于当日
    bad = con.execute('SELECT count(*) FROM %s WHERE fin_pub_date > date' % P).fetchone()[0]
    check(bad == 0, 'fin_pub_date <= date（无未来财务） (违反 %d)' % bad)

    # 4) 关键列空值率
    # ★★ 流通股本必须与股份变动表的【PIT】值一致 —— 这条错误靠对标 JQ 才发现，
    #   代价是 FROEC 长期 +4.44pp 的残差归因不掉。现在在构建时就拦。
    #   基准必须用双键 as-of（pub_date 可见 + change_date 生效）；用 change_date
    #   做基准等于拿 look-ahead 校验 look-ahead，会双双"通过"。
    #   总股本是对照组：它本来就 99.95% 吻合，若它一起掉说明 join 口径写错了。
    r = con.execute("""
        WITH pit AS (
          SELECT code, pub_date, float_sh, total_sh FROM (
            SELECT code, pub_date,
                   arg_max(fs, ord) OVER win AS float_sh,
                   arg_max(ts, ord) OVER win AS total_sh,
                   row_number() OVER (PARTITION BY code, pub_date
                       ORDER BY change_date DESC) AS _rn
            FROM (SELECT code, change_date, pub_date,
                         greatest(share_trade_total - bf_b - bf_h, 0)*1e4 AS fs,
                         share_total*1e4 AS ts,
                         {'c': change_date, 'p': pub_date} AS ord
                  FROM (SELECT * EXCLUDE (share_trade_total),
                          -- 与面板同一护栏：定期报告不得低于最近事件行（见 _shr 注释）
                          CASE WHEN change_reason = '定期报告' AND ev_f IS NOT NULL
                               THEN greatest(share_trade_total, ev_f)
                               ELSE share_trade_total END AS share_trade_total
                        FROM (SELECT *,
                          COALESCE(share_b, last_value(share_b IGNORE NULLS) OVER w, 0) bf_b,
                          COALESCE(share_h, last_value(share_h IGNORE NULLS) OVER w, 0) bf_h,
                          last_value(CASE WHEN change_reason <> '定期报告'
                              THEN share_trade_total END IGNORE NULLS) OVER w AS ev_f
                        FROM read_parquet('%s')
                        WHERE share_trade_total > 0 AND pub_date IS NOT NULL
                        WINDOW w AS (PARTITION BY code ORDER BY change_date, pub_date
                            ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW))))
            WINDOW win AS (PARTITION BY code ORDER BY pub_date
                ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW)
          ) WHERE _rn = 1
        ), pn AS (
          SELECT jq_code AS code, date,
                 floatmv/nullif(close_bfq,0) AS mfs,
                 totalmv/nullif(close_bfq,0) AS mts
          FROM %s
          WHERE close_bfq > 0 AND floatmv > 0 AND date >= DATE '2016-01-01'
        )
        SELECT round(100.0*sum(CASE WHEN abs(pn.mfs/k.float_sh-1)<0.01
                                    THEN 1 ELSE 0 END)/count(*), 2),
               round(100.0*sum(CASE WHEN pn.mfs < k.float_sh*0.9
                                    THEN 1 ELSE 0 END)/count(*), 2),
               round(100.0*sum(CASE WHEN pn.mts IS NULL OR abs(pn.mts/k.total_sh-1)<0.01
                                    THEN 1 ELSE 0 END)/count(*), 2)
        FROM pn ASOF JOIN pit k ON k.code=pn.code AND k.pub_date<=pn.date
    """ % (os.path.join(ROOT, 'std', 'share_change.parquet'), P)).fetchone()
    check(r[0] >= 99.0, '流通股本与股份变动表 PIT 值吻合 %.2f%% >= 99%%' % r[0])
    check(r[1] <= 0.5, '流通股本被低估>10%% 仅 %.2f%%（单向偏差，小市值按升序选股）' % r[1])
    check(r[2] >= 99.0, '总股本吻合 %.2f%% >= 99%%（对照组）' % r[2])

    # ★ 上面三条把护栏加进了【两侧】，对护栏本身是同义反复。所以单独盯住护栏的
    #   【规模】：源数据若变（修好了、或坏得更多），这个比例会动，
    #   必须被发现而不是静默通过。
    r = con.execute("""
        WITH s AS (
          SELECT change_reason, share_trade_total f,
                 last_value(CASE WHEN change_reason <> '定期报告'
                     THEN share_trade_total END IGNORE NULLS) OVER w AS ev_f
          FROM read_parquet('%s')
          WHERE share_trade_total > 0 AND pub_date IS NOT NULL
          WINDOW w AS (PARTITION BY code ORDER BY change_date, pub_date
              ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW))
        SELECT round(100.0*sum(CASE WHEN change_reason='定期报告' AND ev_f IS NOT NULL
                 AND f < ev_f - 1e-6 THEN 1 ELSE 0 END)/count(*), 2) FROM s
    """ % os.path.join(ROOT, 'std', 'share_change.parquet')).fetchone()
    check(4.0 <= r[0] <= 10.0,
          '定期报告回退被护栏修正 %.2f%% 的行（预期 4~10%%，建立时实测 6.90%%）'
          % r[0])

    for col, lim in (('close_hfq', 0.001), ('floatmv', 0.02), ('sw_l1_name', 0.05)):
        r = con.execute('SELECT avg(CASE WHEN %s IS NULL THEN 1.0 ELSE 0 END) FROM %s'
                        % (col, P)).fetchone()[0]
        check(r <= lim, '%s 空值率 %.3f%% <= %.1f%%' % (col, r * 100, lim * 100))

    # 4b) fin_report_date 的空值有两个【已知且正确】的来源, 必须排除后再看:
    #     · 2003 年 —— 财务起点在 2003, 年初尚无任何"已公告"报表可见(82% 空, PIT 正确)
    #     · list_date 为空的票 —— 124 只(主要是 B 股), 聚宽 PIT 维度里无上市日, 亦无财务
    r = con.execute("""SELECT avg(CASE WHEN fin_report_date IS NULL THEN 1.0 ELSE 0 END)
        FROM %s WHERE year(date) > 2003 AND list_date IS NOT NULL""" % P).fetchone()[0]
    check(r <= 0.02, 'fin_report_date 空值率(排除2003与无list_date) %.3f%% <= 2%%' % (r * 100))
    nb = con.execute('SELECT count(*) FROM %s WHERE list_date IS NULL' % P).fetchone()[0]
    print('    [信息] 无 list_date 的行 %s (%.2f%%), 消费方应显式过滤'
          % (format(nb, ','), 100.0 * nb / n))

    # 5) 收益率无异常
    o = con.execute('SELECT count(*) FROM %s WHERE abs(ret_1d) > 0.45' % P).fetchone()[0]
    check(o < n * 0.0001, 'ret_1d 超 ±45%% 的行 %d (< %.0f)' % (o, n * 0.0001))

    # 5b) 🔴 PIT: 不应有上市日之前的行情（已知代码复用案例走白名单）
    rows = con.execute('''SELECT jq_code, count(*) FROM %s WHERE listed_days < 0
        GROUP BY 1 ORDER BY 2 DESC''' % P).fetchall()
    codes = {r[0] for r in rows}
    unexpected = codes - KNOWN_PRELIST_KLINE
    check(not unexpected, 'listed_days < 0 仅限已知代码复用案例 (意外: %s)'
          % (sorted(unexpected) or '无'))
    if rows:
        print('    [信息] 上市前有 K 线（代码复用）: '
              + ', '.join('%s(%d行)' % (c, n) for c, n in rows))

    # 5c) 涨跌停规则可信率（不可信的行几乎全是新股无涨跌幅限制期）
    r = con.execute('SELECT avg(CASE WHEN limit_rule_ok THEN 1.0 ELSE 0 END) FROM %s'
                    % P).fetchone()[0]
    check(r >= 0.999, 'limit_rule_ok 占比 %.4f%% >= 99.9%%' % (r * 100))

    # 5d) 估值列合理性
    r = con.execute("""SELECT
        avg(CASE WHEN pb IS NULL THEN 1.0 ELSE 0 END) pb_null,
        median(pb) pb_med, median(pe_ttm) pe_med, median(roe_ttm) roe_med
        FROM %s WHERE year(date) >= 2010""" % P).fetchone()
    check(r[0] <= 0.10, 'pb 空值率 %.2f%% <= 10%%' % (r[0] * 100))
    check(1.0 <= r[1] <= 5.0, 'pb 中位数 %.2f 在 [1,5]' % r[1])
    check(15 <= r[2] <= 60, 'pe_ttm 中位数 %.1f 在 [15,60]' % r[2])
    check(0.02 <= r[3] <= 0.15, 'roe_ttm 中位数 %.4f 在 [0.02,0.15]' % r[3])
    # TTM 自洽：年报当期 np_ttm 必须等于年报本身
    d = con.execute("""SELECT count(*) FROM %s
        WHERE month(fin_report_date)=12 AND np_ttm IS NOT NULL
          AND abs(np_ttm - net_profit_parent) > 1""" % P).fetchone()[0]
    check(d == 0, '年报当期 np_ttm == 年报净利 (违反 %d)' % d)

    # 5e) 单季自洽：Q1 的单季必须等于累计
    d = con.execute("""SELECT count(*) FROM %s
        WHERE quarter(fin_report_date)=1 AND np_q IS NOT NULL
          AND abs(np_q - net_profit_parent) > 1""" % P).fetchone()[0]
    check(d == 0, 'Q1 单季 == 累计 (违反 %d)' % d)
    r = con.execute('SELECT avg(CASE WHEN np_q IS NULL THEN 1.0 ELSE 0 END) FROM %s '
                    'WHERE fin_report_date IS NOT NULL' % P).fetchone()[0]
    check(r <= 0.05, 'np_q 空值率 %.2f%% <= 5%%（报告期不连续时置 NULL）' % (r * 100))

    # 6) 每日股票数: 用分位而非 min —— min 会被真实的极端停牌日打到
    #    (2015-07-09 千股停牌只有 1,408 只, kline 里也是 1,408, 是历史事实不是缺陷)
    r = con.execute("""SELECT quantile_cont(c,0.05), median(c), max(c) FROM
        (SELECT date, count(*) c FROM %s WHERE date >= DATE '2015-01-01' GROUP BY 1)"""
        % P).fetchone()
    check(r[0] >= 2000 and 2500 <= r[1] <= 6000 and r[2] <= 6500,
          '2015后每日股票数 p5=%.0f 中位=%.0f 上限=%.0f' % r)
    ex = con.execute("""SELECT date, c FROM (SELECT date, count(*) c FROM %s
        WHERE date >= DATE '2015-01-01' GROUP BY 1) WHERE c < 2000 ORDER BY c LIMIT 3"""
        % P).fetchall()
    if ex:
        print('    [信息] 极端停牌日: ' + ', '.join('%s(%d只)' % (d, c) for d, c in ex))

    # 6b) 停牌表：2015 股灾三天应是历史峰值
    pp = os.path.join(OUT_PAUSED, 'paused.parquet')
    if os.path.exists(pp):
        top = con.execute("SELECT date FROM read_parquet('%s') GROUP BY 1 "
                          "ORDER BY count(*) DESC LIMIT 5" % pp).fetchall()
        tops = {str(r[0])[:10] for r in top}
        hit = len(tops & {'2015-07-08', '2015-07-09', '2015-07-10',
                          '2015-07-13', '2015-09-07'})
        check(hit >= 2, '停牌峰值命中 2015 股灾 (%d/5 天在前五: %s)'
              % (hit, sorted(tops)))

    # 7) PIT 抽查：601766 在 2015-06-30 的财务应是 2015Q1
    r = con.execute("""SELECT fin_report_date, fin_pub_date, sw_l1_name FROM %s
        WHERE jq_code='601766.XSHG' AND date=DATE '2015-06-30'""" % P).fetchone()
    ok = r and str(r[0])[:10] == '2015-03-31'
    check(ok, 'PIT 抽查 601766@2015-06-30 财务=2015Q1 (实际 %s, 公告 %s, 行业 %s)'
          % (str(r[0])[:10] if r else '无', str(r[1])[:10] if r else '-',
             r[2] if r else '-'))
    return n


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--year', type=int)
    ap.add_argument('--verify', action='store_true')
    a = ap.parse_args()
    for d in (OUT, OUT_PAUSED):
        if not os.path.exists(d):
            os.makedirs(d)
    con = duckdb.connect(DB)
    prepare(con)
    if not a.verify:
        print('=' * 72); print('构建 mart/panel_daily'); print('=' * 72)
        years = [a.year] if a.year else range(START_YEAR, END_YEAR + 1)
        tot = sum(build_year(con, y) for y in years)
        print('  合计 %s 行' % format(tot, ','))
        if not a.year:
            build_paused(con)
    if a.verify or not a.year:
        verify(con)
    con.close()
    if FAILURES:
        print('\n❌ %d 项未通过:' % len(FAILURES))
        for m in FAILURES:
            print('   -', m)
        # ★ 面板是先写盘、后校验的，所以校验失败时坏数据【已经在磁盘上】。
        #   只 exit(1) 不够 —— 调用方若不检查退出码（我自己就漏过一次），
        #   下游会静默地在坏面板上跑出「看着正常」的结果。
        #   落一个 _FAILED 标记，让 PanelFeed 直接拒绝加载。
        with open(os.path.join(OUT, '_FAILED'), 'w', encoding='utf-8') as f:
            f.write('\n'.join(FAILURES) + '\n')
        print('   已落 %s —— 下游会拒绝加载此面板' % os.path.join(OUT, '_FAILED'))
        sys.exit(1)
    # 成功则清除上一次的失败标记
    _mk = os.path.join(OUT, '_FAILED')
    if os.path.exists(_mk):
        os.remove(_mk)
    print('\n✅ 全部通过')


if __name__ == '__main__':
    main()
