#!/bin/bash
# 并行下载通达信数据 (sh、sz、bj、gbbq、base)

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# 支持通过环境变量 VIPDOC_DIR 指定下载目录，默认下载到 ../vipdoc
VIPDOC_DIR="${VIPDOC_DIR:-$SCRIPT_DIR/../vipdoc}"
TMP_DIR="$SCRIPT_DIR/vipdoc/tmp"

mkdir -p "$VIPDOC_DIR" "$TMP_DIR"
cd "$VIPDOC_DIR"

echo "======================================="
echo "  并行下载通达信数据"
echo "======================================="
echo "下载目录: $VIPDOC_DIR"
date

# 定义要下载的文件（使用普通数组）
FILES=(
    "shlday.zip|https://www.tdx.com.cn/products/data/data/vipdoc/shlday.zip"
    "szlday.zip|https://www.tdx.com.cn/products/data/data/vipdoc/szlday.zip"
    "bjlday.zip|https://www.tdx.com.cn/products/data/data/vipdoc/bjlday.zip"
    "gbbq.zip|https://www.tdx.com.cn/products/data/data/dbf/gbbq.zip"
    "base.zip|https://www.tdx.com.cn/products/data/data/vipdoc/base.zip"
)

# 单个文件下载函数
download_file() {
    local FILENAME="$1"
    local URL="$2"
    echo "[$FILENAME] 开始下载"
    if curl -L -s -o "$TMP_DIR/$FILENAME" "$URL"; then
        echo "[$FILENAME] ✅ 下载成功"
        return 0
    else
        echo "[$FILENAME] ❌ 下载失败"
        return 1
    fi
}

# 开始并行下载
echo ""
echo "开始并行下载 ${#FILES[@]} 个文件..."
echo ""

PIDS=()
for FILE_INFO in "${FILES[@]}"; do
    IFS="|" read -r FILENAME URL <<< "$FILE_INFO"
    download_file "$FILENAME" "$URL" &
    PIDS+=($!)
done

# 等待所有后台进程完成
echo "等待所有下载完成..."
FAILURES=0
for PID in "${PIDS[@]}"; do
    wait "$PID" || ((FAILURES++))
done

echo ""
if [ "$FAILURES" -eq 0 ]; then
    echo "✅ 所有文件下载成功"
else
    echo "⚠️  有 $FAILURES 个文件下载失败"
fi

# 解压所有下载的文件
echo ""
echo "======================================="
echo "  解压数据文件"
echo "======================================="

for FILE_INFO in "${FILES[@]}"; do
    IFS="|" read -r FILENAME _ <<< "$FILE_INFO"
    FILEPATH="$TMP_DIR/$FILENAME"
    if [ -f "$FILEPATH" ]; then
        echo "解压: $FILENAME"
        unzip -o -q "$FILEPATH" -d "$VIPDOC_DIR"
    fi
done

# 清理临时文件
rm -rf "$TMP_DIR"

echo ""
echo "======================================="
echo "  下载和解压完成!"
echo "======================================="
ls -la "$VIPDOC_DIR"
