#!/bin/bash
# 启动 Nginx + Cloudflare Tunnel
# 用法：bash nginx/start-with-nginx.sh

set -e
cd "$(dirname "$0")/.."

echo "=== 1. 检查 Nginx ==="
if ! command -v nginx &>/dev/null; then
  echo "Nginx 未安装，请先运行: brew install nginx"
  exit 1
fi

echo "=== 2. 启动/重载 Nginx（监听 8080）==="
# 用项目里的本地配置
nginx -c "$(pwd)/nginx/nginx.local.conf" -t && \
nginx -c "$(pwd)/nginx/nginx.local.conf" -s reload 2>/dev/null || \
nginx -c "$(pwd)/nginx/nginx.local.conf"

echo "=== 3. 启动 Cloudflare Tunnel（指向 Nginx 8080）==="
echo "请在另一个终端运行："
echo "  cloudflared tunnel --url http://127.0.0.1:8080"
echo ""
echo "或者如果已有命名 tunnel："
echo "  cloudflared tunnel --config nginx/cloudflared-config.yml run"
