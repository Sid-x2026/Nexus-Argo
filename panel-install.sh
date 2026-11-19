#!/usr/bin/env bash
# Nexus-Argo 管理面板 — 一键 SSH 安装
# 用法 (VPS 上 root 执行):
#   bash <(curl -Ls https://raw.githubusercontent.com/Sid-x2026/Nexus-Argo/main/panel-install.sh)
#   bash panel-install.sh uninstall     # 只拆面板, 不动节点部署
#
# 默认只装面板 (不需要 Token): 装完用 http://VPS-IP:2053 + 账号密码进面板,
# 在面板里填设置、点部署, 自动完成部署。装完会问要不要绑定隧道:
#   要绑: 提供 Token + 面板域名 + 面板端口, 装 cloudflared、绑面板到隧道,
#   之后走隧道域名进面板, 验证后可关闭 SSH 端口 22。
#   只用直连协议: 跳过, 直接用面板 (端口访问) 装直连协议。
# 面板只有一个版本, 隧道版与端口版功能完全一样, 区别只在访问方式。
# 安装顺序只有一种: 都是先装面板, 区别只在面板怎么连 (绑定隧道 vs IP 直连)。
set -euo pipefail

PANEL_PORT="${PANEL_PORT:-2053}"
RAW_BASE="${RAW_BASE:-https://raw.githubusercontent.com/Sid-x2026/Nexus-Argo/main}"
WORKDIR="/opt/nexus-argo"
PANEL_DIR="$WORKDIR/panel"

log() { echo "[nexus-panel] $*"; }
die() { echo "[nexus-panel] ERROR: $*" >&2; exit 1; }

if [ "${1:-install}" = "uninstall" ]; then
  log "只拆面板 (节点部署不动, 要清节点请在面板里点清除部署, 或跑 install.sh uninstall) ..."
  docker rm -f nexus-panel nexus-panel-argo >/dev/null 2>&1 || true
  docker rmi nexus-panel >/dev/null 2>&1 || true
  rm -rf "$PANEL_DIR"
  log "Done. 面板已拆, 节点部署还在。"
  exit 0
fi

[ "$(id -u)" = "0" ] || die "run as root"
command -v curl >/dev/null 2>&1 || { log "curl 缺失, 正在安装 ..."; apt-get update -qq && apt-get install -y -qq curl openssl; }
command -v openssl >/dev/null 2>&1 || { apt-get update -qq && apt-get install -y -qq openssl; }

# ---- Docker: 没有就装 (Ubuntu/Debian 用 docker.io 就够) ----
if ! command -v docker >/dev/null 2>&1; then
  log "docker 缺失, 正在安装 docker.io ..."
  apt-get update -qq && apt-get install -y -qq docker.io
  systemctl enable --now docker >/dev/null 2>&1 || service docker start >/dev/null 2>&1 || true
fi
docker info >/dev/null 2>&1 || die "docker 装好了但没跑起来, 先检查 systemctl status docker"

# ---- 下文件: 面板 + 部署脚本 + 订阅服务 ----
mkdir -p "$PANEL_DIR" "$WORKDIR/web"
dl() { # dl <repo路径> <本地路径>
  if ! curl -fsSL "$RAW_BASE/$1" -o "$2"; then
    die "下载失败: $RAW_BASE/$1 (检查网络 / 仓库路径)"
  fi
}
dl "panel/panel-server.py" "$PANEL_DIR/panel-server.py"
dl "panel/panel.html" "$PANEL_DIR/panel.html"
dl "panel/Dockerfile" "$PANEL_DIR/Dockerfile"
dl "install.sh" "$WORKDIR/install.sh"
chmod +x "$WORKDIR/install.sh"
dl "web/sub-server.py" "$WORKDIR/web/sub-server.py"
dl "web/sub-generator.html" "$WORKDIR/web/sub-generator.html" || log "WARNING: sub-generator.html 没拿到, 订阅生成器面板不可用 (不影响部署)"

# ---- 账号: 已有就保留, 没有就随机生成一个 ----
if [ -f "$PANEL_DIR/auth.json" ]; then
  log "已有面板账号, 保留不变 (忘记密码可删 $PANEL_DIR/auth.json 后重跑安装)"
  PASS=""
else
  PASS="$(openssl rand -hex 8)"
  SALT="$(openssl rand -hex 8)"
  HASH="$(printf '%s%s' "$SALT" "$PASS" | sha256sum | cut -d' ' -f1)"
  printf '{"user": "admin", "salt": "%s", "pass_hash": "%s"}\n' "$SALT" "$HASH" > "$PANEL_DIR/auth.json"
  printf '%s\n' "$PASS" > "$PANEL_DIR/password.txt"
  chmod 600 "$PANEL_DIR/auth.json" "$PANEL_DIR/password.txt"
fi

# ---- 起面板容器: host 网络 (面板端口直接是宿主端口, 部署时的
# 127.0.0.1 健康检查也能通), 挂 docker.sock 管宿主 Docker ----
docker build -q -t nexus-panel "$PANEL_DIR" || die "面板镜像构建失败"
docker rm -f nexus-panel >/dev/null 2>&1 || true
docker run -d --name nexus-panel --restart unless-stopped --network host \
  -v /var/run/docker.sock:/var/run/docker.sock \
  -v "$WORKDIR:/opt/nexus-argo" \
  -e PANEL_DIR="$PANEL_DIR" -e PANEL_PORT="$PANEL_PORT" \
  nexus-panel || die "面板容器启动失败"

# ---- 等面板起来 ----
for _ in $(seq 1 20); do
  if curl -fsS --max-time 2 -o /dev/null "http://127.0.0.1:$PANEL_PORT/" 2>/dev/null; then
    break
  fi
  sleep 1
done
curl -fsS --max-time 5 -o /dev/null "http://127.0.0.1:$PANEL_PORT/" 2>/dev/null \
  || { echo "[nexus-panel] ERROR: 面板没起来, 看日志: docker logs nexus-panel"; exit 1; }

PUB_IP="$(curl -fsS --max-time 5 https://ifconfig.me/ip 2>/dev/null || curl -fsS --max-time 5 https://api.ipify.org 2>/dev/null || echo "<VPS-IP>")"
PANEL_URL="http://$PUB_IP:$PANEL_PORT"
TUNNEL_URL=""

# ---- 选装隧道版: 交互式问一句 (非交互执行时跳过, 以后在面板里绑) ----
if [ -t 0 ]; then
  echo ""
  read -r -p "是否绑定隧道 (把面板绑到 Argo 隧道, 之后可关闭 SSH)? [y/N] " _bind
  if [ "$_bind" = "y" ] || [ "$_bind" = "Y" ]; then
    read -r -s -p "Argo Tunnel Token: " ARGO_TOKEN; echo ""
    read -r -p "面板域名 (例如 panel.ahstshu.eu.cc): " PANEL_DOMAIN
    read -r -p "面板端口 [默认 $PANEL_PORT]: " _pp
    PANEL_PORT_BIND="${_pp:-$PANEL_PORT}"
    [ -n "$ARGO_TOKEN" ] && [ -n "$PANEL_DOMAIN" ] || die "Token 和面板域名都要填"
    docker rm -f nexus-panel-argo >/dev/null 2>&1 || true
    docker run -d --name nexus-panel-argo --restart unless-stopped --network host \
      cloudflare/cloudflared:latest tunnel --no-autoupdate run --token "$ARGO_TOKEN" \
      || die "cloudflared 启动失败"
    TUNNEL_URL="https://$PANEL_DOMAIN/"
    echo ""
    log "隧道容器已起, 还差一步: 去 Cloudflare Zero Trust 里加一条路由:"
    log "  $PANEL_DOMAIN -> http://127.0.0.1:$PANEL_PORT_BIND"
    log "加完立刻生效, 之后用 $TUNNEL_URL 进面板。"
  fi
fi

echo ""
echo "================ 面板已安装 ================"
echo "面板地址: $PANEL_URL"
[ -n "$TUNNEL_URL" ] && echo "隧道地址: $TUNNEL_URL (加完上面那条路由后可用)"
echo "账号: admin"
if [ -n "$PASS" ]; then
  echo "密码: $PASS   (只显示这一次, 也存在 $PANEL_DIR/password.txt)"
else
  echo "密码: 不变 (存在 $PANEL_DIR/password.txt)"
fi
echo ""
echo "接下来: 打开面板 -> 填设置 -> 点「开始部署」, 部署日志和检查在面板里看。"
echo "只用直连协议: 不用绑隧道, 直接用面板装直连协议就行。"
echo "=============================================="
