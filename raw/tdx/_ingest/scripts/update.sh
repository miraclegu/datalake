#!/bin/bash
# tdx.db 增量更新
# 使用场景：日常更新数据
# 注意: 数据格式已统一，volume/turnover 单位为"手*100"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

echo "======================================="
echo "  tdx.db 增量更新"
echo "======================================="
date
echo ""

# 1. 运行 tdx2db cron 更新数据库和复权因子
echo "1. 更新数据库和复权因子..."
./tdx2db cron --dburi "duckdb://./tdx.db"

# 2. 计算技术指标
echo ""
echo "2. 计算技术指标..."
python3 fast_update_indicators.py

echo ""
echo "======================================="
echo "  更新完成!"
echo "======================================="
