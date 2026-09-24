#!/usr/bin/env python3
"""把通达信 gbbq（股本变迁）里的【公司行动】接入 PIT 库。

    python datalake/build/load_tdx_gbbq.py          # 导出 + 自证
    python datalake/build/load_tdx_gbbq.py --check  # 只自证，不写

## 🔴 为什么要它：聚宽那份【需要手动导出】，实盘因此丢过钱

`std/dividend.parquet` 来自聚宽增量（B 腿，手动导）。2026-09-23 实测它停在
**除权日 2026-09-09 / 公告日 2026-08-31**，于是红利账户三笔中期分红
（平安 09-10、中石油 09-16、吉比特 09-17，共 **3,560 元**）没有入账 ——
除权日股价掉了、钱没进来，**浮盈与总权益都低估**，而它不报错。

gbbq 随 `tdx2db cron` **每天自动同步**，覆盖更全、还含**已公告未除权**的：

    覆盖(2016~2026-09-09)  gbbq 41,225 条 vs 聚宽 35,593 条
                           共有 35,585、金额一致率 **99.99%**
                           只有 gbbq 的 5,640 条、只有聚宽的 8 条
    新鲜度                 gbbq 最新 2026-10-08（**提前 15 天**）
                           聚宽最新 2026-09-09

★ 聚宽那份**不删**，留作交叉校验（同「交易日历本地就能算，`grab_calendar`
  留着当交叉校验」那条）。两边"金额差"多半是**粒度**不同不是口径不同：
  同日多笔（年度+特别）gbbq 合并一条、聚宽分几条 ——
  `300750 2026-04-22` 的 69.57 = 21.78 + 47.79，按 (code,date) 汇总后才可比。

🔴 **不要用 `raw/hf/parquet/xdxr/gbbq.parquet`** —— 它停在 2026-08-25、
  **没接进 `sync_daily.sh`**，与那份同花顺概念是同一个陷阱：
  给出一个月前的数据**而页面上看着像今天的**。

## 🔴🔴 c1~c4 的含义是【对数定出来的，不是猜的】

通达信文档对这四列的说法各版本不一，所以判据取**复权因子**这个可证的事实：

    新价 = (旧价 − c1/10 + c2×c4/10) / (1 + c3/10 + c4/10)
    因子比 = 旧价 / 新价

对 `category=1` 的 **35,621** 个样本（2016~2026-09-09，已排除停牌伪影），
与面板 `hfq_factor` 的比值**平均误差 0.000000、最大误差 0.000000**：

    c1 = 每 10 股派现（税前，元）     c2 = 配股价（元）
    c3 = 每 10 股送转股               c4 = 每 10 股配股数

## 🔴 只有 category=1 影响价格

| category | 条数 | 伴随因子跳变 | 结论 |
|---|---|---|---|
| **1** | 41,900 | 35,621（85.0%） | **除权除息，唯一影响价格的** |
| 9 / 2 / 5 | 4,815 / 444 / 92,291 | 3,258 / 214 / 328 | **100% 同日也有 cat=1** -> 不独立影响价格 |
| 3 / 8 / 11 / 15 | 1,297 / 451 / 1,241 / 90 | 0 | 不影响价格 |

★ cat=1 那 15% 没跳变的（6,279 条）里 **6,179 条那天面板根本没有行情**
  （停牌 / 未上市 / 已退市）—— 不是漏处理。
★ 用同一条公式去拟合 9 / 2，平均误差 1.5 —— **它们的 c1~c4 不是这套含义**，
  所以本 loader 只取 category=1（多取一类就会把别的语义当成除权算）。

## 自证：不一致就【拒绝写出】

同 `build_trade_calendar.py` 那条纪律 —— 每次生成都重跑对数。
判据是上面那条公式与 `hfq_factor` 的逐条比对，超阈值即中止。
"""
import argparse
import os
import sys

import duckdb

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TDX = '/Users/guhao/finacial/tdx2db/tdx.db'
OUT = os.path.join(ROOT, 'raw', 'tdx', 'gbbq.parquet')
PANEL = os.path.join(ROOT, 'mart', 'panel_daily', 'panel_*.parquet')

# 🔴 只取除权除息。别为"看着相关"就多收一类 —— 9/2/5 的 c1~c4 是另一套语义
CATEGORY_XDXR = 1
# 自证阈值：实测平均误差 0.000000，留一点余量给浮点
TOL_MEAN = 1e-6
TOL_BAD_RATIO = 0.002          # 允许 0.2% 的单条超差（数据本身的个例）


def _con():
    con = duckdb.connect()
    con.execute("ATTACH '%s' AS tdx (READ_ONLY)" % TDX)
    return con


SEL = """
SELECT
  symbol,
  CASE WHEN symbol LIKE 'sh%' THEN substr(symbol, 3) || '.XSHG'
       WHEN symbol LIKE 'sz%' THEN substr(symbol, 3) || '.XSHE'
       ELSE NULL END                         AS jq_code,
  date                                       AS ex_date,
  c1 / 10.0                                  AS cash_per_share,
  c3 / 10.0                                  AS split_per_share,
  c4 / 10.0                                  AS rights_per_share,
  c2                                         AS rights_price,
  c1 AS raw_c1, c2 AS raw_c2, c3 AS raw_c3, c4 AS raw_c4
FROM tdx.raw_gbbq
WHERE category = {cat}
""".replace('{cat}', str(CATEGORY_XDXR))


def verify(con, since='2016-01-01', until='2026-09-09'):
    """与复权因子逐条对数 —— 这是 c1~c4 含义的【唯一】判据。"""
    q = """
    WITH gb AS (GBBQ_SEL),
    p AS (SELECT jq_code, date, close_bfq, hfq_factor,
            lag(hfq_factor) OVER w pf, lag(close_bfq) OVER w pc, lag(date) OVER w pd
          FROM read_parquet('{panel}')
          WHERE date BETWEEN DATE '{a}' AND DATE '{b}'
          WINDOW w AS (PARTITION BY jq_code ORDER BY date)),
    cal AS (SELECT DISTINCT date FROM read_parquet('{panel}')),
    ord AS (SELECT date, row_number() OVER (ORDER BY date) i FROM cal),
    -- 🔴 排除停牌伪影：前一个有行情的日子必须是【紧邻的上一个交易日】，
    --   否则 lag(factor) 会把停牌期间的变化归到复牌日（broker.py 里那条
    --   "已撤回的证据"就栽在这里）
    jump AS (SELECT p.jq_code, p.date, p.pc, p.hfq_factor / p.pf r
             FROM p JOIN ord a ON a.date = p.date JOIN ord b ON b.date = p.pd
             WHERE p.pf IS NOT NULL AND p.hfq_factor IS DISTINCT FROM p.pf
               AND a.i - b.i = 1)
    SELECT count(*) n,
      avg(abs(j.r - j.pc / nullif(
          (j.pc - gb.cash_per_share + gb.rights_price * gb.rights_per_share)
          / (1 + gb.split_per_share + gb.rights_per_share), 0))) mean_err,
      count(*) FILTER (WHERE abs(j.r - j.pc / nullif(
          (j.pc - gb.cash_per_share + gb.rights_price * gb.rights_per_share)
          / (1 + gb.split_per_share + gb.rights_per_share), 0)) > 1e-4) bad
    FROM gb JOIN jump j ON j.jq_code = gb.jq_code AND j.date = gb.ex_date
    """.replace('GBBQ_SEL', SEL)
    r = con.execute(q.format(panel=PANEL, a=since, b=until)).fetchone()
    n, mean_err, bad = r[0], float(r[1] or 0), r[2]
    ok = n > 10000 and mean_err <= TOL_MEAN and bad <= n * TOL_BAD_RATIO
    print('  自证：%d 条样本，平均误差 %.9f，超差 %d 条（%.3f%%）-> %s'
          % (n, mean_err, bad, 100.0 * bad / max(n, 1), 'OK' if ok else '🔴 不通过'))
    return ok


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--check', action='store_true', help='只自证，不写出')
    a = ap.parse_args()
    con = _con()
    if not verify(con):
        print('🔴 除权公式与复权因子对不上 —— 拒绝写出（c1~c4 的含义可能变了）')
        sys.exit(2)
    if a.check:
        return
    tmp = OUT + '.tmp'
    con.execute("COPY (%s ORDER BY jq_code, ex_date) TO '%s' "
                "(FORMAT parquet, COMPRESSION zstd)" % (SEL, tmp))
    os.replace(tmp, OUT)          # 原子替换
    d = con.execute("SELECT count(*), min(ex_date), max(ex_date) FROM read_parquet('%s')"
                    % OUT).fetchone()
    print('  -> %s  %d 条  %s ~ %s' % (os.path.relpath(OUT, ROOT), d[0], d[1], d[2]))


if __name__ == '__main__':
    main()
