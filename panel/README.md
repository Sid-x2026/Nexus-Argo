# Nexus-Argo 管理面板 v1 — 精简版

只做两件事：**核心部署 + 自定义订阅地址**。裂变 / 优选生成器不在面板里做
（手动用别的工具完成；生成器方案保留在 `web/sub-generator.html`，
后面可能还要拿出来用）。

## 一键 SSH 安装

VPS 上 root 执行一条命令装面板（默认不需要 Token）：

```bash
bash <(curl -Ls https://raw.githubusercontent.com/Sid-x2026/Nexus-Argo/main/panel-install.sh)
```

装完会问要不要绑定隧道：

- 要绑：提供 Token + 面板域名 + 面板端口，装 cloudflared、绑面板到隧道。
  还差一步：去 Cloudflare Zero Trust 里加一条路由
  `面板域名 -> http://127.0.0.1:面板端口`，加完立刻生效。
  之后走隧道域名进面板，验证后可关闭 SSH 端口 22。
- 只用直连协议：跳过，`http://VPS-IP:2053` 进面板装直连协议就行。

面板只有一个版本，隧道版与端口版功能完全一样，区别只在访问方式。
安装顺序只有一种：都是先装面板，区别只在面板怎么连（绑定隧道 vs IP 直连）。

装完输出面板地址、账号（admin）和随机密码（只显示一次，也存在
`/opt/nexus-argo/panel/password.txt`）。

## 面板里部署

打开面板 → 填设置 → 点「开始部署」，面板在 VPS 上自动完成部署：

- 部署方式：Argo 隧道（默认）/ 直连节点，两种可以一起装。
- 协议各自勾选：隧道 Xray xhttp h1/h2/h3、Sing-box ws/httpupgrade/grpc；
  直连 VLESS+TLS（Xray/Sing-box）、QUIC（Xray）。
- 订阅：订阅域名、订阅端口、自定义订阅路径（默认 `sub`）。
- 部署日志实时显示，部署完做健康检查（容器运行状态 + 订阅接口出数 +
  `?addrs=` 现场裂变检查），通过才算完成。
- 部署是后台任务，浏览器断开不影响；部署时保留面板自身的隧道路由，
  cloudflared 重启导致页面短暂断开时自动重连。
- 重新部署：改配置后点「开始部署」就是覆盖安装（安全的）。
- 清除部署：删节点容器和配置文件，面板本身保留，配置还在，可一键重部署。

## 自定义订阅地址

订阅地址形如 `https://订阅域名/订阅路径/raw/UUID`。订阅路径在部署表单里填，
默认 `sub`；已部署的可以在「订阅地址」页直接改，订阅服务正在运行时
重启生效，不用重新部署。

订阅统一拉取隧道节点（裂变后）和直连节点（原样），一个订阅里全有。

## 文件

| 文件 | 说明 |
|---|---|
| `panel-install.sh` | 一键 SSH 安装 / 卸载面板（只拆面板，不动节点部署） |
| `panel/panel-server.py` | 面板后端（Python 标准库，无依赖） |
| `panel/panel.html` | 面板前端（单文件，无外部依赖） |
| `panel/Dockerfile` | 面板镜像（python:3-alpine + docker-cli） |
| `install.sh` | 部署脚本（面板后台调用，直连模式 + SUB_PATH） |
| `web/sub-server.py` | 订阅服务（SUB_PATH + 直连节点进订阅） |

面板容器用 `--network host` 跑，并挂载 `/var/run/docker.sock` 和
`/opt/nexus-argo`，所以面板就是在 VPS 本地执行部署，和 SSH 里跑命令一样。

## 拆面板

```bash
bash panel-install.sh uninstall   # 只拆面板, 节点部署不动
```

要清节点部署：在面板里点「清除部署」，或跑 `install.sh uninstall`。
