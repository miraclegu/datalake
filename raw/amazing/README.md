# raw/amazing —— 银河证券 AmazingData（第四个源）

手册正本：`finacial/AmazingData开发手册.pdf`（V1.0.24 / 2025-12-16，148 页）。
本文件是**读完那 148 页的结论**，以后不用重读 PDF。

装配与体检：`python3 datalake/setup_amazing.py`（判据都在那个文件的 docstring 里）。

---

## 🔴 定位：不替代 tdx / 聚宽，是第四个源

唯一的硬伤是**行情起点 2013 年**（手册 §2.2 明写；示例代码里也是
`start_date=20130101`）：

| 品种 | 起点 |
|---|---|
| 股票 / 指数 / 债券 / 场内基金 | **2013 年至今** |
| 期权 | 2015 年至今 |
| 期货（中金所） | 2010-04 至今 |
| 港股通 | 2023 年至今 |

对本项目的量化影响（实测）：

```
本地面板        2003-01-02 ~ 2026-09-03   16,253,495 行
2013 年之前         3,775,651 行（23.2%）
归档回测里 2013 前起的   138 / 649 次（21%）—— 全是 2005/2006 起的长周期
```

**丢掉的不是"更多数据"，是极端市场状态的样本**：2006-2007 大牛市 +
2008 崩盘。而 froec_traded 那条 edge 正是被 **2006-2015 的样本外**否证的
（样本内 +1.87pp / 样本外 −8.86pp，符号翻转；2007 年单年差 −83.8pp）。
所以 tdx 那份 1990 起的全历史**不能扔**，它的用途收窄为「做样本外检验」。

---

## 它比现有源强的地方（四条真值钱的）

### ① `get_history_stock_status` 一张表替掉三个自制模块

日频，字段：

```
PRECLOSE  HIGH_LIMITED  LOW_LIMITED  PRICE_HIGH_LMT_RATE  PRICE_LOW_LMT_RATE
IS_ST_SEC  IS_SUSP_SEC  IS_WD_SEC  IS_XR_SEC
```

本项目现在是：涨跌停价**自己按规则算 + 自校验**（`limit_rule_ok` 99.97%，
漏的 0.03% 全是「新股首日 +44%」「退市整理期首日无限制」这类特例）、
ST 靠 `public_status` 字符串、停牌是单独一张稀疏表。
这些都能换成**交易所口径的现成字段**。

### ② 行业分类带 `INDATE` / `OUTDATE`

`get_industry_constituent` 直接给 type-2 区间（纳入日 / 剔除日）。
本项目现在是从 285,781 条 as-of 快照**推**出 8,748 条区间。

⚠️ **但手册里没写是哪一套分类**（全文搜"申万"零命中），只有
`LEVEL_TYPE` 1/2/3 级 + `LEVEL1_NAME`/`LEVEL2_NAME`/`LEVEL3_NAME`。
🔴 如果不是申万，红利策略与板块页的口径就变了 —— **必须实测对数**。

### ③ 有行业指数的真实点位

`get_industry_daily`：`OPEN/HIGH/CLOSE/LOW/AMOUNT/VOLUME/PB/PE/TOTAL_CAP/
A_FLOAT_CAP/PRE_CLOSE`。
CLAUDE.md 里写着「涨幅是成分**等权平均**…**本地没有板块指数点位**」——
这条缺口它能补。

### ④ 财报保留「更正前」版本

`STATEMENT_TYPE` 码表共 26 种，其中
**`5 = 合并报表(更正前)`**：「出更正公告后，把合并报表的记录改为
合并报表(更正前)；复制原来的记录，更正后报表类型改为合并报表」。

🔴 这正对上本项目那条已知缺陷 —— README「财报重述被压平，信息滞后中位数
47 天」：同一 `(code, end_date)` 会被多次公告（业绩快报先出、定期报告后出），
而本地把它压平了、丢了版本。它保留了版本。

### ⑤ 全新能力（本项目现在一个都没有）

融资融券（成交汇总 + 交易明细）、龙虎榜、大宗交易、十大股东、股东户数、
股权冻结/质押、可转债全套（发行/份额/转股/修正/赎回/回售/停复牌，11 个接口）、
国债收益率、ETF 申赎/份额/IOPV、期权（基本资料/标准合约/月合约变动）、
指数成分**权重**（`WEIGHT`/`WEIGHT_FACTOR`/`FREE_SHARE_RATIO`/`CALC_SHARE`）。

### ⑥ 顺带绕开两个已知 bug 的源头

- `volume` 单位那个 100 倍坑：它的 K 线直接给 `amount`（成交总金额）
- `amount` 偏大那 74,969 行

---

## TTM / 单季 / 估值：能推，而且单季它直接给

- **单季**：`STATEMENT_TYPE = 2`（合并报表·单季度 = 本期 − 上一季），
  官方算好的，不用自己减
- **TTM**：四个单季相加
- **估值**：市值 = 股本 × 收盘价（`TOT_SHARE`/`FLOAT_A_SHARE` 都有），
  再除财务指标
- 所以"没有 TTM/估值因子"不算缺口 —— 那本来就是本项目 `std`/`mart` 层的活

🔴 但 26 种 `STATEMENT_TYPE` 意味着**取数时必须显式指定**，否则同一
`(code, report_date)` 会有多行、混合口径。这是个**新坑**：
不指定不会报错，只是数字对不上。

---

## 字段覆盖对照（本项目实际要的，逐个查过）

| 本项目要的 | AmazingData | |
|---|---|---|
| 日线 OHLCV + 成交额 | `query_kline` → `open/high/low/close/volume/amount` | ✅ |
| 后复权因子 | `get_backward_factor`（另有单次复权 `get_adj_factor`） | ✅ |
| 财务三表 | 资产负债 / 现金流 / 利润 | ✅ |
| **实际公告日（PIT）** | `ANN_DATE` **+ `ACTUAL_ANN_DATE`** | ✅ |
| 业绩快报 / 预告 | | ✅ |
| 分红 | 每股派息(税前) / `DATE_EQY_RECORD` 股权登记日 / `EX_DIVIDEND_DATE` / `DIV_PROGRESS` 进度码表 | ✅ |
| 股本 | `TOT_SHARE` / `FLOAT_SHARE` / `FLOAT_A_SHARE` | ✅ |
| 限售解禁 | | ✅ |
| 行业分类 | `get_industry_constituent`（带 `INDATE`/`OUTDATE`） | ⚠️ 哪套未知 |
| 指数成分 + 权重 | `get_index_weight`（50/300/500/800/1000） | ✅ |
| ST / 停牌 / 涨跌停 | `get_history_stock_status` | ✅ |
| 上市 / 退市日，**含已退市标的** | `get_stock_basic`（`IS_LISTED` 1上市/3终止） | ✅ |
| 交易日历 | `get_calendar` | ✅ |
| **通达信板块 926 个** | 没有 | 🔴 保留 tdx |

---

## 🔴 三个已知的坑（还没踩，先记着）

1. **`pip install AmazingData` 装的是另一个项目。** PyPI 上那个是
   `0.0.3`（gitee.com/zhanggao2013，13 KB，作者邮箱还是模板占位
   `your_email@example.com`），与银河的 `1.0.24` 无关；`tgw` 在 PyPI 上
   根本不存在。—— 与 `tdx2db` 那次**同一个坑**：装上去"看着成功"，
   然后 API 全对不上。只能用网盘下载的 wheel 离线装。
2. **macOS 能不能用是开放问题。** 手册推荐环境是 REDHAT 7.x / Windows 10，
   **没列 macOS**。wheel tag 是 `cpXX-none-any` —— `cpXX` 声明 CPython ABI、
   `none-any` 声明"无 ABI 要求、平台无关"，两者同时出现说明手工指定了 tag。
   **如果包里有为 Linux/Windows 编的 .so，`any` 就是谎的：装得上、import 才炸。**
   所以 `setup_amazing.py` 的体检分两步（pip 装得上 / `import` 真的成功），
   并且会扫 wheel 里有没有 `.so/.pyd/.dylib`。
   本机实测：`py3-none-any` ✓ 可装、`cp313-none-any` ✓、**`cp312-none-any` ✗**
   （Python 3.13 只认 cp313）—— 下载时要挑对版本。
3. **要券商权限**：账号/密码/ip/端口「需联系您的开户营业部申请开通」
   （手册 §3.5.1.1）。费用手册里没写。

---

## 接进来之前必须对的三样数

照本项目探针的规矩：**探针只做验证，且带已知真值当场对数** ——
"有值但对不上"比"没有值"更危险（f133 那次：招商银行股息率差一倍，
而那个数看着完全正常）。

| 对什么 | 与谁比 | 为什么它最容易错 |
|---|---|---|
| `HIGH_LIMITED` / `LOW_LIMITED` | 面板自算的涨跌停价 | 本项目 `limit_rule_ok` 99.97%，漏的是新股首日/退市整理期 —— 正好用它验 |
| `ACTUAL_ANN_DATE` | 聚宽 `pub_date` | PIT 的命门，差一天就是未来函数 |
| `LEVEL1_NAME` | 申万一级 | 手册没写是哪一套；不是申万的话红利/板块页口径就变了 |

## 分层落点（照 datalake 的三层判据）

```
raw/amazing/_ingest/     SDK 缓存、账号（account.json 不入 git）、采集脚本
raw/amazing/<表名>/      按年冻结的 parquet（原样保存，可追溯）
std/                     跨源决策后的标准层
mart/                    面板
```

★ `_ingest/account.json` **不会**被提交 —— datalake 的 `.gitignore` 是
白名单式（只放行 `*.py`/`*.md`/`*.sh`/`*.sql`），`.json` 天然进不去。
