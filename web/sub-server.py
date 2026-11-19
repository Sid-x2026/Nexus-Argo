#!/usr/bin/env python3
# Nexus-Argo 订阅服务 v3.2 — 动态订阅, 不用 Worker (2026-11-19)
#
# v3 (nginx 静态文件) 丢掉的唯一东西是 Worker 的 ?addrs= 现场裂变:
# 换优选地址要重跑部署命令。这个服务把它拿回来, 但不把 Worker
# 拿回来 — 裂变逻辑直接跑在 VPS 上, 和 install.sh 用同一套变量、
# 同一套 vless 生成规则, 所以没有任何东西要两处同步。
#
# 跑法 (install.sh 起 nexus-sub 容器, python:3-alpine, 只有标准库):
#   python3 /app/sub-server.py
#   环境变量全部由 install.sh 从部署命令传进来:
#     NODE_UUID, NODE_PREFIX (旧单前缀, 仅作回退),
#     NODE_PREFIX_ARGO (默认 "Argo", 隧道节点备注前缀),
#     NODE_PREFIX_DIRECT (默认 "直连", 直连节点备注前缀),
#     XRAY_DOMAIN, XRAY_CONFIGS, SINGBOX_DOMAIN, SINGBOX_CONFIGS,
#     ENABLE_XRAY, ENABLE_SINGBOX,
#     OPT_ADDRS (默认优选地址, 不带 ?addrs= 时用它),
#     SUB_PORT (监听端口, 默认 18080),
#     SUB_PATH (订阅路径前缀, 默认 sub, 面板自定义订阅地址),
#     ENABLE_DIRECT, DIRECT_DOMAIN, DIRECT_XRAY_TLS,
#     DIRECT_XRAY_TLS_PORT, DIRECT_XRAY_QUIC, DIRECT_XRAY_QUIC_PORT,
#     DIRECT_SB_TLS, DIRECT_SB_TLS_PORT (直连节点, 原样进订阅, 不裂变)
#     直连新协议 (与 install.sh 同一套变量、同一套链接规则, 改一处
#     要改另一处; 只有开启的协议才渲染、才进订阅):
#       DIRECT_VLESS_TCP / _PORT (默认 8082, 明文 TCP),
#       DIRECT_VLESS_REALITY / _PORT (默认 9447, Reality),
#       DIRECT_VLESS_WS / _PORT (默认 8083, 明文 ws, path /ws),
#       DIRECT_VLESS_GRPC / _PORT (默认 8084, gRPC+TLS),
#       DIRECT_TROJAN / _PORT (默认 9443, 密码 = NODE_UUID),
#       DIRECT_VMESS_WS / _PORT (默认 9444, ws+TLS, alterId 0),
#       DIRECT_SB_TROJAN / _PORT (默认 9445),
#       DIRECT_SS / _PORT (默认 8388, aes-256-gcm, 密码 = NODE_UUID),
#       DIRECT_HY2 / _PORT (默认 9446, UDP, 密码 = NODE_UUID, alpn h3)
#     CERT_MODE (selfsigned|upload|acme): TLS 直连链接带 allowInsecure=1,
#     acme 有效证书时不带。Reality 公钥/shortId/dest 由 install.sh
#     以 REALITY_PUBLIC_KEY / REALITY_SHORT_ID / REALITY_DEST /
#     REALITY_EFF_SNI 环境变量传进来 (nexus-sub 容器不挂载 reality.json);
#     指纹 / flow / 加密方式 / spiderX 以 REALITY_FINGERPRINT /
#     REALITY_FLOW / REALITY_ENCRYPTION / REALITY_SPIDERX 传进来,
#     客户端加密串以 REALITY_ENC_CLIENT 传进来 (install.sh 已拼好的
#     完整 mlkem768x25519plus.…0rtt.<公钥>, "none" = 不开加密层)。
#   容器内监听 0.0.0.0 (Docker 端口映射进来的流量到的是容器网卡,
#   不是 loopback), 主机侧 docker 只绑 127.0.0.1:$SUB_PORT,
#   公网入口只有 Cloudflare Zero Trust 的一条路由:
#     <SUB_DOMAIN> -> http://127.0.0.1:<SUB_PORT>
#
# 路由 (输出一律标准 base64, 和 Worker / v3 静态文件格式一致,
# 客户端不用改设置; SUB_PATH 默认 sub, 可改):
#   GET /                              订阅生成器面板 (sub-generator.html)
#   GET /config.json                   部署时的 UUID / 默认优选地址, 供面板用
#   GET /help.txt                      纯文本说明
#   GET /<SUB_PATH>/raw/<uuid>         原生订阅, 1 协议 = 1 节点 (隧道 + 直连)
#   GET /<SUB_PATH>/merged/<uuid>?addrs=a,b
#       动态订阅: 隧道节点按 addrs 现场裂变, 直连节点原样跟上;
#       不带 addrs 就用部署时的 OPT_ADDRS
#       &format=singbox: 只去掉 xhttp 节点与带 VLESS 加密层的节点
#       (Sing-box 没有 xhttp 传输, 也不支持 VLESS Encryption, 其余
#       都留着: trojan/vmess/ss/hysteria2/vless 含 quic/reality 都
#       支持, 开了加密层的 Reality 节点除外), 仍是 base64;
#       &format=clash: Mihomo YAML
#       (QUIC 与带 VLESS 加密层的节点除外, Mihomo 不支持 QUIC /
#       VLESS Encryption; 支持 trojan/vmess/ss/
#       hysteria2/vless-reality)
#   GET /<SUB_PATH>/merged?url=<外部订阅地址>&addrs=a,b
#       外部订阅裂变 (Worker v2 就有这个路由, v3 静态文件做不到),
#       同样支持 &format=singbox / clash
#   GET /<SUB_PATH>/raw.txt /<SUB_PATH>/merged.txt   明文版, 排查用
#   uuid 不对 -> 404 (v3 的 nginx 不校验 uuid, 这里补上了)
#
# 安全边界: 只做 GET, 只认这几个路径, 只换 vless 链接里的 host
# (其余部分原样保留, SNI 不变), addrs 最多 20 个且只接受
# 主机名/IP 字符, 外部订阅只拉 http/https 且 10 秒超时。
# 订阅地址本身就是凭证, 不要把它发给不该拿到节点的人。

import base64
import json
import os
import re
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# 生成器面板: / 返回 web/sub-generator.html (install.sh 挂到 /app/),
# 文件不存在时回退到纯文本说明。面板是独立页面, 跟着 VPS 部署
# 一起走, 不用 Worker、不用另外部署。
_GENERATOR_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sub-generator.html")

UUID = os.environ.get("NODE_UUID", "").strip()
# 节点前缀分开 (Part A/B 合同): 隧道节点用 Argo 前缀, 直连节点用
# 直连前缀。旧单前缀 NODE_PREFIX 只作回退 (新变量为空时用它)。
_NODE_PREFIX_RAW = os.environ.get("NODE_PREFIX", "").strip()
PREFIX_ARGO = os.environ.get("NODE_PREFIX_ARGO", "").strip() or _NODE_PREFIX_RAW or "Argo"
PREFIX_DIRECT = os.environ.get("NODE_PREFIX_DIRECT", "").strip() or _NODE_PREFIX_RAW or "直连"
PREFIX = PREFIX_ARGO  # 兼容旧字段: /config.json 的 prefix 仍是隧道前缀
XRAY_DOMAIN = os.environ.get("XRAY_DOMAIN", "").strip()
XRAY_CONFIGS = os.environ.get("XRAY_CONFIGS", "")
SINGBOX_DOMAIN = os.environ.get("SINGBOX_DOMAIN", "").strip()
SINGBOX_CONFIGS = os.environ.get("SINGBOX_CONFIGS", "")
ENABLE_XRAY = os.environ.get("ENABLE_XRAY", "true").lower() in ("true", "1", "yes")
ENABLE_SINGBOX = os.environ.get("ENABLE_SINGBOX", "true").lower() in ("true", "1", "yes")
OPT_ADDRS = [a.strip() for a in os.environ.get("OPT_ADDRS", "cloudflare.com").split(",") if a.strip()] or ["cloudflare.com"]
SUB_PORT = int(os.environ.get("SUB_PORT", "18080") or 18080)
# 自定义订阅地址: 订阅路径前缀, 默认 sub, 面板里可改
SUB_PATH = (os.environ.get("SUB_PATH", "sub") or "sub").strip().strip("/") or "sub"
if not re.match(r"^[A-Za-z0-9._-]+$", SUB_PATH):
    SUB_PATH = "sub"
# 直连节点: 地址就是 VPS 自己, 原样进订阅, 不参与优选地址裂变
ENABLE_DIRECT = os.environ.get("ENABLE_DIRECT", "false").lower() in ("true", "1", "yes")
DIRECT_DOMAIN = os.environ.get("DIRECT_DOMAIN", "").strip()
# 新名 DIRECT_VLESS_TLS/QUIC 与旧名 DIRECT_XRAY_TLS/QUIC 是同一协议的
# 两个名字 (install.sh 与 panel 两边都传, 以新名为准); 只传其一时也要认。
DIRECT_XRAY_TLS = (os.environ.get("DIRECT_VLESS_TLS", os.environ.get("DIRECT_XRAY_TLS", "true")) or "true").lower() in ("true", "1", "yes")
DIRECT_XRAY_TLS_PORT = int(os.environ.get("DIRECT_VLESS_TLS_PORT", os.environ.get("DIRECT_XRAY_TLS_PORT", "443")) or 443)
DIRECT_XRAY_QUIC = (os.environ.get("DIRECT_VLESS_QUIC", os.environ.get("DIRECT_XRAY_QUIC", "false")) or "false").lower() in ("true", "1", "yes")
DIRECT_XRAY_QUIC_PORT = int(os.environ.get("DIRECT_VLESS_QUIC_PORT", os.environ.get("DIRECT_XRAY_QUIC_PORT", "443")) or 443)
DIRECT_SB_TLS = os.environ.get("DIRECT_SB_TLS", "true").lower() in ("true", "1", "yes")
DIRECT_SB_TLS_PORT = int(os.environ.get("DIRECT_SB_TLS_PORT", "8443") or 8443)
DIRECT_VLESS_TCP = os.environ.get("DIRECT_VLESS_TCP", "false").lower() in ("true", "1", "yes")
DIRECT_VLESS_TCP_PORT = int(os.environ.get("DIRECT_VLESS_TCP_PORT", "8082") or 8082)
DIRECT_VLESS_REALITY = os.environ.get("DIRECT_VLESS_REALITY", "false").lower() in ("true", "1", "yes")
DIRECT_VLESS_REALITY_PORT = int(os.environ.get("DIRECT_VLESS_REALITY_PORT", "9447") or 9447)
DIRECT_VLESS_WS = os.environ.get("DIRECT_VLESS_WS", "false").lower() in ("true", "1", "yes")
DIRECT_VLESS_WS_PORT = int(os.environ.get("DIRECT_VLESS_WS_PORT", "8083") or 8083)
DIRECT_VLESS_GRPC = os.environ.get("DIRECT_VLESS_GRPC", "false").lower() in ("true", "1", "yes")
DIRECT_VLESS_GRPC_PORT = int(os.environ.get("DIRECT_VLESS_GRPC_PORT", "8084") or 8084)
DIRECT_TROJAN = os.environ.get("DIRECT_TROJAN", "false").lower() in ("true", "1", "yes")
DIRECT_TROJAN_PORT = int(os.environ.get("DIRECT_TROJAN_PORT", "9443") or 9443)
DIRECT_VMESS_WS = os.environ.get("DIRECT_VMESS_WS", "false").lower() in ("true", "1", "yes")
DIRECT_VMESS_WS_PORT = int(os.environ.get("DIRECT_VMESS_WS_PORT", "9444") or 9444)
DIRECT_SB_TROJAN = os.environ.get("DIRECT_SB_TROJAN", "false").lower() in ("true", "1", "yes")
DIRECT_SB_TROJAN_PORT = int(os.environ.get("DIRECT_SB_TROJAN_PORT", "9445") or 9445)
DIRECT_SS = os.environ.get("DIRECT_SS", "false").lower() in ("true", "1", "yes")
DIRECT_SS_PORT = int(os.environ.get("DIRECT_SS_PORT", "8388") or 8388)
DIRECT_HY2 = os.environ.get("DIRECT_HY2", "false").lower() in ("true", "1", "yes")
DIRECT_HY2_PORT = int(os.environ.get("DIRECT_HY2_PORT", "9446") or 9446)
CERT_MODE = os.environ.get("CERT_MODE", "selfsigned").strip().lower() or "selfsigned"
REALITY_DEST = os.environ.get("REALITY_DEST", "").strip()
REALITY_SNI = os.environ.get("REALITY_SNI", "").strip()
REALITY_EFF_SNI = os.environ.get("REALITY_EFF_SNI", "").strip() or REALITY_SNI or REALITY_DEST
REALITY_PUBLIC_KEY = os.environ.get("REALITY_PUBLIC_KEY", "").strip()
REALITY_SHORT_ID = os.environ.get("REALITY_SHORT_ID", "").strip()
# Reality 链接参数 (Part A/B 合同): 指纹 / flow / 加密方式 /
# spiderX, 与 install.sh 同一套默认值。REALITY_ENC_CLIENT 是
# install.sh 已拼好的完整客户端加密串 ("none" = 不开加密层)。
REALITY_FINGERPRINT = os.environ.get("REALITY_FINGERPRINT", "").strip().lower() or "chrome"
REALITY_FLOW = os.environ.get("REALITY_FLOW", "").strip() or "xtls-rprx-vision"
REALITY_ENCRYPTION = os.environ.get("REALITY_ENCRYPTION", "").strip() or "mlkem768x25519plus.native"
REALITY_SPIDERX = os.environ.get("REALITY_SPIDERX", "").strip() or "/"
REALITY_ENC_CLIENT = os.environ.get("REALITY_ENC_CLIENT", "").strip() or "none"

HELP = (
    "Nexus-Argo 动态订阅\n\n"
    f"GET /{SUB_PATH}/raw/<uuid>\n"
    "    原生订阅 (隧道 + 直连)\n\n"
    f"GET /{SUB_PATH}/merged/<uuid>?addrs=a,b,c\n"
    "    动态订阅: 隧道节点按 addrs 现场裂变, 直连节点原样跟上\n"
    "    不带 addrs: 用部署时的优选地址\n\n"
    f"GET /{SUB_PATH}/merged/<uuid>?addrs=a,b,c&format=singbox\n"
    "    Sing-box 订阅: 只去掉 xhttp 节点与带 VLESS 加密层的节点\n"
    "    (base64, Sing-box 没有 xhttp 传输、也不支持 VLESS Encryption;\n"
    "    trojan/vmess/ss/hysteria2/vless 含 quic/reality 都保留, 开了\n"
    "    加密层的 Reality 节点除外)\n\n"
    f"GET /{SUB_PATH}/merged/<uuid>?addrs=a,b,c&format=clash\n"
    "    Clash 订阅: Mihomo YAML (xhttp 需 Mihomo 1.19+, QUIC 与带\n"
    "    VLESS 加密层的节点除外; 支持 trojan/vmess/ss/\n"
    "    hysteria2/vless-reality)\n\n"
    "直连协议: Xray VLESS 明文 TCP / TLS / Reality / 明文 WS / gRPC+TLS /\n"
    "QUIC、Trojan+TLS、VMess+WS+TLS; Sing-box VLESS+TLS、Trojan+TLS、\n"
    "Shadowsocks (aes-256-gcm)、Hysteria2。直连节点原样进订阅, 不裂变。\n\n"
    f"GET /{SUB_PATH}/merged/<uuid>?addrs=a,b,c&format=auto\n"
    "    通用订阅: 按客户端 User-Agent 自适应 (Clash 系返回 YAML,\n"
    "    Sing-box 系返回去掉 xhttp 后的 base64, 其余返回 base64)\n\n"
    f"GET /{SUB_PATH}/merged?url=<外部订阅地址>&addrs=a,b,c\n"
    "    外部订阅裂变\n\n"
    "不走 Worker, 裂变在 VPS 本地完成。\n"
)

# 和 install.sh 里的 vless_link() 同一套规则, 改一处要改另一处:
# Xray xhttp_h1 http/1.1 / h2 / h3, h3 的 mode 是 packet-up 其余 auto;
# Sing-box ws/httpupgrade 走 http/1.1 (h2 会破坏 Upgrade 握手,
# VPS 实测 2026-11-18), grpc 走 h2, serviceName=grpc。
def parse_configs(s):
    out = []
    for item in (s or "").split(","):
        item = item.strip()
        if not item:
            continue
        proto, _, port = item.partition(":")
        out.append((proto.strip(), (port.strip() or "443")))
    return out


def vless_link(addr, domain, proto, port, core):
    if core == "xray":
        alpn = {"xhttp_h1": "http/1.1", "xhttp_h2": "h2", "xhttp_h3": "h3"}.get(proto, "h2")
        mode = "packet-up" if proto == "xhttp_h3" else "auto"
        return (f"vless://{UUID}@{addr}:{port}/?encryption=none&security=tls"
                f"&sni={domain}&type=xhttp&mode={mode}&path=%2F&alpn={alpn}"
                f"#{PREFIX_ARGO}丨Xray丨{proto}")
    if proto == "grpc":
        svc = "&serviceName=grpc"
    elif proto == "httpupgrade":
        svc = "&path=%2Fhttpupgrade"
    else:
        svc = "&path=%2Fws"
    alpn = "h2" if proto == "grpc" else "http/1.1"
    return (f"vless://{UUID}@{addr}:{port}/?encryption=none&security=tls"
            f"&sni={domain}&type={proto}{svc}&alpn={alpn}"
            f"#{PREFIX_ARGO}丨Singbox丨{proto}")


def tunnel_links():
    links = []
    if ENABLE_XRAY:
        for proto, port in parse_configs(XRAY_CONFIGS):
            if XRAY_DOMAIN:
                links.append(vless_link(XRAY_DOMAIN, XRAY_DOMAIN, proto, port, "xray"))
    if ENABLE_SINGBOX:
        for proto, port in parse_configs(SINGBOX_CONFIGS):
            if SINGBOX_DOMAIN:
                links.append(vless_link(SINGBOX_DOMAIN, SINGBOX_DOMAIN, proto, port, "sb"))
    return links


def _ai():
    # TLS 直连链接的 allowInsecure 后缀 (acme 有效证书时不带)。
    return "" if CERT_MODE == "acme" else "&allowInsecure=1"


def _vmess_link():
    cfg = {"v": "2", "ps": f"{PREFIX_DIRECT}丨Xray-VMess-WS", "add": DIRECT_DOMAIN,
           "port": str(DIRECT_VMESS_WS_PORT), "id": UUID, "aid": "0", "net": "ws",
           "type": "none", "host": DIRECT_DOMAIN, "path": "/ws", "tls": "tls",
           "sni": DIRECT_DOMAIN}
    return "vmess://" + base64.b64encode(json.dumps(cfg, ensure_ascii=False).encode()).decode()


def _ss_link():
    raw = f"aes-256-gcm:{UUID}@{DIRECT_DOMAIN}:{DIRECT_SS_PORT}".encode()
    return ("ss://" + base64.urlsafe_b64encode(raw).decode().rstrip("=")
            + f"#{PREFIX_DIRECT}丨Singbox-SS")


def direct_links():
    # 直连节点原样进订阅, 不参与优选地址裂变 (地址就是 VPS 自己)。
    # 与 install.sh 里的 direct 列表同一套规则, 改一处要改另一处。
    # TLS 直连链接带 allowInsecure=1 (acme 有效证书时除外); 明文
    # TCP / 明文 WS 不带; 直连是备用, 不是主路径。
    links = []
    if not ENABLE_DIRECT or not DIRECT_DOMAIN:
        return links
    dd = DIRECT_DOMAIN
    if DIRECT_XRAY_TLS:
        links.append(f"vless://{UUID}@{dd}:{DIRECT_XRAY_TLS_PORT}/?encryption=none&security=tls"
                     f"&sni={dd}&type=tcp{_ai()}"
                     f"#{PREFIX_DIRECT}丨Xray-TLS")
    if DIRECT_VLESS_TCP:
        links.append(f"vless://{UUID}@{dd}:{DIRECT_VLESS_TCP_PORT}/?encryption=none&type=tcp"
                     f"#{PREFIX_DIRECT}丨Xray-TCP")
    if DIRECT_VLESS_REALITY:
        # Reality 链接 (与 install.sh 里同一套规则, 改一处要改另一处):
        # 地址 = 直连域名 (或 VPS IP), SNI = 大厂域名 (reality_dest,
        # 或 reality_sni); 带指纹 / flow / 加密层 / spiderX (spiderX
        # 只在不是默认值 "/" 时才带)。
        _rlink = (f"vless://{UUID}@{dd}:{DIRECT_VLESS_REALITY_PORT}/"
                  f"?encryption={REALITY_ENC_CLIENT}&security=reality"
                  f"&pbk={REALITY_PUBLIC_KEY}&sid={REALITY_SHORT_ID}"
                  f"&sni={REALITY_EFF_SNI}&fp={REALITY_FINGERPRINT}")
        if REALITY_FLOW and REALITY_FLOW.lower() != "none":
            _rlink += f"&flow={REALITY_FLOW}"
        _rlink += "&type=tcp"
        if REALITY_SPIDERX != "/":
            _rlink += "&spiderX=" + urllib.parse.quote(REALITY_SPIDERX, safe="/")
        _rlink += f"#{PREFIX_DIRECT}丨Xray-Reality"
        links.append(_rlink)
    if DIRECT_VLESS_WS:
        links.append(f"vless://{UUID}@{dd}:{DIRECT_VLESS_WS_PORT}/?encryption=none&type=ws&path=%2Fws"
                     f"#{PREFIX_DIRECT}丨Xray-WS")
    if DIRECT_VLESS_GRPC:
        links.append(f"vless://{UUID}@{dd}:{DIRECT_VLESS_GRPC_PORT}/?encryption=none&security=tls"
                     f"&sni={dd}&type=grpc&serviceName=grpc{_ai()}"
                     f"#{PREFIX_DIRECT}丨Xray-gRPC")
    if DIRECT_XRAY_QUIC:
        links.append(f"vless://{UUID}@{dd}:{DIRECT_XRAY_QUIC_PORT}/?encryption=none&security=tls"
                     f"&sni={dd}&type=quic&quicSecurity=none&headerType=none{_ai()}"
                     f"#{PREFIX_DIRECT}丨Xray-QUIC")
    if DIRECT_TROJAN:
        links.append(f"trojan://{UUID}@{dd}:{DIRECT_TROJAN_PORT}?security=tls&sni={dd}{_ai()}"
                     f"#{PREFIX_DIRECT}丨Xray-Trojan")
    if DIRECT_VMESS_WS:
        links.append(_vmess_link())
    if DIRECT_SB_TLS:
        links.append(f"vless://{UUID}@{dd}:{DIRECT_SB_TLS_PORT}/?encryption=none&security=tls"
                     f"&sni={dd}&type=tcp{_ai()}"
                     f"#{PREFIX_DIRECT}丨Singbox-TLS")
    if DIRECT_SB_TROJAN:
        links.append(f"trojan://{UUID}@{dd}:{DIRECT_SB_TROJAN_PORT}?security=tls&sni={dd}{_ai()}"
                     f"#{PREFIX_DIRECT}丨Singbox-Trojan")
    if DIRECT_SS:
        links.append(_ss_link())
    if DIRECT_HY2:
        links.append(f"hysteria2://{UUID}@{dd}:{DIRECT_HY2_PORT}?insecure=1&sni={dd}"
                     f"#{PREFIX_DIRECT}丨Singbox-Hysteria2")
    return links


def native_links():
    return tunnel_links() + direct_links()


def decode_subscription(text):
    trimmed = (text or "").strip()
    if not trimmed:
        return []
    if "://" in trimmed:
        return [l.strip() for l in trimmed.splitlines() if l.strip()]
    try:
        cleaned = re.sub(r"\s+", "", trimmed)
        std = cleaned.replace("-", "+").replace("_", "/")
        padded = std + "=" * ((4 - len(std) % 4) % 4)
        decoded = base64.b64decode(padded).decode("utf-8")
        lines = [l.strip() for l in decoded.splitlines() if l.strip()]
        if lines and any("://" in l for l in lines):
            return lines
    except Exception:
        pass
    return [l.strip() for l in trimmed.splitlines() if l.strip()]


def encode_subscription(lines):
    text = "\n".join(lines) + ("\n" if lines else "")
    return base64.b64encode(text.encode()).decode()


# vless 链接解析 (供格式转换用): 只认 vless://, 取 uuid/host/port/
# type/sni/path/mode/serviceName/alpn/encryption 和备注。备注是
# "前缀丨内核丨协议" (隧道, 如 Argo丨Xray丨xhttp_h1) 或
# "前缀丨协议名" (直连, 如 直连丨Xray-TLS)。
def _vless_parts(link):
    try:
        u = urllib.parse.urlparse(link)
        if u.scheme != "vless" or not u.hostname or not u.port:
            return None
        q = urllib.parse.parse_qs(u.query)
        get = lambda k, d="": (q.get(k, [d]) or [d])[0]
        return {
            "scheme": "vless",
            "uuid": u.username or "",
            "password": u.username or "",
            "addr": u.hostname,
            "port": u.port,
            "type": get("type", ""),
            "security": get("security", ""),
            "sni": get("sni", ""),
            "path": get("path", "/"),
            "mode": get("mode", "auto"),
            "svc": get("serviceName", ""),
            "alpn": get("alpn", ""),
            "allow_insecure": get("allowInsecure", "") == "1",
            "pbk": get("pbk", ""),
            "sid": get("sid", ""),
            "fp": get("fp", ""),
            "flow": get("flow", ""),
            "encryption": get("encryption", ""),
            "spiderx": get("spiderX", ""),
            "cipher": "",
            "remark": urllib.parse.unquote(u.fragment or ""),
        }
    except Exception:
        return None


def _vless_type(link):
    p = _vless_parts(link)
    return p["type"] if p else ""


def _vless_encryption(link):
    # vless 链接的 VLESS 加密层参数 ("" 或 "none" = 没开加密层)。
    p = _vless_parts(link)
    return (p or {}).get("encryption", "") or ""


def _has_vless_encryption(link):
    # Sing-box / Clash (Mihomo) 不支持 VLESS Encryption, 带加密层
    # 的节点在这些格式里要去掉 (和 xhttp 之于 Sing-box 一个道理)。
    enc = _vless_encryption(link)
    return enc not in ("", "none")


def _b64decode(s):
    std = (s or "").replace("-", "+").replace("_", "/")
    return base64.b64decode(std + "=" * ((4 - len(std) % 4) % 4))


def _parse_link(link):
    # 通用链接解析 (供格式转换用): 返回统一的 dict, 未知/非法返回 None。
    #   scheme, uuid/password, addr, port, type, security, sni, path,
    #   svc, alpn, remark, allow_insecure, pbk/sid (reality), cipher。
    # 支持 vless://、trojan://、vmess:// (base64 JSON)、ss://
    # (base64 method:password@host:port, 或 SIP002 形式)、hysteria2://。
    if not link or "://" not in link:
        return None
    scheme = link.split("://", 1)[0].lower()
    if scheme == "vless":
        return _vless_parts(link)
    try:
        if scheme == "trojan":
            u = urllib.parse.urlparse(link)
            if not u.username or not u.hostname or not u.port:
                return None
            q = urllib.parse.parse_qs(u.query)
            get = lambda k, d="": (q.get(k, [d]) or [d])[0]
            return {"scheme": "trojan", "uuid": u.username, "password": u.username,
                    "addr": u.hostname, "port": u.port, "type": "tcp",
                    "security": get("security", "tls"), "sni": get("sni", ""),
                    "path": "/", "svc": "", "alpn": get("alpn", ""),
                    "allow_insecure": get("allowInsecure", "") == "1",
                    "pbk": "", "sid": "", "fp": "", "cipher": "",
                    "remark": urllib.parse.unquote(u.fragment or "")}
        if scheme == "vmess":
            body = link[len("vmess://"):].split("#", 1)[0]
            frag = urllib.parse.unquote(link.split("#", 1)[1]) if "#" in link else ""
            cfg = json.loads(_b64decode(body).decode("utf-8"))
            return {"scheme": "vmess", "uuid": cfg.get("id", ""), "password": cfg.get("id", ""),
                    "addr": cfg.get("add", ""), "port": int(cfg.get("port", 0) or 0),
                    "type": cfg.get("net", "ws"), "security": "tls" if cfg.get("tls") == "tls" else "none",
                    "sni": cfg.get("sni", "") or cfg.get("host", ""),
                    "path": cfg.get("path", "/ws"), "svc": "", "alpn": "",
                    "allow_insecure": False, "pbk": "", "sid": "", "fp": "",
                    "cipher": "auto", "remark": frag or cfg.get("ps", "")}
        if scheme == "ss":
            body = link[len("ss://"):].split("#", 1)[0]
            frag = urllib.parse.unquote(link.split("#", 1)[1]) if "#" in link else ""
            if "@" in body and not body.startswith(("eyJ",)):
                # SIP002 形式: ss://base64(method:password)@host:port
                userinfo, _, hostport = body.rpartition("@")
                try:
                    method, _, password = _b64decode(userinfo).decode("utf-8").partition(":")
                except Exception:
                    return None
                host, _, port = hostport.partition(":")
                return {"scheme": "ss", "uuid": password, "password": password,
                        "addr": host, "port": int(port or 0), "type": "ss",
                        "security": "none", "sni": "", "path": "/", "svc": "",
                        "alpn": "", "allow_insecure": False, "pbk": "", "sid": "",
                        "fp": "", "cipher": method,
                        "remark": frag}
            decoded = _b64decode(body).decode("utf-8")
            method, _, rest = decoded.partition(":")
            password, _, hostport = rest.partition("@")
            host, _, port = hostport.partition(":")
            if not method or not password or not host or not port:
                return None
            return {"scheme": "ss", "uuid": password, "password": password,
                    "addr": host, "port": int(port), "type": "ss",
                    "security": "none", "sni": "", "path": "/", "svc": "",
                    "alpn": "", "allow_insecure": False, "pbk": "", "sid": "",
                    "fp": "", "cipher": method, "remark": frag}
        if scheme == "hysteria2":
            u = urllib.parse.urlparse(link)
            if not u.username or not u.hostname or not u.port:
                return None
            q = urllib.parse.parse_qs(u.query)
            get = lambda k, d="": (q.get(k, [d]) or [d])[0]
            return {"scheme": "hysteria2", "uuid": u.username, "password": u.username,
                    "addr": u.hostname, "port": u.port, "type": "hysteria2",
                    "security": "tls", "sni": get("sni", ""),
                    "path": "/", "svc": "", "alpn": get("alpn", ""),
                    "allow_insecure": get("insecure", "") in ("1", "true"),
                    "pbk": "", "sid": "", "fp": "", "cipher": "",
                    "remark": urllib.parse.unquote(u.fragment or "")}
    except Exception:
        return None
    return None


def _yq(s):
    return '"' + str(s).replace("\\", "\\\\").replace('"', '\\"') + '"'


def clash_yaml(links):
    # 链接 -> Mihomo (Clash Meta) YAML。只用标准库手写, 不引新依赖。
    # 传输映射 (Mihomo 1.19+ 才支持 xhttp; 原版 Clash 不支持 VLESS):
    #   vless xhttp       -> network: xhttp + xhttp-opts {path, mode}
    #   vless ws          -> network: ws + ws-opts {path, headers Host}
    #   vless httpupgrade -> network: ws + ws-opts + v2ray-http-upgrade: true
    #   vless grpc        -> network: grpc + grpc-opts {grpc-service-name}
    #   vless tcp (TLS)   -> 明文 TCP, tls 由 security 决定; 明文 TCP 不带 tls
    #   vless reality     -> network: tcp + reality-opts {public-key, short-id}
    #   trojan            -> type: trojan, password, sni, alpn
    #   vmess             -> type: vmess, uuid, alterId 0, cipher auto, ws, tls
    #   ss                -> type: ss, cipher, password
    #   hysteria2         -> type: hysteria2, password, sni, alpn [h3]
    # quic 跳过: Mihomo 不支持 QUIC。
    proxies = []
    for link in links:
        p = _parse_link(link)
        if not p or not p["uuid"] or not p["addr"] or not p["port"]:
            continue
        if p["scheme"] == "vless" and p["type"] not in ("xhttp", "ws", "httpupgrade", "grpc", "tcp"):
            continue
        if p["scheme"] == "vless" and (p.get("encryption", "") or "none") not in ("", "none"):
            # Mihomo / Clash 不支持 VLESS Encryption: 带加密层的节点
            # 在 Clash 订阅里会静默失败, 不如不给 (Sing-box 格式同理)。
            continue
        if p["scheme"] not in ("vless", "trojan", "vmess", "ss", "hysteria2"):
            continue
        proxies.append(p)
    if not proxies:
        return ""
    out = ["# Nexus-Argo Clash 订阅 (Mihomo / Clash Meta 1.19+; 原版 Clash 不支持 VLESS)",
           "proxies:"]
    names = []
    for p in proxies:
        # 节点名 = 完整备注 (含前缀) + 地址: 隧道 Argo-Xray-xhttp_h2,
        # 直连 直连-Xray-TLS。备注有 3 段 (隧道) 或 2 段 (直连)。
        parts = [s for s in (p["remark"] or "").split("丨") if s]
        name = f"{'-'.join(parts)} @ {p['addr']}" if parts else f"{p['scheme']} @ {p['addr']}"
        names.append(name)
        out.append(f"  - name: {_yq(name)}")
        if p["scheme"] == "trojan":
            out.append("    type: trojan")
            out.append(f"    server: {_yq(p['addr'])}")
            out.append(f"    port: {p['port']}")
            out.append(f"    password: {_yq(p['password'])}")
            if p["sni"]:
                out.append(f"    sni: {_yq(p['sni'])}")
            if p["alpn"]:
                out.append("    alpn:")
                out.append(f"      - {_yq(p['alpn'])}")
            if p["allow_insecure"]:
                out.append("    skip-cert-verify: true")
            out.append("    udp: true")
            continue
        if p["scheme"] == "vmess":
            out.append("    type: vmess")
            out.append(f"    server: {_yq(p['addr'])}")
            out.append(f"    port: {p['port']}")
            out.append(f"    uuid: {_yq(p['uuid'])}")
            out.append("    alterId: 0")
            out.append("    cipher: auto")
            out.append(f"    tls: {'true' if p['security'] == 'tls' else 'false'}")
            if p["sni"]:
                out.append(f"    servername: {_yq(p['sni'])}")
            if p["type"] == "ws":
                out.append("    network: ws")
                out.append("    ws-opts:")
                out.append(f"      path: {_yq(p['path'])}")
                out.append("      headers:")
                out.append(f"        Host: {_yq(p['sni'] or p['addr'])}")
            out.append("    udp: true")
            continue
        if p["scheme"] == "ss":
            out.append("    type: ss")
            out.append(f"    server: {_yq(p['addr'])}")
            out.append(f"    port: {p['port']}")
            out.append(f"    cipher: {_yq(p['cipher'] or 'aes-256-gcm')}")
            out.append(f"    password: {_yq(p['password'])}")
            out.append("    udp: true")
            continue
        if p["scheme"] == "hysteria2":
            out.append("    type: hysteria2")
            out.append(f"    server: {_yq(p['addr'])}")
            out.append(f"    port: {p['port']}")
            out.append(f"    password: {_yq(p['password'])}")
            if p["sni"]:
                out.append(f"    sni: {_yq(p['sni'])}")
            out.append("    alpn:")
            for a in (p["alpn"].split(",") if p["alpn"] else ["h3"]):
                out.append(f"      - {_yq(a.strip())}")
            if p["allow_insecure"]:
                out.append("    skip-cert-verify: true")
            continue
        # vless
        out.append("    type: vless")
        out.append(f"    server: {_yq(p['addr'])}")
        out.append(f"    port: {p['port']}")
        out.append(f"    uuid: {_yq(p['uuid'])}")
        tls = p["security"] in ("tls", "reality")
        out.append(f"    tls: {'true' if tls else 'false'}")
        if p["sni"]:
            out.append(f"    servername: {_yq(p['sni'])}")
        if p["alpn"]:
            out.append("    alpn:")
            out.append(f"      - {_yq(p['alpn'])}")
        if p["allow_insecure"]:
            out.append("    skip-cert-verify: true")
        t = p["type"]
        if t == "xhttp":
            out.append("    network: xhttp")
            out.append("    xhttp-opts:")
            out.append(f"      path: {_yq(p['path'])}")
            out.append(f"      mode: {_yq(p['mode'])}")
        elif t == "ws":
            out.append("    network: ws")
            out.append("    ws-opts:")
            out.append(f"      path: {_yq(p['path'])}")
            out.append("      headers:")
            out.append(f"        Host: {_yq(p['sni'] or p['addr'])}")
        elif t == "httpupgrade":
            out.append("    network: ws")
            out.append("    ws-opts:")
            out.append(f"      path: {_yq(p['path'])}")
            out.append("      headers:")
            out.append(f"        Host: {_yq(p['sni'] or p['addr'])}")
            out.append("    v2ray-http-upgrade: true")
        elif t == "grpc":
            out.append("    network: grpc")
            out.append("    grpc-opts:")
            out.append(f"      grpc-service-name: {_yq(p['svc'] or 'grpc')}")
        elif t == "tcp":
            if p["security"] == "reality":
                out.append("    network: tcp")
                out.append("    reality-opts:")
                out.append(f"      public-key: {_yq(p['pbk'])}")
                out.append(f"      short-id: {_yq(p['sid'])}")
            # 直连 TLS / 明文 TCP: Mihomo 默认就是 TCP, 不用写 network
        out.append("    udp: true")
    out.append("proxy-groups:")
    _gname = PREFIX_ARGO if PREFIX_ARGO.lower().endswith("argo") else PREFIX_ARGO + "-Argo"
    out.append(f"  - name: {_yq(_gname)}")
    out.append("    type: select")
    out.append("    proxies:")
    for n in names:
        out.append(f"      - {_yq(n)}")
    return "\n".join(out) + "\n"


# vless://uuid@host:port/...#remark, 只换 host, 其余原样保留 (SNI 不变)
_FISSION_RE = re.compile(r"^([a-zA-Z0-9+.-]+://[^@]+@)([^:/?#]+)(:\d+.*)$")


def fission_link(link, addr):
    m = _FISSION_RE.match(link)
    if not m:
        return None
    return m.group(1) + addr + m.group(3)


def fission(lines, addrs):
    out = []
    for link in lines:
        for addr in addrs:
            f = fission_link(link, addr)
            if f:
                out.append(f)
    return out


_ADDR_RE = re.compile(r"^[A-Za-z0-9._-]+(:\d+)?$")


def parse_addrs(query):
    raw = (query.get("addrs", [""])[0] or "")
    addrs = [a.strip() for a in raw.split(",") if a.strip()]
    if not addrs:
        return list(OPT_ADDRS)
    # 最多 20 个, 只接受主机名/IP 字符, 拒绝路径/查询注入
    addrs = [a for a in addrs if _ADDR_RE.match(a)][:20]
    return addrs or list(OPT_ADDRS)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def _send(self, body, status=200, ctype="text/plain; charset=utf-8"):
        data = body.encode()
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(data)

    def _sub(self, body):
        self._send(encode_subscription(body))

    def _generator(self):
        try:
            with open(_GENERATOR_PATH, "rb") as f:
                data = f.read()
        except OSError:
            self._send(HELP)
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(data)

    def _config(self):
        self._send(json.dumps({
            "uuid": UUID, "prefix": PREFIX, "prefix_argo": PREFIX_ARGO,
            "prefix_direct": PREFIX_DIRECT, "opt_addrs": OPT_ADDRS,
            "xray_domain": XRAY_DOMAIN, "singbox_domain": SINGBOX_DOMAIN,
            "sub_path": SUB_PATH,
        }, ensure_ascii=False), ctype="application/json; charset=utf-8")

    def do_GET(self):
        url = urllib.parse.urlparse(self.path)
        path = url.path
        query = urllib.parse.parse_qs(url.query)

        if path in ("/", ""):
            self._generator()
            return

        if path == "/help.txt":
            self._send(HELP)
            return

        if path == "/config.json":
            self._config()
            return

        if not UUID:
            self._send("未配置 NODE_UUID", 500)
            return

        if path == f"/{SUB_PATH}/raw.txt":
            self._send("\n".join(native_links()) + "\n")
            return
        if path == f"/{SUB_PATH}/merged.txt":
            self._send("\n".join(fission(tunnel_links(), list(OPT_ADDRS)) + direct_links()) + "\n")
            return

        raw_m = re.match(r"^/" + re.escape(SUB_PATH) + r"/raw/(.+)$", path)
        if raw_m:
            if raw_m.group(1) != UUID:
                self._send("uuid 不匹配", 404)
                return
            links = native_links()
            if not links:
                self._send("未配置任何核心", 500)
                return
            self._sub(links)
            return

        merged_m = re.match(r"^/" + re.escape(SUB_PATH) + r"/merged(?:/(.+))?$", path)
        if merged_m:
            addrs = parse_addrs(query)
            external = (query.get("url", [""])[0] or "").strip()
            if external:
                if not external.startswith(("http://", "https://")):
                    self._send("url 必须是 http(s) 地址", 400)
                    return
                try:
                    req = urllib.request.Request(external, headers={"User-Agent": "nexus-sub"})
                    with urllib.request.urlopen(req, timeout=10) as r:
                        lines = decode_subscription(r.read().decode("utf-8", "replace"))
                except Exception as e:
                    self._send(f"拉取外部订阅失败: {e}", 502)
                    return
                merged = fission(lines, addrs)
            else:
                if not merged_m.group(1) or merged_m.group(1) != UUID:
                    self._send(f"uuid 不匹配: /{SUB_PATH}/merged/<uuid>?addrs=a,b,c", 404)
                    return
                # 只有隧道节点裂变, 直连节点原样跟上
                merged = fission(tunnel_links(), addrs) + direct_links()
            if not merged:
                self._send("订阅为空", 502)
                return
            fmt = (query.get("format", [""])[0] or "").strip().lower()
            if fmt == "auto":
                # 通用自适应: 按拉取的客户端 User-Agent 返回对应格式
                ua = (self.headers.get("User-Agent") or "").lower()
                if "clash" in ua or "mihomo" in ua:
                    yaml = clash_yaml(merged)
                    if not yaml:
                        self._send("订阅为空: 没有可转换的节点", 502)
                        return
                    self._send(yaml, ctype="text/yaml; charset=utf-8")
                elif any(k in ua for k in ("sing-box", "singbox", "nekobox", "hiddify", "sfi")):
                    merged = [l for l in merged
                              if _vless_type(l) != "xhttp" and not _has_vless_encryption(l)]
                    if not merged:
                        self._send("订阅为空: 没有 Sing-box 支持的节点", 502)
                        return
                    self._sub(merged)
                else:
                    self._sub(merged)
            elif fmt == "singbox":
                # Sing-box 没有 xhttp 传输, 也不支持 VLESS Encryption:
                # 只去掉 xhttp 与带加密层的节点, 其余都留着
                merged = [l for l in merged
                          if _vless_type(l) != "xhttp" and not _has_vless_encryption(l)]
                if not merged:
                    self._send("订阅为空: 没有 Sing-box 支持的节点", 502)
                    return
                self._sub(merged)
            elif fmt == "clash":
                yaml = clash_yaml(merged)
                if not yaml:
                    self._send("订阅为空: 没有可转换的节点", 502)
                    return
                self._send(yaml, ctype="text/yaml; charset=utf-8")
            elif fmt:
                self._send("format 只支持 singbox / clash / auto", 400)
            else:
                self._sub(merged)
            return

        self._send("404: 未知路径, 访问 / 看说明", 404)


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", SUB_PORT), Handler).serve_forever()
