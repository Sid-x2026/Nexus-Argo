#!/bin/bash

# 确保以 root 权限运行
[[ $EUID -ne 0 ]] && echo -e "\033[31m❌ 请使用 root 权限运行此脚本！(sudo -i)\033[0m" && exit 1

# 颜色定义
RED="\033[31m"
GREEN="\033[32m"
YELLOW="\033[33m"
CYAN="\033[36m"
NC="\033[0m"

WORK_DIR="/opt/nexus-ago"

# ==========================================
# 卸载与深度清理逻辑 (响应 web 面板 uninstall 指令)
# ==========================================
if [ "${1:-}" = "uninstall" ]; then
    echo -e "${YELLOW}⏳ 正在执行 Nexus-Argo 卸载与清理程序...${NC}"
    
    # 1. 停止并删除关联的 Docker 容器
    echo -e "${CYAN}▶ 正在停止并清理 Docker 容器 (xray, sing-box, argo)...${NC}"
    docker rm -f nexus-xray nexus-singbox nexus-argo 2>/dev/null
    
    # 2. 删除本地工作目录与配置
    if [ -d "$WORK_DIR" ]; then
        echo -e "${CYAN}▶ 正在清理工作目录与配置文件 (${WORK_DIR})...${NC}"
        rm -rf "$WORK_DIR"
    fi
    
    echo -e "${GREEN}==========================================${NC}"
    echo -e "${GREEN}✨ Nexus-Argo 已成功卸载，服务器环境已还原！${NC}"
    echo -e "${GREEN}==========================================${NC}"
    exit 0
fi

echo -e "${GREEN}==========================================${NC}"
echo -e "${GREEN}    Nexus-Argo 自动化部署与运维系统 v1.0    ${NC}"
echo -e "${GREEN}==========================================${NC}"

# 1. 优先读取环境变量（适配网页一键复制命令），若无则交互式输入
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

# 接收并适配 Web 面板传递的各项核心开关与参数
ENABLE_XRAY="${ENABLE_XRAY:-true}"
XRAY_PORT="${XRAY_PORT:-8001}"
XRAY_DOMAIN="${XRAY_DOMAIN:-xray.yourdomain.com}"

ENABLE_SINGBOX="${ENABLE_SINGBOX:-true}"
SINGBOX_PORT="${SINGBOX_PORT:-8002}"
SINGBOX_DOMAIN="${SINGBOX_DOMAIN:-singbox.yourdomain.com}"

ENABLE_GATEWAY="${ENABLE_GATEWAY:-false}"
PROXY_GATEWAY="${PROXY_GATEWAY:-127.0.0.1:7928}"

# 2. 检查并安装 Docker
if ! command -v docker &> /dev/null; then
    echo -e "${YELLOW}⏳ 检测到未安装 Docker，正在自动安装...${NC}"
    curl -fsSL https://get.docker.com | sh
    systemctl enable --now docker
else
    echo -e "${GREEN}✅ Docker 已安装${NC}"
fi

# 3. 创建工作目录
mkdir -p "$WORK_DIR/config" "$WORK_DIR/web"
cd "$WORK_DIR"

# 4. 下载 GitHub 上的 Web 面板源码
echo -e "${YELLOW}⏳ 正在从 GitHub 同步 Web 面板源码...${NC}"
REPO_RAW_URL="https://raw.githubusercontent.com/Sid-x2026/Nexus-Argo/main"
curl -s -o "$WORK_DIR/web/index.html" "$REPO_RAW_URL/web/index.html"

# 5. 生成/渲染后端内核配置文件
echo -e "${YELLOW}⏳ 正在生成双核代理配置文件...${NC}"

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
        "tlsSettings": {
          "serverName": "$XRAY_DOMAIN"
        }
      }
    }
  ],
  "outbounds": [{ "protocol": "freedom" }]
}
EOF
    echo -e "${GREEN}✅ Xray 配置文件已生成 (端口: $XRAY_PORT)${NC}"
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
      "transport": {
        "type": "httpupgrade"
      }
    }
  ],
  "outbounds": [{ "type": "direct" }]
}
EOF
    echo -e "${GREEN}✅ Sing-box 配置文件已生成 (端口: $SINGBOX_PORT)${NC}"
fi

# 6. 清理旧容器并启动新容器集群
echo -e "${YELLOW}⏳ 正在启动 Docker 容器集群...${NC}"
docker rm -f nexus-xray nexus-singbox nexus-argo 2>/dev/null

if [ "$ENABLE_XRAY" = "true" ]; then
    docker run -d \
        --name nexus-xray \
        --restart always \
        -p 127.0.0.1:$XRAY_PORT:$XRAY_PORT \
        -v "$WORK_DIR/config/xray_config.json:/etc/xray/config.json" \
        teddysun/xray:latest
fi

if [ "$ENABLE_SINGBOX" = "true" ]; then
    docker run -d \
        --name nexus-singbox \
        --restart always \
        -p 127.0.0.1:$SINGBOX_PORT:$SINGBOX_PORT \
        -v "$WORK_DIR/config/singbox_config.json:/etc/sing-box/config.json" \
        sagernet/sing-box:latest run -c /etc/sing-box/config.json
fi

# 启动 Cloudflare Argo 隧道
docker run -d \
    --name nexus-argo \
    --restart always \
    cloudflare/cloudflared:latest tunnel run --token "$ARGO_TOKEN"

echo -e "\n${GREEN}==========================================${NC}"
echo -e "${GREEN}🎉 Nexus-Argo 自动化部署全部完成！${NC}"
echo -e "${GREEN}📌 节点 UUID: ${USER_UUID}${NC}"
echo -e "${GREEN}📌 Xray 状态: ${ENABLE_XRAY} (端口 ${XRAY_PORT})${NC}"
echo -e "${GREEN}📌 Sing-box 状态: ${ENABLE_SINGBOX} (端口 ${SINGBOX_PORT})${NC}"
echo -e "${GREEN}==========================================${NC}"
