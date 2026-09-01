#!/bin/bash
# Linux 版: 批量上传 .md 文件到 Dify 知识库
# 用法: bash _run_upload.sh
set -e

cd "$(dirname "$0")"

echo "===== 上传 全页面需求 ====="
python3 dify_upload.py "dataset-rcjZZdzkXDnxGfUaLmyRZDHO" "全页面需求" "/data/PrdReq" --skip=NoUse

echo ""
echo "===== 上传 测试方案 ====="
python3 dify_upload.py "dataset-rcjZZdzkXDnxGfUaLmyRZDHO" "测试方案" "/data/测试方案_md"

echo ""
echo "===== 上传完成 ====="