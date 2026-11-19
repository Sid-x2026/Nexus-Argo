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
#     NODE_UUID, NODE_PREFIX,
#     XRAY_DOMAIN, XRAY_CONFIGS, SINGBOX_DOMAIN, SINGBOX_CONFIGS,
#     ENABLE_XRAY, ENABLE_SINGBOX,
#     OPT_ADDRS (默认优选地址, 不带 ?addrs= 时用它),
#     SUB_PORT (监听端口, 默认 18080),
#     SUB_PATH (订阅路径前缀, 默认 sub, 面板自定义订阅地址),
#     ENABLE_DIRECT, DIRECT_DOMAIN, DIRECT_XRAY_TLS,
#     DIRECT_XRAY_TLS_PORT, DIRECT_XRAY_QUIC, DIRECT_XRAY_QUIC_PORT,
#     DIRECT_SB_TLS, DIRECT_SB_TLS_PORT (直连节点, 原样进订阅, 不裂变)
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
#       &format=singbox: 只去掉 xhttp 节点 (Sing-box 没有 xhttp
#       传输), 仍是 base64; &format=clash: Mihomo YAML (QUIC 除外,
#       Mihomo 不支持 QUIC)
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
PREFIX = os.environ.get("NODE_PREFIX", "Nexus").strip() or "Nexus"
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
DIRECT_XRAY_TLS = os.environ.get("DIRECT_XRAY_TLS", "true").lower() in ("true", "1", "yes")
DIRECT_XRAY_TLS_PORT = int(os.environ.get("DIRECT_XRAY_TLS_PORT", "443") or 443)
DIRECT_XRAY_QUIC = os.environ.get("DIRECT_XRAY_QUIC", "false").lower() in ("true", "1", "yes")
DIRECT_XRAY_QUIC_PORT = int(os.environ.get("DIRECT_XRAY_QUIC_PORT", "443") or 443)
DIRECT_SB_TLS = os.environ.get("DIRECT_SB_TLS", "true").lower() in ("true", "1", "yes")
DIRECT_SB_TLS_PORT = int(os.environ.get("DIRECT_SB_TLS_PORT", "8443") or 8443)

HELP = (
    "Nexus-Argo 动态订阅\n\n"
    f"GET /{SUB_PATH}/raw/<uuid>\n"
    "    原生订阅 (隧道 + 直连)\n\n"
    f"GET /{SUB_PATH}/merged/<uuid>?addrs=a,b,c\n"
    "    动态订阅: 隧道节点按 addrs 现场裂变, 直连节点原样跟上\n"
    "    不带 addrs: 用部署时的优选地址\n\n"
    f"GET /{SUB_PATH}/merged/<uuid>?addrs=a,b,c&format=singbox\n"
    "    Sing-box 订阅: 去掉 xhttp 节点 (base64, Sing-box 没有 xhttp 传输)\n\n"
    f"GET /{SUB_PATH}/merged/<uuid>?addrs=a,b,c&format=clash\n"
    "    Clash 订阅: Mihomo YAML (xhttp 需 Mihomo 1.19+, QUIC 除外)\n\n"
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
                f"#{PREFIX}丨Xray丨{proto}")
    if proto == "grpc":
        svc = "&serviceName=grpc"
    elif proto == "httpupgrade":
        svc = "&path=%2Fhttpupgrade"
    else:
        svc = "&path=%2Fws"
    alpn = "h2" if proto == "grpc" else "http/1.1"
    return (f"vless://{UUID}@{addr}:{port}/?encryption=none&security=tls"
            f"&sni={domain}&type={proto}{svc}&alpn={alpn}"
            f"#{PREFIX}丨Singbox丨{proto}")


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


def direct_links():
    # 直连节点原样进订阅, 不参与优选地址裂变 (地址就是 VPS 自己)。
    # 自签名证书, 客户端需 allowInsecure=1, 仅作备用。
    links = []
    if not ENABLE_DIRECT or not DIRECT_DOMAIN:
        return links
    if DIRECT_XRAY_TLS:
        links.append(f"vless://{UUID}@{DIRECT_DOMAIN}:{DIRECT_XRAY_TLS_PORT}/?encryption=none&security=tls"
                     f"&sni={DIRECT_DOMAIN}&type=tcp&allowInsecure=1"
                     f"#{PREFIX}丨直连丨Xray-TLS")
    if DIRECT_XRAY_QUIC:
        links.append(f"vless://{UUID}@{DIRECT_DOMAIN}:{DIRECT_XRAY_QUIC_PORT}/?encryption=none&security=tls"
                     f"&sni={DIRECT_DOMAIN}&type=quic&quicSecurity=none&headerType=none&allowInsecure=1"
                     f"#{PREFIX}丨直连丨Xray-QUIC")
    if DIRECT_SB_TLS:
        links.append(f"vless://{UUID}@{DIRECT_DOMAIN}:{DIRECT_SB_TLS_PORT}/?encryption=none&security=tls"
                     f"&sni={DIRECT_DOMAIN}&type=tcp&allowInsecure=1"
                     f"#{PREFIX}丨直连丨Singbox-TLS")
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
# type/sni/path/mode/serviceName/alpn 和备注。备注里是
# "前缀丨内核丨协议" (如 Nexus丨Xray丨xhttp_h1)。
def _vless_parts(link):
    try:
        u = urllib.parse.urlparse(link)
        if u.scheme != "vless" or not u.hostname or not u.port:
            return None
        q = urllib.parse.parse_qs(u.query)
        get = lambda k, d="": (q.get(k, [d]) or [d])[0]
        return {
            "uuid": u.username or "",
            "addr": u.hostname,
            "port": u.port,
            "type": get("type", ""),
            "sni": get("sni", ""),
            "path": get("path", "/"),
            "mode": get("mode", "auto"),
            "svc": get("serviceName", ""),
            "alpn": get("alpn", ""),
            "remark": urllib.parse.unquote(u.fragment or ""),
        }
    except Exception:
        return None


def _vless_type(link):
    p = _vless_parts(link)
    return p["type"] if p else ""


def _yq(s):
    return '"' + str(s).replace("\\", "\\\\").replace('"', '\\"') + '"'


# vless 链接 -> Mihomo (Clash Meta) YAML。只用标准库手写, 不引新依赖。
# 传输映射 (Mihomo 1.19+ 才支持 xhttp; 原版 Clash 不支持 VLESS):
#   xhttp       -> network: xhttp + xhttp-opts {path, mode}
#   ws          -> network: ws + ws-opts {path, headers Host}
#   httpupgrade -> network: ws + ws-opts + v2ray-http-upgrade: true
#   grpc        -> network: grpc + grpc-opts {grpc-service-name}
# Sing-box 没有 xhttp 传输, 所以 ?format=singbox 只留 ws/httpupgrade/grpc。
def clash_yaml(links):
    proxies = []
    for link in links:
        p = _vless_parts(link)
        # quic 跳过: Mihomo 不支持 QUIC; tcp (直连 TLS) 是明文 TCP, 支持
        if not p or not p["uuid"] or p["type"] not in ("xhttp", "ws", "httpupgrade", "grpc", "tcp"):
            continue
        proxies.append(p)
    if not proxies:
        return ""
    out = ["# Nexus-Argo Clash 订阅 (Mihomo / Clash Meta 1.19+; 原版 Clash 不支持 VLESS)",
           "proxies:"]
    names = []
    for p in proxies:
        parts = (p["remark"] or "").split("丨")
        name = f"{parts[1]}-{parts[2]} @ {p['addr']}" if len(parts) >= 3 else f"vless @ {p['addr']}"
        names.append(name)
        out.append(f"  - name: {_yq(name)}")
        out.append("    type: vless")
        out.append(f"    server: {_yq(p['addr'])}")
        out.append(f"    port: {p['port']}")
        out.append(f"    uuid: {_yq(p['uuid'])}")
        out.append("    tls: true")
        if p["sni"]:
            out.append(f"    servername: {_yq(p['sni'])}")
        if p["alpn"]:
            out.append("    alpn:")
            out.append(f"      - {_yq(p['alpn'])}")
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
            pass  # 直连 TLS, 明文 TCP, Mihomo 默认就是 TCP, 不用写 network
        out.append("    udp: true")
    out.append("proxy-groups:")
    out.append(f"  - name: {_yq(PREFIX + '-Argo')}")
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
            "uuid": UUID, "prefix": PREFIX, "opt_addrs": OPT_ADDRS,
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
                        self._send("订阅为空: 没有可转换的 VLESS 节点", 502)
                        return
                    self._send(yaml, ctype="text/yaml; charset=utf-8")
                elif any(k in ua for k in ("sing-box", "singbox", "nekobox", "hiddify", "sfi")):
                    merged = [l for l in merged if _vless_type(l) != "xhttp"]
                    if not merged:
                        self._send("订阅为空: 没有 Sing-box 支持的节点", 502)
                        return
                    self._sub(merged)
                else:
                    self._sub(merged)
            elif fmt == "singbox":
                # Sing-box 没有 xhttp 传输: 只去掉 xhttp, 其余都留着
                merged = [l for l in merged if _vless_type(l) != "xhttp"]
                if not merged:
                    self._send("订阅为空: 没有 Sing-box 支持的节点", 502)
                    return
                self._sub(merged)
            elif fmt == "clash":
                yaml = clash_yaml(merged)
                if not yaml:
                    self._send("订阅为空: 没有可转换的 VLESS 节点", 502)
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
