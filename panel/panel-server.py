#!/usr/bin/env python3
# Nexus-Argo 管理面板 v2 — 精简版 (2026-11-19)
#
# 只做两件事: 核心部署 + 自定义订阅地址。裂变/优选生成器不在面板里,
# 手动用别的工具完成 (生成器方案保留在 web/sub-generator.html)。
#
# v2 新增: 直连协议扩展 (VLESS TCP/TLS/REALITY/WS/gRPC、Trojan、
# VMess WS、Sing-box Trojan、Shadowsocks、Hysteria2)、证书模式
# (selfsigned / upload / acme) 与证书状态查询、REALITY 目标延迟探测。
# 旧字段名 direct_xray_tls 仍可用, 会映射到 direct_vless_tls。
#
# 跑法 (panel-install.sh 起 nexus-panel 容器, python:3-alpine, 只有标准库):
#   python3 /app/panel-server.py
#   环境变量: PANEL_DIR (默认 /opt/nexus-argo/panel, 放 config.json /
#   auth.json / deploy.log / deploy-status.json), WORKDIR (默认
#   /opt/nexus-argo), PANEL_PORT (默认 2053)。
#   容器用 --network host 跑, 并挂 -v /var/run/docker.sock 和
#   -v /opt/nexus-argo:/opt/nexus-argo, 所以面板就是在 VPS 本地
#   执行部署: 调 install.sh, 调 docker, 和 SSH 里跑命令是一样的。
#
# 路由 (除 /api/login 外全部要登录, Cookie 会话):
#   GET  /                        面板页面 (panel.html)
#   POST /api/login               {user, password} -> 会话 Cookie
#   POST /api/logout              清会话
#   GET  /api/status              各服务运行状态 + 节点数 + 订阅地址
#   GET  /api/config              当前部署配置 (无则返回默认)
#   POST /api/config              校验并保存部署配置
#   POST /api/deploy              后台跑 install.sh (只允许一个在跑)
#   GET  /api/deploy/status       部署状态 (idle/running/ok/failed)
#   GET  /api/deploy/log?offset=N 部署日志增量 (按字节偏移)
#   GET  /api/subscription        订阅地址 + 自定义路径
#   POST /api/subscription        {sub_path} 改订阅路径, 重启 nexus-sub 生效
#   GET  /<uuid>/<sub_path>/raw|merged  面板订阅 (公开, 免登录):
#        面板收到后转发给本机 nexus-sub, 订阅不再需要单独的域名/端口/路由,
#        地址就是面板自己的域名或 IP:端口 + UUID + 订阅路径。
#   GET  /api/cert               证书状态 (是否已部署 / 颁发给谁 / 到期时间 / 续期方式)
#   POST /api/reality-scan       3X-UI 式 Reality 目标扫描 (域名/IP/CIDR, 返回
#                                 目标/状态/TLS/ALPN/密钥交换/证书/延迟表)
#   GET  /api/reality-keys       公钥 + 私钥 + Short IDs
#   POST /api/reality-keys       {action: regenerate|clear} 重新生成 / 清空密钥
#   GET  /api/outbounds          出站列表 (默认 direct + blocked)
#   POST /api/outbounds          整表保存出站 (本版只存面板, 不接进核心渲染)
#   POST /api/outbounds/test     全部测试 (测每条出站的延迟)
#   POST /api/clear               清除本次部署 (节点容器 + 配置文件, 面板本身保留)
#
# 安全边界: 面板用账号密码登录 (默认 admin, 密码安装时随机生成,
# 只存 salt+sha256); 部署是后台任务, 浏览器断开不影响; 部署时
# 保留面板自身的隧道路由, cloudflared 重启导致页面短暂断开时
# 前端自动重连。订阅地址本身就是凭证, 不要发给不该拿到节点的人。

import datetime
import hashlib
import ipaddress
import json
import os
import re
import secrets
import shutil
import subprocess
import threading
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PANEL_DIR = os.environ.get("PANEL_DIR", "/opt/nexus-argo/panel").rstrip("/")
WORKDIR = os.environ.get("WORKDIR", "/opt/nexus-argo").rstrip("/")
PANEL_PORT = int(os.environ.get("PANEL_PORT", "2053") or 2053)
INSTALL_SH = os.path.join(WORKDIR, "install.sh")
_HERE = os.path.dirname(os.path.abspath(__file__))
_PANEL_HTML = os.path.join(_HERE, "panel.html")
# 面板后缀 (默认 /Nexus): 安装时 PANEL_PATH 环境变量传入, 也可以在 panel-info.json 里改
import os as _os
PANEL_PATH = _os.environ.get("PANEL_PATH", "/Nexus").strip() or ""
if PANEL_PATH == "/":
    PANEL_PATH = ""

CONFIG_PATH = os.path.join(PANEL_DIR, "config.json")
AUTH_PATH = os.path.join(PANEL_DIR, "auth.json")
LOG_PATH = os.path.join(PANEL_DIR, "deploy.log")
STATUS_PATH = os.path.join(PANEL_DIR, "deploy-status.json")

UUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
SUB_PATH_RE = re.compile(r"^[A-Za-z0-9._-]{1,32}$")
RESERVED_SUB_PATH = {"api", "config.json", "help.txt"}
DOMAIN_RE = re.compile(r"^[A-Za-z0-9._-]+$")
IPV4_RE = re.compile(r"^\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}$")


def _is_ipv4(s):
    """严格的 IPv4 判断 (每段 0-255), 避免把 999.1.1.1 当成地址。"""
    if not s or not IPV4_RE.match(s):
        return False
    return all(0 <= int(p) <= 255 for p in s.split("."))


def _is_domain(s):
    """域名判断: 符合 DOMAIN_RE 且不是 IP 地址。"""
    return bool(s) and bool(DOMAIN_RE.match(s)) and not _is_ipv4(s)


def _split_host_port(s):
    """把 "host" / "host:443" 拆成 (host, port), 端口缺省 443。
    端口非法时返回 (host, 443), host 本身非法时返回 ("", 443)。"""
    s = str(s or "").strip()
    if not s:
        return "", 443
    if s.startswith("["):
        # [IPv6]:port, Reality 目标不用 IPv6, 直接当非法
        return "", 443
    if ":" in s:
        host, _, port_s = s.rpartition(":")
        try:
            port = int(port_s)
        except Exception:
            return host.strip(), 443
        if not 1 <= port <= 65535:
            return host.strip(), 443
        return host.strip(), port
    return s, 443

# 部署过的服务容器 (健康检查用, 和 install.sh 里起的名字一致)
SERVICES = ["nexus-argo", "nexus-panel-argo", "nexus-xray", "nexus-singbox", "nexus-sb-dispatch", "nexus-sub", "nexus-web-panel"]
NODE_CONTAINERS = ("nexus-xray", "nexus-singbox", "nexus-sb-dispatch", "nexus-argo", "nexus-sub", "nexus-web-panel")

DEFAULT_CONFIG = {
    "node_uuid": "",
    "node_prefix": "Nexus",
    # 节点前缀分开: 隧道与直连各一个 (Part A/B 合同字段)。
    # node_prefix 只作兼容: 旧配置里只有它时, 读出来当 argo_prefix。
    "argo_prefix": "Argo",
    "direct_prefix": "直连",
    "argo_token": "",
    "tunnel_protocol": "auto",
    "enable_xray": True,
    "xray_port": 8001,
    "xray_domain": "xr.ahstshu.eu.cc",
    "xray_protocols": {"xhttp_h1": 443, "xhttp_h2": 443, "xhttp_h3": 443},
    "enable_singbox": True,
    "singbox_port": 8002,
    "singbox_domain": "sb.ahstshu.eu.cc",
    "singbox_protocols": {"ws": 8443, "httpupgrade": 8443, "grpc": 8443},
    "opt_addrs": ["cloudflare.com"],
    "sub_host": "",
    "sub_domain": "sub.ahstshu.eu.cc",
    "sub_port": 18080,
    "sub_path": "sub",
    "enable_direct": False,
    "direct_domain": "",
    "direct_address": "",
    "direct_sni": "",
    "client_selector": "auto",
    "client_mode": "suitable",
    # 直连 VLESS 系列 (TCP/TLS/REALITY/WS/gRPC)
    "direct_vless_tcp": False,
    "direct_vless_tcp_port": 8082,
    "direct_vless_tls": True,
    "direct_vless_tls_port": 443,
    "direct_vless_reality": False,
    "direct_vless_reality_port": 9447,
    "reality_dest": "",
    "reality_sni": "",
    # Reality 额外参数 (Part A/B 合同字段): 指纹 / flow / 加密方式 /
    # spiderX。publicKey 与 shortId 不放配置里, 自动生成存 reality.json。
    "reality_fingerprint": "chrome",
    "reality_flow": "xtls-rprx-vision",
    "reality_encryption": "mlkem768x25519plus.native",
    "reality_spiderx": "/",
    # Reality 3X-UI 式字段: 目标带端口 (reality_dest, 如
    # www.cloudflare.com:443)、最大时间差、客户端版本范围、多个
    # Short ID (reality_short_ids, 第一项即 reality.json 的 short_id)。
    "reality_max_time_diff": 0,
    "reality_min_client_ver": "1.0.0",
    "reality_max_client_ver": "x.y.z",
    "reality_short_ids": [],
    "direct_vless_ws": False,
    "direct_vless_ws_port": 8083,
    "direct_vless_grpc": False,
    "direct_vless_grpc_port": 8084,
    # 直连 WS path 与 gRPC serviceName: 每次部署随机生成一对, 不放
    # 面板里给用户填。隧道不用 path (默认配好, 分发器靠固定 path 路由)。
    "direct_ws_path": "/ws",
    "direct_grpc_service": "grpc",
    # 直连其它协议
    "direct_trojan": False,
    "direct_trojan_port": 9443,
    "direct_vmess_ws": False,
    "direct_vmess_ws_port": 9444,
    "direct_sb_trojan": False,
    "direct_sb_trojan_port": 9445,
    "direct_ss": False,
    "direct_ss_port": 8388,
    "direct_hy2": False,
    "direct_hy2_port": 9446,
    # 证书: selfsigned 自签名 / upload 上传 / acme 自动签发
    "cert_mode": "acme",
    "cert_pem": "",
    "cert_key": "",
    # 旧字段名 (兼容): direct_xray_tls -> direct_vless_tls,
    # direct_sb_tls 保持原义。
    "direct_xray_tls": True,
    "direct_xray_tls_port": 443,
    "direct_sb_tls": True,
    "direct_sb_tls_port": 8443,
}

# 直连协议表: (开关字段, 端口字段, 正式名称, 是否走 TCP)
# Hysteria2 走 UDP, 端口只校验范围, 不参与 TCP 端口冲突检查。
DIRECT_PROTOCOLS = [
    ("direct_vless_tcp", "direct_vless_tcp_port", "直连 VLESS TCP", True),
    ("direct_vless_tls", "direct_vless_tls_port", "直连 VLESS TLS", True),
    ("direct_vless_reality", "direct_vless_reality_port", "直连 VLESS REALITY", True),
    ("direct_vless_ws", "direct_vless_ws_port", "直连 VLESS WebSocket", True),
    ("direct_vless_grpc", "direct_vless_grpc_port", "直连 VLESS gRPC", True),
    ("direct_trojan", "direct_trojan_port", "直连 Trojan", True),
    ("direct_vmess_ws", "direct_vmess_ws_port", "直连 VMess WebSocket", True),
    ("direct_sb_trojan", "direct_sb_trojan_port", "直连 Sing-box Trojan", True),
    ("direct_ss", "direct_ss_port", "直连 Shadowsocks", True),
    ("direct_hy2", "direct_hy2_port", "直连 Hysteria2", False),
    ("direct_sb_tls", "direct_sb_tls_port", "直连 Sing-box TLS", True),
]

# 旧字段名 -> 新字段名 (只在新字段没出现时补, 新字段优先)
_DIRECT_ALIASES = (
    ("direct_vless_tls", "direct_xray_tls"),
    ("direct_vless_tls_port", "direct_xray_tls_port"),
)


# ---- 出站 (3X-UI 式统一管理, 第一版只做管理 + 测速) ----
# 默认两条: direct 直出 (freedom)、blocked 拦截 (blackhole)。
# 出站 / 路由存在面板里 (PANEL_DIR/outbounds.json, routes.json),
# 部署时经 config_to_env 以 NEXUS_OUTBOUNDS / NEXUS_ROUTES (JSON)
# 传给 install.sh, 由核心渲染写进 Xray 与 Sing-box 配置。
OUTBOUNDS_PATH = os.path.join(PANEL_DIR, "outbounds.json")
ROUTES_PATH = os.path.join(PANEL_DIR, "routes.json")
OUTBOUND_TYPES = {"freedom", "blackhole", "socks", "http", "vless",
                  "vmess", "trojan", "shadowsocks", "hysteria2",
                  "wireguard", "dns"}
DEFAULT_OUTBOUNDS = [
    {"tag": "direct", "type": "freedom", "address": "", "port": None,
     "username": "", "password": "", "uuid": "", "method": "aes-256-gcm",
     "tls": True, "private_key": "", "peer_public_key": "", "local_address": "",
     "export": "", "country": "", "traffic": "", "latency_ms": None},
    {"tag": "blocked", "type": "blackhole", "address": "", "port": None,
     "username": "", "password": "", "uuid": "", "method": "aes-256-gcm",
     "tls": True, "private_key": "", "peer_public_key": "", "local_address": "",
     "export": "", "country": "", "traffic": "", "latency_ms": None},
]


def _to_bool(v, default=True):
    if v is None:
        return default
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return bool(v)
    return str(v).strip().lower() in ("true", "1", "yes", "on")


def _to_port(v):
    try:
        p = int(v)
    except Exception:
        return None
    return p if 1 <= p <= 65535 else None


def _clean_outbound(o):
    """校验并规范一条出站; 返回 (cleaned, error)。error 非空时拒绝整表。"""
    tag = str(o.get("tag") or "").strip()
    if not tag:
        return None, "出站标签不能为空"
    otype = str(o.get("type") or "freedom").strip().lower()
    if otype not in OUTBOUND_TYPES:
        return None, f"出站 {tag} 的类型不正确"
    addr = str(o.get("address") or "").strip()
    port = _to_port(o.get("port"))
    username = str(o.get("username") or "").strip()
    password = str(o.get("password") or "")
    uuid = str(o.get("uuid") or "").strip()
    method = str(o.get("method") or "aes-256-gcm").strip() or "aes-256-gcm"
    need_addr = otype in ("socks", "http", "vless", "vmess", "trojan",
                          "shadowsocks", "hysteria2", "wireguard")
    if need_addr and not addr:
        return None, f"出站 {tag} 需要填写地址"
    if need_addr and port is None:
        return None, f"出站 {tag} 需要填写有效端口 (1-65535)"
    if otype in ("vless", "vmess") and not uuid:
        return None, f"出站 {tag} 需要填写 UUID"
    if otype in ("trojan", "shadowsocks", "hysteria2") and not password:
        return None, f"出站 {tag} 需要填写密码"
    if otype == "wireguard" and not (o.get("private_key") or "").strip():
        return None, f"出站 {tag} 需要填写私钥"
    if otype == "wireguard" and not (o.get("peer_public_key") or "").strip():
        return None, f"出站 {tag} 需要填写对端公钥"
    return {
        "tag": tag, "type": otype, "address": addr, "port": port,
        "username": username, "password": password, "uuid": uuid,
        "method": method, "tls": _to_bool(o.get("tls"), True),
        "private_key": str(o.get("private_key") or "").strip(),
        "peer_public_key": str(o.get("peer_public_key") or "").strip(),
        "local_address": str(o.get("local_address") or "").strip(),
        "export": str(o.get("export") or "").strip(),
        "country": str(o.get("country") or "").strip(),
        "traffic": str(o.get("traffic") or "").strip(),
        "latency_ms": o.get("latency_ms"),
    }, ""


def load_outbounds():
    """读出站列表; 文件不存在或损坏时返回默认两条, 不报错。"""
    data = _load_json(OUTBOUNDS_PATH, None)
    if isinstance(data, dict) and isinstance(data.get("outbounds"), list):
        data = data["outbounds"]
    if not isinstance(data, list):
        return [dict(o) for o in DEFAULT_OUTBOUNDS]
    out = []
    seen = set()
    for o in data:
        if not isinstance(o, dict):
            continue
        tag = str(o.get("tag") or "").strip()
        if not tag or tag in seen:
            continue
        seen.add(tag)
        out.append({
            "tag": tag,
            "type": str(o.get("type") or "freedom").strip().lower() or "freedom",
            "address": str(o.get("address") or "").strip(),
            "port": _to_port(o.get("port")),
            "username": str(o.get("username") or "").strip(),
            "password": str(o.get("password") or ""),
            "uuid": str(o.get("uuid") or "").strip(),
            "method": str(o.get("method") or "aes-256-gcm").strip() or "aes-256-gcm",
            "tls": _to_bool(o.get("tls"), True),
            "private_key": str(o.get("private_key") or "").strip(),
            "peer_public_key": str(o.get("peer_public_key") or "").strip(),
            "local_address": str(o.get("local_address") or "").strip(),
            "export": str(o.get("export") or "").strip(),
            "country": str(o.get("country") or "").strip(),
            "traffic": str(o.get("traffic") or "").strip(),
            "latency_ms": o.get("latency_ms"),
        })
    return out or [dict(o) for o in DEFAULT_OUTBOUNDS]


def _apply_direct_aliases(raw):
    """把旧字段名映射到新字段名, 返回一份新字典 (不改原字典)。"""
    if not isinstance(raw, dict):
        return {}
    raw = dict(raw)
    for new, old in _DIRECT_ALIASES:
        if new not in raw and old in raw:
            raw[new] = raw[old]
    return raw

_sessions = {}  # token -> expiry (秒时间戳), 只放内存, 面板重启后重新登录
_sessions_lock = threading.Lock()
_deploy_lock = threading.Lock()
_deploy_thread = None
_public_ip_cache = {"ip": None, "at": 0}


def _load_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else default
    except Exception:
        return default


def _save_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def load_config():
    cfg = dict(DEFAULT_CONFIG)
    saved = _load_json(CONFIG_PATH, {})
    saved = _apply_direct_aliases(saved)
    for k, v in saved.items():
        if k in cfg:
            cfg[k] = v
    # 旧配置只有单个 node_prefix 时, 它就是隧道前缀 (直连前缀用默认值)。
    # 全新配置 (saved 里没有 node_prefix) 不动, argo_prefix 保持默认 "Argo"。
    # 新配置以 argo_prefix 为准, node_prefix 跟着它走, 保持兼容。
    if not saved.get("argo_prefix") and saved.get("node_prefix"):
        cfg["argo_prefix"] = cfg["node_prefix"]
    cfg["node_prefix"] = cfg["argo_prefix"]
    # 旧字段与新字段保持一致 (新字段为准, 旧配置里只有旧字段时已在上面映射过)
    cfg["direct_xray_tls"] = cfg["direct_vless_tls"]
    cfg["direct_xray_tls_port"] = cfg["direct_vless_tls_port"]
    return cfg


def load_auth():
    return _load_json(AUTH_PATH, {})


def _check_password(password):
    auth = load_auth()
    user = auth.get("user", "")
    salt = auth.get("salt", "")
    want = auth.get("pass_hash", "")
    if not user or not salt or not want:
        return False
    got = hashlib.sha256((salt + (password or "")).encode()).hexdigest()
    return secrets.compare_digest(got, want)


def _new_session():
    token = secrets.token_urlsafe(32)
    with _sessions_lock:
        _sessions[token] = time.time() + 24 * 3600
    return token


def _valid_session(token):
    if not token:
        return False
    with _sessions_lock:
        exp = _sessions.get(token)
        if not exp:
            return False
        if exp < time.time():
            del _sessions[token]
            return False
        return True


def _drop_session(token):
    if not token:
        return
    with _sessions_lock:
        _sessions.pop(token, None)


def _as_bool(v, default=False):
    if isinstance(v, bool):
        return v
    if isinstance(v, str):
        return v.lower() in ("true", "1", "yes", "y")
    return bool(v) if v is not None else default


def _as_int(v, default):
    try:
        return int(v)
    except Exception:
        return default


def _port_ok(v):
    return isinstance(v, int) and 1 <= v <= 65535


def validate_config(raw):
    """返回 (cleaned_config, [error, ...])。cleaned 是补全默认后的配置。
    报错都点名具体字段, 用正式中文, 不用笼统说法。
    注意: /api/subscription 也调这里, 但只取含「订阅路径」的报错,
    所以其它报错里不要出现「订阅路径」四个字。"""
    cfg = dict(DEFAULT_CONFIG)
    if isinstance(raw, dict):
        raw = _apply_direct_aliases(raw)
        for k, v in raw.items():
            if k in cfg:
                cfg[k] = v
    errors = []

    cfg["enable_xray"] = _as_bool(cfg.get("enable_xray"), True)
    cfg["enable_singbox"] = _as_bool(cfg.get("enable_singbox"), True)
    tunnel_on = cfg["enable_xray"] or cfg["enable_singbox"]

    cfg["node_uuid"] = str(cfg.get("node_uuid") or "").strip()
    cfg["argo_token"] = str(cfg.get("argo_token") or "").strip()
    if tunnel_on:
        if not cfg["argo_token"]:
            errors.append("Argo Tunnel Token 未填写, 请填写 Cloudflare 隧道 Token")
        if not UUID_RE.match(cfg["node_uuid"]):
            errors.append("节点 UUID 格式不正确, 请点「安全生成」重新生成一个")
    elif cfg["node_uuid"] and not UUID_RE.match(cfg["node_uuid"]):
        errors.append("节点 UUID 格式不正确, 请点「安全生成」重新生成一个")

    # 节点前缀分开: 旧客户端只发 node_prefix (没发 argo_prefix) 时,
    # 它就是隧道前缀; 否则 argo_prefix 为准。
    _raw = raw if isinstance(raw, dict) else {}
    if (not str(_raw.get("argo_prefix") or "").strip()) and _raw.get("node_prefix"):
        cfg["argo_prefix"] = str(_raw.get("node_prefix")).strip()
    cfg["argo_prefix"] = str(cfg.get("argo_prefix") or "Argo").strip() or "Argo"
    cfg["direct_prefix"] = str(cfg.get("direct_prefix") or "直连").strip() or "直连"
    cfg["node_prefix"] = cfg["argo_prefix"]
    cfg["tunnel_protocol"] = str(cfg.get("tunnel_protocol") or "auto").strip().lower()
    if cfg["tunnel_protocol"] not in ("auto", "http2", "quic"):
        errors.append("隧道传输只能是 auto / http2 / quic")

    cfg["xray_port"] = _as_int(cfg.get("xray_port"), 8001)
    cfg["singbox_port"] = _as_int(cfg.get("singbox_port"), 8002)
    cfg["sub_port"] = 18080  # 订阅服务内部端口, 不再由用户填, 面板代理到本机 nexus-sub
    # 订阅地址主机: 域名或 IP, 可带端口 (面板域名 / IP:面板端口); 为空则用请求时的 Host
    _sh = str(cfg.get("sub_host") or cfg.get("sub_domain") or "").strip()
    _sh_host, _sh_port = _split_host_port(_sh) if _sh else ("", 443)
    if _sh and not (_is_domain(_sh_host) or _is_ipv4(_sh_host)):
        errors.append("订阅地址格式不正确, 请填域名或 IP, 可带端口")
        _sh_host, _sh_port = "", 443
    cfg["sub_host"] = f"{_sh_host}:{_sh_port}" if _sh_host and _sh_port != 443 else _sh_host
    cfg["sub_domain"] = _sh_host
    for name, label in (("xray_port", "Xray 回源端口"),
                        ("singbox_port", "Sing-box 回源端口"),
                        ("sub_port", "订阅端口")):
        if not _port_ok(cfg[name]):
            errors.append(f"{label}必须是 1-65535 的端口")
    # ---- 端口冲突 (同传输层才算冲突) ----
    # 只校验已启用的端口: 没启用的核心 / 没勾选的协议, 端口再冲突也不拦保存。
    # 不能冲突: 面板端口、订阅端口、Xray / Sing-box 回源端口、Sing-box
    # 分发器回源+1/+2/+3、直连 TCP 协议端口 (彼此之间及与上述端口之间)、
    # 系统端口 22 / 80。可以共用: 同核心的隧道协议共用一个端口
    # (Xray h1/h2/h3 同 443、Sing-box ws/httpupgrade/grpc 同 8443);
    # TCP 与 UDP 同号 (如 TCP 443 与 Hy2 UDP 443)。
    # 固定保留端口先占位, 启用的端口按顺序加入, 撞上就报错 (只报第一处)。
    fixed = {}  # 端口 -> 名称 (已占用的端口)
    for port, label in ((PANEL_PORT, f"面板端口 {PANEL_PORT}"), (8080, "面板端口 8080"),
                        (22, "系统端口 22"), (80, "系统端口 80")):
        fixed.setdefault(port, label)
    if _port_ok(cfg["sub_port"]):
        if cfg["sub_port"] in fixed:
            errors.append(f"订阅端口与{fixed[cfg['sub_port']]}冲突, 请更换")
        else:
            fixed[cfg["sub_port"]] = "订阅端口"
    if cfg["enable_xray"] and _port_ok(cfg["xray_port"]):
        if cfg["xray_port"] in fixed:
            errors.append(f"Xray 回源端口与{fixed[cfg['xray_port']]}冲突, 请更换")
        else:
            fixed[cfg["xray_port"]] = "Xray 回源端口"
    dispatch_ports = set()
    if cfg["enable_singbox"] and _port_ok(cfg["singbox_port"]):
        if cfg["singbox_port"] > 65532:
            errors.append("Sing-box 回源端口不能大于 65532 (分发器要用 +1/+2/+3)")
        if cfg["singbox_port"] in fixed:
            errors.append(f"Sing-box 回源端口与{fixed[cfg['singbox_port']]}冲突, 请更换")
        else:
            fixed[cfg["singbox_port"]] = "Sing-box 回源端口"
            for off in (1, 2, 3):
                dp = cfg["singbox_port"] + off
                dispatch_ports.add(dp)
                if dp in fixed:
                    errors.append(f"Sing-box 分发器内部端口 {dp} 与{fixed[dp]}冲突, 请更换")
                    break

    for key in ("xray_domain", "singbox_domain", "sub_domain"):
        cfg[key] = str(cfg.get(key) or "").strip()
    if cfg["enable_xray"] and not cfg["xray_domain"]:
        errors.append("启用 Xray 时请填写 Xray 子域名")
    if cfg["enable_singbox"] and not cfg["singbox_domain"]:
        errors.append("启用 Sing-box 时请填写 Sing-box 子域名")

    def _proto_map(raw_map, allowed, label):
        out = {}
        if isinstance(raw_map, dict):
            for proto, port in raw_map.items():
                if proto in allowed:
                    out[proto] = _as_int(port, 443)
        for proto, port in out.items():
            if not _port_ok(port):
                errors.append(f"{label} {proto} 的端口必须是 1-65535")
        return out

    cfg["xray_protocols"] = _proto_map(cfg.get("xray_protocols"), ("xhttp_h1", "xhttp_h2", "xhttp_h3"), "Xray")
    cfg["singbox_protocols"] = _proto_map(cfg.get("singbox_protocols"), ("ws", "httpupgrade", "grpc"), "Sing-box")
    if cfg["enable_xray"] and not cfg["xray_protocols"]:
        errors.append("启用 Xray 时至少勾选一个 Xray 协议")
    if cfg["enable_singbox"] and not cfg["singbox_protocols"]:
        errors.append("启用 Sing-box 时至少勾选一个 Sing-box 协议")

    addrs = cfg.get("opt_addrs")
    if isinstance(addrs, str):
        addrs = [a.strip() for a in addrs.replace("\n", ",").split(",") if a.strip()]
    if not isinstance(addrs, list) or not addrs:
        addrs = ["cloudflare.com"]
    cfg["opt_addrs"] = [str(a).strip() for a in addrs if str(a).strip()][:6] or ["cloudflare.com"]

    cfg["sub_path"] = str(cfg.get("sub_path") or "sub").strip()
    if not SUB_PATH_RE.match(cfg["sub_path"]) or cfg["sub_path"] in RESERVED_SUB_PATH:
        errors.append("订阅路径只能是字母/数字/._- (1-32 个字符), 不能是 api / config.json / help.txt")

    # ---- 直连 ----
    cfg["enable_direct"] = _as_bool(cfg.get("enable_direct"), False)
    cfg["direct_address"] = str(cfg.get("direct_address") or cfg.get("direct_domain") or "").strip()
    cfg["direct_domain"] = cfg["direct_address"]
    cfg["direct_sni"] = str(cfg.get("direct_sni") or "").strip()
    cfg["client_selector"] = str(cfg.get("client_selector") or "auto").strip()
    cfg["client_mode"] = str(cfg.get("client_mode") or "suitable").strip()
    # 直连域名可为空 (空 = 自动探测公网 IP); 填了就必须是域名或 IPv4
    if cfg["direct_domain"] and not (DOMAIN_RE.match(cfg["direct_domain"]) or _is_ipv4(cfg["direct_domain"])):
        errors.append("直连域名格式不正确, 请填写域名或 IPv4 地址")

    for flag, port_key, _label, _is_tcp in DIRECT_PROTOCOLS:
        default_on = flag in ("direct_vless_tls", "direct_sb_tls")
        cfg[flag] = _as_bool(cfg.get(flag), default_on)
    _direct_port_defaults = {
        "direct_vless_tcp_port": 8082, "direct_vless_tls_port": 443,
        "direct_vless_reality_port": 9447, "direct_vless_ws_port": 8083,
        "direct_vless_grpc_port": 8084,
        "direct_trojan_port": 9443, "direct_vmess_ws_port": 9444,
        "direct_sb_trojan_port": 9445, "direct_ss_port": 8388,
        "direct_hy2_port": 9446, "direct_sb_tls_port": 8443,
    }
    for port_key, default in _direct_port_defaults.items():
        cfg[port_key] = _as_int(cfg.get(port_key), default)
    # 旧字段与新字段保持一致 (新字段为准)
    cfg["direct_xray_tls"] = cfg["direct_vless_tls"]
    cfg["direct_xray_tls_port"] = cfg["direct_vless_tls_port"]
    # 直连端口范围 (全部协议, 含 UDP 的 Hysteria2)
    for _flag, port_key, label, _is_tcp in DIRECT_PROTOCOLS:
        if not _port_ok(cfg[port_key]):
            errors.append(f"{label}端口必须是 1-65535 的端口")

    # Reality 目标带端口 (3X-UI 式, 如 www.cloudflare.com:443)。
    # 存的就是带端口的写法, 部署时 install.sh / sub-server 会剥掉
    # 端口再用; 旧配置只有域名时补上 :443。
    cfg["reality_dest"] = str(cfg.get("reality_dest") or "").strip()
    _dest_host, _dest_port = _split_host_port(cfg["reality_dest"])
    if _dest_host:
        cfg["reality_dest"] = f"{_dest_host}:{_dest_port}"
    cfg["reality_sni"] = str(cfg.get("reality_sni") or "").strip()
    cfg["reality_fingerprint"] = str(cfg.get("reality_fingerprint") or "chrome").strip().lower() or "chrome"
    cfg["reality_flow"] = str(cfg.get("reality_flow") or "xtls-rprx-vision").strip() or "xtls-rprx-vision"
    cfg["reality_encryption"] = str(cfg.get("reality_encryption") or "mlkem768x25519plus.native").strip() or "mlkem768x25519plus.native"
    cfg["reality_spiderx"] = str(cfg.get("reality_spiderx") or "/").strip() or "/"
    cfg["reality_max_time_diff"] = _as_int(cfg.get("reality_max_time_diff"), 0)
    cfg["reality_min_client_ver"] = str(cfg.get("reality_min_client_ver") or "1.0.0").strip() or "1.0.0"
    cfg["reality_max_client_ver"] = str(cfg.get("reality_max_client_ver") or "x.y.z").strip() or "x.y.z"
    # Short IDs: 多个, 每个 2-16 位 hex, 去重, 最多 8 个。
    # 前端没发时沿用 reality.json 里的 (第一项即旧 short_id)。
    _sids = cfg.get("reality_short_ids")
    if isinstance(_sids, str):
        _sids = [s.strip() for s in _sids.replace(",", " ").split() if s.strip()]
    if not isinstance(_sids, list) or not _sids:
        _file_keys = _load_reality_keys()
        _sids = _file_keys.get("short_ids") or ([_file_keys["short_id"]] if _file_keys.get("short_id") else [])
    _clean_sids = []
    for _s in _sids:
        _s = str(_s).strip().lower()
        if re.fullmatch(r"[0-9a-f]{2,16}", _s) and _s not in _clean_sids:
            _clean_sids.append(_s)
    cfg["reality_short_ids"] = _clean_sids[:8]
    cfg["cert_mode"] = str(cfg.get("cert_mode") or "acme").strip().lower()
    cfg["cert_pem"] = str(cfg.get("cert_pem") or "")
    cfg["cert_key"] = str(cfg.get("cert_key") or "")

    if cfg["enable_direct"]:
        enabled_direct = [(label, cfg[port_key])
                          for flag, port_key, label, _is_tcp in DIRECT_PROTOCOLS
                          if cfg[flag]]
        if not enabled_direct:
            errors.append("启用直连时至少勾选一个直连协议")
        # TCP 直连端口之间不能冲突, 也不能和面板端口 / 系统端口 / 订阅端口 /
        # 回源端口 / 分发器内部端口冲突; Hysteria2 走 UDP, 不参与
        # TCP 冲突检查 (TCP 与 UDP 同号可以共用)。
        # 只检查已勾选的协议 —— 没勾选的协议端口不拦保存, 勾选时再校验。
        # fixed 与 dispatch_ports 是上面回源端口一段建好的已占用端口表。
        tcp_ports = [(label, cfg[port_key])
                     for flag, port_key, label, is_tcp in DIRECT_PROTOCOLS
                     if is_tcp and cfg[flag]]
        seen = {}
        for label, port in tcp_ports:
            if port in seen:
                errors.append(f"{label}端口与{seen[port]}端口冲突, 请更换")
            elif port in fixed:
                errors.append(f"{label}端口与{fixed[port]}冲突, 请更换")
            elif port in dispatch_ports:
                errors.append(f"{label}端口与 Sing-box 分发器内部端口 {port} 冲突, 请更换")
            else:
                seen[port] = label

    # REALITY 开了就必须配目标 (域名, 不能是 IP, 可带 :端口); 直连没开
    # 时不校验, 否则只保存隧道配置也会被直连的残留设置拦住。
    if cfg["enable_direct"] and cfg["direct_vless_reality"]:
        _dh, _dp = _split_host_port(cfg["reality_dest"])
        if not cfg["reality_dest"]:
            errors.append("REALITY 目标未填写, 请填写一个常用网站域名 (如 www.microsoft.com:443)")
        elif not _is_domain(_dh):
            errors.append("REALITY 目标格式不正确, 请填写域名 (不能是 IP 地址, 可带 :443 端口)")
        if cfg["reality_sni"] and not _is_domain(cfg["reality_sni"]):
            errors.append("REALITY SNI 格式不正确, 请填写域名 (不能是 IP 地址)")
        if cfg["reality_fingerprint"] not in ("chrome", "firefox", "safari"):
            errors.append("REALITY 指纹只能是 chrome / firefox / safari")
        if cfg["reality_max_time_diff"] < 0:
            errors.append("REALITY 最大时间差不能小于 0")
        for _ver_key, _ver_label in (("reality_min_client_ver", "REALITY 最小客户端版本"),
                                     ("reality_max_client_ver", "REALITY 最大客户端版本")):
            if not re.fullmatch(r"[0-9xXyYzZ][0-9xXyYzZ.]*", cfg[_ver_key]):
                errors.append(f"{_ver_label}格式不正确, 如 1.0.0 或 x.y.z")

    # 需证书的直连协议 (Reality、VLESS+TCP、VLESS+WS、Shadowsocks、Hysteria2 不需要证书);
    # 只有勾了需证书协议时, 证书模式校验才生效。
    _need_cert = cfg["enable_direct"] and any(
        cfg.get(k) for k in ("direct_vless_tls", "direct_vless_grpc", "direct_trojan",
                             "direct_vmess_ws", "direct_sb_trojan", "direct_sb_tls"))
    # 证书模式: 只在直连开启时校验; 直连没开时证书设置不拦保存。
    if _need_cert and cfg["cert_mode"] not in ("selfsigned", "upload", "acme"):
        errors.append("证书模式不正确, 只能选择自签名 / 上传 / ACME")
    elif _need_cert and cfg["cert_mode"] == "upload":
        if not cfg["cert_pem"].strip():
            errors.append("证书内容未填写, 上传证书模式下请填写证书内容 (PEM)")
        elif "BEGIN CERTIFICATE" not in cfg["cert_pem"]:
            errors.append("证书内容格式不正确, 应为 PEM 格式证书 (包含 BEGIN CERTIFICATE)")
        if not cfg["cert_key"].strip():
            errors.append("私钥内容未填写, 上传证书模式下请填写私钥内容 (PEM)")
        elif "BEGIN PRIVATE KEY" not in cfg["cert_key"] and "BEGIN RSA PRIVATE KEY" not in cfg["cert_key"]:
            errors.append("私钥内容格式不正确, 应为 PEM 格式私钥 (包含 BEGIN PRIVATE KEY)")
    elif _need_cert and cfg["cert_mode"] == "acme":
        if not cfg["direct_domain"]:
            errors.append("直连域名未填写, ACME 模式需要填写直连域名 (不能为空)")
        elif not _is_domain(cfg["direct_domain"]):
            errors.append("直连域名格式不正确, ACME 模式需要填写域名 (不能是 IP 地址)")

    if not cfg["enable_xray"] and not cfg["enable_singbox"] and not cfg["enable_direct"]:
        errors.append("至少启用一个核心或直连")
    return cfg, errors


# 旧路由 {type, value} -> 新路由 {match_type, values} 的迁移:
# domain -> domain_suffix (按域名分流一般指该域名及其子域名),
# ip -> ip_cidr, port -> port。
_ROUTE_TYPE_MAP = {"domain": "domain_suffix", "ip": "ip_cidr", "port": "port"}
ROUTE_MATCH_TYPES = ("domain", "domain_suffix", "ip_cidr", "port")


def _split_values(v):
    """匹配值拆成列表: 支持换行 / 逗号分隔, 去空去重保序。"""
    if isinstance(v, list):
        parts = []
        for x in v:
            parts.extend(str(x).replace(",", "\n").splitlines())
    else:
        parts = str(v or "").replace(",", "\n").splitlines()
    out, seen = [], set()
    for p in parts:
        p = p.strip()
        if p and p not in seen:
            seen.add(p)
            out.append(p)
    return out


def _valid_port_value(v):
    v = str(v).strip()
    if "-" in v:
        a, _, b = v.partition("-")
        try:
            a, b = int(a), int(b)
        except Exception:
            return False
        return 1 <= a < b <= 65535
    try:
        return 1 <= int(v) <= 65535
    except Exception:
        return False


def load_routes():
    """读路由规则 (按保存顺序 = 匹配优先级, 先命中先生效)。
    返回 [{match_type, values, outbound}]。"""
    data = _load_json(ROUTES_PATH, None)
    if isinstance(data, dict) and isinstance(data.get("routes"), list):
        data = data["routes"]
    if not isinstance(data, list):
        return []
    out = []
    for r in data:
        if not isinstance(r, dict):
            continue
        if "match_type" in r:
            mt = str(r.get("match_type") or "domain_suffix").strip().lower()
            if mt not in ROUTE_MATCH_TYPES:
                mt = "domain_suffix"
        else:
            # 旧格式只有 type, 按迁移表走 (domain 当初指域名及其子域名)
            mt = _ROUTE_TYPE_MAP.get(str(r.get("type") or "").strip().lower(), "domain_suffix")
        if "values" in r:
            vals = _split_values(r.get("values"))
        else:
            vals = _split_values(r.get("value"))
        if not vals:
            continue
        if mt in ("domain", "domain_suffix"):
            vals = [v[2:] if v.startswith("*.") else v for v in vals]
        out.append({"match_type": mt, "values": vals,
                    "outbound": str(r.get("outbound") or "direct").strip() or "direct"})
    return out


def _reality_json_path():
    return os.path.join(WORKDIR, "direct", "reality.json")


def _load_reality_json():
    """读 reality.json 原样返回 (dict), 不存在返回 {}。"""
    try:
        with open(_reality_json_path(), "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_reality_json(data):
    d = os.path.join(WORKDIR, "direct")
    os.makedirs(d, exist_ok=True)
    path = _reality_json_path()
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def _b64url(raw):
    import base64
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _gen_x25519_keypair():
    """生成一对 X25519 密钥 (与 install.sh 同一套做法): 私钥取 DER
    末 32 字节 base64url, 公钥同理。失败返回 ("", "")。"""
    try:
        priv_der = subprocess.run(
            ["openssl", "genpkey", "-algorithm", "X25519"],
            capture_output=True, timeout=10).stdout
        # genpkey 输出 PEM, 转 DER 再取末 32 字节
        proc = subprocess.run(["openssl", "pkey", "-outform", "DER"],
                              input=priv_der, capture_output=True, timeout=10)
        der = proc.stdout
        if len(der) < 32:
            return "", ""
        private_key = _b64url(der[-32:])
        proc2 = subprocess.run(["openssl", "pkey", "-pubout", "-outform", "DER"],
                               input=priv_der, capture_output=True, timeout=10)
        pub_der = proc2.stdout
        if len(pub_der) < 32:
            return "", ""
        return private_key, _b64url(pub_der[-32:])
    except Exception:
        return "", ""


def _load_reality_keys():
    """读 $WORKDIR/direct/reality.json (install.sh 生成的 REALITY 密钥)。
    文件不存在返回空值, 不报错。键名兼容几种写法。
    short_ids 是列表, 第一项即旧的 short_id。"""
    data = _load_reality_json()

    def _pick(*names):
        for n in names:
            v = data.get(n)
            if isinstance(v, str) and v:
                return v
        return ""

    short_id = _pick("short_id", "shortId", "REALITY_SHORT_ID")
    sids = data.get("short_ids")
    if not isinstance(sids, list):
        sids = [short_id] if short_id else []
    sids = [str(s).strip().lower() for s in sids if str(s).strip()][:8]
    return {
        "private_key": _pick("private_key", "privateKey", "REALITY_PRIVATE_KEY"),
        "public_key": _pick("public_key", "publicKey", "REALITY_PUBLIC_KEY"),
        "short_id": short_id,
        "short_ids": sids,
    }


def _write_upload_cert(cfg):
    """cert_mode=upload 时, 把配置里的证书写到 $WORKDIR/direct/cert.pem /
    key.pem (600)。只在部署时调, 不要在保存配置时写, 避免没部署的
    配置覆盖线上正在用的证书。返回 (ok, error_message)。"""
    if cfg.get("cert_mode") != "upload":
        return True, ""
    pem = cfg.get("cert_pem") or ""
    key = cfg.get("cert_key") or ""
    if not pem or not key:
        return False, "上传证书模式下证书或私钥为空"
    d = os.path.join(WORKDIR, "direct")
    try:
        os.makedirs(d, exist_ok=True)
        for name, content in (("cert.pem", pem), ("key.pem", key)):
            path = os.path.join(d, name)
            with open(path, "w", encoding="utf-8") as f:
                f.write(content if content.endswith("\n") else content + "\n")
            os.chmod(path, 0o600)
    except OSError as e:
        return False, f"写入证书文件失败: {e}"
    return True, ""


def config_to_env(cfg):
    """部署配置 -> install.sh 的环境变量。"""
    def _cfgs(m):
        return ",".join(f"{p}:{port}" for p, port in m.items())

    def _b(v):
        return "true" if v else "false"
    reality = _load_reality_keys()
    # 需证书的直连协议; 没勾时传 selfsigned 给 install.sh, 不跑 ACME (配置里存的 cert_mode 不动)
    _need_cert = cfg["enable_direct"] and any(
        cfg.get(k) for k in ("direct_vless_tls", "direct_vless_grpc", "direct_trojan",
                             "direct_vmess_ws", "direct_sb_trojan", "direct_sb_tls"))
    env = {
        "NODE_UUID": cfg["node_uuid"],
        "NODE_PREFIX": cfg["argo_prefix"],
        "NODE_PREFIX_ARGO": cfg["argo_prefix"],
        "NODE_PREFIX_DIRECT": cfg["direct_prefix"],
        "ARGO_TOKEN": cfg["argo_token"],
        "TUNNEL_PROTOCOL": cfg["tunnel_protocol"],
        "ENABLE_XRAY": _b(cfg["enable_xray"]),
        "XRAY_PORT": str(cfg["xray_port"]),
        "XRAY_DOMAIN": cfg["xray_domain"],
        "XRAY_CONFIGS": _cfgs(cfg["xray_protocols"]),
        "ENABLE_SINGBOX": _b(cfg["enable_singbox"]),
        "SINGBOX_PORT": str(cfg["singbox_port"]),
        "SINGBOX_DOMAIN": cfg["singbox_domain"],
        "SINGBOX_CONFIGS": _cfgs(cfg["singbox_protocols"]),
        "OPT_ADDRS": ",".join(cfg["opt_addrs"]),
        "SUB_DOMAIN": cfg["sub_domain"],
        "SUB_PORT": str(cfg["sub_port"]),
        "SUB_PATH": cfg["sub_path"],
        "ENABLE_DIRECT": _b(cfg["enable_direct"]),
        "DIRECT_DOMAIN": cfg["direct_domain"],
        "DIRECT_ADDRESS": cfg.get("direct_address") or cfg["direct_domain"],
        "DIRECT_SNI": cfg.get("direct_sni") or "",
        "DIRECT_VLESS_TCP": _b(cfg["direct_vless_tcp"]),
        "DIRECT_VLESS_TCP_PORT": str(cfg["direct_vless_tcp_port"]),
        "DIRECT_VLESS_TLS": _b(cfg["direct_vless_tls"]),
        "DIRECT_VLESS_TLS_PORT": str(cfg["direct_vless_tls_port"]),
        "DIRECT_VLESS_REALITY": _b(cfg["direct_vless_reality"]),
        "DIRECT_VLESS_REALITY_PORT": str(cfg["direct_vless_reality_port"]),
        "REALITY_DEST": cfg["reality_dest"],
        "REALITY_SNI": cfg["reality_sni"],
        "REALITY_FINGERPRINT": cfg["reality_fingerprint"],
        "REALITY_FLOW": cfg["reality_flow"],
        "REALITY_ENCRYPTION": cfg["reality_encryption"],
        "REALITY_SPIDERX": cfg["reality_spiderx"],
        "REALITY_PRIVATE_KEY": reality["private_key"],
        "REALITY_PUBLIC_KEY": reality["public_key"],
        # Short ID 用配置里的第一项, 没配才用 reality.json 里的。
        "REALITY_SHORT_ID": (cfg.get("reality_short_ids") or [None])[0] or reality["short_id"],
        "REALITY_SHORT_IDS": ",".join(cfg.get("reality_short_ids") or reality.get("short_ids") or []),
        "REALITY_MAX_TIME_DIFF": str(cfg.get("reality_max_time_diff", 0)),
        "REALITY_MIN_CLIENT_VER": cfg.get("reality_min_client_ver", "1.0.0"),
        "REALITY_MAX_CLIENT_VER": cfg.get("reality_max_client_ver", "x.y.z"),
        "DIRECT_VLESS_WS": _b(cfg["direct_vless_ws"]),
        "DIRECT_VLESS_WS_PORT": str(cfg["direct_vless_ws_port"]),
        "DIRECT_VLESS_GRPC": _b(cfg["direct_vless_grpc"]),
        "DIRECT_VLESS_GRPC_PORT": str(cfg["direct_vless_grpc_port"]),
        "DIRECT_WS_PATH": cfg.get("direct_ws_path") or "/ws",
        "DIRECT_GRPC_SERVICE": cfg.get("direct_grpc_service") or "grpc",
        "DIRECT_TROJAN": _b(cfg["direct_trojan"]),
        "DIRECT_TROJAN_PORT": str(cfg["direct_trojan_port"]),
        "DIRECT_VMESS_WS": _b(cfg["direct_vmess_ws"]),
        "DIRECT_VMESS_WS_PORT": str(cfg["direct_vmess_ws_port"]),
        "DIRECT_SB_TROJAN": _b(cfg["direct_sb_trojan"]),
        "DIRECT_SB_TROJAN_PORT": str(cfg["direct_sb_trojan_port"]),
        "DIRECT_SS": _b(cfg["direct_ss"]),
        "DIRECT_SS_PORT": str(cfg["direct_ss_port"]),
        "DIRECT_HY2": _b(cfg["direct_hy2"]),
        "DIRECT_HY2_PORT": str(cfg["direct_hy2_port"]),
        "CERT_MODE": cfg["cert_mode"] if _need_cert else "selfsigned",
        # 旧变量名 (兼容): 和新变量同值
        "DIRECT_XRAY_TLS": _b(cfg["direct_vless_tls"]),
        "DIRECT_XRAY_TLS_PORT": str(cfg["direct_vless_tls_port"]),
        "DIRECT_SB_TLS": _b(cfg["direct_sb_tls"]),
        "DIRECT_SB_TLS_PORT": str(cfg["direct_sb_tls_port"]),
        # 出站 / 路由 (面板定义, 部署时写进核心配置)
        "NEXUS_OUTBOUNDS": json.dumps(load_outbounds()),
        "NEXUS_ROUTES": json.dumps(load_routes()),
    }
    return env


def deploy_status():
    return _load_json(STATUS_PATH, {"state": "idle"})


def _set_deploy_status(state, **extra):
    st = {"state": state, "at": time.strftime("%Y-%m-%d %H:%M:%S")}
    st.update(extra)
    _save_json(STATUS_PATH, st)
    return st


def _run(cmd, timeout=10):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.stdout.strip(), r.returncode
    except Exception:
        return "", -1


_DOCKER_CACHE = {"t": 0.0, "states": {}}
def _docker_states():
    # 刷新慢的主因: /api/status 原来对每个容器逐个 docker inspect (8 次串行)。
    # 改成一次 docker ps 拿全部状态, 3 秒缓存, 刷新从 4-5 秒降到 1 秒左右。
    import time as _time
    now = _time.time()
    if now - _DOCKER_CACHE["t"] < 3 and _DOCKER_CACHE["states"]:
        return _DOCKER_CACHE["states"]
    out, code = _run(["docker", "ps", "-a", "--format", "{{.Names}} {{.Status}}"], timeout=5)
    states = {}
    if code == 0:
        for line in out.splitlines():
            parts = line.split(None, 1)
            if parts:
                states[parts[0]] = "running" if len(parts) > 1 and parts[1].startswith("Up") else "stopped"
    _DOCKER_CACHE.update({"t": now, "states": states})
    return states

def _docker_running(name):
    states = _docker_states()
    if name not in states:
        return "not_installed"
    return states[name]


def _is_deployed():
    # 任一节点容器存在(运行中或已停止)即视为已有部署; 面板隧道 nexus-panel-argo 不算
    return any(_docker_running(n) != "not_installed" for n in NODE_CONTAINERS)


def _argo_tunnel_state():
    """Argo 隧道状态 = 节点隧道 nexus-argo 与面板隧道 nexus-panel-argo
    任一在跑即运行中。只查 nexus-argo 会漏掉面板隧道, 导致用户明明
    已通过隧道进面板, 总览却显示灰色 (2026-11-19 实测)。"""
    a = _docker_running("nexus-argo")
    b = _docker_running("nexus-panel-argo")
    if "running" in (a, b):
        return "running"
    if "stopped" in (a, b):
        return "stopped"
    return "not_installed"


def _count_lines(path):
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            return sum(1 for line in f if line.strip())
    except Exception:
        return 0


def _public_ip():
    now = time.time()
    if _public_ip_cache["ip"] and now - _public_ip_cache["at"] < 3600:
        return _public_ip_cache["ip"]
    ip = ""
    for url in ("https://ifconfig.me/ip", "https://api.ipify.org"):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "nexus-panel"})
            with urllib.request.urlopen(req, timeout=3) as r:
                ip = r.read().decode().strip()
            if re.match(r"^\d+\.\d+\.\d+\.\d+$", ip):
                break
            ip = ""
        except Exception:
            continue
    _public_ip_cache["ip"] = ip
    _public_ip_cache["at"] = now
    return ip


def _sub_base(cfg, host=None, scheme=None):
    """面板订阅 base: 优先用配置里的 sub_host (面板域名 / IP:端口),
    没配就用请求时的 Host; 域名默认 https, IP 默认 http (带面板端口)。"""
    _h = (cfg.get("sub_host") or "").strip() or (host or "").strip()
    if not _h:
        return None
    _hh, _pp = _split_host_port(_h)
    if not _hh:
        return None
    _sc = (scheme or "").strip().lower()
    if _sc not in ("http", "https"):
        _sc = "http" if _is_ipv4(_hh) else "https"
    if _pp == 443 and _is_ipv4(_hh):
        _hp = f"{_hh}:{PANEL_PORT}"
    elif _pp == 443:
        _hp = _hh
    else:
        _hp = f"{_hh}:{_pp}"
    return f"{_sc}://{_hp}"


def subscription_urls(cfg, host=None, scheme=None):
    """订阅地址 (按当前配置算, 改 sub_path 后实时变)。
    面板订阅: 地址就是面板自己的域名或 IP:端口 + UUID + 订阅路径。"""
    uuid = cfg["node_uuid"]
    sub_path = cfg.get("sub_path") or "sub"
    base = _sub_base(cfg, host, scheme)
    out = {
        "sub_host": cfg.get("sub_host") or "",
        "sub_domain": cfg.get("sub_domain") or "",
        "sub_path": sub_path,
        "sub_port": cfg.get("sub_port") or 18080,
        "raw_url": f"{base}/{uuid}/{sub_path}/raw" if base and uuid else "",
        "merged_url": f"{base}/{uuid}/{sub_path}/merged" if base and uuid else "",
        "generator_url": "",
        "raw_count": _count_lines(os.path.join(WORKDIR, "sub", "raw.txt")),
        "merged_count": _count_lines(os.path.join(WORKDIR, "sub", "merged.txt")),
    }
    return out


_PATH_ALPHABET = "abcdefghijklmnopqrstuvwxyz0123456789"


def _gen_direct_paths():
    """直连 WS path 与 gRPC serviceName: 每次部署随机生成一对
    (nx + 10 位字母数字, WS 带 / 前缀), 与隧道默认值区分开,
    避免用公开的 /ws、grpc 特征。返回 (ws_path, grpc_service)。
    生成后存进部署配置, inbound 与订阅链接同读此值。"""
    rand = "".join(secrets.choice(_PATH_ALPHABET) for _ in range(10))
    rand2 = "".join(secrets.choice(_PATH_ALPHABET) for _ in range(10))
    return "/nx" + rand, "nx" + rand2


def _deploy_worker(cfg):
    """后台跑 install.sh, 日志写 deploy.log, 状态写 deploy-status.json。"""
    global _deploy_thread
    started = time.time()
    _set_deploy_status("running", started_at=time.strftime("%Y-%m-%d %H:%M:%S"))
    env = dict(os.environ)
    env.update(config_to_env(cfg))
    try:
        with open(LOG_PATH, "wb") as logf:
            proc = subprocess.Popen(
                ["bash", INSTALL_SH], env=env, cwd=WORKDIR,
                stdout=logf, stderr=subprocess.STDOUT,
            )
            code = proc.wait()
    except Exception as e:
        code = -1
        try:
            with open(LOG_PATH, "ab") as logf:
                logf.write(f"\n[nexus-panel] 启动部署失败: {e}\n".encode())
        except Exception:
            pass
    elapsed = int(time.time() - started)
    if code == 0:
        _set_deploy_status("ok", exit_code=0, elapsed_s=elapsed,
                           finished_at=time.strftime("%Y-%m-%d %H:%M:%S"),
                           message="部署完成, 健康检查通过")
    else:
        _set_deploy_status("failed", exit_code=code, elapsed_s=elapsed,
                           finished_at=time.strftime("%Y-%m-%d %H:%M:%S"),
                           message=f"部署失败 (退出码 {code}), 看日志找原因")
    with _deploy_lock:
        _deploy_thread = None


def _restart_sub(cfg):
    """改订阅路径后重启 nexus-sub 生效 (不用重部署, 几秒就好)。
    复刻 install.sh 里起 nexus-sub 的那条 docker run。"""
    sub_py = os.path.join(WORKDIR, "web", "sub-server.py")
    gen_html = os.path.join(WORKDIR, "web", "sub-generator.html")
    if not os.path.isfile(sub_py):
        return False, "web/sub-server.py 不存在, 请先部署一次"
    env = config_to_env(cfg)
    cmds = [["docker", "rm", "-f", "nexus-sub"]]
    run = ["docker", "run", "-d", "--name", "nexus-sub", "--restart", "unless-stopped",
           "-p", f"127.0.0.1:{cfg['sub_port']}:{cfg['sub_port']}"]
    # 全部部署变量都传进去 (和 config_to_env 同一份, 含 DIRECT_* / REALITY_* / CERT_MODE),
    # 这样订阅里直连节点的链接也能按最新配置生成
    for k, v in env.items():
        run += ["-e", f"{k}={v}"]
    run += ["-v", f"{sub_py}:/app/sub-server.py:ro"]
    if os.path.isfile(gen_html):
        run += ["-v", f"{gen_html}:/app/sub-generator.html:ro"]
    run += ["python:3-alpine", "python3", "/app/sub-server.py"]
    cmds.append(run)
    for cmd in cmds:
        out, code = _run(cmd, timeout=30)
        if code != 0 and cmd[1] == "run":
            return False, f"启动 nexus-sub 失败: {out[:200]}"
    time.sleep(1)
    if _docker_running("nexus-sub") != "running":
        return False, "nexus-sub 启动后不在运行, 看 docker logs nexus-sub"
    return True, "订阅服务已重启, 新订阅地址已生效"


def _parse_openssl_date(s):
    """openssl notAfter 如 "Nov 19 00:00:00 2036 GMT" -> date, 解析失败返回 None。"""
    try:
        return datetime.datetime.strptime(s.strip(), "%b %d %H:%M:%S %Y %Z").date()
    except Exception:
        return None


def cert_info(cfg):
    """证书状态。证书内容本身绝不返回, 只返回主题、到期日和续期方式。"""
    mode = cfg.get("cert_mode") or "selfsigned"
    cert_path = os.path.join(WORKDIR, "direct", "cert.pem")
    if mode == "selfsigned":
        renew_note = "无需续期 (10年)"
    elif mode == "acme":
        renew_note = "ACME 自动续期 (90天)"
    else:
        renew_note = "到期前请重新上传"
    base = {"installed": False, "mode": mode, "message": "未部署"}
    if not os.path.isfile(cert_path):
        return base
    base = {"installed": True, "mode": mode,
            "auto_renew": mode == "acme", "renew_note": renew_note}
    out, code = _run(["openssl", "x509", "-in", cert_path, "-noout",
                      "-subject", "-enddate"], timeout=5)
    if code != 0 or not out:
        base.update({"subject": "", "expiry": "", "days_left": None,
                     "message": "证书信息读取失败, 请检查证书文件"})
        return base
    subject = ""
    expiry_raw = ""
    for line in out.splitlines():
        line = line.strip()
        if line.startswith("subject="):
            raw_subj = line[len("subject="):].strip()
            m = re.search(r"CN\s*=\s*([^,/]+)", raw_subj)
            subject = m.group(1).strip() if m else raw_subj
        elif line.startswith("notAfter="):
            expiry_raw = line[len("notAfter="):].strip()
    exp_date = _parse_openssl_date(expiry_raw)
    if exp_date is None:
        base.update({"subject": subject, "expiry": "", "days_left": None,
                     "message": "证书到期时间解析失败"})
        return base
    base.update({"subject": subject, "expiry": exp_date.isoformat(),
                 "days_left": (exp_date - datetime.date.today()).days})
    return base


# ---- Reality 目标扫描 (3X-UI 式) ----
REALITY_SCAN_CANDIDATES = [
    "www.samsung.com", "www.amd.com", "www.intel.com",
    "www.microsoft.com", "aws.amazon.com", "www.nvidia.com",
    "www.amazon.com", "www.sony.com", "www.cloudflare.com",
]


def _scan_target_list(target):
    """把用户输入解析成待扫目标列表: 域名 / IP / CIDR (取前 8 个);
    留空用常用候选。返回 [host, ...] (不带端口)。"""
    t = str(target or "").strip()
    if not t:
        return list(REALITY_SCAN_CANDIDATES)
    host, _port = _split_host_port(t)
    if "/" in t:
        try:
            net = ipaddress.ip_network(t, strict=False)
        except Exception:
            return []
        hosts = [str(h) for h in net.hosts()]
        return hosts[:8] or [str(net.network_address)]
    if _is_ipv4(host):
        return [host]
    if _is_domain(host):
        return [host]
    return []


_SCAN_SSL_CTX = None
def _scan_ssl_ctx():
    global _SCAN_SSL_CTX
    if _SCAN_SSL_CTX is None:
        import ssl
        _SCAN_SSL_CTX = ssl.create_default_context()
    return _SCAN_SSL_CTX


def _probe_reality_target(host):
    """对一个目标做 TLS 握手探测, 返回 3X-UI 式的行:
    {target, domain, status, tls, alpn, key_exchange, cert, latency_ms}。
    任何一步失败都记「不可用」, 不抛错、不阻塞。"""
    row = {"target": f"{host}:443", "domain": host, "status": "不可用",
           "tls": "", "alpn": "", "key_exchange": "", "cert": "",
           "cert_expiry": "", "cert_chain": "", "latency_ms": None}
    # 延迟: 只测 TCP+TLS 握手时间 (3X-UI 式), 不发完整 HTTP 请求。
    # DNS 在计时外解析; SSL 上下文全局复用 —— 建上下文要读 CA 包很慢,
    # 每次新建会把延迟抬到几百毫秒, 不能算进握手时间里。
    try:
        import socket
        infos = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
        addr = infos[0][4]
        t0 = time.monotonic()
        with socket.create_connection(addr, timeout=5) as sock:
            with _scan_ssl_ctx().wrap_socket(sock, server_hostname=host) as ssock:
                pass
        row["latency_ms"] = int(round((time.monotonic() - t0) * 1000))
    except Exception:
        pass
    # TLS 细节: openssl s_client 握手, 读协议 / ALPN / 临时密钥 / 证书 CN
    try:
        proc = subprocess.run(
            ["openssl", "s_client", "-connect", f"{host}:443",
             "-servername", host, "-alpn", "h2"],
            input=b"", capture_output=True, timeout=6)
        text = (proc.stdout + proc.stderr).decode("utf-8", "replace")
        m = re.search(r"Protocol\s*:\s*(TLSv[\d.]+)", text)
        if m:
            row["tls"] = m.group(1).replace("TLSv", "")
        m = re.search(r"ALPN protocol\s*:\s*(\S+)", text)
        if m:
            row["alpn"] = m.group(1)
        m = re.search(r"(?i)server temp key\s*:\s*([A-Za-z0-9_\-]+)", text)
        if not m:
            m = re.search(r"(?i)temp key\s*:\s*([A-Za-z0-9_\-]+)", text)
        if not m:
            m = re.search(r"(?i)key exchange\s*:\s*([A-Za-z0-9_\-]+)", text)
        if m:
            row["key_exchange"] = m.group(1)
        m = re.search(r"subject=CN\s*=\s*([^\n,;]+)", text)
        if not m:
            m = re.search(r"subject.*CN\s*=\s*([^\n,;/]+)", text)
        if m:
            row["cert"] = m.group(1).strip()
        if row["tls"]:
            row["status"] = "可用"
        # 证书有效期 + 证书链: openssl x509 读叶证书 dates, s_client 输出数链深度
        m = re.search(r"Certificate chain\s*\n((?:\s*\d+ s:.*\n?)+)", text)
        if m:
            depths = re.findall(r"^\s*(\d+)\s+s:", m.group(1), re.M)
            row["cert_chain"] = f"{len(depths)} 级" if depths else ""
    except Exception:
        pass
    try:
        proc = subprocess.run(
            ["sh", "-c", f"echo | openssl s_client -connect {host}:443 -servername {host} 2>/dev/null | openssl x509 -noout -dates 2>/dev/null"],
            capture_output=True, timeout=8)
        t = proc.stdout.decode("utf-8", "replace")
        m = re.search(r"notAfter=([^\n]+)", t)
        if m:
            row["cert_expiry"] = m.group(1).strip()
    except Exception:
        pass
    return row


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def _send_json(self, obj, status=200, extra_headers=None):
        data = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        if extra_headers:
            for k, v in extra_headers.items():
                self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def _send_file(self, path, ctype):
        try:
            with open(path, "rb") as f:
                data = f.read()
        except OSError:
            self._send_json({"error": "文件不存在"}, 404)
            return
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _proxy_sub(self, url):
        """面板订阅代理: /<uuid>/<sub_path>/raw|merged[?format=...] 转发给
        本机 nexus-sub (127.0.0.1:订阅内部端口), 面板域名/IP 直接出订阅。"""
        cfg = load_config()
        target = f"http://127.0.0.1:{cfg.get('sub_port') or 18080}{url.path}"
        if url.query:
            target += "?" + url.query
        try:
            req = urllib.request.Request(target, method="GET")
            with urllib.request.urlopen(req, timeout=20) as r:
                data = r.read()
                self.send_response(r.status)
                self.send_header("Content-Type", r.headers.get("Content-Type") or "text/plain; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(data)
        except urllib.error.HTTPError as e:
            data = e.read()
            self.send_response(e.code)
            self.send_header("Content-Type", e.headers.get("Content-Type") or "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        except Exception:
            self._send_json({"error": "订阅服务未运行, 请先部署"}, 502)

    def _cookie_token(self):
        raw = self.headers.get("Cookie") or ""
        for part in raw.split(";"):
            part = part.strip()
            if part.startswith("nexus_panel="):
                return part[len("nexus_panel="):]
        return ""

    def _authed(self):
        return _valid_session(self._cookie_token())

    def _need_auth(self):
        if not self._authed():
            self._send_json({"error": "未登录"}, 401)
            return False
        return True

    def _read_json(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except Exception:
            length = 0
        if length <= 0 or length > 65536:
            return {}
        try:
            return json.loads(self.rfile.read(length).decode("utf-8") or "{}")
        except Exception:
            return {}

    def do_GET(self):
        url = urllib.parse.urlparse(self.path)
        path = url.path

        _pp = PANEL_PATH.rstrip("/") if PANEL_PATH else ""
        if _pp and path == "/":
            self.send_response(302); self.send_header("Location", _pp); self.end_headers(); return
        if path in ("/", "/index.html") or (_pp and path in (_pp, _pp + "/index.html")):
            self._send_file(_PANEL_HTML, "text/html; charset=utf-8")
            return
        _m = re.match(r"^/([0-9a-fA-F-]{36})/", path)
        if _m and UUID_RE.match(_m.group(1)):
            # 面板订阅 (公开, 免登录): 转发给本机 nexus-sub
            self._proxy_sub(url)
            return
        if path == "/api/status":
            if not self._need_auth():
                return
            cfg = load_config()
            services = [{"name": n, "state": _docker_running(n)} for n in SERVICES]
            # nexus-argo 那一项取合并后的隧道状态 (节点隧道或面板隧道任一在跑)
            for svc in services:
                if svc["name"] == "nexus-argo":
                    svc["state"] = _argo_tunnel_state()
            st = deploy_status()
            sub = subscription_urls(cfg, self.headers.get("Host"), self.headers.get("X-Forwarded-Proto"))
            self._send_json({
                "deployed": _is_deployed(),
                "deploy_state": st.get("state", "idle"),
                "services": services,
                "nodes": {"native": sub["raw_count"], "dynamic": sub["merged_count"]},
                "subscription": sub,
                "public_ip": _public_ip(),
                "panel_port": PANEL_PORT,
            })
            return
        if path == "/api/config":
            if not self._need_auth():
                return
            self._send_json(load_config())
            return
        if path == "/api/deploy/status":
            if not self._need_auth():
                return
            self._send_json(deploy_status())
            return
        if path == "/api/deploy/log":
            if not self._need_auth():
                return
            query = urllib.parse.parse_qs(url.query)
            try:
                offset = int((query.get("offset", ["0"])[0] or "0"))
            except Exception:
                offset = 0
            try:
                with open(LOG_PATH, "rb") as f:
                    f.seek(0, os.SEEK_END)
                    size = f.tell()
                    offset = max(0, min(offset, size))
                    f.seek(offset)
                    data = f.read().decode("utf-8", "replace")
            except OSError:
                data, size = "", 0
            self._send_json({"log": data, "offset": size, "state": deploy_status().get("state", "idle")})
            return
        if path == "/api/subscription":
            if not self._need_auth():
                return
            self._send_json(subscription_urls(load_config(), self.headers.get("Host"), self.headers.get("X-Forwarded-Proto")))
            return
        if path == "/api/cert":
            if not self._need_auth():
                return
            self._send_json(cert_info(load_config()))
            return
        if path == "/api/reality-keys":
            if not self._need_auth():
                return
            keys = _load_reality_keys()
            self._send_json({"public_key": keys["public_key"],
                             "private_key": keys["private_key"],
                             "short_ids": keys["short_ids"],
                             "has_private_key": bool(keys["private_key"])})
            return
        if path == "/api/outbounds":
            if not self._need_auth():
                return
            self._send_json({"outbounds": load_outbounds()})
            return
        if path == "/api/routes":
            if not self._need_auth():
                return
            self._send_json({"routes": load_routes()})
            return
        self._send_json({"error": "未知路径"}, 404)

    def do_POST(self):
        url = urllib.parse.urlparse(self.path)
        path = url.path

        if path == "/api/login":
            body = self._read_json()
            auth = load_auth()
            user = str(body.get("user") or "").strip()
            if auth.get("user") and user == auth["user"] and _check_password(body.get("password")):
                token = _new_session()
                self._send_json({"ok": True}, extra_headers={
                    "Set-Cookie": f"nexus_panel={token}; HttpOnly; Path=/; SameSite=Lax; Max-Age=86400",
                })
            else:
                self._send_json({"error": "账号或密码不正确"}, 401)
            return

        if path == "/api/logout":
            _drop_session(self._cookie_token())
            self._send_json({"ok": True}, extra_headers={
                "Set-Cookie": "nexus_panel=; HttpOnly; Path=/; SameSite=Lax; Max-Age=0",
            })
            return

        if not self._need_auth():
            return
        body = self._read_json()

        if path == "/api/config":
            cfg, errors = validate_config(body)
            if errors:
                self._send_json({"error": "；".join(errors)}, 400)
                return
            _save_json(CONFIG_PATH, cfg)
            self._send_json({"ok": True, "config": cfg})
            return

        if path == "/api/deploy":
            global _deploy_thread
            cfg, errors = validate_config(body if body else load_config())
            if errors:
                self._send_json({"error": "；".join(errors)}, 400)
                return
            if _is_deployed():
                self._send_json({"error": "已有部署，请先清除部署后再重新部署"}, 409)
                return
            # 直连 WS path 与 gRPC serviceName 每次部署重新随机生成
            cfg["direct_ws_path"], cfg["direct_grpc_service"] = _gen_direct_paths()
            _save_json(CONFIG_PATH, cfg)
            # 上传证书模式: 部署开始前先把证书写到磁盘 (只在这里写,
            # 保存配置时不写, 避免没部署的配置覆盖线上证书)
            ok, msg = _write_upload_cert(cfg)
            if not ok:
                self._send_json({"error": msg}, 500)
                return
            with _deploy_lock:
                if _deploy_thread is not None and _deploy_thread.is_alive():
                    self._send_json({"error": "已有部署在跑, 请等它完成"}, 409)
                    return
                if not os.path.isfile(INSTALL_SH):
                    self._send_json({"error": "install.sh 不存在, 面板安装不完整"}, 500)
                    return
                _deploy_thread = threading.Thread(target=_deploy_worker, args=(cfg,), daemon=True)
                _deploy_thread.start()
            self._send_json({"ok": True, "message": "部署已开始, 看日志跟进"})
            return

        if path == "/api/subscription":
            sub_path = str(body.get("sub_path") or "").strip()
            cfg = load_config()
            cfg["sub_path"] = sub_path or "sub"
            _, errors = validate_config(cfg)
            path_errors = [e for e in errors if "订阅路径" in e]
            if path_errors:
                self._send_json({"error": "；".join(path_errors)}, 400)
                return
            _save_json(CONFIG_PATH, cfg)
            # nexus-sub 在跑就重启它生效, 没跑就下次部署时生效
            if _docker_running("nexus-sub") == "running":
                ok, msg = _restart_sub(cfg)
                if not ok:
                    self._send_json({"error": msg}, 500)
                    return
                self._send_json({"ok": True, "message": msg, "subscription": subscription_urls(cfg)})
            else:
                self._send_json({"ok": True, "message": "已保存, 下次部署时生效", "subscription": subscription_urls(cfg)})
            return

        if path == "/api/reality-scan":
            # 3X-UI 式目标扫描: 输入域名 / IP / CIDR, 留空用常用候选;
            # 返回每行的目标 / 状态 / TLS / ALPN / 密钥交换 / 证书 / 延迟,
            # 按延迟排序。并行探测、每目标 6 秒超时, 失败记「不可用」,
            # 绝不阻塞部署。
            if shutil.which("curl") is None and shutil.which("openssl") is None:
                self._send_json({"error": "curl / openssl 都不可用"}, 500)
                return
            hosts = _scan_target_list(body.get("target") or body.get("domain") or "")
            if not hosts:
                self._send_json({"error": "目标格式不正确, 请填域名、IP 或 CIDR (如 1.2.3.0/24)"}, 400)
                return
            with ThreadPoolExecutor(max_workers=min(9, len(hosts))) as ex:
                results = list(ex.map(_probe_reality_target, hosts))
            results.sort(key=lambda r: (r["latency_ms"] is None, r["latency_ms"] or 0))
            best = next((r["target"] for r in results if r["status"] == "可用"), None)
            self._send_json({"results": results, "best": best})
            return

        if path == "/api/reality-keys":
            # 公钥 / 私钥 / Short IDs 查询与密钥管理。私钥随公钥一同返回前端 (仅登录后可见)。
            # body: {} 查询; {action:"regenerate"} 重新生成密钥对
            # (Short IDs 不变, 没有就生成三个); {action:"clear"} 清空
            # 密钥与 Short IDs (下次部署时 install.sh 会重新生成)。
            action = str(body.get("action") or "").strip().lower()
            data = _load_reality_json()
            if action == "regenerate":
                priv, pub = _gen_x25519_keypair()
                if not priv or not pub:
                    self._send_json({"error": "生成密钥失败, 请检查 openssl"}, 500)
                    return
                data["private_key"] = priv
                data["public_key"] = pub
                if not data.get("short_ids") and not data.get("short_id"):
                    _sids = set()
                    while len(_sids) < 3:
                        _sids.add(secrets.token_hex(4))
                    data["short_ids"] = sorted(_sids)
                    data["short_id"] = data["short_ids"][0]
                elif data.get("short_ids"):
                    data["short_id"] = data["short_ids"][0]
                _save_reality_json(data)
            elif action == "clear":
                data["private_key"] = ""
                data["public_key"] = ""
                data["short_id"] = ""
                data["short_ids"] = []
                _save_reality_json(data)
            elif action:
                self._send_json({"error": "未知的操作"}, 400)
                return
            keys = _load_reality_keys()
            self._send_json({"public_key": keys["public_key"],
                             "private_key": keys["private_key"],
                             "short_ids": keys["short_ids"],
                             "has_private_key": bool(keys["private_key"])})
            return

        if path == "/api/outbounds":
            # 出站管理: 整表保存。标签唯一、类型在白名单内才收;
            # 各类型必填字段见 _clean_outbound。部署时经
            # NEXUS_OUTBOUNDS 传给 install.sh 写进核心配置。
            raw_list = body.get("outbounds")
            if not isinstance(raw_list, list):
                self._send_json({"error": "outbounds 须为列表"}, 400)
                return
            cleaned, seen = [], set()
            for o in raw_list:
                if not isinstance(o, dict):
                    continue
                tag = str(o.get("tag") or "").strip()
                if not tag or tag in seen:
                    continue
                seen.add(tag)
                rec, err = _clean_outbound(o)
                if err:
                    self._send_json({"error": err}, 400)
                    return
                cleaned.append(rec)
            if not cleaned:
                self._send_json({"error": "至少保留一条出站"}, 400)
                return
            _save_json(OUTBOUNDS_PATH, {"outbounds": cleaned})
            self._send_json({"ok": True, "outbounds": cleaned})
            return

        if path == "/api/routes":
            # 路由规则: 按保存顺序匹配, 先命中先生效。目标出站须已存在
            # (direct/blocked 永远存在); inline_socks 表示在规则里直
            # 接填外部 SOCKS, 后端自动生成一个命名的 socks 出站。
            raw_list = body.get("routes")
            if not isinstance(raw_list, list):
                self._send_json({"error": "routes 须为列表"}, 400)
                return
            outbounds = load_outbounds()
            tags = {o["tag"] for o in outbounds}
            created = []
            cleaned = []
            for r in raw_list:
                if not isinstance(r, dict):
                    continue
                if "match_type" in r:
                    mt = str(r.get("match_type") or "").strip().lower()
                    if mt not in ROUTE_MATCH_TYPES:
                        self._send_json({"error": "路由匹配类型只能是 domain / domain_suffix / ip_cidr / port"}, 400)
                        return
                else:
                    mt = _ROUTE_TYPE_MAP.get(str(r.get("type") or "").strip().lower())
                    if mt not in ROUTE_MATCH_TYPES:
                        self._send_json({"error": "路由匹配类型只能是 domain / domain_suffix / ip_cidr / port"}, 400)
                        return
                vals = _split_values(r.get("values") if "values" in r else r.get("value"))
                if not vals:
                    continue
                if mt in ("domain", "domain_suffix"):
                    vals = [v[2:] if v.startswith("*.") else v for v in vals]
                    bad = [v for v in vals if not v or any(c.isspace() for c in v) or "/" in v]
                    if bad:
                        self._send_json({"error": f"域名格式不正确: {bad[0]}"}, 400)
                        return
                elif mt == "ip_cidr":
                    for v in vals:
                        try:
                            ipaddress.ip_network(v, strict=False)
                        except Exception:
                            self._send_json({"error": f"IP 段格式不正确: {v}"}, 400)
                            return
                elif mt == "port":
                    for v in vals:
                        if not _valid_port_value(v):
                            self._send_json({"error": f"端口格式不正确: {v} (填 1-65535 或 80-443)"}, 400)
                            return
                target = str(r.get("outbound") or "direct").strip() or "direct"
                inline = r.get("inline_socks")
                if isinstance(inline, dict):
                    addr = str(inline.get("address") or "").strip()
                    port = _to_port(inline.get("port"))
                    if not addr or port is None:
                        self._send_json({"error": "外部 SOCKS 需要填写地址和有效端口"}, 400)
                        return
                    n = 1
                    while f"socks{n}" in tags:
                        n += 1
                    target = f"socks{n}"
                    rec, _err = _clean_outbound({
                        "tag": target, "type": "socks", "address": addr, "port": port,
                        "username": str(inline.get("username") or "").strip(),
                        "password": str(inline.get("password") or ""),
                    })
                    outbounds.append(rec)
                    tags.add(target)
                    created.append(target)
                elif target not in tags:
                    self._send_json({"error": f"路由目标出站不存在: {target}"}, 400)
                    return
                cleaned.append({"match_type": mt, "values": vals, "outbound": target})
            if created:
                _save_json(OUTBOUNDS_PATH, {"outbounds": outbounds})
            _save_json(ROUTES_PATH, {"routes": cleaned})
            self._send_json({"ok": True, "routes": cleaned, "created_outbounds": created})
            return

        if path == "/api/outbounds/test":
            # 全部测试: 对每条出站测一次延迟。blackhole 不测,
            # 直接记拦截; direct / 有地址的测到该地址的延迟。
            wanted = body.get("tags")
            all_out = load_outbounds()
            targets = all_out if not isinstance(wanted, list) else \
                [o for o in all_out if o["tag"] in wanted]

            def _test_one(o):
                if o["type"] == "blackhole":
                    return {"tag": o["tag"], "latency_ms": None, "status": "拦截"}
                url = None
                addr = (o.get("address") or "").strip()
                if addr:
                    host, _p = _split_host_port(addr)
                    url = f"https://{host}/" if host else None
                if not url:
                    url = "https://www.cloudflare.com/"
                out, code = _run(["curl", "-o", "/dev/null", "-s", "-w",
                                  "%{time_total}", "--max-time", "5", url],
                                 timeout=6)
                if code == 0 and out:
                    try:
                        return {"tag": o["tag"],
                                "latency_ms": int(round(float(out.strip()) * 1000)),
                                "status": "可用"}
                    except Exception:
                        pass
                return {"tag": o["tag"], "latency_ms": None, "status": "不可用"}

            with ThreadPoolExecutor(max_workers=min(8, len(targets) or 1)) as ex:
                results = list(ex.map(_test_one, targets))
            self._send_json({"results": results})
            return

        if path == "/api/clear":
            # 清除本次部署 (节点容器 + 配置文件), 面板本身保留, 配置保留可一键重部署
            for name in ("nexus-xray", "nexus-singbox", "nexus-sb-dispatch", "nexus-argo", "nexus-sub", "nexus-web-panel"):
                _run(["docker", "rm", "-f", name], timeout=30)
            _DOCKER_CACHE.update({"t": 0, "states": {}})
            for sub in ("xray", "singbox", "sub", "sb-dispatch", "web"):
                _run(["rm", "-rf", os.path.join(WORKDIR, sub)], timeout=10)
            for f in ("ROUTES.md",):
                try:
                    os.remove(os.path.join(WORKDIR, f))
                except OSError:
                    pass
            _set_deploy_status("idle", message="部署已清除")
            self._send_json({"ok": True, "message": "部署已清除, 配置还在, 可以一键重新部署"})
            return

        self._send_json({"error": "未知路径"}, 404)


def main():
    os.makedirs(PANEL_DIR, exist_ok=True)
    if not os.path.isfile(AUTH_PATH):
        print("[nexus-panel] ERROR: auth.json 不存在 — 面板安装不完整, 请重跑 panel-install.sh")
        raise SystemExit(1)
    if not os.path.isfile(_PANEL_HTML):
        print("[nexus-panel] ERROR: panel.html 不存在 — 面板安装不完整")
        raise SystemExit(1)
    server = ThreadingHTTPServer(("0.0.0.0", PANEL_PORT), Handler)
    print(f"[nexus-panel] 面板已启动: http://0.0.0.0:{PANEL_PORT} (PANEL_DIR={PANEL_DIR})")
    server.serve_forever()


if __name__ == "__main__":
    main()
