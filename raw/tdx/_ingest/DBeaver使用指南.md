# 🦆 DBeaver 连接 DuckDB 指南

## 📋 前置准备

- **数据库文件路径: `/Users/guhao/finacial/tdx2db/tdx.db`
- **已安装: DBeaver

---

## 🚀 第一步：下载 DuckDB 驱动

### 第一次使用前需要安装 DuckDB 驱动

1. **打开 DBeaver**

2. **进入驱动管理器**
   - 菜单栏: **数据库** → **驱动管理器**
   - 或者快捷键: `Cmd + Shift + D` (Mac)

3. **安装 DuckDB 驱动**
   - 在驱动管理器中搜索 "DuckDB"
   - 选中 DuckDB 驱动
   - 点击 **下载/更新** 按钮
   - 等待驱动安装完成

---

## 📝 第二步：创建连接

### 方法一：快速新建数据库连接

1. **新建连接**
   - 点击左上角的 **新建数据库连接** 图标
   - 或者: 数据库 → 新建连接

2. **选择 DuckDB**
   - 在数据库类型列表中找到 **DuckDB**
   - 选中后点击 **下一步**

3. **配置连接**
   - **连接类型**: 选择 **文件** (File)
   - **路径**: 点击 **浏览**，选择:
     `/Users/guhao/finacial/tdx2db/tdx.db`

4. **测试连接**
   - 点击 **测试连接** 按钮
   - 如果显示连接成功，点击 **完成**

5. **保存连接**
   - 连接名称可以填写: `TDX 股票数据`
   - 点击 **完成**

---

### 方法二：通过导入数据库文件

1. 在 DBeaver 主界面，选择 **文件** → **打开**

2. 选择 `/Users/guhao/finacial/tdx2db/tdx.db`

3. DBeaver 会自动识别为 DuckDB 数据库

---

## 🔍 第三步：浏览数据

### 连接成功后，你可以：

#### 1. **查看所有表**
   - 在左侧导航栏展开连接
   - 展开 **Tables** 文件夹
   - 看到所有可用的表

#### 2. **查看表数据**
   - 双击某个表（如 `v_stock_bfq`）
   - 右侧会显示数据
   - 点击 **数据** 标签查看数据内容

#### 3. **编写 SQL 查询**
   - 点击 **SQL** 编辑器标签
   - 或按 `F3` 打开 SQL 编辑器
   - 编写 SQL 语句查询数据

---

## 💡 常用查询示例

### 查询某只股票最近30天的数据

```sql
SELECT 
    date,
    open,
    high,
    low,
    close,
    volume,
    amount,
    change_pct
FROM v_stock_bfq
WHERE symbol = 'sz001309'
ORDER BY date DESC
LIMIT 30;
```

### 查询某天所有股票的涨跌幅排行

```sql
SELECT 
    s.symbol,
    n.name,
    s.close,
    s.change_pct,
    s.turnover,
    s.amount
FROM v_stock_bfq s
LEFT JOIN raw_symbol_name n ON s.symbol = n.symbol
WHERE s.date = '2026-05-22'
ORDER BY s.change_pct DESC
LIMIT 50;
```

### 查询带技术指标的数据

```sql
SELECT 
    s.date,
    s.close,
    s.change_pct,
    i.MA5,
    i.MA10,
    i.MA20,
    i.MACD,
    i.MACD_Signal,
    i.KDJ_K,
    i.KDJ_D
FROM v_stock_bfq s
JOIN stock_indicators i 
    ON s.symbol = i.symbol 
    AND s.date = i.date
WHERE s.symbol = 'sh600519'
ORDER BY s.date DESC
LIMIT 20;
```

### 查询复权数据对比

```sql
-- 前复权
SELECT date, open, high, low, close
FROM v_stock_qfq
WHERE symbol = 'sz001309'
ORDER BY date DESC
LIMIT 10;

-- 后复权
SELECT date, open, high, low, close
FROM v_stock_hfq
WHERE symbol = 'sz001309'
ORDER BY date DESC
LIMIT 10;
```

---

## 📊 主要表说明

| 表名 | 说明 | 推荐用途 |
|------|------|----------|
| `v_stock_bfq` | 股票不复权视图 | ✅ 日常查询 |
| `v_stock_qfq` | 股票前复权视图 | 做趋势分析 |
| `v_stock_hfq` | 股票后复权视图 | 做回测 |
| `stock_indicators` | 技术指标表 | MA、MACD、KDJ等 |
| `raw_symbol_name` | 股票名称表 | 搜索股票名称 |

---

## 🎨 DBeaver 小技巧

### 1. **数据过滤**
   - 在数据表视图中，点击列标题可以排序
   - 右键点击数据可以筛选

### 2. **图表可视化**
   - 选中查询结果
   - 右键 → **可视化
   - 可以生成折线图、柱状图等

### 3. **导出数据**
   - 选中查询结果
   - 右键 → **导出结果集**
   - 可以导出为 CSV、Excel 等格式

### 4. **保存 SQL 脚本**
   - 编写常用查询
   - 保存为 .sql 文件
   - 下次直接打开使用

---

## ❓ 常见问题

### Q: 连接时提示找不到驱动？
A: 先在驱动管理器中安装 DuckDB 驱动

### Q: 数据库文件被占用？
A: 关闭其他可能使用该数据库的程序

### Q: 查询速度慢？
A: 确保 SQL 中增加 `LIMIT`，避免一次查询太多数据

### Q: 如何查看表结构？
A: 右键点击表 → **查看表** → **属性**

---

## 📚 更多资源

- DBeaver 官网: https://dbeaver.io/
- DuckDB 文档: https://duckdb.org/docs/
- 完整使用指南: 同目录 `DuckDB使用指南.md
