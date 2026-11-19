#!/usr/bin/env bash
# Nexus-Argo 一键清理 — 安装失败 / 想从零重来时用
# 用法 (VPS 上 root 执行, 一条命令):
#   bash <(curl -Ls https://raw.githubusercontent.com/Sid-x2026/Nexus-Argo/main/cleanup.sh)
#
# 清掉的东西 (只动 Nexus-Argo 自己的, 别的都不动):
#   容器: nexus-panel nexus-panel-argo nexus-xray nexus-singbox
#         nexus-sb-dispatch nexus-argo nexus-web-panel nexus-sub
#   镜像: nexus-panel
#   目录: /opt/nexus-argo 整个 (含面板账号、部署配置、残留的下载文件,
#         以及 Docker 误建的同名目录, 例如 web/sub-generator.html 被建成了目录)
# 不动的东西: Docker 本身、其他容器和镜像、Cloudflare 隧道 / Token /
#   域名 / Zero Trust 路由 (云端的都不受影响, 重装时直接复用)。
# 清完可直接重跑面板安装, 从零开始:
#   bash <(curl -Ls https://raw.githubusercontent.com/Sid-x2026/Nexus-Argo/main/panel-install.sh)
set -uo pipefail

# ---- 终端显示: LOGO 横幅 + 分隔线 + 颜色 (只在终端里生效) ----
if [ -t 1 ] && [ "${NO_COLOR:-}" = "" ]; then
  C_RESET=$'\033[0m'; C_BOLD=$'\033[1m'; C_GREEN=$'\033[1;32m'
  C_YELLOW=$'\033[1;33m'; C_RED=$'\033[1;31m'; C_CYAN=$'\033[1;36m'; C_BLUE=$'\033[1;34m'
else
  C_RESET=''; C_BOLD=''; C_GREEN=''; C_YELLOW=''; C_RED=''; C_CYAN=''; C_BLUE=''
fi
sep() { echo "=================================================================="; }
step() { echo ""; echo "${C_BLUE}▶ $*${C_RESET}"; }
ok()   { echo "${C_GREEN}✔ $*${C_RESET}"; }
warn() { echo "${C_YELLOW}⚠ $*${C_RESET}"; }

[ "$(id -u)" = "0" ] || { echo "[nexus-cleanup] ERROR: 请用 root 执行" >&2; exit 1; }

sep
echo "${C_CYAN}${C_BOLD}  Nexus-Argo  一键清理${C_RESET}"
echo "  只清 Nexus-Argo 自己的容器 / 镜像 / /opt/nexus-argo, 不动其他东西"
sep

# ---- 步骤 1/3  容器: 有就停并删, 没有就跳过 ----
step "步骤 1/3  清理容器"
_removed=0
if command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; then
  for c in nexus-panel nexus-panel-argo nexus-xray nexus-singbox nexus-sb-dispatch nexus-argo nexus-web-panel nexus-sub; do
    if docker ps -a --format '{{.Names}}' 2>/dev/null | grep -qx "$c"; then
      docker rm -f "$c" >/dev/null 2>&1 || true
      echo "  已删容器: $c"
      _removed=$(( _removed + 1 ))
    fi
  done
  [ "$_removed" = "0" ] && echo "  没有 Nexus-Argo 容器, 跳过"
else
  warn "Docker 不可用或没跑起来, 跳过容器清理 (只清目录)"
fi

# ---- 步骤 2/3  镜像 + 目录 ----
step "步骤 2/3  清理镜像与目录"
if command -v docker >/dev/null 2>&1 && docker images -q nexus-panel 2>/dev/null | grep -q .; then
  docker rmi nexus-panel >/dev/null 2>&1 || true
  echo "  已删镜像: nexus-panel"
else
  echo "  没有 nexus-panel 镜像, 跳过"
fi
if [ -e /opt/nexus-argo ]; then
  rm -rf /opt/nexus-argo
  echo "  已删目录: /opt/nexus-argo"
else
  echo "  没有 /opt/nexus-argo, 跳过"
fi

# ---- 步骤 3/3  核对残留 ----
step "步骤 3/3  核对残留"
_left=0
if command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; then
  _left=$(docker ps -a --format '{{.Names}}' 2>/dev/null | grep -c '^nexus-' || true)
  [ "$_left" = "0" ] && ok "容器: 无残留" || warn "容器: 还有 $_left 个 nexus-* 容器, 手动看一下: docker ps -a | grep nexus-"
fi
if [ -e /opt/nexus-argo ]; then
  warn "目录: /opt/nexus-argo 还在, 手动看一下"
else
  ok "目录: /opt/nexus-argo 已清掉"
fi

echo ""
sep
echo "${C_GREEN}${C_BOLD}  ★ 清理完成, VPS 已回到干净状态 ★${C_RESET}"
sep
echo "Cloudflare 隧道 / Token / 域名 / Zero Trust 路由都不受影响, 重装时直接复用。"
echo "现在可以直接重装面板:"
echo "  ${C_GREEN}bash <(curl -Ls https://raw.githubusercontent.com/Sid-x2026/Nexus-Argo/main/panel-install.sh)${C_RESET}"
sep
