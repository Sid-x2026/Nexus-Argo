#!/usr/bin/env python3
# Nexus-Argo 管理面板 v1 — 精简版 (2026-11-19)
#
# 只做两件事: 核心部署 + 自定义订阅地址。裂变/优选生成器不在面板里,
# 手动用别的工具完成 (生成器方案保留在 web/sub-generator.html)。
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
#   POST /api/clear               清除本次部署 (节点容器 + 配置文件, 面板本身保留)
#
# 安全边界: 面板用账号密码登录 (默认 admin, 密码安装时随机生成,
# 只存 salt+sha256); 部署是后台任务, 浏览器断开不影响; 部署时
# 保留面板自身的隧道路由, cloudflared 重启导致页面短暂断开时
# 前端自动重连。订阅地址本身就是凭证, 不要发给不该拿到节点的人。

import hashlib
import json
import os
import re
import secrets
import subprocess
import threading
import time
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PANEL_DIR = os.environ.get("PANEL_DIR", "/opt/nexus-argo/panel").rstrip("/")
WORKDIR = os.environ.get("WORKDIR", "/opt/nexus-argo").rstrip("/")
PANEL_PORT = int(os.environ.get("PANEL_PORT", "2053") or 2053)
INSTALL_SH = os.path.join(WORKDIR, "install.sh")
_HERE = os.path.dirname(os.path.abspath(__file__))
_PANEL_HTML = os.path.join(_HERE, "panel.html")

CONFIG_PATH = os.path.join(PANEL_DIR, "config.json")
AUTH_PATH = os.path.join(PANEL_DIR, "auth.json")
LOG_PATH = os.path.join(PANEL_DIR, "deploy.log")
STATUS_PATH = os.path.join(PANEL_DIR, "deploy-status.json")

UUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
SUB_PATH_RE = re.compile(r"^[A-Za-z0-9._-]{1,32}$")
RESERVED_SUB_PATH = {"api", "config.json", "help.txt"}
DOMAIN_RE = re.compile(r"^[A-Za-z0-9._-]+$")

# 部署过的服务容器 (健康检查用, 和 install.sh 里起的名字一致)
SERVICES = ["nexus-argo", "nexus-xray", "nexus-singbox", "nexus-sb-dispatch", "nexus-sub", "nexus-web-panel"]

DEFAULT_CONFIG = {
    "node_uuid": "",
    "node_prefix": "Nexus",
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
    "sub_domain": "sub.ahstshu.eu.cc",
    "sub_port": 18080,
    "sub_path": "sub",
    "enable_direct": False,
    "direct_domain": "",
    "direct_xray_tls": True,
    "direct_xray_tls_port": 443,
    "direct_xray_quic": False,
    "direct_xray_quic_port": 443,
    "direct_sb_tls": True,
    "direct_sb_tls_port": 8443,
}

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
    for k, v in saved.items():
        if k in cfg:
            cfg[k] = v
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
    """返回 (cleaned_config, [error, ...])。cleaned 是补全默认后的配置。"""
    cfg = dict(DEFAULT_CONFIG)
    if isinstance(raw, dict):
        for k, v in raw.items():
            if k in cfg:
                cfg[k] = v
    errors = []

    cfg["node_uuid"] = str(cfg.get("node_uuid") or "").strip()
    if not UUID_RE.match(cfg["node_uuid"]):
        errors.append("节点 UUID 格式不正确, 请点「安全生成」重新生成一个")

    cfg["node_prefix"] = str(cfg.get("node_prefix") or "Nexus").strip() or "Nexus"
    cfg["argo_token"] = str(cfg.get("argo_token") or "").strip()
    cfg["tunnel_protocol"] = str(cfg.get("tunnel_protocol") or "auto").strip().lower()
    if cfg["tunnel_protocol"] not in ("auto", "http2", "quic"):
        errors.append("隧道传输只能是 auto / http2 / quic")

    cfg["enable_xray"] = _as_bool(cfg.get("enable_xray"), True)
    cfg["enable_singbox"] = _as_bool(cfg.get("enable_singbox"), True)
    cfg["xray_port"] = _as_int(cfg.get("xray_port"), 8001)
    cfg["singbox_port"] = _as_int(cfg.get("singbox_port"), 8002)
    cfg["sub_port"] = _as_int(cfg.get("sub_port"), 18080)
    for name in ("xray_port", "singbox_port", "sub_port"):
        if not _port_ok(cfg[name]):
            errors.append(f"{name} 必须是 1-65535 的端口")
    if cfg["xray_port"] == cfg["singbox_port"]:
        errors.append("Xray 与 Sing-box 回源端口冲突, 请改成不一样的")
    if cfg["xray_port"] == 8080 or cfg["singbox_port"] == 8080:
        errors.append("回源端口不能是 8080 (面板端口)")
    if cfg["sub_port"] in (8080, cfg["xray_port"], cfg["singbox_port"]):
        errors.append("订阅端口不能是 8080, 也不能和回源端口相同")
    if cfg["enable_singbox"] and cfg["singbox_port"] > 65532:
        errors.append("Sing-box 回源端口不能大于 65532 (分发器要用 +1/+2/+3)")
    if cfg["enable_singbox"]:
        for off in (1, 2, 3):
            if cfg["singbox_port"] + off in (cfg["xray_port"], 8080, cfg["sub_port"]):
                errors.append(f"Sing-box 分发器内部端口 {cfg['singbox_port'] + off} 与其它端口冲突")
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
    cfg["opt_addrs"] = [str(a).strip() for a in addrs if str(a).strip()][:20] or ["cloudflare.com"]

    cfg["sub_path"] = str(cfg.get("sub_path") or "sub").strip()
    if not SUB_PATH_RE.match(cfg["sub_path"]) or cfg["sub_path"] in RESERVED_SUB_PATH:
        errors.append("订阅路径只能是字母/数字/._- (1-32 个字符), 不能是 api / config.json / help.txt")

    cfg["enable_direct"] = _as_bool(cfg.get("enable_direct"), False)
    cfg["direct_domain"] = str(cfg.get("direct_domain") or "").strip()
    cfg["direct_xray_tls"] = _as_bool(cfg.get("direct_xray_tls"), True)
    cfg["direct_xray_tls_port"] = _as_int(cfg.get("direct_xray_tls_port"), 443)
    cfg["direct_xray_quic"] = _as_bool(cfg.get("direct_xray_quic"), False)
    cfg["direct_xray_quic_port"] = _as_int(cfg.get("direct_xray_quic_port"), 443)
    cfg["direct_sb_tls"] = _as_bool(cfg.get("direct_sb_tls"), True)
    cfg["direct_sb_tls_port"] = _as_int(cfg.get("direct_sb_tls_port"), 8443)
    for name in ("direct_xray_tls_port", "direct_xray_quic_port", "direct_sb_tls_port"):
        if not _port_ok(cfg[name]):
            errors.append(f"{name} 必须是 1-65535 的端口")
    if cfg["enable_direct"]:
        if not (cfg["direct_xray_tls"] or cfg["direct_xray_quic"] or cfg["direct_sb_tls"]):
            errors.append("启用直连时至少勾选一个直连协议")
        if cfg["direct_xray_tls"] and cfg["direct_xray_tls_port"] in (8080, cfg["xray_port"], cfg["singbox_port"], cfg["sub_port"]):
            errors.append("直连 Xray TLS 端口与其它端口冲突")
        if cfg["direct_sb_tls"] and cfg["direct_sb_tls_port"] in (8080, cfg["xray_port"], cfg["singbox_port"], cfg["sub_port"], cfg["direct_xray_tls_port"]):
            errors.append("直连 Sing-box TLS 端口与其它端口冲突")
        # QUIC 走 UDP, 端口号可以和 TCP 端口相同, 只校验范围

    if not cfg["enable_xray"] and not cfg["enable_singbox"] and not cfg["enable_direct"]:
        errors.append("至少启用一个核心或直连")
    return cfg, errors


def config_to_env(cfg):
    """部署配置 -> install.sh 的环境变量。"""
    def _cfgs(m):
        return ",".join(f"{p}:{port}" for p, port in m.items())
    env = {
        "NODE_UUID": cfg["node_uuid"],
        "NODE_PREFIX": cfg["node_prefix"],
        "ARGO_TOKEN": cfg["argo_token"],
        "TUNNEL_PROTOCOL": cfg["tunnel_protocol"],
        "ENABLE_XRAY": "true" if cfg["enable_xray"] else "false",
        "XRAY_PORT": str(cfg["xray_port"]),
        "XRAY_DOMAIN": cfg["xray_domain"],
        "XRAY_CONFIGS": _cfgs(cfg["xray_protocols"]),
        "ENABLE_SINGBOX": "true" if cfg["enable_singbox"] else "false",
        "SINGBOX_PORT": str(cfg["singbox_port"]),
        "SINGBOX_DOMAIN": cfg["singbox_domain"],
        "SINGBOX_CONFIGS": _cfgs(cfg["singbox_protocols"]),
        "OPT_ADDRS": ",".join(cfg["opt_addrs"]),
        "SUB_DOMAIN": cfg["sub_domain"],
        "SUB_PORT": str(cfg["sub_port"]),
        "SUB_PATH": cfg["sub_path"],
        "ENABLE_DIRECT": "true" if cfg["enable_direct"] else "false",
        "DIRECT_DOMAIN": cfg["direct_domain"],
        "DIRECT_XRAY_TLS": "true" if cfg["direct_xray_tls"] else "false",
        "DIRECT_XRAY_TLS_PORT": str(cfg["direct_xray_tls_port"]),
        "DIRECT_XRAY_QUIC": "true" if cfg["direct_xray_quic"] else "false",
        "DIRECT_XRAY_QUIC_PORT": str(cfg["direct_xray_quic_port"]),
        "DIRECT_SB_TLS": "true" if cfg["direct_sb_tls"] else "false",
        "DIRECT_SB_TLS_PORT": str(cfg["direct_sb_tls_port"]),
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


def _docker_running(name):
    out, code = _run(["docker", "inspect", "-f", "{{.State.Running}}", name], timeout=5)
    if code != 0:
        return "not_installed"
    return "running" if out.strip() == "true" else "stopped"


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


def subscription_urls(cfg):
    """订阅地址 (按当前配置算, 改 sub_path 后实时变)。"""
    uuid = cfg["node_uuid"]
    sub_path = cfg.get("sub_path") or "sub"
    base = None
    if cfg.get("sub_domain"):
        base = f"https://{cfg['sub_domain']}/{sub_path}"
    out = {
        "sub_domain": cfg.get("sub_domain") or "",
        "sub_path": sub_path,
        "sub_port": cfg.get("sub_port") or 18080,
        "raw_url": f"{base}/raw/{uuid}" if base and uuid else "",
        "merged_url": f"{base}/merged/{uuid}" if base and uuid else "",
        "generator_url": f"https://{cfg['sub_domain']}/" if cfg.get("sub_domain") else "",
        "raw_count": _count_lines(os.path.join(WORKDIR, "sub", "raw.txt")),
        "merged_count": _count_lines(os.path.join(WORKDIR, "sub", "merged.txt")),
    }
    return out


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
    for k in ("NODE_UUID", "NODE_PREFIX", "XRAY_DOMAIN", "XRAY_CONFIGS",
              "SINGBOX_DOMAIN", "SINGBOX_CONFIGS", "ENABLE_XRAY", "ENABLE_SINGBOX",
              "OPT_ADDRS", "SUB_PORT", "SUB_PATH", "ENABLE_DIRECT", "DIRECT_DOMAIN",
              "DIRECT_XRAY_TLS", "DIRECT_XRAY_TLS_PORT", "DIRECT_XRAY_QUIC",
              "DIRECT_XRAY_QUIC_PORT", "DIRECT_SB_TLS", "DIRECT_SB_TLS_PORT"):
        run += ["-e", f"{k}={env[k]}"]
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

        if path in ("/", "/index.html"):
            self._send_file(_PANEL_HTML, "text/html; charset=utf-8")
            return
        if path == "/api/status":
            if not self._need_auth():
                return
            cfg = load_config()
            services = [{"name": n, "state": _docker_running(n)} for n in SERVICES]
            st = deploy_status()
            sub = subscription_urls(cfg)
            self._send_json({
                "deployed": st.get("state") == "ok" or os.path.exists(os.path.join(WORKDIR, "sub", "raw.txt")),
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
            self._send_json(subscription_urls(load_config()))
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
            _save_json(CONFIG_PATH, cfg)
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

        if path == "/api/clear":
            # 清除本次部署 (节点容器 + 配置文件), 面板本身保留, 配置保留可一键重部署
            for name in ("nexus-xray", "nexus-singbox", "nexus-sb-dispatch", "nexus-argo", "nexus-sub", "nexus-web-panel"):
                _run(["docker", "rm", "-f", name], timeout=30)
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
