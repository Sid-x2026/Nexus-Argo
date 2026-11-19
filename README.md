# Nexus-Argo

VPS 多协议代理节点自动化部署与统一回源管理：Web 面板 + `install.sh`，Cloudflare Argo Tunnel + Xray-core / Sing-box。

> v3.6 前端已按上传的 `web/index.html` 原样放入本仓库。先读
> `FRONTEND_CONTRACT.md` —— 它记录了前端实际生成的契约和 9 处
> 与介绍文字不一致 / 待修复的地方，后端是按**代码实际行为**对齐的，
> 不是按介绍文字。

## 架构

```
浏览器面板 :8080 (nginx, web/index.html)
  └─ 生成 SSH 一键部署命令（环境变量，见下）
        └─ install.sh (root, Docker + python3)
             ├─ nexus-xray     Xray-core, 统一回源端口 8001
             ├─ nexus-singbox  Sing-box, 3 个 127.0.0.1 内口（回源口+1/+2/+3）
             ├─ nexus-sb-dispatch nginx, 监听 Sing-box 回源端口，按路径分发
             ├─ nexus-argo     cloudflared, Argo Tunnel
             └─ nexus-web-panel nginx:alpine, :8080, 兼 /sub/* 订阅
```

每核心单端口：Xray 协议共用 8001，Sing-box 协议共用 8002，客户端
外部端口默认 443，TLS 由 Cloudflare 终止。

## 整体思路（2026-11-18 确认）

- **双核可同时部署**：Xray 和 Sing-box 各 3 种协议，可单核也可双核。
- **共用一条隧道**：同一个 Argo Token，但两个子域名、两条路由：
  域名 A → `127.0.0.1:8001`（Xray），域名 B → `127.0.0.1:8002`（Sing-box）。
  节点端口（客户端看到的，如 443）可任意设，同核流量统一走到回源端口，
  回源端口就是隧道绑定域名对应的端口——两个端口配合就能正常转发。
- **部署流**：面板填参数 → 生成 SSH 一键命令 → VPS 上直接跑 `install.sh`。
- **优选地址裂变**：优选地址栏填优选 IP / 域名，不填默认用 CF 官方优选域名；
  填 1 个或多个，就替换原生节点的 address，本地裂变出多个节点。
- **订阅分两种**：
  - 原生 / 静态：1 条协议 = 1 个节点。Xray 全选 3 条协议就是 3 个原生节点。
  - 动态：拉原生订阅，再按优选地址裂变。如原生 3 个 × 优选 3 个 = 9 个，
    生成一个新的动态订阅。
  - 静态订阅地址可手动改成别的订阅：动态订阅也可以拿那个外部订阅做底，
    再按优选 IP 裂变——相当于市面上的节点订阅生成器。
- **订阅拉取**：计划把前端部署在 Cloudflare Worker 里，由 Worker 去拉服务器订阅。

## 核心 v1（已完成，2026-11-18）

两处断点已修：
1. **隧道路由**：删掉了无效的本地 `config.yml`。Token 隧道是 Dashboard
   托管的，`install.sh` 会在 `/opt/nexus-argo/ROUTES.md` 里写出要建的两条
   路由（域名 A → `http://127.0.0.1:8001`，域名 B → `http://127.0.0.1:8002`），
   在 Cloudflare Zero Trust 里建好再测隧道。隧道传输默认 `auto`，
   `install.sh` 会显式传 `--protocol auto`。
2. **动态订阅知道优选地址了**：前端 SSH 命令现在传 `NODE_PREFIX` 和
   `OPT_ADDRS`（逗号分隔，默认 `cloudflare.com`），`install.sh` 存到
   `/opt/nexus-argo/sub/opt_addrs.txt`，并用它裂变生成 `merged.txt`；
   动态订阅 URL 也带上了 `?addrs=...`。
   实测：原生 6 条（Xray 3 + Sing-box 3）× 优选 3 个 = 动态 18 条。

前端还修了两处：UUID 生成改用 `crypto.randomUUID()`（有回退），
回源端口冲突时两个端口输入框标红（`install.sh` 仍会直接拦截退出）。

## 面板安装：两种装法

面板用 `panel-install.sh` 一键装到 VPS 上（root 执行），装完在面板里填设置、点部署。两种装法，功能完全一样，区别只在怎么进面板：

### ① SSH 面板安装（交互，默认）

```bash
bash <(curl -Ls https://raw.githubusercontent.com/Sid-x2026/Nexus-Argo/main/panel-install.sh)
```

一步步确认：装完问要不要绑定隧道，要绑就提供 Token + 面板域名 + 端口（端口须与 Zero Trust 路由端口一致），装完用隧道域名进面板；不绑就直接用 `http://<VPS-IP>:<端口>` 进面板。默认账号 `admin`，默认密码 `admin`（临时默认，登录后尽快改密码）。

### ② Argo 绑定安装（变量一键）

Token、域名、端口直接写进命令里，改变量就直接装完出结果，不做二次确认，省事傻瓜一点。参数用双引号框起来，只在引号里填（下面是占位值，不要填真实域名）：

```bash
ARGO_TOKEN="your-token" PANEL_DOMAIN="your-panel-domain" PANEL_PORT="2053" \
  bash <(curl -Ls https://raw.githubusercontent.com/Sid-x2026/Nexus-Argo/main/panel-install.sh)
```

| 变量 | 说明 | 默认 |
|---|---|---|
| `ARGO_TOKEN` | Cloudflare Argo Tunnel Token | 无，不写就是直连 / IP 进面板 |
| `PANEL_DOMAIN` | 隧道面板域名（Zero Trust 里路由到面板端口） | 无，不写就是直连 / IP 进面板 |
| `PANEL_PORT` | 面板端口，须与 Zero Trust 路由端口一致 | `2053` |

只给其中一个变量（只有 Token 没有域名，或反过来）时，按交互安装走，会再问一遍。变量安装不做二次确认：命令里写什么就按什么装，写错了改命令重跑。装完记得去 Cloudflare Zero Trust 里加路由：`PANEL_DOMAIN -> http://127.0.0.1:<PANEL_PORT>`。

### 卸载 / 一键清理

- 只拆面板（节点不动）：`bash panel-install.sh uninstall`
- 只清节点（面板保留）：`bash /opt/nexus-argo/install.sh uninstall`，或面板里点「清除部署」
- 全部清掉，从零重来（面板 + 节点 + `/opt/nexus-argo`）：

```bash
bash <(curl -Ls https://raw.githubusercontent.com/Sid-x2026/Nexus-Argo/main/cleanup.sh)
```

以上三种都不动 Cloudflare 隧道 / Token / 域名 / Zero Trust 路由，云端配置重装时直接复用。清完可直接重跑 panel-install.sh 重新安装面板。

## 快速开始

1. 在面板填 UUID、域名、Argo Token、优选地址，复制 SSH 命令；
   或直接在 VPS 上运行（先替换变量）：

```bash
NODE_UUID="<uuid>" ARGO_TOKEN="<token>" \
ENABLE_XRAY="true" XRAY_PORT="8001" XRAY_DOMAIN="xray.yourdomain.com" \
XRAY_CONFIGS="xhttp_h1:443,xhttp_h2:443,xhttp_h3:443" \
ENABLE_SINGBOX="true" SINGBOX_PORT="8002" SINGBOX_DOMAIN="singbox.yourdomain.com" \
SINGBOX_CONFIGS="httpupgrade:443,ws:443,grpc:443" \
NODE_PREFIX="优选JP" OPT_ADDRS="cloudflare.com" TUNNEL_PROTOCOL="auto" \
bash <(curl -Ls https://raw.githubusercontent.com/Sid-x2026/Nexus-Argo/main/install.sh)
```

2. 面板：`http://<VPS-IP>:8080` —— **不要直接暴露公网**，见安全一节。
3. 卸载：

```bash
bash <(curl -Ls https://raw.githubusercontent.com/Sid-x2026/Nexus-Argo/main/install.sh) uninstall
```

## 文件

| 文件 | 说明 |
|---|---|
| `install.sh` | 安装 / 卸载、校验、Python JSON 渲染、拉起容器、健康巡检 |
| `web/index.html` | 前端 v3.6，已打核心补丁（见上） |
| `web/nginx.conf` | 面板 + `/<uuid>/sub/merged`、`/ <uuid>/sub/raw`（UUID 在前） |
| `FRONTEND_CONTRACT.md` | 前端契约与 9 处不一致 / 待修复项 |

## 安全

- 面板无登录：8080 只放内网 / localhost，或用 ufw / Cloudflare Access 限制。
- 部署前替换默认 UUID，不要用 HTML 里的示例 UUID。
- `install.sh` 会校验端口 1–65535、Xray/Sing-box 回源端口不冲突、
  不等于 8080、UUID 格式、至少启用一个核心。
- 健康巡检用 `docker inspect .State.Running`，失败时输出最后 20 行日志并退出。

## 已知限制（v1）

- **决策 2026-11-18：纯 Argo CF，不直连。** 只保留 Xray 的 3 个走隧道协议
  `xhttp_h1 / xhttp_h2 / xhttp_h3`，放弃第 4 个 `xhttp quic h3 udp` 直连协议。
  隧道传输用 cloudflared 默认 `auto`（先 QUIC 7844 UDP，失败回落 HTTP/2 443 TCP），
  不强制 `--protocol`。这不影响客户端的 h1/h2/h3——边缘终结 TLS 后再经隧道回源。

- **Sing-box 分发层（2026-11-18 已实现）**：一个 Sing-box inbound 只能有一种
  transport，所以后端给每个协议一个 127.0.0.1 内口（回源口+1 ws、+2 httpupgrade、
  +3 grpc），前面加 `nexus-sb-dispatch`（nginx）监听回源端口，按路径分发：
  `/ws` → ws 内口，`/httpupgrade` → httpupgrade 内口，`/grpc` → grpc 内口。
  前端 Sing-box 链接的 path 也已同步改成 `/ws`、`/httpupgrade`、`serviceName=grpc`。
  隧道路由不变，仍指到回源端口。回源端口须 ≤ 65532，内口不得撞 Xray 回源口 / 8080。
  详见 `DISPATCHER_DESIGN.md`。httpupgrade 走 CF 隧道是否能通，仍需 VPS 实测确认。

- 前端所有 Xray 链接的 path 都是 `/`，Xray 是每核心一个统一 inbound（union alpn/mode），
  SHU 2026-11-18 确认 Xray 三协议应该都可以通，待 VPS 实测逐条确认。
- 订阅 URL 里的 `<uuid>` 段 nginx 不做校验；动态订阅的 `?addrs=` 参数
  目前由 nginx 忽略，裂变列表以部署时 `OPT_ADDRS` 为准（Worker 版再按 URL 参数实时裂变）。
- 直连 VLESS+QUIC 已于 2026-11-19 删除：最新 Xray 26.11.9 移除了独立 QUIC 传输；HTTP/2 系传输（gRPC / TLS）为替代。
