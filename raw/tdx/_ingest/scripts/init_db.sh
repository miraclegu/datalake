#!/bin/bash
# tdx2db 数据库初始化脚本
# 功能: 1.下载所有文件 2.将文件数据导入到数据库中 3.计算指标到数据库中

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# ===== 主流程 =====
echo ""
echo "======================================="
echo "  tdx2db 数据库初始化"
echo "======================================="
echo ""
date
echo ""

# 步骤 1: 检查是否已存在数据库
if [ -f "../tdx.db" ]; then
    echo "⚠️  警告: 数据库文件 ../tdx.db 已存在！"
    echo ""
    read -p "是否要删除并重新初始化？(y/N): " -n 1 -r
    echo ""
    if [[ ! $REPLY =~ ^[Yy]$ ]]; then
        echo "操作已取消。"
        exit 0
    fi
    echo "正在删除旧数据库..."
    rm -f "../tdx.db"
fi

# 步骤 2: 下载所有数据文件
echo ""
echo "【1/4】下载所有通达信数据文件..."
echo "注意: 此过程可能需要较长时间，请耐心等待..."
echo ""

# 设置下载目录为 ../vipdoc 并运行下载脚本
VIPDOC_DIR="$SCRIPT_DIR/../vipdoc" ./parallel_download.sh

if [ $? -ne 0 ]; then
    echo "❌ 下载数据失败！"
    exit 1
fi

# 步骤 3: 将文件数据导入到数据库中
echo ""
echo "【2/4】初始化数据库并导入数据..."
cd ..
./tdx2db init --dburi 'duckdb://./tdx.db' --dayfiledir ./vipdoc

if [ $? -ne 0 ]; then
    echo "❌ 初始化数据库失败！"
    cd "$SCRIPT_DIR"
    exit 1
fi

# 步骤 4: 计算技术指标
echo ""
echo "【3/4】计算技术指标..."
cd "$SCRIPT_DIR"
python3 calculate_indicators.py --db ../tdx.db

if [ $? -ne 0 ]; then
    echo "⚠️  计算技术指标失败，但数据库已初始化完成！"
fi

# 步骤 5: 验证结果
echo ""
echo "【4/4】验证数据库状态..."
python3 -c '
import duckdb
try:
    conn = duckdb.connect("../tdx.db")
    
    print("=" * 50)
    print("  数据库状态")
    print("=" * 50)
    
    # 检查 raw_kline_daily
    kline_result = conn.execute("""
        SELECT 
            COUNT(*) as total_rows,
            MIN(date) as min_date,
            MAX(date) as max_date
        FROM raw_kline_daily
    """).df()
    print(f"raw_kline_daily: {kline_result[\"total_rows\"][0]:,} 条记录")
    print(f"  日期范围: {kline_result[\"min_date\"][0]} ~ {kline_result[\"max_date\"][0]}")
    
    # 检查其他表
    tables = ["raw_adjust_factor", "raw_gbbq", "raw_symbol_name", "raw_basic_daily", "stock_indicators"]
    print()
    for table in tables:
        try:
            count_result = conn.execute(f"SELECT COUNT(*) FROM {table}").df()
            print(f"{table:20s}: {count_result.iloc[0, 0]:,} 条记录")
        except Exception:
            print(f"{table:20s}: (表不存在)")
    
    conn.close()
    print()
    print("=" * 50)
    print("  初始化完成！")
    print("=" * 50)
except Exception as e:
    print(f"验证失败: {e}")
'

# 总结
echo ""
echo "======================================="
echo "  数据库初始化完成!"
echo "======================================="
echo ""
