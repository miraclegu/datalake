#!/bin/bash
# tdx2db 完整更新脚本
# 功能: 更新数据库 → 计算技术指标 → 验证结果
# 注意: 数据格式已统一，volume/turnover 单位为"手*100"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR/.."

# ===== 主流程 =====
echo ""
echo "======================================="
echo "  tdx2db 完整数据更新"
echo "======================================="
echo ""
date
echo ""

# 步骤 1: 检查更新前状态
echo ""
echo "【1/3】检查更新前数据库状态..."
python3 -c '
import duckdb
try:
    conn = duckdb.connect("./tdx.db")
    kline_result = conn.execute("SELECT MAX(date) as max_date FROM raw_kline_daily").df()
    max_dt = kline_result["max_date"][0]
    print(f"更新前最新日期: {max_dt}")
    conn.close()
except Exception as e:
    print(f"检查失败: {e}")
'

# 步骤 2: 更新数据库（tdx2db cron 会自动下载所有需要的数据）
echo ""
echo "【2/3】更新数据库和复权因子..."
./tdx2db cron --dburi "duckdb://./tdx.db"

if [ $? -ne 0 ]; then
    echo "❌ 更新数据库失败！"
    exit 1
fi

# 步骤 3: 计算技术指标（增量更新，优化版）
echo ""
echo "【3/3】计算技术指标（增量更新，优化版）..."
python3 ./scripts/fast_update_indicators.py

if [ $? -ne 0 ]; then
    echo "❌ 计算技术指标失败！"
    exit 1
fi

# 步骤 4: 验证结果
echo ""
echo "检查更新后数据库状态..."
python3 -c '
import duckdb
try:
    conn = duckdb.connect("./tdx.db")
    kline_result = conn.execute("""
        SELECT 
            COUNT(*) as total_rows,
            MIN(date) as min_date,
            MAX(date) as max_date
        FROM raw_kline_daily
    """).df()
    
    total_rows = kline_result["total_rows"][0]
    min_date = kline_result["min_date"][0]
    max_date = kline_result["max_date"][0]
    
    print("=" * 50)
    print("  raw_kline_daily 表信息")
    print("=" * 50)
    print(f"总记录数: {total_rows:,}")
    print(f"最早日期: {min_date}")
    print(f"最新日期: {max_date}")
    
    tables = ["raw_adjust_factor", "raw_gbbq", "raw_symbol_name", "stock_indicators"]
    print()
    print("=" * 50)
    print("  其他表信息")
    print("=" * 50)
    
    for table in tables:
        try:
            count_result = conn.execute(f"SELECT COUNT(*) FROM {table}").df()
            print(f"{table:20s}: {count_result.iloc[0, 0]:,} 条记录")
        except Exception:
            print(f"{table:20s}: (表不存在)")
    
    conn.close()
except Exception as e:
    print(f"检查失败: {e}")
'

# 总结
echo ""
echo "======================================="
echo "  更新完成!"
echo "======================================="
echo ""
