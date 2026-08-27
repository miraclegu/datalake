#!/bin/bash
# 仅下载通达信股本变迁(gbbq)数据
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VIPDOC_DIR="$SCRIPT_DIR/vipdoc"
TMP_DIR="$VIPDOC_DIR/tmp"

mkdir -p "$VIPDOC_DIR" "$TMP_DIR"
cd "$VIPDOC_DIR"

echo "=== 仅下载通达信股本变迁(gbbq)数据 ==="

# 下载股本变迁(gbbq)数据
echo ""
echo "正在下载 gbbq 数据..."
GBBQ_URL="https://www.tdx.com.cn/products/data/data/dbf/gbbq.zip"
FILE=$(basename "$GBBQ_URL")

echo "下载地址: $GBBQ_URL"
if curl -L -o "$TMP_DIR/$FILE" "$GBBQ_URL"; then
    echo "✅ 下载成功: $FILE"
    echo "正在解压: $FILE"
    unzip -o -q "$TMP_DIR/$FILE" -d "$VIPDOC_DIR"
    echo "✅ 解压完成"
else
    echo "❌ 下载失败: $FILE"
fi

# 清理临时文件
rm -rf "$TMP_DIR"

echo ""
echo "=== 下载完成 ==="
echo "目录内容:"
ls -la "$VIPDOC_DIR"

echo ""
echo "目录结构:"
tree -L 2 "$VIPDOC_DIR" 2>/dev/null || find "$VIPDOC_DIR" -maxdepth 2 -type d
