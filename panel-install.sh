#!/usr/bin/env bash
# Nexus-Argo 管理面板 — 一键 SSH 安装
# 用法 (VPS 上 root 执行), 两种装法:
#   ① SSH 面板安装 (交互, 默认): 一步步确认, 装完用 IP:端口进面板。
#       bash <(curl -Ls https://raw.githubusercontent.com/Sid-x2026/Nexus-Argo/main/panel-install.sh)
#   ② Argo 绑定安装 (变量一键): Token / 域名 / 端口写进命令里, 直接按
#       变量装, 不做二次确认, 装完走隧道域名进面板。参数用双引号框起来,
#       只在引号里填占位值 (示例用英文占位, 不要填真实域名):
#       ARGO_TOKEN="your-token" PANEL_DOMAIN="your-panel-domain" PANEL_PORT="2053" \
#         bash <(curl -Ls https://raw.githubusercontent.com/Sid-x2026/Nexus-Argo/main/panel-install.sh)
#       不写 Token / 域名就是直连 / IP 进面板 (走①)。
#   bash panel-install.sh uninstall     # 只拆面板, 不动节点部署
#   bash panel-install.sh info          # 呼出面板信息: 登录地址/域名/后缀/端口/账号
#
# 默认只装面板 (不需要 Token): 装完用 http://VPS-IP:2053 + 账号密码进面板,
# 在面板里填设置、点部署, 自动完成部署。装完会问要不要绑定隧道:
#   要绑: 提供 Token + 面板域名 + 面板端口, 装 cloudflared、绑面板到隧道,
#   之后走隧道域名进面板, 验证后可关闭 SSH 端口 22。
#   只用直连协议: 跳过, 直接用面板 (端口访问) 装直连协议。
# 面板只有一个版本, 隧道版与端口版功能完全一样, 区别只在访问方式。
# 安装顺序只有一种: 都是先装面板, 区别只在面板怎么连 (绑定隧道 vs IP 直连)。
set -euo pipefail

# ---- 终端显示: LOGO 横幅 + 分隔线 + 颜色 ----
# 只在终端里生效 (接终端才有颜色); 输出被管道/重定向时自动退回纯文本,
# 所以面板日志、文件记录里不会出现乱码。NO_COLOR=1 可强制关掉颜色。
if [ -t 1 ] && [ "${NO_COLOR:-}" = "" ]; then
  C_RESET=$'\033[0m'; C_BOLD=$'\033[1m'; C_GREEN=$'\033[1;32m'
  C_YELLOW=$'\033[1;33m'; C_RED=$'\033[1;31m'; C_CYAN=$'\033[1;36m'; C_BLUE=$'\033[1;34m'
else
  C_RESET=''; C_BOLD=''; C_GREEN=''; C_YELLOW=''; C_RED=''; C_CYAN=''; C_BLUE=''
fi
sep() { echo "=================================================================="; }
banner() {
  sep
  echo "${C_CYAN}${C_BOLD}  Nexus-Argo  管理面板 · 一键安装${C_RESET}"
  echo "  Nexus-Argo Panel Installer  |  先装面板, 再在面板里部署节点"
  sep
}
step() { echo ""; echo "${C_BLUE}▶ $*${C_RESET}"; }
ok()   { echo "${C_GREEN}✔ $*${C_RESET}"; }
warn() { echo "${C_YELLOW}⚠ $*${C_RESET}"; }

# ---- 居中显示: 只用于最后的完成提示块 ----
# 宽度来源: tput cols (stdout 接终端) → stty size </dev/tty (stdout 被管道
# 但还有控制终端时, 只读终端尺寸、不读输入) → $COLUMNS → 拿不到就 0。
# 宽度 < 60 列或拿不到时退回左对齐, 不报错。颜色沿用上面的 [ -t 1 ]
# 规则 (管道输出时 C_* 本来就是空的, 所以管道下既无颜色码、也不会乱码)。
term_width() {
  local w=""
  w="$(tput cols 2>/dev/null || true)"
  if [ -z "$w" ] || ! [[ "$w" =~ ^[0-9]+$ ]]; then
    if [ -e /dev/tty ]; then
      # 2>/dev/null 写在整组外面: /dev/tty 打不开时的报错也压得住,
      # 不会每行往终端 / 日志里刷 "No such device"。
      w="$( { stty size </dev/tty || true; } 2>/dev/null | awk '{print $2}')"
    fi
  fi
  if [ -z "$w" ] || ! [[ "$w" =~ ^[0-9]+$ ]]; then w="${COLUMNS:-}"; fi
  if [ -z "$w" ] || ! [[ "$w" =~ ^[0-9]+$ ]]; then w=0; fi
  printf '%s' "$w"
}
# display_width <纯文本>: 按终端列数算宽度, 与 locale 无关
# (中文 / ★ 这类多字节字符按 2 列算, ASCII 按 1 列算;
#  只按字节数算, 不依赖 ${#} 在不同 locale 下是字符还是字节)。
display_width() {
  local text="$1" bytes nonascii mb
  bytes="$(printf '%s' "$text" | wc -c)"
  nonascii="$(printf '%s' "$text" | LC_ALL=C tr -d '\000-\177' | wc -c)"
  mb="$(printf '%s' "$text" | LC_ALL=C grep -o $'[\xc2-\xf4]' | wc -l || true)"
  printf '%s' $(( bytes - nonascii + 2 * mb ))
}
# center_line <整行文本, 可带颜色码>: 先去掉 ANSI 颜色码再算宽度,
# 左边补空格居中; 宽度 < 60 时原样左对齐输出。
center_line() {
  local line="$1" plain wide pad=0 w
  plain="$(printf '%s' "$line" | sed 's/\x1b\[[0-9;]*m//g')"
  w="$(term_width)"
  if [ "$w" -ge 60 ]; then
    wide="$(display_width "$plain")"
    [ "$wide" -lt "$w" ] && pad=$(( (w - wide) / 2 ))
  fi
  printf '%*s%s\n' "$pad" '' "$line"
}

PANEL_PORT="${PANEL_PORT:-2053}"
PANEL_PATH="${PANEL_PATH:-/Nexus}"
RAW_BASE="${RAW_BASE:-https://raw.githubusercontent.com/Sid-x2026/Nexus-Argo/main}"
WORKDIR="/opt/nexus-argo"
PANEL_DIR="$WORKDIR/panel"

log() { echo "[nexus-panel] $*"; }
die() { echo "[nexus-panel] ERROR: $*" >&2; exit 1; }

if [ "${1:-}" = "info" ]; then
  echo "Nexus-Argo 面板信息"
  echo "  面板目录: $PANEL_DIR"
  if [ -f "$PANEL_DIR/panel-info.json" ]; then cat "$PANEL_DIR/panel-info.json"; else echo "  (未找到 panel-info.json, 可能是旧版安装, 请重装一次)"; fi
  echo "  面板容器: $(docker ps --format '{{.Names}} {{.Status}}' 2>/dev/null | grep nexus-panel || echo '未运行')"
  echo "  订阅端口/路径: 见面板「订阅地址」页"
  exit 0
fi
if [ "${1:-install}" = "uninstall" ]; then
  log "只拆面板 (节点部署不动, 要清节点请在面板里点清除部署, 或跑 install.sh uninstall) ..."
  docker rm -f nexus-panel nexus-panel-argo >/dev/null 2>&1 || true
  docker rmi nexus-panel >/dev/null 2>&1 || true
  rm -rf "$PANEL_DIR"
  log "Done. 面板已拆, 节点部署还在。"
  exit 0
fi

[ "$(id -u)" = "0" ] || die "run as root"
banner
step "步骤 1/5  检查环境"
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
step "步骤 2/5  下载面板文件"
mkdir -p "$PANEL_DIR" "$WORKDIR/web"
# 必需文件: 下载失败重试 3 次, 仍失败才中断 (并删掉下载一半的残文件)。
dl() { # dl <repo路径> <本地路径>
  local i
  for i in 1 2 3; do
    rm -f "$2"
    if curl -fsSL --max-time 30 "$RAW_BASE/$1" -o "$2"; then
      return 0
    fi
    sleep 1
  done
  rm -f "$2"
  die "下载失败 (已重试 3 次): $RAW_BASE/$1 (检查网络 / 仓库里有没有这个文件)"
}
# 可选文件: 失败只警告、继续安装, 绝不中断。
# (之前这里的 bug: dl() 内部直接 die, 后面那个 || log 根本跑不到,
#  所以一个可选文件 404 就把整个安装炸了。2026-11-19 实测: 仓库
#  web/ 里没有 sub-generator.html, 安装在步骤 2/5 退出。)
dl_optional() { # dl_optional <repo路径> <本地路径> <拿不到时的影响>
  rm -f "$2"
  if curl -fsSL --max-time 30 "$RAW_BASE/$1" -o "$2"; then
    return 0
  fi
  rm -f "$2"
  warn "可选文件没拿到: $1 —— $3"
  return 0
}
dl "panel/panel-server.py" "$PANEL_DIR/panel-server.py"
dl "panel/panel.html" "$PANEL_DIR/panel.html"
dl "panel/Dockerfile" "$PANEL_DIR/Dockerfile"
dl "install.sh" "$WORKDIR/install.sh"
chmod +x "$WORKDIR/install.sh"
dl "web/sub-server.py" "$WORKDIR/web/sub-server.py"
dl_optional "web/sub-generator.html" "$WORKDIR/web/sub-generator.html" "订阅生成器面板暂时不可用, 不影响面板安装和节点部署; 把 web/sub-generator.html 传到仓库后重跑一次安装即可补上"
# 下载完当场核对一遍, 缺什么立刻说清楚, 不留到后面才报错。
ok "必需文件已下载: panel-server.py / panel.html / Dockerfile / install.sh / sub-server.py"
if [ -f "$WORKDIR/web/sub-generator.html" ]; then
  ok "可选文件已下载: sub-generator.html"
else
  warn "sub-generator.html 未下载 (见上面警告), 继续安装"
fi

# ---- 账号: 已有就保留, 没有就用默认账号密码 ----
# 2026-11-19 SHU: 默认登录密码暂时和用户名一样, 全部用 admin。
# 注意: admin/admin 是弱密码, 只是方便第一次登录, 登录后请尽快改密码。
step "步骤 3/5  生成面板账号"
if [ -f "$PANEL_DIR/auth.json" ]; then
  log "已有面板账号, 保留不变 (忘记密码可删 $PANEL_DIR/auth.json 后重跑安装)"
  PASS=""
else
  PASS="admin"
  SALT="$(openssl rand -hex 8)"
  HASH="$(printf '%s%s' "$SALT" "$PASS" | sha256sum | cut -d' ' -f1)"
  printf '{"user": "admin", "salt": "%s", "pass_hash": "%s"}\n' "$SALT" "$HASH" > "$PANEL_DIR/auth.json"
  printf '%s\n' "$PASS" > "$PANEL_DIR/password.txt"
  chmod 600 "$PANEL_DIR/auth.json" "$PANEL_DIR/password.txt"
fi

# ---- 起面板容器: host 网络 (面板端口直接是宿主端口, 部署时的
# 127.0.0.1 健康检查也能通), 挂 docker.sock 管宿主 Docker ----
step "步骤 4/5  启动面板容器"
docker build -q -t nexus-panel "$PANEL_DIR" || die "面板镜像构建失败"
docker rm -f nexus-panel >/dev/null 2>&1 || true
docker run -d --name nexus-panel --restart unless-stopped --network host \
  -v /var/run/docker.sock:/var/run/docker.sock \
  -v "$WORKDIR:/opt/nexus-argo" \
  -e PANEL_DIR="$PANEL_DIR" -e PANEL_PORT="$PANEL_PORT" -e PANEL_PATH="$PANEL_PATH" \
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

# ---- 选装隧道版: 必须明确选 y 或 n 才继续 ----
# 2026-11-19 实测问题: 原来只问一次, 直接回车 / 没输入就当 N 跳过,
# 看起来像脚本突然跳出了安装过程。所以现在: 没选到有效答案就一直问,
# 不会因为一次空输入就结束; 提问从 /dev/tty 读, bash <(curl ...) 时
# stdin 被占用 / 读到 EOF 也不会直接跳过。
# ask_tty <变量> <提示> [hide]: 先从 /dev/tty 读, 打不开就退回 stdin。
# 两边都读到 EOF 时设 ASK_EOF=1, 调用方停在原地等 1 秒再问, 不自动跳过。
# 2026-11-19 实测 bug: 原来用 read -p + 2>/dev/null </dev/tty, -p 的提示
# 是打到 stderr 上的, 被 2>/dev/null 一起吃掉了, 所以输 Token 时上面
# 什么都不显示。现在提示先由 printf 单独打到终端, read 不再带 -p。
ASK_EOF=0
ask_tty() {
  local _var="$1" _prompt="$2" _hide="${3:-}" _line="" _ok=0
  ASK_EOF=0
  # 重定向顺序: 2>/dev/null 必须写在 > /dev/tty 前面, 否则打不开 /dev/tty
  # 的报错压不住, 反而提示本身会被吃掉。
  if [ -e /dev/tty ]; then printf '%s' "$_prompt" 2>/dev/null > /dev/tty || printf '%s' "$_prompt" >&2
  else printf '%s' "$_prompt" >&2; fi
  if [ -e /dev/tty ]; then
    if [ -n "$_hide" ]; then { read -r -s _line </dev/tty; } 2>/dev/null && _ok=1; printf '\n' 2>/dev/null > /dev/tty || echo ""
    else { read -r _line </dev/tty; } 2>/dev/null && _ok=1; fi
  fi
  if [ "$_ok" = "0" ]; then
    if [ -n "$_hide" ]; then read -r -s _line 2>/dev/null && _ok=1; echo ""
    else read -r _line 2>/dev/null && _ok=1; fi
  fi
  [ "$_ok" = "0" ] && ASK_EOF=1
  printf -v "$_var" '%s' "$_line"
}
# do_bind_tunnel: 真正绑隧道 (面板改端口 + 起 cloudflared + 设 TUNNEL_URL)。
# 交互安装与变量一键安装共用: 变量安装直接调它, 不做二次确认。
do_bind_tunnel() {
  # 填了新端口就真把面板重启到那个端口, 路由和面板地址一起改
  # (2026-11-19 实测 bug: 原来只改路由那行字, 面板还监听旧端口)。
  if [ "$PANEL_PORT_BIND" != "$PANEL_PORT" ]; then
    log "面板端口改为 $PANEL_PORT_BIND, 正在重启面板 ..."
    docker rm -f nexus-panel >/dev/null 2>&1 || true
    docker run -d --name nexus-panel --restart unless-stopped --network host \
      -v /var/run/docker.sock:/var/run/docker.sock \
      -v "$WORKDIR:/opt/nexus-argo" \
      -e PANEL_DIR="$PANEL_DIR" -e PANEL_PORT="$PANEL_PORT_BIND" -e PANEL_PATH="$PANEL_PATH" \
      nexus-panel || die "面板容器重启失败"
    for _ in $(seq 1 20); do
      if curl -fsS --max-time 2 -o /dev/null "http://127.0.0.1:$PANEL_PORT_BIND/" 2>/dev/null; then break; fi
      sleep 1
    done
    curl -fsS --max-time 5 -o /dev/null "http://127.0.0.1:$PANEL_PORT_BIND/" 2>/dev/null \
      || die "面板在新端口 $PANEL_PORT_BIND 上没起来, 看日志: docker logs nexus-panel"
    PANEL_PORT="$PANEL_PORT_BIND"
    PANEL_URL="http://$PUB_IP:$PANEL_PORT"
    ok "面板已改到端口 $PANEL_PORT, 地址: $PANEL_URL"
  fi
  docker rm -f nexus-panel-argo >/dev/null 2>&1 || true
  docker run -d --name nexus-panel-argo --restart unless-stopped --network host \
    cloudflare/cloudflared:latest tunnel --no-autoupdate run --token "$ARGO_TOKEN" \
    || die "cloudflared 启动失败"
  case "$PANEL_PATH" in /*) ;; *) PANEL_PATH="/$PANEL_PATH";; esac
  TUNNEL_URL="https://$PANEL_DOMAIN$PANEL_PATH"
  echo ""
  sep
  echo "${C_YELLOW}${C_BOLD}  ⚠ 还差一步: 去 Cloudflare Zero Trust 里加一条路由${C_RESET}"
  echo "  ${C_BOLD}$PANEL_DOMAIN -> http://127.0.0.1:$PANEL_PORT_BIND${C_RESET}"
  echo "  加完立刻生效, 之后用 $TUNNEL_URL 进面板。"
  sep
}

step "步骤 5/5  绑定隧道 (可选)"
# 变量一键安装 (Argo 绑定安装): 命令里同时给了 ARGO_TOKEN 与
# PANEL_DOMAIN, 直接按变量装, 不做二次确认; 没给就走下面的交互安装。
if [ -n "${ARGO_TOKEN:-}" ] && [ -n "${PANEL_DOMAIN:-}" ]; then
  PANEL_PORT_BIND="${PANEL_PORT:-2053}"
  case "$PANEL_PORT_BIND" in ''|*[!0-9]*) die "PANEL_PORT 必须是 1-65535 的数字, got '$PANEL_PORT_BIND'";; esac
  (( PANEL_PORT_BIND >= 1 && PANEL_PORT_BIND <= 65535 )) || die "PANEL_PORT 必须是 1-65535 的数字, got '$PANEL_PORT_BIND'"
  log "变量一键安装 (Argo 绑定安装): Token + 域名已由命令传入, 直接安装, 不再逐项确认。"
  do_bind_tunnel
elif [ -t 0 ]; then
  _bind=""
  echo ""
  sep
  echo "${C_CYAN}${C_BOLD}  ★ 请确认: 是否安装 Argo 隧道版面板? ★${C_RESET}"
  sep
  echo "  y = 安装隧道版: 需要 Token + 面板域名 + 端口, 走隧道域名进面板。"
  echo "  n = 不安装隧道版: 直接用 IP:端口进面板部署节点。"
  echo "  ${C_YELLOW}备注: 不绑隧道也能正常部署, 面板功能完全一样, 区别只在进面板的方式, 以后随时可以再绑。${C_RESET}"
  sep
  while true; do
    ask_tty _bind "请输入 y 或 n: "
    # 没做选择就停在这里等, 不自动跳过、不往下跑 (SHU 2026-11-19 20:10 CST)。
    # 读到 EOF 也一样: 等 1 秒再问, 不当成 n。
    if [ "$ASK_EOF" = "1" ]; then echo "未收到输入, 请重新输入 y 或 n。"; sleep 1; continue; fi
    case "$_bind" in
      y|Y) break;;
      n|N) echo "已选择跳过隧道绑定, 继续安装。"; break;;
      *) echo "未收到有效选择, 请重新输入 y 或 n (直接回车不会跳过)。";;
    esac
  done
  if [ "$_bind" = "y" ] || [ "$_bind" = "Y" ]; then
    ARGO_TOKEN=""; PANEL_DOMAIN=""
    # SHU 2026-11-19 20:21 CST: Token 要明文显示, 不然没法确认输的对不对;
    # 20:21 CST: 不对要能改 — 输完先回显、问一句对不对, 错了就重新输入,
    # 对了才往下走, 没选就停在这里等。
    while true; do
      ARGO_TOKEN=""
      while [ -z "$ARGO_TOKEN" ]; do
        ask_tty ARGO_TOKEN "Argo Tunnel Token: "
        if [ "$ASK_EOF" = "1" ]; then echo "未收到输入, 请重新输入 Token。"; sleep 1; continue; fi
        [ -n "$ARGO_TOKEN" ] || echo "Token 不能为空, 请重新输入。"
      done
      echo "已输入 Token: $ARGO_TOKEN"
      _tok_ok=""
      while true; do
        ask_tty _tok_ok "这个 Token 对吗? (回车默认 y 确认 / n 重新输入): "
        if [ "$ASK_EOF" = "1" ]; then echo "未收到输入, 请重新输入 y 或 n。"; sleep 1; continue; fi
        case "$_tok_ok" in
          ""|y|Y) break;;
          n|N) echo "重新输入 Token:"; break;;
          *) echo "请输入 y 或 n (回车默认 y)。";;
        esac
      done
      case "$_tok_ok" in
        ""|y|Y) break;;
        *) continue;;
      esac
    done
    # 面板域名: 同样二次确认, 回车默认 y。
    while true; do
      PANEL_DOMAIN=""
      while [ -z "$PANEL_DOMAIN" ]; do
        ask_tty PANEL_DOMAIN "隧道面板域名 (例如 panel.ahstshu.eu.cc): "
        if [ "$ASK_EOF" = "1" ]; then echo "未收到输入, 请重新输入隧道面板域名。"; sleep 1; continue; fi
        [ -n "$PANEL_DOMAIN" ] || echo "隧道面板域名不能为空, 请重新输入。"
      done
      echo "已输入隧道面板域名: $PANEL_DOMAIN"
      _dom_ok=""
      while true; do
        ask_tty _dom_ok "这个隧道面板域名对吗? (回车默认 y 确认 / n 重新输入): "
        if [ "$ASK_EOF" = "1" ]; then echo "未收到输入, 请重新输入 y 或 n。"; sleep 1; continue; fi
        case "$_dom_ok" in
          ""|y|Y) break;;
          n|N) echo "重新输入隧道面板域名:"; break;;
          *) echo "请输入 y 或 n (回车默认 y)。";;
        esac
      done
      case "$_dom_ok" in
        ""|y|Y) break;;
        *) continue;;
      esac
    done
    # 面板端口: 须与 Zero Trust 路由端口一致, 面板本身也会改到这个端口,
    # 面板端口 / 路由端口 / 面板地址三处必须一致。
    while true; do
      ask_tty _pp "面板端口 [默认 $PANEL_PORT, 须与 Zero Trust 路由端口一致]: "
      PANEL_PORT_BIND="${_pp:-$PANEL_PORT}"
      if ! [[ "$PANEL_PORT_BIND" =~ ^[0-9]+$ ]] || (( PANEL_PORT_BIND < 1 || PANEL_PORT_BIND > 65535 )); then
        echo "面板端口必须是 1-65535 的数字, 收到的是 '$PANEL_PORT_BIND', 请重新输入。"
        continue
      fi
      echo "已设置面板端口: $PANEL_PORT_BIND (Zero Trust 路由请填: $PANEL_DOMAIN -> http://127.0.0.1:$PANEL_PORT_BIND)"
      ask_tty _pp2 "面板后缀 [默认 /Nexus, 回车即默认, 不加后缀填 /]: "
      PANEL_PATH="${_pp2:-/Nexus}"; [ "$PANEL_PATH" = "/" ] && PANEL_PATH=""
      echo "已设置面板后缀: ${PANEL_PATH:-无}"
      _port_ok=""
      while true; do
        ask_tty _port_ok "这个端口对吗? (回车默认 y 确认 / n 重新输入): "
        if [ "$ASK_EOF" = "1" ]; then echo "未收到输入, 请重新输入 y 或 n。"; sleep 1; continue; fi
        case "$_port_ok" in
          ""|y|Y) break;;
          n|N) echo "重新输入面板端口:"; break;;
          *) echo "请输入 y 或 n (回车默认 y)。";;
        esac
      done
      case "$_port_ok" in
        ""|y|Y) break;;
        *) continue;;
      esac
    done
    do_bind_tunnel
  fi
else
  echo "非交互执行, 已跳过隧道绑定 (以后随时可以再绑)。"
fi

echo ""
echo "接下来: 打开面板 -> 填设置 -> 点「开始部署」, 部署日志和检查在面板里看。"
echo "只用直连协议: 不用绑隧道, 直接用面板装直连协议就行。"
if [ -n "$PASS" ]; then
  echo "密码也存在 $PANEL_DIR/password.txt (权限 600)"
fi
printf '{\n  "panel_url": "%s",\n  "tunnel_panel_url": "%s",\n  "panel_domain": "%s",\n  "panel_path": "%s",\n  "panel_port": "%s",\n  "account": "admin"\n}\n' "$PANEL_URL" "${TUNNEL_URL:-}" "${PANEL_DOMAIN:-}" "${PANEL_PATH:-}" "$PANEL_PORT" > "$PANEL_DIR/panel-info.json" 2>/dev/null || true
echo ""
sep
echo "=================================================================="
echo "  ★ 面板已安装完成, 请保存下面这几行 ★"
echo "=================================================================="
# 关键内容最后显示, 地址对齐, 隧道域名在前、IP 地址在后
if [ -n "$TUNNEL_URL" ]; then
  printf '  %s %s%s%s %s\n' "隧道面板地址:" "${C_BOLD}${C_GREEN}" "$TUNNEL_URL" "${C_RESET}" "（需在 Zero Trust 中添加对应路由后方可访问）"
fi
printf '  %s     %s%s%s\n' "面板地址:" "${C_BOLD}${C_GREEN}" "$PANEL_URL" "${C_RESET}"
printf '  %s         %s\n' "账号:" "admin"
if [ -n "$PASS" ]; then
  printf '  %s         %s%s%s  %s\n' "密码:" "${C_BOLD}${C_YELLOW}" "$PASS" "${C_RESET}" "← 只显示这一次, 请立即保存"
else
  printf '  %s         %s\n' "密码:" "不变"
fi
echo ""
echo "  卸载方式 (简单版):"
echo "    只拆面板:  bash panel-install.sh uninstall"
echo "    只清节点:  bash /opt/nexus-argo/install.sh uninstall"
echo "    全部清掉:  bash <(curl -Ls https://raw.githubusercontent.com/Sid-x2026/Nexus-Argo/main/cleanup.sh)"
echo "    （重装想快一点: 别跑 cleanup 清镜像, 镜像还在就不用重新拉）"
echo "=================================================================="
echo ""
echo "本次安装内容: 只装了管理面板 (nexus-panel, 端口 $PANEL_PORT), 节点尚未部署, 请在面板里填设置点「开始部署」。"
[ -n "$TUNNEL_URL" ] && echo "面板隧道容器: nexus-panel-argo (cloudflared, 隧道面板域名 $PANEL_DOMAIN)"
echo "面板文件: /opt/nexus-argo/panel/ (panel-server.py / panel.html / Dockerfile / auth.json / password.txt)"
echo "部署脚本与订阅服务: /opt/nexus-argo/install.sh、/opt/nexus-argo/web/sub-server.py"
echo "卸载都不动 Cloudflare 隧道 / Token / 域名 / Zero Trust 路由, 云端配置重装时直接复用。"
sep
