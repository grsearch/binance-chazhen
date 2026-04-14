#!/bin/bash
# 插针策略后端启动脚本
# 用法：bash start.sh

set -e
cd "$(dirname "$0")"

echo "=============================="
echo " 插针策略 Bot  v2.0"
echo "=============================="

# 检查 Python
if ! command -v python3 &>/dev/null; then
  echo "[错误] 未找到 python3，请先安装"
  exit 1
fi

# 安装依赖
echo "[1/3] 安装依赖..."
pip3 install -r requirements.txt -q --break-system-packages 2>/dev/null || \
pip3 install -r requirements.txt -q

# 创建数据目录
mkdir -p data
echo "[2/3] 数据目录: $(pwd)/data"

# 启动服务
echo "[3/3] 启动服务 端口 8888..."
echo ""
echo "  Dashboard: http://$(hostname -I | awk '{print $1}'):8888"
echo "  本地访问:  http://127.0.0.1:8888"
echo ""
echo "  Ctrl+C 停止服务"
echo "=============================="

python3 server.py --host 0.0.0.0 --port 8888
