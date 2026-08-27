# tdx2db 数据库表字段文档

> ⚠️ **本文档已过期（2026-08-23）**。库在 2026-08-23 做过一次瘦身，删掉了 20 张表 + 23 个视图
> （34.66 GiB → 约 5 GiB）。本文档里描述的 `stocks`、`stock_indicators`、`buy_signals`、
> `sell_signals`、`daily_stop_loss` 等表，除 `stock_indicators` 外均已不存在。
> **当前表清单、删除依据与重建手册见 `docs/guides/tdx-db-schema-and-rebuild.md`。**
> 本文档保留仅供查阅历史字段定义。
>
> 数据库: tdx2db/tdx.db (DuckDB)
> 生成时间: 2026-06-12
> 数据来源: 通达信 .day 文件 + 派生计算

---

## 一、表总览

### 基础数据层（tdx2db Go 工具自动生成）

| 表名 | 类型 | 说明 | 创建来源 |
|:--|:--|:--|:--|
| `_meta` | 表 | 数据库元信息 | tdx2db 自动 |
| `stocks` | 表 | 股票代码与上市日期 | tdx2db 自动 |
| `raw_kline_daily` | 表 | 日线K线（OHLCV） | 通达信 .day 文件 |
| `raw_kline_1min` | 表 | 1分钟K线（当前为空） | 通达信客户端 |
| `raw_gbbq` | 表 | 股本变迁（送转配） | 通达信 gbbq 文件 |
| `raw_adjust_factor` | 表 | 复权因子 | tdx2db 自动计算 |
| `raw_basic_daily` | 表 | 基础日线（涨跌幅/换手/市值） | tdx2db 自动计算 |
| `raw_symbol_name` | 表 | 证券名称 | 通达信 base 文件 |
| `raw_symbol_class` | 表 | 证券分类（类型/板块） | 通达信 base 文件 |
| `raw_holidays` | 表 | 节假日列表 | tdx2db 内置 |
| `raw_tdx_blocks_info` | 表 | 通达信板块信息 | 通达信 base 文件 |
| `raw_tdx_blocks_member` | 表 | 通达信板块成份股 | 通达信 base 文件 |

### 复权视图层（tdx2db 自动生成）

| 视图名 | 说明 |
|:--|:--|
| `v_stock_bfq` | 股票不复权日线 |
| `v_stock_hfq` | 股票后复权日线 |
| `v_stock_qfq` | 股票前复权日线 |
| `v_etf_bfq` | ETF 不复权日线 |
| `v_etf_hfq` | ETF 后复权日线 |
| `v_etf_qfq` | ETF 前复权日线 |

### 技术指标层（Python 脚本生成）

| 表名 | 说明 | 创建脚本 |
|:--|:--|:--|
| `stock_indicators` | 技术指标（MA/MACD/RSI/ATR等） | `scripts/indicators.py` |
| `daily_stop_loss` | ATR原始止损价数据 | `AlphaMiner/verification/scripts/precompute_stop_loss.py` |

### 信号层（Python 脚本生成）

| 表名 | 说明 | 创建脚本 |
|:--|:--|:--|
| `buy_signals` | 买入信号记录 | `AlphaMiner/verification/scripts/create_signal_tables_tdxdb.py` |
| `sell_signals` | 卖出信号记录 | 同上 |

---

## 二、各表字段详情

---

### 2.1 `_meta` — 元数据表

| 字段名 | 类型 | 说明 |
|:--|:--|:--|
| `key` | VARCHAR | 键名 |
| `value` | VARCHAR | 值 |

---

### 2.2 `stocks` — 股票代码表

| 字段名 | 类型 | 说明 |
|:--|:--|:--|
| `symbol` | VARCHAR | 证券代码（如 sz000001 / sh600519） |
| `listing_date` | VARCHAR | 上市日期（格式: YYYY-MM-DD） |

> **代码格式**: `sh`/`sz` + 6位数字，sh = 沪市，sz = 深市

---

### 2.3 `raw_kline_daily` — 日线K线原始表

| 字段名 | 类型 | 说明 |
|:--|:--|:--|
| `symbol` | VARCHAR | 证券代码 |
| `open` | DOUBLE | 开盘价 |
| `high` | DOUBLE | 最高价 |
| `low` | DOUBLE | 最低价 |
| `close` | DOUBLE | 收盘价 |
| `amount` | DOUBLE | 成交额（元） |
| `volume` | BIGINT | 成交量（股） |
| `date` | DATE | 日期 |

> **数据范围**: 1990-12-19 至最新交易日。数据为未复权原始价格。

---

### 2.4 `raw_kline_1min` — 1分钟K线表

| 字段名 | 类型 | 说明 |
|:--|:--|:--|
| `symbol` | VARCHAR | 证券代码 |
| `open` | DOUBLE | 开盘价 |
| `high` | DOUBLE | 最高价 |
| `low` | DOUBLE | 最低价 |
| `close` | DOUBLE | 收盘价 |
| `amount` | DOUBLE | 成交额 |
| `volume` | BIGINT | 成交量 |
| `datetime` | TIMESTAMP WITH TIME ZONE | 时间 |

> **当前为空表**，仅 Windows 本地客户端支持实时数据导入。

---

### 2.5 `raw_gbbq` — 股本变迁表

| 字段名 | 类型 | 说明 |
|:--|:--|:--|
| `category` | BIGINT | 变更类别 |
| `symbol` | VARCHAR | 证券代码 |
| `date` | DATE | 变更日期 |
| `c1` | DOUBLE | 送股 |
| `c2` | DOUBLE | 配股 |
| `c3` | DOUBLE | 派息 |
| `c4` | DOUBLE | 其他 |

> 数据来自通达信 gbbq 文件，是复权因子计算的基础数据。

---

### 2.6 `raw_adjust_factor` — 复权因子表

| 字段名 | 类型 | 说明 |
|:--|:--|:--|
| `symbol` | VARCHAR | 证券代码 |
| `date` | DATE | 日期 |
| `hfq_factor` | DOUBLE | 后复权因子 |

> 根据 `raw_gbbq` 和 `raw_kline_daily` 自动计算。当日价格 × `hfq_factor` = 后复权价格。

---

### 2.7 `raw_basic_daily` — 基础日线数据表

| 字段名 | 类型 | 说明 |
|:--|:--|:--|
| `symbol` | VARCHAR | 证券代码 |
| `date` | DATE | 日期 |
| `close` | DOUBLE | 收盘价 |
| `preclose` | DOUBLE | 昨日收盘价 |
| `change_pct` | DOUBLE | 涨跌幅（%） |
| `amplitude` | DOUBLE | 振幅（%） |
| `turnover` | DOUBLE | 换手率（%） |
| `floatmv` | DOUBLE | 流通市值 |
| `totalmv` | DOUBLE | 总市值 |

---

### 2.8 `raw_symbol_name` — 证券名称表

| 字段名 | 类型 | 说明 |
|:--|:--|:--|
| `symbol` | VARCHAR | 证券代码 |
| `name` | VARCHAR | 中文名称 |

---

### 2.9 `raw_symbol_class` — 证券分类表

| 字段名 | 类型 | 说明 |
|:--|:--|:--|
| `symbol` | VARCHAR | 证券代码 |
| `type` | VARCHAR | 类型（stock/etf/index/cbond等） |

---

### 2.10 `raw_holidays` — 节假日表

| 字段名 | 类型 | 说明 |
|:--|:--|:--|
| `date` | DATE | 节假日日期 |

---

### 2.11 `raw_tdx_blocks_info` — 通达信板块信息表

| 字段名 | 类型 | 说明 |
|:--|:--|:--|
| `block_name` | VARCHAR | 板块名称 |
| `block_type` | VARCHAR | 板块类型（概念/行业/地域等） |

---

### 2.12 `raw_tdx_blocks_member` — 通达信板块成份股表

| 字段名 | 类型 | 说明 |
|:--|:--|:--|
| `block_name` | VARCHAR | 板块名称 |
| `symbol` | VARCHAR | 成份股代码 |

---

### 2.13 复权视图 — `v_stock_*` / `v_etf_*`

所有复权视图结构一致，以 `v_stock_hfq` 为例：

| 字段名 | 类型 | 说明 |
|:--|:--|:--|
| `symbol` | VARCHAR | 证券代码 |
| `date` | DATE | 日期 |
| `open` | DOUBLE | 开盘价（后复权） |
| `high` | DOUBLE | 最高价（后复权） |
| `low` | DOUBLE | 最低价（后复权） |
| `close` | DOUBLE | 收盘价（后复权） |
| `amount` | DOUBLE | 成交额 |
| `volume` | BIGINT | 成交量 |

> **复权说明**: hfq = 后复权，bfq = 不复权，qfq = 前复权
> **信号系统统一使用后复权价格**，确保历史信号的可比性。

---

### 2.14 `stock_indicators` — 技术指标表

| 字段名 | 类型 | 说明 |
|:--|:--|:--|
| `symbol` | VARCHAR | 证券代码 |
| `date` | VARCHAR | 日期（YYYY-MM-DD） |
| `open` | DOUBLE | 开盘价 |
| `high` | DOUBLE | 最高价 |
| `low` | DOUBLE | 最低价 |
| `close` | DOUBLE | 收盘价 |
| `amount` | DOUBLE | 成交额 |
| `volume` | BIGINT | 成交量 |
| | | |
| **均线类** | | |
| `MA5` | DOUBLE | 5日均线 |
| `MA10` | DOUBLE | 10日均线 |
| `MA20` | DOUBLE | 20日均线 |
| `MA60` | DOUBLE | 60日均线 |
| `MA200` | DOUBLE | 200日均线 |
| `MA250` | DOUBLE | 250日均线（年线） |
| `MA500` | DOUBLE | 500日均线 |
| | | |
| **EMA 类** | | |
| `EMA5` | DOUBLE | 5日指数移动平均 |
| `EMA10` | DOUBLE | 10日指数移动平均 |
| `EMA12` | DOUBLE | 12日指数移动平均 |
| `EMA20` | DOUBLE | 20日指数移动平均 |
| `EMA26` | DOUBLE | 26日指数移动平均 |
| | | |
| **MACD** | | |
| `MACD` | DOUBLE | MACD 值（DIF） |
| `MACD_Signal` | DOUBLE | MACD 信号线（DEA） |
| `MACD_Hist` | DOUBLE | MACD 柱（2×(DIF-DEA)） |
| | | |
| **RSI** | | |
| `RSI6` | DOUBLE | 6日 RSI |
| `RSI14` | DOUBLE | 14日 RSI |
| `RSI24` | DOUBLE | 24日 RSI |
| | | |
| **KDJ** | | |
| `KDJ_K` | DOUBLE | KDJ K 值 |
| `KDJ_D` | DOUBLE | KDJ D 值 |
| `KDJ_J` | DOUBLE | KDJ J 值 |
| | | |
| **布林带** | | |
| `BOLL_Mid` | DOUBLE | 布林带中轨（MA20） |
| `BOLL_Upper` | DOUBLE | 布林带上轨 |
| `BOLL_Lower` | DOUBLE | 布林带下轨 |
| | | |
| **波动率 / 趋势** | | |
| `ATR` | DOUBLE | 平均真实波幅（20日） |
| `OBV` | DOUBLE | 能量潮指标 |
| `VWAP` | DOUBLE | 成交量加权均价 |
| `CCI10` | DOUBLE | 10日商品通道指数 |
| `CCI20` | DOUBLE | 20日商品通道指数 |
| `ROC12` | DOUBLE | 12日变动率 |
| `WR` | DOUBLE | 威廉指标 |
| `PDI` | DOUBLE | 上升动向指标（+DI） |
| `MDI` | DOUBLE | 下降动向指标（-DI） |
| `ADX` | DOUBLE | 平均趋向指数 |
| | | |
| **乖离率** | | |
| `BIAS6` | DOUBLE | 6日乖离率 |
| `BIAS20` | DOUBLE | 20日乖离率 |
| `BIAS60` | DOUBLE | 60日乖离率 |
| | | |
| **收益 / 风险** | | |
| `DAILY_RETURN` | DOUBLE | 日收益率 |
| `VOLATILITY_20` | DOUBLE | 20日波动率 |
| `VOLATILITY_60` | DOUBLE | 60日波动率 |
| `RETURN_5` | DOUBLE | 5日累计收益率 |
| `RETURN_10` | DOUBLE | 10日累计收益率 |
| `RETURN_20` | DOUBLE | 20日累计收益率 |
| `RETURN_60` | DOUBLE | 60日累计收益率 |
| `DRAWDOWN_20` | DOUBLE | 20日最大回撤 |
| `DRAWDOWN_60` | DOUBLE | 60日最大回撤 |
| | | |
| **其他** | | |
| `high_20d` | DOUBLE | 20日最高价 |
| `trailing_stop` | DOUBLE | 跟踪止损价 |
| `volume_ma5` | DOUBLE | 5日均量 |
| `volume_ma10` | DOUBLE | 10日均量 |
| `volume_ma20` | DOUBLE | 20日均量 |

> **创建脚本**: `tdx2db/scripts/indicators.py`，基于 `v_stock_hfq` 计算。
> **价格口径**: 所有价格类指标基于后复权价格。

---

### 2.15 `daily_stop_loss` — 每日ATR原始止损价

| 字段名 | 类型 | 说明 |
|:--|:--|:--|
| `symbol` | VARCHAR | 证券代码 |
| `date` | VARCHAR | 日期（YYYY-MM-DD） |
| `high_20d` | DOUBLE | 20日最高价（后复权） |
| `atr_20` | DOUBLE | 20日平均真实波幅 |
| `atr_stop_price` | DOUBLE | 每日原始ATR止损价 = high_20d - 3×ATR20 |
| `atr_stop_price_35` | DOUBLE | 每日原始ATR止损价3.5倍 = high_20d - 3.5×ATR20 |

> **重要**: `atr_stop_price` 是**每日原始值，不含棘轮累积**。
> 棘轮止损（累积最大值）由卖出信号生成脚本按买入信号粒度实时计算，
> 确保棘轮从买入日起始，而非从股票上市日起累积。
>
> **创建脚本**: `AlphaMiner/verification/scripts/precompute_stop_loss.py`
> **数据来源**: 从 `stock_indicators` 读取 `HIGH_20D` 和 `ATR`
> **用途**: 为卖出信号生成脚本（如 `STOP_COMBO_10PCT_3ATR`）提供预计算数据

---

### 2.16 `buy_signals` — 买入信号表

| 字段名 | 类型 | 说明 |
|:--|:--|:--|
| `id` | INTEGER | 主键 |
| `strategy_code` | VARCHAR | 买入策略代码（见下方枚举） |
| `stock_code` | VARCHAR | 股票代码 |
| `signal_date` | VARCHAR | 买入信号触发日期（YYYY-MM-DD） |
| `close` | DOUBLE | 信号日收盘价（后复权） |
| `status` | VARCHAR | 信号日状态（NORMAL / LIMIT_UP_OPEN / LIMIT_DOWN_OPEN） |
| `t1_open` | DOUBLE | T+1日开盘价（后复权） |
| `t1_close` | DOUBLE | T+1日收盘价（后复权） |
| `t1_status` | VARCHAR | T+1日状态 |
| `t1_limit_up_next_available` | VARCHAR | T+1日为一字涨停时的首个可交易日 |
| `t2_date` | VARCHAR | T+2日日期 |
| `t2_open` | DOUBLE | T+2日开盘价（后复权） |
| `t2_close` | DOUBLE | T+2日收盘价（后复权） |
| `t2_status` | VARCHAR | T+2日状态 |
| `t2_limit_up_next_available` | VARCHAR | T+2日为一字涨停时的首个可交易日 |
| `days_since_last_signal` | INTEGER | 距同策略上一次买入信号的交易日数 |
| `signal_count_20d` | INTEGER | 前20个交易日同策略买入信号次数 |
| `signal_count_40d` | INTEGER | 前40个交易日同策略买入信号次数 |

> **买入策略代码枚举**:
>
> | 代码 | 说明 |
> |:--|:--|
> | `HIGH_20D_BREAKOUT` | 20日高点突破 |
> | `MA_CROSS` | 通用均线交叉 |
> | `MA_CROSS_5_10` / `MA_CROSS_5_20` / `MA_CROSS_10_20` / `MA_CROSS_20_60` | 指定周期均线交叉 |
> | `BREAKOUT` | 突破关键价位 |
> | `BOLL_BAND` | 布林带突破 |
> | `RSI_OVERSOLD` / `RSI_OVERBOUGHT` | RSI超卖/超买 |
> | `MACD_CROSS` | MACD金叉/死叉 |
> | `VOLUME_SPIKE` | 量能异动 |
> | `PRICE_REVERSAL` | 价格反转 |
>
> **创建脚本**: `AlphaMiner/verification/scripts/create_signal_tables_tdxdb.py`
> **信号生成**: `AlphaMiner/verification/scripts/buy/generate_high_20d_breakout_signals_batch.py`

---

### 2.17 `sell_signals` — 卖出信号表

| 字段名 | 类型 | 说明 |
|:--|:--|:--|
| `id` | INTEGER | 主键 |
| `buy_signal_id` | INTEGER | 关联的买入信号ID（外键 → `buy_signals.id`） |
| `strategy_code` | VARCHAR | 卖出策略代码（见下方枚举） |
| `stock_code` | VARCHAR | 股票代码（冗余） |
| `signal_date` | VARCHAR | 卖出信号触发日期（YYYY-MM-DD） |
| `execution_type` | VARCHAR | 执行类型（INTRADAY / CLOSE） |
| `trigger_reason` | VARCHAR | 触发原因（如 FIXED_10PCT / HIGH20_ATR3_RATCHET / FIXED_10PCT,HIGH20_ATR3_RATCHET），见 signal_tables_schema.md 0.5节 |
| `trigger_price` | DOUBLE | 理论触发价格（后复权） |
| `close` | DOUBLE | 信号日收盘价（后复权） |
| `low` | DOUBLE | 信号日最低价（后复权） |
| `status` | VARCHAR | 信号日状态（NORMAL / LIMIT_DOWN_OPEN） |
| `limit_down_next_available` | VARCHAR | 信号日为一字跌停时的首个可交易日 |
| `t1_open` | DOUBLE | T+1日开盘价（后复权） |
| `t1_close` | DOUBLE | T+1日收盘价（后复权） |
| `t1_low` | DOUBLE | T+1日最低价（后复权） |
| `t1_status` | VARCHAR | T+1日状态 |
| `t1_limit_down_next_available` | VARCHAR | T+1日为一字跌停时的首个可交易日 |

> **卖出策略代码枚举**:
>
> | 代码 | 说明 |
> |:--|:--|
> | `STOP_10PCT` | 固定百分比止损（买入价-10%） |
> | `STOP_2ATR` | 2倍ATR止损 |
> | `STOP_3ATR` | 3倍ATR棘轮止损 |
> | `STOP_COMBO_10PCT_3ATR` | 组合止损：10%固定 + 3倍ATR棘轮（OR逻辑） |
> | `STOP_TRAILING` | 移动止损 |
> | `TP_TARGET` | 目标止盈 |
> | `TP_TRAILING` | 移动止盈 |
> | `TIME_EXIT` | 时间止损 |
>
> **执行类型枚举**:
>
> | 值 | 说明 |
> |:--|:--|
> | `INTRADAY` | 盘中触及触发价即生效 |
> | `CLOSE` | 收盘价触发 |
>
> **创建脚本**: `AlphaMiner/verification/scripts/create_signal_tables_tdxdb.py`

---

## 三、索引列表

### buy_signals

| 索引名 | 字段 |
|:--|:--|
| `idx_buy_signals_strategy` | `strategy_code` |
| `idx_buy_signals_stock` | `stock_code` |
| `idx_buy_signals_date` | `signal_date` |

### sell_signals

| 索引名 | 字段 |
|:--|:--|
| `idx_sell_signals_buy_id` | `buy_signal_id` |
| `idx_sell_signals_strategy` | `strategy_code` |
| `idx_sell_signals_stock` | `stock_code` |

### daily_stop_loss

| 索引名 | 字段 |
|:--|:--|
| `(PRIMARY KEY)` | `symbol, date` |

---

## 四、表关系图

```
                     通达信数据源
                          │
              ┌───────────┼───────────┐
              ▼           ▼           ▼
        raw_kline_daily  raw_gbbq  raw_symbol_name
              │           │           raw_symbol_class
              ▼           ▼           raw_holidays
        raw_adjust_factor  │
              │           │
              └─────┬─────┘
                    ▼
            ┌─────────────┐
            │ 复权视图     │
            │ v_stock_hfq │  ← 信号系统统一使用后复权
            └──────┬──────┘
                   │
      ┌────────────┼────────────┐
      ▼            ▼            ▼
stock_indicators  daily_stop_loss
      │            │
      ▼            │
 buy_signals ──────┘
      │
      ▼
 sell_signals
```

---

## 五、数据更新流程

```bash
# 1. 更新基础数据（通达信 .day 文件导入）
cd tdx2db && bash scripts/full_update.sh

# 2. 计算技术指标
python3 tdx2db/scripts/indicators.py

# 3. 预计算ATR止损原始数据
python3 AlphaMiner/verification/scripts/precompute_stop_loss.py

# 4. 生成买入信号
python3 AlphaMiner/verification/scripts/buy/generate_high_20d_breakout_signals_batch.py \
    --db tdx2db/tdx.db

# 5. 生成卖出信号
python3 AlphaMiner/verification/scripts/sell/generate_sell_STOP_COMBO_10PCT_3ATR.py \
    --db tdx2db/tdx.db
```
