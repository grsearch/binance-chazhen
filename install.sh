#!/bin/bash
# ================================================
# 插针策略 Bot — 一键安装脚本
# 适用：OpenCloudOS / CentOS / RHEL / TencentOS
# ================================================
set -e

INSTALL_DIR="/opt/spike-bot"
SERVICE_NAME="spike-bot"
PYTHON_MIN="3.10"

echo ""
echo "================================================"
echo " 插针策略 Bot 安装程序"
echo " 目标目录: $INSTALL_DIR"
echo "================================================"
echo ""

# ── 检查root ──────────────────────────────────────
if [ "$EUID" -ne 0 ]; then
    echo "[错误] 请用 root 用户运行：sudo bash install.sh"
    exit 1
fi

# ── 检测包管理器 ──────────────────────────────────
if command -v dnf &>/dev/null; then
    PKG="dnf"
elif command -v yum &>/dev/null; then
    PKG="yum"
elif command -v apt-get &>/dev/null; then
    PKG="apt-get"
else
    echo "[错误] 未找到包管理器"
    exit 1
fi
echo "[1/7] 包管理器: $PKG"

# ── 安装系统依赖 ──────────────────────────────────
echo "[2/7] 安装系统依赖..."
if [ "$PKG" = "dnf" ] || [ "$PKG" = "yum" ]; then
    $PKG install -y python3 python3-pip python3-devel gcc curl wget 2>/dev/null || true
else
    apt-get update -qq
    apt-get install -y python3 python3-pip python3-dev gcc curl wget 2>/dev/null || true
fi

# ── 检查Python版本 ────────────────────────────────
echo "[3/7] 检查Python版本..."
PYTHON_CMD=""
for cmd in python3.12 python3.11 python3.10 python3; do
    if command -v $cmd &>/dev/null; then
        VER=$($cmd -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
        MAJOR=$(echo $VER | cut -d. -f1)
        MINOR=$(echo $VER | cut -d. -f2)
        if [ "$MAJOR" -ge 3 ] && [ "$MINOR" -ge 10 ]; then
            PYTHON_CMD=$cmd
            echo "    使用: $cmd ($VER)"
            break
        fi
    fi
done

if [ -z "$PYTHON_CMD" ]; then
    echo "[错误] 需要 Python 3.10+，正在尝试安装..."
    if [ "$PKG" = "dnf" ]; then
        dnf install -y python3.11 || dnf install -y python3.10 || {
            echo "[错误] 无法安装 Python 3.10+，请手动安装后重试"
            exit 1
        }
        PYTHON_CMD=$(command -v python3.11 || command -v python3.10)
    fi
fi

# ── 复制文件 ──────────────────────────────────────
echo "[4/7] 部署文件到 $INSTALL_DIR ..."
mkdir -p "$INSTALL_DIR/live/data"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

# 复制所有文件
cp "$SCRIPT_DIR/dashboard.html" "$INSTALL_DIR/"
cp "$SCRIPT_DIR/live/"*.py      "$INSTALL_DIR/live/"
cp "$SCRIPT_DIR/live/requirements.txt" "$INSTALL_DIR/live/"
chmod -R 750 "$INSTALL_DIR"

# ── 安装Python依赖 ────────────────────────────────
echo "[5/7] 安装Python依赖..."
cd "$INSTALL_DIR/live"

# 优先用venv避免系统Python冲突
$PYTHON_CMD -m venv --system-site-packages "$INSTALL_DIR/venv" 2>/dev/null || true

if [ -f "$INSTALL_DIR/venv/bin/pip" ]; then
    VENV_PIP="$INSTALL_DIR/venv/bin/pip"
    VENV_PYTHON="$INSTALL_DIR/venv/bin/python3"
    echo "    使用虚拟环境"
else
    VENV_PIP="pip3"
    VENV_PYTHON="$PYTHON_CMD"
    echo "    使用系统Python"
fi

$VENV_PIP install --quiet --upgrade pip
$VENV_PIP install --quiet flask flask-cors "websocket-client>=1.7.0"

# ── 时区设置（UTC，与币安保持一致） ──────────────
echo "[6/7] 设置时区为 UTC..."
if command -v timedatectl &>/dev/null; then
    timedatectl set-timezone UTC
else
    ln -sf /usr/share/zoneinfo/UTC /etc/localtime
fi

# ── 创建 systemd 服务 ─────────────────────────────
echo "[7/7] 配置 systemd 服务..."

cat > "/etc/systemd/system/${SERVICE_NAME}.service" << SVCEOF
[Unit]
Description=Spike Strategy Bot
Documentation=https://github.com/yourname/spike-strategy
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=root
WorkingDirectory=${INSTALL_DIR}/live
ExecStart=${VENV_PYTHON} ${INSTALL_DIR}/live/server.py --host 0.0.0.0 --port 8888
Restart=always
RestartSec=10
StartLimitInterval=60
StartLimitBurst=5

# 环境变量
Environment=PYTHONUNBUFFERED=1
Environment=PYTHONDONTWRITEBYTECODE=1
Environment=TZ=UTC

# 资源限制（4C8G服务器适合）
MemoryMax=512M
CPUQuota=200%

# 日志（交给 journald 管理，自动轮转）
StandardOutput=journal
StandardError=journal
SyslogIdentifier=${SERVICE_NAME}

[Install]
WantedBy=multi-user.target
SVCEOF

# 配置 journald 日志大小限制
mkdir -p /etc/systemd/journald.conf.d
cat > /etc/systemd/journald.conf.d/spike-bot.conf << JEOF
[Journal]
SystemMaxUse=100M
RuntimeMaxUse=50M
JEOF

systemctl daemon-reload
systemctl enable "$SERVICE_NAME"
systemctl restart "$SERVICE_NAME"

# ── 等待服务启动 ──────────────────────────────────
echo ""
echo "等待服务启动..."
sleep 3
if systemctl is-active --quiet "$SERVICE_NAME"; then
    echo ""
    echo "================================================"
    echo " ✓ 安装成功！"
    echo ""
    echo " Dashboard: http://$(curl -s ifconfig.me 2>/dev/null || hostname -I | awk '{print $1}'):8888"
    echo " 本地访问:  http://127.0.0.1:8888"
    echo ""
    echo " 常用命令:"
    echo "   查看状态: systemctl status $SERVICE_NAME"
    echo "   查看日志: journalctl -u $SERVICE_NAME -f"
    echo "   重启服务: systemctl restart $SERVICE_NAME"
    echo "   停止服务: systemctl stop $SERVICE_NAME"
    echo "================================================"
else
    echo ""
    echo "[警告] 服务启动可能失败，查看日志："
    journalctl -u "$SERVICE_NAME" -n 20 --no-pager
fi
