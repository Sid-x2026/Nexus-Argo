#!/usr/bin/env bash
# =====================================================================
# Nexus-Argo v3.6 自动化部署与运维脚本 (后端服务端修复版)
# =====================================================================

# 确保以 root 权限运行
[[ $EUID -ne 0 ]] && echo "❌ 必须使用 root 权限运行此脚本！(可使用 sudo -i)" && exit 1

GREEN="\033[32m"
YELLOW="\033[33m"
RED="\033[31m"
RESET="\033[0m"

log() { echo -e "${GREEN}[INFO] $1${RESET}"; }
warn() { echo -e "${YELLOW}[WARN] $1${RESET}"; }
err() { echo -e "${RED}[ERROR] $1${RESET}"; exit 1; }

# ==================== 1. 一键卸载与清理逻辑 ====================
if [[ "$1" == "uninstall" ]]; then
    log "正在彻底清理并卸载 Nexus-Argo 相关服务与文件..."
    systemctl stop xray sing-box cloudflared 2>/dev/null
    systemctl disable xray sing-box cloudflared 2>/dev/null
    rm -f /etc/systemd/system/xray.service /etc/systemd/system/sing-box.service /etc/systemd/system/cloudflared*.service
    rm -rf /usr/local/etc/xray /usr/local/etc/sing-box /usr/local/bin/xray /usr/local/bin/sing-box /usr/local/bin/cloudflared
    log "✅ 卸载清理完成！"
    exit 0
fi

log "🚀 开始执行 Nexus-Argo v3.6 服务端自动化部署..."

# ==================== 2. 基础环境初始化 ====================
log "正在更新系统并安装基础依赖 (curl, wget, jq, ufw)..."
apt update -y && apt install -y curl wget jq ufw git

# ==================== 3. 解析传入参数 ====================
NODE_UUID="${NODE_UUID:-98a9eeea-e34d-4a0b-a151-39ec761d83f6}"
ARGO_TOKEN="${ARGO_TOKEN:-}"

ENABLE_XRAY="${ENABLE_XRAY:-true}"
XRAY_PORT="${XRAY_PORT:-8001}"
XRAY_DOMAIN="${XRAY_DOMAIN:-xray.yourdomain.com}"

ENABLE_SINGBOX="${ENABLE_SINGBOX:-true}"
SINGBOX_PORT="${SINGBOX_PORT:-8002}"
SINGBOX_DOMAIN="${SINGBOX_DOMAIN:-singbox.yourdomain.com}"

log "📌 配置参数摘要："
log " - 节点 UUID: $NODE_UUID"
log " - Xray 状态: $ENABLE_XRAY | 回源端口: $XRAY_PORT"
log " - Sing-box 状态: $ENABLE_SINGBOX | 回源端口: $SINGBOX_PORT"

# ==================== 4. 安装核心二进制文件 ====================
if [[ "$ENABLE_XRAY" == "true" ]]; then
    log "正在安装 Xray-core..."
    bash <(curl -L https://github.com/XTLS/Xray-install/raw/main/install-release.sh) @install
fi

if [[ "$ENABLE_SINGBOX" == "true" ]]; then
    log "正在安装 Sing-box..."
    SB_VERSION=$(curl -s "https://api.github.com/repos/SagerNet/sing-box/releases/latest" | jq -r .tag_name | tr -d 'v')
    [[ -z "$SB_VERSION" ]] && SB_VERSION="1.8.4"
    wget -qO /tmp/sing-box.tar.gz "https://github.com/SagerNet/sing-box/releases/download/v${SB_VERSION}/sing-box-${SB_VERSION}-linux-amd64.tar.gz"
    tar -xzf /tmp/sing-box.tar.gz -C /tmp/
    mv /tmp/sing-box-${SB_VERSION}-linux-amd64/sing-box /usr/local/bin/
    chmod +x /usr/local/bin/sing-box
    mkdir -p /usr/local/etc/sing-box
fi

log "正在安装 Cloudflared (Argo)..."
curl -L --output /usr/local/bin/cloudflared https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64
chmod +x /usr/local/bin/cloudflared

# ==================== 5. 生成 Xray 配置与服务 ====================
if [[ "$ENABLE_XRAY" == "true" ]]; then
    log "正在生成 Xray 配置与系统服务..."
    mkdir -p /usr/local/etc/xray

    cat << EOF > /usr/local/etc/xray/config.json
{
  "log": { "loglevel": "warning" },
  "inbounds": [
    {
      "port": ${XRAY_PORT},
      "protocol": "vless",
      "settings": {
        "clients": [
          { "id": "${NODE_UUID}", "level": 0, "email": "admin@xray" }
        ],
        "decryption": "none"
      },
      "streamSettings": {
        "network": "xhttp",
        "security": "none"
      }
    }
  ],
  "outbounds": [
    { "protocol": "freedom", "tag": "direct" },
    { "protocol": "blackhole", "tag": "blocked" }
  ]
}
EOF

    # 显式写入 xray.service
    cat << EOF > /etc/systemd/system/xray.service
[Unit]
Description=Xray Service
After=network.target nss-lookup.target

[Service]
User=root
CapabilityBoundingSet=CAP_NET_ADMIN CAP_NET_BIND_SERVICE
AmbientCapabilities=CAP_NET_ADMIN CAP_NET_BIND_SERVICE
NoNewPrivileges=true
ExecStart=/usr/local/bin/xray run -config /usr/local/etc/xray/config.json
Restart=on-failure
RestartPreventExitStatus=23

[Install]
WantedBy=multi-user.target
EOF

    systemctl daemon-reload
    systemctl restart xray
    systemctl enable xray
    log "✅ Xray 服务配置并启动成功！"
fi

# ==================== 6. 生成 Sing-box 配置与服务 ====================
if [[ "$ENABLE_SINGBOX" == "true" ]]; then
    log "正在生成 Sing-box 配置与系统服务..."
    cat << EOF > /usr/local/etc/sing-box/config.json
{
  "log": { "level": "warn" },
  "inbounds": [
    {
      "type": "vless",
      "tag": "vless-in",
      "listen": "0.0.0.0",
      "listen_port": ${SINGBOX_PORT},
      "users": [
        { "uuid": "${NODE_UUID}", "name": "admin@singbox" }
      ],
      "transport": {
        "type": "httpupgrade"
      }
    }
  ],
  "outbounds": [
    { "type": "direct", "tag": "direct" },
    { "type": "block", "tag": "block" }
  ]
}
EOF

    # 显式写入 sing-box.service
    cat << EOF > /etc/systemd/system/sing-box.service
[Unit]
Description=sing-box Service
Documentation=https://sing-box.sagernet.org
After=network.target nss-lookup.target

[Service]
User=root
ExecStart=/usr/local/bin/sing-box run -c /usr/local/etc/sing-box/config.json
Restart=on-failure
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF

    systemctl daemon-reload
    systemctl restart sing-box
    systemctl enable sing-box
    log "✅ Sing-box 服务配置并启动成功！"
fi

# ==================== 7. 配置并启动 Argo 隧道 ====================
if [[ -n "$ARGO_TOKEN" ]]; then
    log "正在配置并启动 Argo 隧道..."
    cloudflared service install "$ARGO_TOKEN"
    systemctl daemon-reload
    systemctl restart cloudflared
    systemctl enable cloudflared
    log "✅ Argo 隧道已成功绑定并启动！"
else
    warn "⚠️ 未检测到 ARGO_TOKEN，跳过隧道自动启动。"
fi

log "🎉 Nexus-Argo v3.6 服务端部署与配置全部完成！"
