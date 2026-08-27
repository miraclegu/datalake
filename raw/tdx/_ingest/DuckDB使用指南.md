# 🦆 DuckDB 数据使用指南

## 📂 数据库信息

- **数据库位置**: `/Users/guhao/finacial/tdx2db/tdx.db`
- **数据来源**: 通达信数据（tdx2db 工具导入）
- **数据范围**: 1990-12-19 至 2026-05-22（8712个交易日）

---

## 🛠️ 工具列表

### 1. **命令行查询工具**（推荐）
文件: `/Users/guhao/finacial/tdx2db/query_data.py`

### 2. **Python 代码直接查询**
使用 `duckdb` 库

### 3. **DuckDB CLI**
命令行工具

### 4. **GUI 工具**
DBeaver, DataGrip, Tableau 等支持 DuckDB 的工具

---

## 🚀 快速开始 - 使用 query_data.py

### 查看帮助
```bash
cd /Users/guhao/finacial/tdx2db
python3 query_data.py
```

### 查看日期范围
```bash
python3 query_data.py dates
```

### 查询某只股票某天的数据
```bash
python3 query_data.py stock sh600519 2026-05-22
```

### 查询某只股票最近N天的数据
```bash
python3 query_data.py history sz001309 30
```

### 查询某天所有股票数据
```bash
python3 query_data.py day 2026-05-22
```

### 搜索股票
```bash
python3 query_data.py search 茅台
```

---

## 📊 数据库表结构

### 主要数据表

| 表名 | 说明 | 数据量 |
|------|------|--------|
| `raw_kline_daily` | 原始日线OHLCV数据 | 3557万条 |
| `raw_adjust_factor` | 复权因子 | 2296万条 |
| `stock_indicators` | 技术指标（后复权） | 1866万条 |
| `v_stock_bfq` | 股票不复权视图 | 1866万条 |
| `v_stock_qfq` | 股票前复权视图 | 1866万条 |
| `v_stock_hfq` | 股票后复权视图 | 1866万条 |
| `v_etf_*` | ETF复权视图 | 429万条 |

### 其他辅助表

| 表名 | 说明 |
|------|------|
| `raw_symbol_name` | 证券名称表 |
| `raw_symbol_class` | 证券分类表 |
| `raw_holidays` | 节假日表 |
| `raw_gbbq` | 股本变迁表 |
| `raw_tdx_blocks_*` | 板块数据 |

---

## 💻 Python 代码查询示例

### 基础连接
```python
import duckdb

conn = duckdb.connect('/Users/guhao/finacial/tdx2db/tdx.db')

# 查询
df = conn.execute('SELECT * FROM v_stock_bfq LIMIT 10').df()
print(df)

conn.close()
```

### 查询某只股票的数据
```python
import duckdb

conn = duckdb.connect('tdx.db')

# 查询某只股票最近30天的数据
df = conn.execute("""
    SELECT date, open, high, low, close, volume, amount, change_pct
    FROM v_stock_bfq
    WHERE symbol = 'sh600519'
    ORDER BY date DESC
    LIMIT 30
""").df()

print(df)
conn.close()
```

### 查询带技术指标的数据
```python
df = conn.execute("""
    SELECT s.date, s.close, s.change_pct,
           i.MA5, i.MA10, i.MA20,
           i.MACD, i.MACD_Signal, i.MACD_Hist,
           i.KDJ_K, i.KDJ_D, i.KDJ_J
    FROM v_stock_bfq s
    JOIN stock_indicators i ON s.symbol = i.symbol AND s.date = i.date
    WHERE s.symbol = 'sz001309'
    ORDER BY s.date DESC
    LIMIT 20
""").df()
```

### 查询某一天所有股票的涨跌幅
```python
df = conn.execute("""
    SELECT s.symbol, n.name, s.close, s.change_pct
    FROM v_stock_bfq s
    LEFT JOIN raw_symbol_name n ON s.symbol = n.symbol
    WHERE s.date = '2026-05-22'
    ORDER BY s.change_pct DESC
    LIMIT 50
""").df()
```

### 查询带前复权/后复权的数据
```python
# 前复权
df_qfq = conn.execute("""
    SELECT date, open, high, low, close, qfq_factor
    FROM v_stock_qfq
    WHERE symbol = 'sz001309'
    ORDER BY date DESC
    LIMIT 10
""").df()

# 后复权
df_hfq = conn.execute("""
    SELECT date, open, high, low, close, hfq_factor
    FROM v_stock_hfq
    WHERE symbol = 'sz001309'
    ORDER BY date DESC
    LIMIT 10
""").df()
```

---

## 🖥️ 使用 DuckDB CLI

### 安装（可选，Python 已包含）
```bash
# 单独安装 DuckDB CLI
brew install duckdb  # macOS
```

### 连接数据库
```bash
cd /Users/guhao/finacial/tdx2db
duckdb tdx.db
```

### 常用 SQL 命令
```sql
-- 查看所有表
SHOW TABLES;

-- 查看表结构
DESCRIBE v_stock_bfq;

-- 查询数据
SELECT * FROM v_stock_bfq WHERE symbol = 'sh600519' LIMIT 10;

-- 退出
.quit
```

---

## 🎨 GUI 工具

### 1. **DBeaver**（推荐，免费）
1. 下载安装：https://dbeaver.io/
2. 创建新连接：选择 DuckDB
3. 数据库文件路径：`/Users/guhao/finacial/tdx2db/tdx.db`

### 2. **DataGrip**（JetBrains 出品，付费）
1. 下载安装：https://www.jetbrains.com/datagrip/
2. 连接 DuckDB 数据源

### 3. **Tableau**（数据可视化）
可连接 DuckDB 进行数据分析和可视化

---

## 📝 常用查询参考

### 查询某个股票的完整历史
```sql
SELECT date, open, high, low, close, volume, amount
FROM v_stock_bfq
WHERE symbol = 'sh600519'
ORDER BY date;
```

### 查询某天的涨跌停
```sql
SELECT 
    s.symbol, n.name, s.close, s.change_pct, s.turnover
FROM v_stock_bfq s
LEFT JOIN raw_symbol_name n ON s.symbol = n.symbol
WHERE s.date = '2026-05-22'
  AND ABS(s.change_pct) >= 9.9
ORDER BY s.change_pct DESC;
```

### 查询成交量最大的股票
```sql
SELECT 
    s.symbol, n.name, s.amount, s.volume, s.change_pct
FROM v_stock_bfq s
LEFT JOIN raw_symbol_name n ON s.symbol = n.symbol
WHERE s.date = '2026-05-22'
ORDER BY s.amount DESC
LIMIT 20;
```

### 查询技术指标（MACD金叉）
```sql
WITH stock_data AS (
    SELECT 
        date, close, 
        MACD, MACD_Signal,
        LAG(MACD) OVER (ORDER BY date) as prev_macd,
        LAG(MACD_Signal) OVER (ORDER BY date) as prev_signal
    FROM stock_indicators
    WHERE symbol = 'sz001309'
    ORDER BY date DESC
    LIMIT 60
)
SELECT *
FROM stock_data
WHERE MACD > MACD_Signal 
  AND prev_macd <= prev_signal
ORDER BY date DESC
LIMIT 5;
```

---

## ⚡ 性能提示

1. **日期过滤**: 尽量在 WHERE 子句中指定日期
2. **LIMIT**: 查询大量数据时使用 LIMIT
3. **索引**: DuckDB 自动优化，无需手动建索引
4. **视图**: 推荐使用 `v_stock_bfq/qfq/hfq` 视图查询

---

## 📚 更多资源

- DuckDB 官网: https://duckdb.org/
- SQL 参考: https://duckdb.org/docs/sql/introduction
- tdx2db 工具: 见同目录 README.md
