#!/usr/bin/env bash
set -euo pipefail

# ==============================================================================
# 刘春霖小楷书房 — Ubuntu 私有主机「字库 100% 防盗」一键部署脚本
# ==============================================================================
# 核心安全机制：
#   1. 将 LiuChunlin_v003_Golden.zip 解压至非 Web 私有目录 /opt/liuchunlin_private
#   2. 对 .ttf 字库设置 chmod 600 严格权限（仅后端进程可读，绝无任何静态文件路由）
#   3. 启动 FastAPI 渲染服务（127.0.0.1:8088，不直接暴露公网端口）
#   4. 通过 Cloudflare Tunnel (cloudflared) 提供免费 HTTPS 加密接口，隐藏服务器真实 IP
# ==============================================================================

ZIP_PATH="${1:-./LiuChunlin_v003_Golden.zip}"
INSTALL_DIR="/opt/liuchunlin_private"

if [ ! -f "$ZIP_PATH" ]; then
  echo "请将 LiuChunlin_v003_Golden.zip 上传至当前目录后再执行："
  echo "  sudo bash deploy_ubuntu.sh ./LiuChunlin_v003_Golden.zip"
  exit 1
fi

echo "[1/4] 安装系统依赖与 Python 运行环境..."
apt-get update -y
apt-get install -y python3 python3-venv python3-pip unzip libgl1 libglib2.0-0

echo "[2/4] 部署 v003 字库至隔离目录 ${INSTALL_DIR} 并锁定权限 (chmod 600)..."
mkdir -p "${INSTALL_DIR}"
unzip -q -o "$ZIP_PATH" -d "${INSTALL_DIR}"
cp -f "$(dirname "$0")/app.py" "${INSTALL_DIR}/v003/app.py"

# 严格锁定字库权限，禁止除当前服务用户外的任何读取
chmod 700 "${INSTALL_DIR}"
chmod 600 "${INSTALL_DIR}"/v003/output/*.ttf "${INSTALL_DIR}"/v003/*.ttf

echo "[3/4] 创建虚拟环境并安装 FastAPI / Segno / FontTools..."
python3 -m venv "${INSTALL_DIR}/venv"
"${INSTALL_DIR}/venv/bin/pip" install --upgrade pip
"${INSTALL_DIR}/venv/bin/pip" install fastapi uvicorn pillow numpy scipy opencv-python-headless fonttools segno pydantic

echo "[4/4] 配置 systemd 后台常驻服务 (liuchunlin-shufa.service)..."
cat >/etc/systemd/system/liuchunlin-shufa.service <<EOF
[Unit]
Description=Liu Chunlin Calligraphy Rendering Service (v003 Hardened)
After=network.target

[Service]
Type=simple
WorkingDirectory=${INSTALL_DIR}/v003
Environment="LIUCHUNLIN_FONT_DIR=${INSTALL_DIR}/v003/output"
Environment="LIUCHUNLIN_QUOTA_DB=${INSTALL_DIR}/ip_quota.sqlite3"
Environment="LIUCHUNLIN_SITE_URL=https://dvbs2000.github.io/liuchunlin-shufa/"
Environment="LIUCHUNLIN_HOURLY_LIMIT=10"
ExecStart=${INSTALL_DIR}/venv/bin/uvicorn app:app --host 127.0.0.1 --port 8088 --workers 1
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable --now liuchunlin-shufa.service

echo "=============================================================================="
echo "✅ 刘春霖小楷渲染服务已启动于本地 127.0.0.1:8088（单 IP 每日限 2 次，字库已 chmod 600 锁定）"
echo "👉 如需一键生成免费 HTTPS 公网接口供 GitHub Pages 调用，可执行："
echo "   cloudflared tunnel --url http://127.0.0.1:8088"
echo "=============================================================================="
