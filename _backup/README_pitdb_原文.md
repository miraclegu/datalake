# pitdb — Point-in-Time 回测数据库

> 最后更新: 2026-08-24
> 方案: `docs/requirements/pit-database-plan.md`
> 前置教训: `docs/reports/2026-08-23-conclusion.md`

---

## 这是什么

一个能回答**「在某一天，我当时能知道什么」**的数据库。

判断一个库能不能用于回测，就看这一个问题。`tdx2db` 答不了——它是"当前快照 + 价格历史"的设计，
名称/分类/板块每次更新都被最新值覆盖（type-1 覆盖写），当天的"当时状态"一旦错过就永久丢失。

pitdb 的每一个会变的属性都带生效期（type-2），所以这个查询是成立的：

```sql
-- 2015-06-30 那天，中国中车叫什么、属什么行业、当时能看到哪期财报
    jq_code  当时名称   当时行业   后复权收盘   总市值亿  可见归母净利亿      报告期      公告日
601766.XSHG 中国中车   机械设备I     20.39    5010.2       9.72  2015-03-31  2015-04-30
```

`601766` 在 2015-06-08 才由"中国南车"更名为"中国中车"，查询取到的是**当时正确的名称**；
财报是当时**已公告**的 2015Q1，不是后来才出的中报。全链路无未来函数。

---

## 快速开始

```python
import duckdb
con = duckdb.connect('/Users/guhao/finacial/pitdb/pit.db', read_only=True)

# 库本身只有 0.5 MB —— 只有视图, 数据全在 parquet 里
con.execute("SELECT view_name FROM duckdb_views() WHERE NOT internal").df()
```

**财务优先用 L2 规范视图**，PIT 语义已经封装好：

```sql
-- 当时已公告的最新一期, 三个条件都不用自己写
SELECT * FROM fin_visible_at(DATE '2015-06-30')
```

维度仍需显式写条件（少一个就有未来函数）：

```sql
WHERE valid_from <= :d AND (valid_to IS NULL OR valid_to > :d)
  AND known_from <= :d          -- 当时的事实 + 当时已知道
```

直接查原始财务表时，三个条件缺一不可：

```sql
WHERE report_date <= :d AND pub_date <= :d AND pub_date > report_date
```

---

## 数据清单

### 行情（`kline_*`，2003-01-02 ~ 2026-08-24，10,355 只标的）

| 视图 | 行数 | 说明 |
|---|---|---|
| `kline_raw` | 24,075,338 | 不复权 OHLCV + amount |
| `kline_bfq` | 24,075,338 | 同上 + `hfq_factor` 列 |
| `kline_hfq` | 24,075,338 | `round(price * COALESCE(hfq_factor,1), 2)` |
| `kline_qfq` | 24,075,338 | `round(price * hfq_factor / latest_hfq, 2)` |
| `basic_daily` | 21,160,667 | preclose / turnover / **floatmv / totalmv** / change_pct / amplitude |
| `adjust_factor` | 21,160,667 | 后复权因子 |
| `trading_calendar` | 5,740 | 交易日 + 序号 |

复权公式**精确复刻 tdx 视图含 `round(,2)`**，实测与 `v_stock_hfq` 逐行相等（778 万行全等），
所以现有 screener 迁过来结果不变。

**只存原始价 + 因子，不存复权价**（2026-08-23 已验证：后复权序列以最早日为锚、历史不变；
前复权会随新除权整体平移）。

### PIT 维度

| 视图 | 行数 | 说明 |
|---|---|---|
| `security_universe` | 5,792 | **完整全集**，含已退市。= 年末快照 ∪ 名称史 ∪ 状态史 |
| `security_name` | 14,258 | 名称区间，`valid_from`/`valid_to`/`known_from` |
| `security_industry` | 8,748 | 行业区间，申万一级 + 证监会，**覆盖率 100%** |
| `security_status` | 11,916 | 上市/ST/披星戴帽/暂停/终止上市 事件，双日期 |
| `code_map` | 11,170 | tdx ↔ 聚宽 代码映射 + 匹配类型 |

### 指数成分历史（2005 起，按季）

| 视图/宏 | 内容 |
|---|---|
| `index_member` | **10,732 条成分区间**（191,469 行季度快照压缩 17.8 倍） |
| `index_member_asof` | 原始季度快照 |
| `index_members_at(idx, d)` | **表宏**：某日的成分名单 |

覆盖 7 个宽基：沪深300 / 中证500 / 中证1000 / 中证800 / 上证50 / 创业板指 / 科创50，
各自从发布日起（沪深300 与上证50 到 2005，科创50 到 2020）。

区间带 `is_quarterly_inferred=True` —— 它是从季度快照推断的，不是公告驱动的真实调整日；
相邻两期间隔 >100 天视为中断（季度约 91 天），所以**中途调出再调入会正确分成两段**。

为什么必须用当时的成分名单——**乐视网 2015 年是沪深300 成分股**：

| 代码 | 2015-06-30 时名称 | 退市日 |
|---|---|---|
| 300104 | **乐视网** | 2020-07-20 |
| 002450 | 康得新 | 2021-05-28 |
| 600485 | 信威集团 | 2021-05-31 |
| 000024 | 招商地产 | 2015-12-29 |

用今天的 300 只名单回测 2015 年，等于事先知道乐视、康得新会爆掉并已剔除。
这是指数增强/对冲策略里最隐蔽的一类偏差，曲线只会更好看。

### 基金与指数维度

| 视图 | 行数 | 说明 |
|---|---|---|
| `fund_universe` | 3,492 | ETF 1,741 / 指数 732 / LOF 556 / 其他基金 463 |

去重规则：同一 code 可能同时出现在 `etf` 与 `fund`（后者是超集），按
`etf > lof > index > fund` 保留最具体的分类。

### 财务快照（用于将来识别重述）

| 视图 | 行数 | 说明 |
|---|---|---|
| `fin_snapshots` | 60,460 | 12 个报告期 × 核心字段，带 `snapshot_date` |

聚宽只存最新版本，**重述历史造不出来**——但每季度跑一次
`extract_jq_round2.py` 的 ② 段，就能从现在开始累积版本历史（原理同 P0 每日快照）。
目前只有 1 个快照日，比对重述需要至少 2 个。

### 财务（2003 起，`report_type=0` 合并报表）

| 视图 | 行数 | 列 |
|---|---|---|
| `fin_income` | 273,018 | 68 |
| `fin_balance` | 273,534 | 125 |
| `fin_cashflow` | 272,477 | 92 |
| `fin_indicator` | 275,000 | 51 |

`(code, report_date)` 全部唯一。`pub_date` 是**真实首次公告日**。

### L2 规范层 —— 把语义写进列名

原始列名保留了数据源的命名，而有些名字**不足以表达语义**，会让错误的用法看起来完全合理。
这一层只做重命名与标注，**不做任何计算**。

| 视图/宏 | 内容 |
|---|---|
| `fin_core` | 利润表核心字段。**`net_profit_total`（含少数股东）/ `net_profit_parent`（归母）** 口径写进列名 |
| `fin_ratio` | 财务指标。`roe_parent` / `eps_parent` / `bps` / `nocf_per_share`，归母口径已标注 |
| `fin_visible_at(d)` | **表宏**：传入日期，返回「当时已公告的最新一期」。PIT 三条件已封装 |

为什么必须区分归母与全口径——**66.7% 的记录两者实际有差异**：

| 标的 | 归母净利 | 全口径净利 | 差异 |
|---|---|---|---|
| 万科A | 181.19 亿 | 259.49 亿 | **+43.2%** |
| 中国建筑 | 260.62 亿 | 359.43 亿 | **+37.9%** |
| 中国石化 | 322.07 亿 | 433.46 亿 | +34.6% |
| 中国平安 | 542.03 亿 | 651.78 亿 | +20.2% |

用全口径净利配归母净资产算 ROE，万科会高估 43%。不报错、不异常，只会让价值因子
系统性偏向少数股东权益大的行业（地产、建筑、能源）。

### L0 原始层（`l0_*`）

源数据原样保留（全字符串，不做任何加工），任何加工都在 L1，源头永远可回溯。

---

## 有什么 / 缺什么

### ✅ 已解决（原来的四个痛点）

| 痛点 | 解决方式 |
|---|---|
| 历史已退市股票没有名字 | `security_name` 14,258 条区间。例：`000003.XSHE` 完整还原为 `深金田A(1991) → ST金田A(2000, reason=ST) → PT金田A(2001, reason=PT)` |
| 板块只有当前快照 | `security_industry` 8,748 条区间，96 个季度时点，申万一级 100% 覆盖 |
| 没有财务数据 | 四张表 × 24 年，带真实公告日 |
| 更新时覆盖名称/板块 | 全部 type-2 带生效期；且每日快照持续积累 |

### ❌ 缺什么（明确列出，不假装完整）

| 缺失 | 数量 | 原因 / 影响 |
|---|---|---|
| **北交所** | **591 只股票 + 2 指数** | **此源不可得**（2026-08-25 实测确认）。聚宽 `types=['stock']` 返回 5,211 只，后缀只有 `XSHE`(2897) + `XSHG`(2314)，**无 4/8 开头、无其他后缀**——该免费账号就是没有北交所。它们**有日线**，但没有任何 PIT 维度。替代源：BaoStock 的 `query_stock_basic`（已查证有 `outDate`/`status`，大概率覆盖北交所） |
| 场内基金的 PIT 维度 | 1,249 只 | 见下方「关于 tdx 的 `class='etf'`」。**缺的全部已停止交易**，现存品种 100% 覆盖 |
| 有 PIT 无日线 | 132 只 | 代码变更、B 股等。`code_map.match_type='pit_only'` |
| B 股 | 52 只 | `kline_only_bshare`，有日线无 PIT |
| **财务重述版本** | — | 同一 `(code, report_date, report_type)` 只有 1 条 `pub_date`。`pub_date` 告诉你"何时能开始用"，但**数值是现在库里的版本**——公司后来重述的话，回测用到的是修正后的数字。对 PE/PB/ROE 影响小，对会计质量类因子会失真 |
| **指数成分历史** | — | `l0_idx_weight_month` 只有 1,600 行（**不完整**，整数过滤 bug 所致）。正确路径是 `get_index_stocks(code, date=)`，已验证可回溯到 2005（沪深300 @2005-06-30 = 299 只），**尚未抽取** |
| 2003 年之前 | — | 日线与维度都从 2003 起。理由：财务 `pub_date` 在 2003 前是占位值（见下） |
| 分钟线 | — | 未做。体量是日线 240 倍，而回测目标是日频 |

### 关于 tdx 的 `class='etf'` —— 它不只是 ETF

tdx 把**场内基金**全部归为 `etf`（3,934 只），实际构成（用聚宽分类交叉验证）：

| 聚宽归类 | 数量 | 典型代码段 |
|---|---|---|
| ETF | 1,724 | `sz159`(760) / `sh51x` / `sh56x` / `sh58x` |
| LOF | 553 | `sz160~163` / `sh501` |
| 其他基金（封闭式等） | 408 | `sh508` |
| **聚宽没有** | **1,249** | `sh519`(247, 场外开放式基金代码段) / 已清算的分级基金与 ETF |

其中 `sz150`(307) 是**分级基金子份额**，聚宽归为 fund/lof。

**缺的 1,249 只全部已停止交易**（实测 `仍在交易 = 0`），停更集中在 2010、2015、
2022-07-04 几个时点，是清算退市与数据源切换的痕迹。
→ 结论：**聚宽的基金全集覆盖了全部现存品种**，缺口只在历史遗留，
且这些标的的日线仍然可用，只是没有名称/上市日期等维度。

⚠️ 所以不要把 `code_map.class='etf'` 直接当成"ETF"用 —— 要真 ETF 请
`JOIN fund_universe WHERE sec_kind='etf'`。

---

## 已知陷阱（全部实测，踩过才记下来的）

### 数据语义

1. **`report_type` 必须 = 0**
   `STK_INCOME_STATEMENT` 里 0=合并报表 / 1=母公司，**不是**原始/重述。同一
   `(code, report_date)` 两条记录、`pub_date` 相同。平安银行 2015 年报：
   合并营收 961.6 亿 / 净利 218.65 亿；母公司 734.1 亿 / 198.02 亿。
   不加过滤会在两者间随机取，净利差 10%，**完全静默**。抽取阶段已过滤。

2. **`fin_indicator.net_profit_this_year` 是归母净利润**
   对 `income.np_parent_company_owners` 匹配 **99.75%**；对 `income.net_profit`
   （含少数股东）只有 **33.2%**。**信任数据源 ≠ 知道列的语义。**
   → **处理**：L2 的 `fin_core` / `fin_ratio` 把口径写进列名（`_parent` / `_total`）。
   加载时的对账只在建库那一刻跑一次，拦不住三个月后你写下一句看起来完全合理的 SQL；
   写进列名才是真的修好。**优先用 L2 视图，不要手工 join 原始表。**

3. **`pub_date` 在 2003 年前是占位值**
   实测占位率（`pub_date <= report_date`）：1989-2002 为 0.38~1.00，**2003 起为 0.00**。
   2003 后滞后中位 28~38 天，符合季报 1 个月 / 年报 4 个月的法定期限。
   库里仍有 21 条占位（IPO 时补披露的上市前财务，如国泰君安/浙商证券 2010 年数据），
   用 `pub_date > report_date` 排除。

4. **必须过滤 `source = '定期报告'`**
   `source` 有 9 种取值，31,424 行是 IPO/重组披露材料（招募说明书 16,859 / 预披露公告
   10,275 / 上市公告书 1,602 / …）。它们的 `pub_date` 常晚于报告期好几年——
   `H1008.XSHG` 的 2019 年报在 2022-06-22 才出现。已在加载阶段过滤。

5. **临时代码会被不同公司复用**
   `H1008` / `K0153` / `K1137` / `C06xx` 是待上市公司临时代码，同一 code 对应不同
   `company_id`。这是"code 不能当主键"的实证。已按 `^\d{6}\.(XSHE|XSHG)$` 过滤。

6. **`indicator` 有"两个版本"**
   76 组同一 `(code, report_date)` 有两行，`source_id` 一版有值一版为 NaN，且数值真的不同。
   用 income 当权威参照判定：`source_id` 非空版一致率 98.8%，为空版 91.7%。
   `000806.XSHE` 2020Q1 两版**符号相反**（-838万 vs +838万）——拿错版本会把亏损公司
   当成赚钱的。已按"优先保留 `source_id` 非空"消解。

7. **`index_code` 混合格式，整数比较会静默合并不同指数**
   `IDX_WEIGHT_MONTH` 用 `000300.XSHG`，`CSI_WEIGHT_MONTH` 用 `000001.CSI`，**无纯数字值**。
   用 `== 300` 会让 MySQL 把整列强转数值、截断后缀，两个体系的指数被合并
   （症状是警告 `Truncated incorrect DOUBLE value: '399940.XSHE'`）。

### 这三条的处理强度（分清"已强制"与"仅记录"）

| 陷阱 | 处理方式 | 强度 |
|---|---|---|
| `report_type` | **抽取时就不取母公司行** + 加载断言 `report_type<>0` 为 0 | 数据里根本不存在，想错用也没有 |
| `indicator` 双版本 | 加载时按 `source_id` 非空消解 + 唯一性断言 | 落库后 `(code,report_date)` 唯一 |
| 归母 vs 全口径 | **L2 列名标注**（`net_profit_parent` / `net_profit_total`） | 从"记住这个坑"变成"看名字就知道" |

### 上游数据缺陷（tdx2db）

8. **2026-05-25 TDX 改了价格编码**
   ETF 新数据偏小 10 倍（`fix_etf_price_scale.py` 已修，本库已含修正后数据）；
   **债券/可转债/逆回购 2026-05-25 之前偏大 10 倍**（逆回购年化利率中位显示 21%，
   真实 1.5%）。本库只取 stock/etf/index 三类，不受债券影响——但如果哪天要接债券，
   这个坑还在。

### 工具层

9. **DuckDB `DROP TABLE` 不回收磁盘** —— 实测 402 MiB 库删空后仍是 402 MiB。必须重建到新文件。
   这也是本库用 parquet + 视图而不是单文件大库的原因。
10. **`CREATE TABLE AS SELECT` 不保留约束** —— `duckdb_indexes()` 查不到 PK/UNIQUE，
    要查 `duckdb_constraints()`。
11. **DuckDB 内部 bug**：`BOOLEAN` 列放进 CTE + 多个 LEFT JOIN 会触发
    `ColumnBindingResolver: UBIGINT != BOOLEAN` 断言失败。用等价条件表达式绕开
    （所以查询里写 `pub_date > report_date` 而不是 `pub_date_is_placeholder = false`）。
12. **`SELECT DISTINCT col, row_number() OVER (...)`** 是无效去重 —— `row_number`
    每行唯一，DISTINCT 作用于整行等于没写。必须先在子查询去重再编号。

### 聚宽研究环境

| 约束 | 值 |
|---|---|
| `finance.run_query` 单次上限 | **5,000 行** → 必须按 id 分页 |
| Python / pandas | **3.6** / 很老，**不支持命名聚合、`to_parquet`** |
| 内存 | 800 MB。30 万行 dict 攒内存再建 DataFrame 会 OOM → 逐年落盘 + `gc.collect()` |
| `Out[n]` 输出缓存 | **永不释放**。打印过的 DataFrame 一直占内存 → `get_ipython().magic('reset -f out')` 或重启内核 |
| 文件下载 | **一次只能下一个** → 抽完打成一个 tar 再下 |
| `.csv.gz` 预览 | 报 "not UTF-8 encoded" —— 只是不能*预览*，下载正常 |
| `from jqdata import *` | **必须在模块级**，写函数里是 SyntaxError（`ast.parse` 查不出，要用 `compile()`） |
| `finance` | 星号导入不带入，须单独 `from jqdata import finance` |

---

## 目录结构与占用

```
pitdb/
├── pit.db                 0.5 MB   ← DuckDB, 28 个视图 + 2 个表宏, 不存数据
├── l0/                 1042.7 MB   ← 原始层
│   ├── kline/           389.1 MB   72 文件 (stock/etf/index × 24 年)
│   ├── basic/           405.2 MB   2 文件
│   ├── financials/      233.6 MB   4 文件
│   ├── adjust_factor.parquet
│   └── dim_*.parquet               6 张聚宽源表原样
├── l1/                    0.8 MB   ← 规范层(生效期区间 / 代码映射 / 日历)
├── jqdata/              272.1 MB   ← 聚宽下载的源 CSV(可归档, L0 已有 parquet)
├── snapshots/             0.4 MB   ← 每日 PIT 快照(内容哈希去重)
├── probes/                         ← 能力探测脚本
├── extract/                        ← 聚宽抽取脚本(在研究环境跑)
├── load/                           ← 本地加载脚本(含 build_canonical_views.py)
└── snapshot/                       ← 每日快照脚本
```

---

## 重建步骤（可复制性）

这是项目的真正验收标准：**只用代码 + 声明的源，在干净机器上重建出同样的库**。

```bash
# 1. 聚宽研究环境(整个文件粘进一个 cell)
#    产出 pitdb_out/*.csv 与 pitdb_fin/*.csv.gz, 打包下载
pitdb/extract/extract_jq_dimensions.py     # 维度, 约 60~80 次查询
pitdb/extract/extract_jq_financials.py     # 财务, 约 236 次查询, 可中断续跑
pitdb/extract/extract_jq_round2.py         # 指数成分史 / 财务快照 / 基金维度, 约 660 次

# 2. 把下载的文件放到 pitdb/jqdata/

# 3. 本地加载(顺序有依赖)
python pitdb/load/load_jq_dimensions.py --demo   # 维度 → L0/L1 + 视图
python pitdb/load/load_jq_financials.py --demo   # 财务 → L0 + 口径对账
python pitdb/load/load_tdx_kline.py --demo       # 日线 → L0/L1 + 复权视图
python pitdb/load/load_jq_round2.py --demo        # 指数成分/财务快照/基金维度
python pitdb/load/build_canonical_views.py       # L2 规范视图(语义消歧 + PIT 宏)
```

**第 4 步不可跳过** —— 没有它，`fin_indicator.net_profit_this_year` 是归母这件事
只存在于文档里，代码层面拦不住误用。

每个 loader 都带校验，**失败即非零退出**。校验项包括：唯一性、区间无重叠、
覆盖率、存活偏差交叉验证、复权口径与 tdx 逐行比对、刻度突变检测、交易日历无周末。

日线依赖 `tdx2db/tdx.db`（`ATTACH ... READ_ONLY`），但产出的 parquet 与视图**不依赖它**——
`pit.db` 自包含，拷 `pitdb/` 目录即可迁移。

---

## 每日维护

已接入 `commands/daily/full_update_tdx.sh` 第 5 步：

```bash
python3 pitdb/snapshot/daily_snapshot.py
```

按**内容哈希去重**——名称/分类/板块极少变，内容未变就不写新文件、只在 manifest
记一行。所以 manifest 是完整的逐日 PIT 记录，而磁盘只存不同版本。

**这件事不能漏。** tdx2db 是覆盖写，今天不抓，今天的"当时状态"就永久丢失，
事后无法从任何地方重建。成本是每天几十 KB。

日线增量目前需手工重跑 `load_tdx_kline.py`（全量重建约 8 秒）。

---

## 对照：能否跑通 `JQ/` 里的 69 个聚宽策略

2026-08-25 扫描 `/Users/guhao/finacial/JQ`（69 个文件，81 万字符，小市值 / 红利 / 指数增强三族），
按 API 与字段的实际引用次数逐项核对。

### ✅ 已满足

| 策略调用 | 引用 | pitdb 对应 |
|---|---|---|
| `get_price` 日线 | 134 | `kline_bfq` / `kline_hfq` / `kline_qfq` |
| `valuation.circulating_market_cap` | 166 | `basic_daily.floatmv` |
| `valuation.market_cap` | 13 | `basic_daily.totalmv` |
| `get_current_data().name` | 159 | `security_name`（PIT 名称史 14,258 段，其中 2,989 段含 ST） |
| `.is_st` | 53 | 同上 + `security_status` |
| `.day_open` / `.last_price` | 85 | `kline_bfq.open` / `.close` |
| `get_security_info` / `get_all_securities` | 118 | `security_universe` |
| `get_industry` / `get_stock_industry` | 22 | `security_industry`（PIT 申万一级） |
| `get_index_stocks` | 6 | `index_member_asof` |
| `get_trade_days` | 4 | `trading_calendar` |
| `indicator.eps` / `roe` / `adjusted_profit` | 136 | `fin_indicator` / `fin_ratio` |
| `balance.*` / `income.*` | 6 | `fin_balance` / `fin_income` |
| `finance.STK_STATUS_CHANGE` | 1 | `l0_dim_status_change` |

### ⚠️ 原料齐、需自算（每条都有口径坑）

| 需求 | 引用 | 算法 | 坑 |
|---|---|---|---|
| `pb_ratio` | 15 | `totalmv / equities_parent_company_owners` | 分母必须走 `fin_visible_at(d)`，否则用到未公告的净资产 |
| `pe_ratio` | 12 | `totalmv / 归母净利 TTM` | 聚宽是**归母** TTM；配 `net_profit_total` 会系统性低估 PE |
| `inc_net_profit_year_on_year`<br>`inc_total_revenue_year_on_year`<br>`inc_return` | 36 | `fin_indicator` 的 `*_this_year` / `*_last_year` 配对 | 聚宽这三个字段是**累计同比**还是单季，用之前必须先核一遍，别照名字猜 |
| `turnover_volatility` / `beta` / `sales_growth` 等 | 47 | 由 `basic_daily.turnover` / 日线 / `fin_indicator` 自算 | 要对齐 jqfactor 的窗口长度与去极值口径，否则数值对不上 |

> **`high_limit` / `low_limit`(133) 与 `paused`(66) 已从本表移出** —— 初版把它们判为「可自算」，
> 这是错的：涨跌停比例依板块+ST+注册制时点+上市首日分叉，自算错了**不报错**；
> `paused` 从日线断档推不出盘中临停。两项改为直接抽取，见下方 B1。

**日频 valuation 抽不动**：5,700 只 × 20 年 ≈ 2,800 万行，远超 `get_fundamentals` 的取数上限。
PE/PB 只能自算 —— 而且用 `fin_visible_at` 自算比抄聚宽的日频快照**更** PIT 正确，
因为聚宽快照用的是当时数据库里的财报版本，重述后不可复现。

### ✅ 已补齐（2026-08-25，第三轮抽取）

| 表 | 引用 | 状态 |
|---|---|---|
| **`finance.STK_XR_XD`** | **153** | ✅ **152,193 行 / 5,551 只 / 1990–2026**，视图 `dividend` |
| `finance.STK_FIN_FORCAST` | 1 | ✅ 124,094 行，视图 `dividend_forecast` |
| `finance.STK_HK_HOLD_INFO` | 1 | ⬜ 日频体积大，`extract_jq_round3.py` 里 `SEC_HKHOLD=False` |
| `finance.STK_SHAREHOLDER` | 1 | ⬜ 十大股东 —— 明确暂不做 |

分红数据的实测事实：

- **预案公告日覆盖率 1998 年起 100%**（1995 年 82%、1996 年起 ≥99.6%）。
  只有 13 条（0.0%）需要回退到登记日，且带 `visible_date_is_fallback` 标记。
  **结论：红利策略从 1998 年起可严格 PIT 回测**，无需限制起始年份。
- **预案公告 → 除权平均间隔 57–92 天**（近年约 60 天）。这就是拿除权日当可见日会造成的时序偏移量。
- `plan_progress` 实际字面量共 7 种：`董事会预案`(69,776) / `实施方案`(57,026) /
  `股东大会预案`(25,343) / `取消分红`(37) / `公司预案`(9) / `终止`(1) / `延迟实施`(1)。
  **策略里硬编码的 `STAGE_ORDER` / `CANCEL_STATES` 必须逐一对上这 7 个字面量**，
  漏一个就静默变 NaN→0，去重失效、派现金额被重复累加，且完全不报错。

### 🔬 与通达信 gbbq 的独立对账（两源互不知情）

| 项 | 结果 |
|---|---|
| 可对账记录（按 `a_xr_date` = `gbbq.date` 且 `category=1` 连接） | 53,383 条 |
| `gbbq.c1 / jq.bonus_ratio_rmb` 比值 | 中位数 1.0000，10%/90% 分位均 1.0000 |
| 派现金额 1% 容差内一致率 | **99.75%** |

**单位口径（重要，别记错）**：两者都是 **每 10 股派现（税前，元）**。

比值 1.0 只证明两源**口径相同**，完全没说单位是什么 —— 单位是靠**第三个独立量**钉死的：

```
bonus_amount_rmb(万元) / bonus_ratio_rmb × N = 股本(万股)
```

用 gbbq `category=5`（股本变动，`c4` = 变动后总股本/万股）反解得 N = 10.00。
例：浦发 2026 年 `bonus_ratio_rmb=4.20`、派现总额 1,398,845 万元 → 隐含股本 3,330,583 万股，
与 gbbq 记录的 `3330583.75` 精确吻合。

`bonus_amount_rmb` 单位是**万元**，策略里 `(bonus_amount_rmb / 10000) / market_cap(亿元)` 即由此而来。

#### 为什么 tdx 的 `raw_gbbq` 顶不了 `STK_XR_XD`

本地 `raw_gbbq` 的 `category=1` 有 68,312 条分红记录，已用价格跳空验证语义：
`c1` = 每 10 股派现（税前），`date` = **除权日**（浦发 `c1=4.20` 对应次日价差 ×10 = 4.6，
余量是当日正常波动；茅台 `c1=280.24` 对应 434.7 同理）。

但红利策略的可见性锚点是 **`board_plan_pub_date`（董事会预案公告日）**：

```python
df = _query_dividends(stock_list, finance.STK_XR_XD.board_plan_pub_date, time0, time1, ...)
```

预案公告到除权通常隔 1~2 个月。**拿除权日当可见日 = 反向未来函数** ——
该买入的时点上还"看不到"这笔分红，整个选股时序后移约一个季度。
除此之外 gbbq 还缺三样，全都无法从价格倒推：

- `plan_progress` —— 方案进度，策略靠它对同一方案的预案/股东大会/实施三条记录去重
- `bonus_cancel_pub_date` —— 已取消/终止的分红必须剔除，gbbq 根本不记这类事件
- `report_date` —— 分红归属会计年度，`_dividend_by_fiscal_year` 靠它汇总

结论：**gbbq 只能算「已实施的历史现金流」，做不了 PIT 股息率。**
（已于 2026-08-25 从聚宽补齐 `STK_XR_XD`；gbbq 现在的价值转为**独立对账源**，见上。）

---

### ⛔ 日内数据：策略 106 处引用，本地完全没有

2026-08-25 补扫（第一遍只扫了 API 名与字段名，**漏掉了调度时点与数据频率**）：

| 用法 | 次数 |
|---|---|
| `history(1, unit='1m', field='close')` | 69 |
| `get_price(..., frequency='1m', fields=['close','high_limit'], count=1)` | 36 |

调度时点：`9:05`(34) / `9:30`(38) / `14:00`(34) / `15:10`(35)，另有 `9:15` `9:31` `14:40` `15:30` `16:05`。

`tdx.db` 里有 `raw_kline_1min` 这张表，但**是空的（0 行）**，只有表壳。

**但绝大部分不必真取分钟线**：所有 1m 调用都是 `count=1`，只要某个时点的单根 bar，不要序列。
其中占比最大的 `14:00 check_limit_up`（34 处）语义是「持仓涨停股开板就卖」，
而**日线的 `low` 就能判断开没开板**：

```
low == high_limit  → 全天一字板，没开板
low <  high_limit  → 盘中开过板
```

只要有 `high_limit`（见下方 B1），这个判断不需要分钟数据，损失的只是「几点开的板」。
`9:30` 取现价下单那部分，回测用当日 `open` 近似，误差有界。

真要精确到分钟，全市场全历史是 5,500 只 × 240 根 × 5,000 天 ≈ **66 亿行**，不可行；
只取 9:30/14:00/15:10 三个时点可降到 1/80，但仍是大工程 —— 建议先用上面的近似，
量化出误差后再决定值不值得做。

### ⛔ B1 硬缺口：日频市场状态 + 指数 + 未来日历

抽取脚本：`extract/extract_jq_round4.py`

| 数据项 | 策略引用 | 为什么本地凑不出来 |
|---|---|---|
| `high_limit` / `low_limit` | 133 | **不能自算**：主板 10% / 创业板科创 20% / 北交所 30% / ST 5%，还有注册制改革切换时点与上市首日规则。分叉多，且算错**不报错** |
| `paused` | 66 | 从日线断档只能推「长期停牌」，盘中临停推不出来 |
| `is_st` | 53 | 名称含 `ST` 只覆盖一部分，`get_extras('is_st')` 才是权威口径 |
| 指数成分：`000015` 上证红利 / `399303` 国证2000 / `399101` 中小板综 / `000922` 中证红利 | 31 | `index_member` 只有 7 个宽基，这 4 个一个都没有 |
| 指数日线 | — | `index_daily` 只有 3 个（`000016`/`000300`/`000905`），且停在 **2026-06-17**，比日线晚两个多月 |
| 含未来日的交易日历 | 6 | `trading_calendar` 只到最后一个已过交易日，排不了未来调仓日 |

策略实际设的基准：`000015.XSHG`(15 次，8 个 `BENCHMARK`)、`399303.XSHE`(12 次，11 个 benchmark)、
`399101.XSHE`、`000922.XSHG`、`H00015.XSHG`。

体积控制：涨跌停价是稠密数据，按年切 + gzip；`paused` / `is_st` 是**稀疏**的，
只导出取值为真的行，未出现即为 False —— 这不是抽样，是无损编码。

---

## ⚠️ 口径警告：累计 vs 单季，两个接口是相反的

| 接口 | 口径 |
|---|---|
| `finance.STK_INCOME_STATEMENT`（**pitdb 用的**） | **累计** |
| `get_fundamentals(statDate=)` 季度 | **单季** |

实测（pitdb `fin_core`，平安银行 2017 营收，亿元）：

| 报告期 | 2017-03-31 | 2017-06-30 | 2017-09-30 | 2017-12-31 |
|---|---|---|---|---|
| 营收 | 277.12 | 540.73 | 798.33 | 1057.86 |

单调递增 = 累计。全市场 2017 年 3,205 个四期齐全的样本中，
「四期相加 ≈ 年报」命中 **0 次**。

**所以用 pitdb 算 TTM 不能直接加四期**，必须：

```
TTM = 本期累计 + 上年年报 − 上年同期累计
```

两个接口口径相反，混用会静默出错、不报任何异常。

---

## 下一步

| 优先级 | 事项 |
|---|---|
| ✅ 已完成 | ~~指数成分历史~~ —— 10,732 条区间，7 个宽基，2005 起 |
| ✅ 已完成 | ~~ETF/基金 PIT 维度~~ —— `fund_universe` 3,492 只 |
| ⛔ 此源不可得 | ~~北交所~~ —— 聚宽免费账号无此数据，需换 BaoStock |
| **周期性** | **财务快照每季度跑一次** `extract_jq_round2.py` 的 ② 段（其余两段会自动跳过）。这是积累重述历史的唯一途径，漏一季就少一个版本 |
| 中 | 用 BaoStock 补北交所的上市/退市/名称（它的 `query_stock_basic` 有 `outDate`/`status`） |
| 中 | 指数成分区间目前是**季度快照推断**（`is_quarterly_inferred=True`），真实调整日需另找源 |
| ✅ 已完成 | ~~抽 `STK_XR_XD`~~ —— 152,193 行，与 gbbq 对账一致率 99.75%，红利族已可本地复现 |
| **高** | **跑 `extract/extract_jq_round4.py`（B1）** —— 涨跌停/停牌/is_st/4 个缺失指数/未来日历，不做则小市值族的成交判定不可信 |
| 中 | 自算 PE/PB、`inc_*` 同比，落成 L2 视图（原料已齐，只差口径核对）。涨跌停价**不要自算**，走 B1 |
| 低 | 三时点分钟线（B2）—— 先用「日线 low vs high_limit 判开板」的近似，量化误差后再决定 |
| 低 | 日线增量自动化（现在是全量重建，8 秒，暂时够用） |

---

## 相关文档

- `docs/requirements/pit-database-plan.md` —— 完整方案与已验证事实汇总
- `docs/reports/2026-08-23-conclusion.md` —— 「不要缓存决策，只缓存原语」等设计原则的来源
- `docs/guides/tdx-db-schema-and-rebuild.md` —— 上游 tdx2db 的表清单与重建
