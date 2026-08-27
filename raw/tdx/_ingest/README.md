
# tdx2db 使用指南

## 概述

tdx2db 是一个高性能的通达信数据导入工具，支持：
- ✅ Go 语言实现
- ✅ 支持前收盘价和复权因子自动计算
- ✅ 增量更新支持
- ✅ 数据存储到 DuckDB
- ✅ macOS ARM64 (Apple Silicon) 支持

## 安装

已安装在：`/Users/guhao/finacial/tdx2db/tdx2db`

## 数据初始化

### 1. 下载通达信数据文件

```bash
# 创建数据目录
mkdir -p /Users/guhao/finacial/tdx2db/vipdoc
cd /Users/guhao/finacial/tdx2db/vipdoc

# 下载数据文件
wget https://www.tdx.com.cn/products/data/data/vipdoc/shlday.zip -O shlday.zip
wget https://www.tdx.com.cn/products/data/data/vipdoc/szlday.zip -O szlday.zip
wget https://www.tdx.com.cn/products/data/data/vipdoc/bjlday.zip -O bjlday.zip
wget https://www.tdx.com.cn/products/data/data/vipdoc/tdxzs_day.zip -O tdxzs_day.zip

# 解压
unzip -q shlday.zip
unzip -q szlday.zip
unzip -q bjlday.zip
unzip -q tdxzs_day.zip
```

### 2. 初始化数据库

```bash
cd /Users/guhao/finacial/tdx2db

# 初始化，使用 DuckDB
./tdx2db init --dburi 'duckdb://./tdx.db' --dayfiledir ./vipdoc
```

## 日常更新

```bash
cd /Users/guhao/finacial/tdx2db

# 增量更新数据并计算复权因子
./tdx2db cron --dburi 'duckdb://./tdx.db'
```

## 数据库结构

DuckDB 数据库包含以下表：
- 股票日线数据表
- 复权因子表
- 股本变迁表

## 使用数据

可以通过 Python 的 duckdb 库访问数据：

```python
import duckdb

# 连接数据库
conn = duckdb.connect('/Users/guhao/finacial/tdx2db/tdx.db')

# 查询数据
df = conn.execute("SELECT * FROM stocks WHERE date = '2026-05-22'").df()
print(df)
```

## 注意事项

- 分时数据（分钟线）仅在 Linux 上支持，macOS 不支持
- 每日更新建议在收盘后（16:30 后）进行
