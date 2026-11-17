#!/bin/bash

# 确保以 root 权限运行
[[ $EUID -ne 0 ]] && echo -e "\033[31m❌ 请使用 root 权限运行此脚本！(sudo -i)\033[0m" && exit 1

# 颜色定义
RED="\033[31m"
GREEN="\033[32m"
YELLOW="\033[33m"
NC="\033[0m"

# 工作目录
WORK_DIR="/opt/nexus-ago"

# ==========================================
# 🛡️ 卸载清理函数
# ==========================================
uninstall_nexus() {
    echo -e "\n${YELLOW}⏳ [1/3] 正在停止并清理 Nexus-Argo 相关 Docker 容器...${NC}"
    docker rm -f nexus-xray nexus-singbox nexus-argo 2>/dev/null
    echo -e "${GREEN}✅ 容器已全部清除${NC}"

    echo -e "\n${YELLOW}⏳ [2/3] 正在删除工作目录与配置文件 (${WORK_DIR})...${NC}"
    rm -rf "$WORK_DIR"
    echo -e "${GREEN}✅ 相关文件已清理${NC}"

    echo -e "\n${YELLOW}⏳ [3/3] 清理完成！${NC}"
    echo -e "${GREEN}==========================================${NC}"
    echo -e "${GREEN}🎉 Nexus-Ago 已从您的服务器上彻底卸载！${NC}"
    echo -e "${GREEN}==========================================${NC}\n"
    exit 0
}

# 判断用户是否输入了 uninstall 参数
if [[ "$1" == "uninstall" ]]; then
    echo -e "\n${RED}==========================================${NC}"
    echo -e "${RED}       ⚠️ 警告：即将卸载 Nexus-Ago        ${NC}"
    echo -e "${RED}==========================================${NC}"
    read -p "确定要停止并删除所有容器及配置文件吗？[y/N]: " confirm
    [[ "$confirm" =~ ^[Yy]$ ]] && uninstall_nexus || { echo -e "${YELLOW}已取消卸载。${NC}"; exit 0; }
fi

# ==========================================
# 🚀 正常安装与部署逻辑
# ==========================================
echo -e "${GREEN}==========================================${NC}"
echo -e "${GREEN}    Nexus-Ago 自动化部署与运维系统 v1.0    ${NC}"
echo -e "${GREEN}==========================================${NC}"
echo -e "💡 提示：如需卸载清理，可随时运行：${YELLOW}bash install.sh uninstall${NC}\n"

USER_UUID="${NODE_UUID:-}"
if [ -z "$USER_UUID" ]; then
    read -p "请输入 UUID (留空则自动随机生成): " USER_UUID
    if [ -z "$USER_UUID" ]; then
        USER_UUID=$(cat /proc/sys/kernel/random/uuid)
    fi
fi
echo -e "${GREEN}ℹ️ 当前节点 UUID: ${USER_UUID}${NC}"

ARGO_TOKEN="${ARGO_TOKEN:-}"
if [ -z "$ARGO_TOKEN" ]; then
    read -p "请输入 Cloudflare Argo Token (必填): " ARGO_TOKEN
fi
if [ -z "$ARGO_TOKEN" ]; then
    echo -e "${RED}❌ Argo Token 不能为空！${NC}"
    exit 1
fi

ENABLE_XRAY="${ENABLE_XRAY:-true}"
XRAY_PORT="${XRAY_PORT:-8001}"
XRAY_DOMAIN="${XRAY_DOMAIN:-xray.yourdomain.com}"
ENABLE_SINGBOX="${ENABLE_SINGBOX:-true}"
SINGBOX_PORT="${SINGBOX_PORT:-8002}"
SINGBOX_DOMAIN="${SINGBOX_DOMAIN:-singbox.yourdomain.com}"

if ! command -v docker &> /dev/null; then
    echo -e "${YELLOW}⏳ 检测到未安装 Docker，正在自动安装...${NC}"
    curl -fsSL https://get.docker.com | sh
    systemctl enable --now docker
else
    echo -e "${GREEN}✅ Docker 已安装${NC}"
fi

mkdir -p "$WORK_DIR/config" "$WORK_DIR/web"
cd "$WORK_DIR"

REPO_RAW_URL="https://raw.githubusercontent.com/Sid-x2026/Nexus-Argo/main"
curl -s -o "$WORK_DIR/web/index.html" "$REPO_RAW_URL/web/index.html"

if [ "$ENABLE_XRAY" = "true" ]; then
    cat << EOF > "$WORK_DIR/config/xray_config.json"
{
  "log": { "loglevel": "warning" },
  "inbounds": [
    {
      "port": $XRAY_PORT,
      "protocol": "vless",
      "settings": {
        "clients": [{ "id": "$USER_UUID" }],
        "decryption": "none"
      },
      "streamSettings": {
        "network": "xhttp",
        "security": "tls",
        "tlsSettings": { "serverName": "$XRAY_DOMAIN" }
      }
    }
  ],
  "outbounds": [{ "protocol": "freedom" }]
}
EOF
fi

if [ "$ENABLE_SINGBOX" = "true" ]; then
    cat << EOF > "$WORK_DIR/config/singbox_config.json"
{
  "log": { "level": "warn" },
  "inbounds": [
    {
      "type": "vless",
      "tag": "vless-in",
      "listen": "0.0.0.0",
      "listen_port": $SINGBOX_PORT,
      "users": [{ "uuid": "$USER_UUID" }],
      "transport": { "type": "httpupgrade" }
    }
  ],
  "outbounds": [{ "type": "direct" }]
}
EOF
fi

docker rm -f nexus-xray nexus-singbox nexus-argo 2>/dev/null

if [ "$ENABLE_XRAY" = "true" ]; then
    docker run -d --name nexus-xray --restart always -p 127.0.0.1:$XRAY_PORT:$XRAY_PORT -v "$WORK_DIR/config/xray_config.json:/etc/xray/config.json" teddysun/xray:latest
fi

if [ "$ENABLE_SINGBOX" = "true" ]; then
    docker run -d --name nexus-singbox --restart always -p 127.0.0.1:$SINGBOX_PORT:$SINGBOX_PORT -v "$WORK_DIR/config/singbox_config.json:/etc/sing-box/config.json" sagernet/sing-box:latest run -c /etc/sing-box/config.json
fi

docker run -d --name nexus-argo --restart always cloudflare/cloudflared:latest tunnel run --token "$ARGO_TOKEN"

echo -e "\n${GREEN}🎉 Nexus-Argo 自动化部署全部完成！${NC}"
