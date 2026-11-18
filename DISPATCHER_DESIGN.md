# Sing-box 分发层设计（已实现，2026-11-18）

目标：两个核 6 个节点全部能通。Xray 3 条按 SHU 确认“应该都可以通”，
本设计不动 Xray 的单 inbound（union alpn/mode），只修 Sing-box。

实现位置：`install.sh` 的 `render_singbox()` + 分发器生成段 +
`nexus-sb-dispatch` 容器；前端 `web/index.html` 的 Sing-box 链接 path；
后端 `vless_link()` 的 Sing-box path。验证见 TEST_REPORT_2026-11-18.md
（前端 26/26，前后端 merged 18/18，Sing-box 3 inbound，分发器配置
按 install.sh 原文抽取生成后验证通过）。

## 为什么现在只有 grpc 通

`install.sh` 的 `render_singbox()` 只生成一个 inbound，一个 inbound
只能有一种 transport，按 grpc > httpupgrade > ws 取一个。前端却生成
了 3 种链接，httpupgrade / ws 的那 6 条在服务端没有监听。

## 关键结论（查资料后）

1. **ws 和 httpupgrade 在请求头上几乎无法区分**：httpupgrade 客户端
   发的就是和 ws 一样的 Upgrade 请求（`Upgrade: websocket` +
   `Sec-WebSocket-Key`），区别只在 101 之后——ws 按帧传数据，
   httpupgrade 按原始字节传。所以靠头分发行不通，**必须靠路径分发**。
2. **现在前端三个 Sing-box 链接的 path 都是 `/`**，这就是分发不了的根。
   必须改成每种 transport 一个路径：
   - ws → `path=/ws`
   - httpupgrade → `path=/httpupgrade`
   - grpc → `serviceName=grpc`（即路径 `/grpc`，见下）
3. **grpc 的路径就是 serviceName**：nginx 的标准写法是
   `location /<ServiceName> { grpc_pass grpc://127.0.0.1:<内口>; }`，
   serviceName=`grpc` 时就是 `location /grpc`。
4. **风险（需在 VPS 实测确认）**：有实测记录说 Cloudflare 边缘会把
   httpupgrade 的 Upgrade 请求改写成真正的 WebSocket 握手，sing-box
   的 httpupgrade inbound 会拒绝（`real websocket request received`）。
   所以 httpupgrade 走 CF 隧道是否能通，不能只凭配置推断，必须在
   VPS 上逐条连一次。SHU 之前那个 GitHub 项目如果已经跑通过，
   以它的写法为准——这也是等它地址的原因。

## 拟定方案（已按此实现，2026-11-18）

- 后端：Sing-box 改成 3 个 inbound（只给选中的协议建），都只绑
  `127.0.0.1`，内口 = 回源端口 + 偏移：
  ws → +1，httpupgrade → +2，grpc → +3。
  例：回源 8223 时，内口为 8224 / 8225 / 8226。
  `install.sh` 增加校验：回源端口 ≤ 65532，内口不得等于 Xray 回源口
  和 8080，否则报错让用户换回源端口。
- 分发：新增 `nexus-sb-dispatch` 容器（nginx:alpine，host 网络），
  监听回源端口（8223），按路径转发：
  - `location /ws` → `proxy_pass http://127.0.0.1:<ws 内口>`，
    `proxy_http_version 1.1` + Upgrade / Connection 透传
  - `location /httpupgrade` → 同上，转发到 httpupgrade 内口
    （nginx 在 101 之后只做字节透传，不做 ws 成帧）
  - `location /grpc` → `grpc_pass grpc://127.0.0.1:<grpc 内口>`
  - 其他路径 → 404
  隧道路由不变：域名 B → `http://127.0.0.1:<回源端口>`，现在指到分发器。
- 前端：Sing-box 链接的 path/serviceName 按上表改；`vless_link()` 后端
  同步改；SSH 命令不新增变量（内口由回源端口推导）。
- 健康检查：`docker inspect` 巡检名单加 `nexus-sb-dispatch`；
  卸载时一并清理 `nexus-*`（已覆盖）。
- 单核 / 只选 1–2 个 Sing-box 协议时：只建对应的 inbound 和 location，
  其余路径 404，分发器照常起。

## 待验证（VPS 实测清单）

1. ws、httpupgrade、grpc 各 1 条原生链接，客户端逐条连通。
2. 3 个优选地址 × 6 协议 = 18 条动态链接抽查。
3. httpupgrade 若在 CF 下失败，记录失败现象（客户端报错 / 服务端日志），
   再决定是换实现还是降级（只保留 ws+grpc）。
