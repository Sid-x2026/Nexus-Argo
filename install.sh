#!/bin/bash

# 确保以 root 权限运行
[[ $EUID -ne 0 ]] && echo -e "\033[31m❌ 请使用 root 权限运行此脚本！(sudo -i)\033[0m" && exit 1

# 颜色定义
RED="\033[31m"
GREEN="\033[32m"
YELLOW="\033[33m"
NC="\033[0m"

echo -e "${GREEN}==========================================${NC}"
echo -e "${GREEN}       Nexus-Argo 一键部署向导 v1.0         ${NC}"
echo -e "${GREEN}==========================================${NC}"

# 1. 检查并安装 Docker
if ! command -v docker &> /dev/null; then
    echo -e "${YELLOW}⏳ 检测到未安装 Docker，正在自动安装...${NC}"
    curl -fsSL https://get.docker.com | sh
    systemctl enable --now docker
else
    echo -e "${GREEN}✅ Docker 已安装${NC}"
fi

# 2. 交互式输入配置参数
echo -e "\n${YELLOW}--- 请输入节点配置参数 ---${NC}"
read -p "请输入 UUID (留空则自动随机生成): " USER_UUID
if [ -z "$USER_UUID" ]; then
    USER_UUID=$(cat /proc/sys/kernel/random/uuid)
    echo -e "${GREEN}ℹ️ 已自动生成 UUID: ${USER_UUID}${NC}"
fi

read -p "请输入 Cloudflare Argo Token (必填): " ARGO_TOKEN
if [ -z "$ARGO_TOKEN" ]; then
    echo -e "${RED}❌ Argo Token 不能为空！${NC}"
    exit 1
fi

# 3. 创建工作目录并下载模板
WORK_DIR="/opt/nexus-argo"
mkdir -p "$WORK_DIR/templates" "$WORK_DIR/config"
cd "$WORK_DIR"

echo -e "${YELLOW}⏳ 正在从 GitHub 下载双核配置模板...${NC}"
REPO_RAW_URL="https://raw.githubusercontent.com/2021ahstshu/Nexus-Argo/main"

curl -s -o "$WORK_DIR/templates/xray_config.json.template" "$REPO_RAW_URL/templates/xray_config.json.template"
curl -s -o "$WORK_DIR/templates/singbox_config.json.template" "$REPO_RAW_URL/templates/singbox_config.json.template"

if [ ! -f "$WORK_DIR/templates/xray_config.json.template" ]; then
    echo -e "${RED}❌ 模板下载失败，请检查 GitHub 仓库路径或文件名是否正确！${NC}"
    exit 1
fi

# 4. 动态渲染配置文件（替换 UUID）
echo -e "${YELLOW}⏳ 正在渲染配置文件...${NC}"
sed "s/\${UUID}/$USER_UUID/g" "$WORK_DIR/templates/xray_config.json.template" > "$WORK_DIR/config/xray_config.json"
sed "s/\${UUID}/$USER_UUID/g" "$WORK_DIR/templates/singbox_config.json.template" > "$WORK_DIR/config/singbox_config.json"

# 5. 清理旧容器并启动新容器
echo -e "${YELLOW}⏳ 正在通过 Docker 启动双核与 Argo 隧道...${NC}"
docker rm -f nexus-xray nexus-singbox nexus-argo 2>/dev/null

# 启动 Xray 容器
docker run -d \
    --name nexus-xray \
    --restart always \
    -v "$WORK_DIR/config/xray_config.json:/etc/xray/config.json" \
    teddysun/xray:latest

# 启动 Sing-box 容器
docker run -d \
    --name nexus-singbox \
    --restart always \
    -v "$WORK_DIR/config/singbox_config.json:/etc/sing-box/config.json" \
    sagernet/sing-box:latest run -c /etc/sing-box/config.json

# 启动 Cloudflare Argo 隧道容器
docker run -d \
    --name nexus-argo \
    --restart always \
    cloudflare/cloudflared:latest tunnel run --token "$ARGO_TOKEN"

echo -e "\n${GREEN}==========================================${NC}"
echo -e "${GREEN}🎉 Nexus-Argo 部署成功！${NC}"
echo -e "${GREEN}📌 您的 UUID: ${USER_UUID}${NC}"
echo -e "${GREEN}==========================================${NC}"
