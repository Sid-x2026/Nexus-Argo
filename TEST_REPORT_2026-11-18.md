# 前端 + 后端联调测试报告 — 2026-11-18

测试参数（按截图里的端口值）：UUID `<test-uuid>`，
前缀 `优选JP`，Xray 域名 `xray.example.com` 端口 8222，
Sing-box 域名 `singbox.example.com` 端口 8223，
优选地址 `1.1.1.1, 2.2.2.2, 3.3.3.3`，Token 用占位值（未用真实 Token）。
（报告里的域名已换成 example.com 占位，实测时用的是截图里的测试域名。）
测试方法：jsdom 真实加载 `web/index.html` 的内联 JS（`innerText` 做了
textContent 别名，因为 jsdom 本身不支持 innerText，属测试工具限制，
不是前端问题）+ 单独抽取 `install.sh` 里的 Python 渲染器跑同一组参数。

## 通过（26/26 前端断言，分发层实现后）

- 默认值：SSH `XRAY_PORT=8001 / SINGBOX_PORT=8002`，`OPT_ADDRS=cloudflare.com`，各 3 节点。
- 截图值：SSH 含 `XRAY_PORT="8222" / SINGBOX_PORT="8223" / OPT_ADDRS="1.1.1.1,2.2.2.2,3.3.3.3" / NODE_PREFIX="优选JP"`。
- 端口同步：下面改 8222/8223，上面回源端口显示同步变成 8222/8223。
- 裂变：Xray 3 协议 × 3 优选 = 9 节点，Sing-box 同样 9 节点；链接地址已换成优选 IP，`sni` 保留隧道域名。
- 动态订阅：`https://xray.example.com/sub/merged/<uuid>?addrs=1.1.1.1%2C2.2.2.2%2C3.3.3.3`；
  静态订阅：`https://xray.example.com/sub/raw/<uuid>`。
- 端口冲突：两核都设 8222 时，两个输入框边框都变红（`#ef4444`）。
- 单核：关掉 Sing-box 后 SSH 为 `ENABLE_SINGBOX="false"`，Sing-box 区显示“未启用”，Xray 仍 3 节点。
- UUID：`generateUUID()` 生成标准 UUID v4（crypto.randomUUID）。
- 端口校验：99999 钳到 65535，0 钳到 1。

## 后端一致性

- Xray 配置监听 8222，Sing-box 监听 8223。
- 原生订阅 `sub/raw.txt` 6 条，动态订阅 `sub/merged.txt` 18 条（6 × 3）。
- **前端节点池 == 后端 merged.txt，18/18 逐条相同**（排序后 diff 无差异）。

## 失败 / 已知问题（测试确认，不是推测）

1. **Sing-box 只有 grpc 能通（严重）——已修，2026-11-18**：原渲染器一个 inbound
   只能选一种 transport，按 grpc > httpupgrade > ws 优先级只取了 grpc。
   现已改成 3 个 inbound（127.0.0.1，回源口+1 ws /ws、+2 httpupgrade /httpupgrade、
   +3 grpc /grpc）+ `nexus-sb-dispatch` nginx 按路径分发，前端和后端链接的
   path 已同步。本地验证：Sing-box 配置 3 inbound、端口与 transport 全对，
   分发器配置按 `install.sh` 原文抽取生成后验证通过（全选和只选 ws 两种情况），
   前后端 merged 18/18 相同。**真正通不通仍需 VPS 实测**，尤其 httpupgrade
   走 CF 隧道那条（见下）。
2. **httpupgrade 走 CF 隧道是剩余风险**：有实测记录说 Cloudflare 边缘会把
   httpupgrade 的 Upgrade 请求改写成真正的 WebSocket，sing-box 的 httpupgrade
   inbound 会拒绝。本地配置验证无法排除这一点，必须在 VPS 上逐条连一次。
3. Xray 侧是 1 个统一 inbound，`mode=packet-up, alpn=[h2,h3,http/1.1]`，
   三个前端链接只在 alpn/mode 参数上不同——这符合“统一回源”设计，
   SHU 确认应该都可以通，是否能同时接 h1/h2/h3 需在 VPS 实测时用客户端逐条连一次确认。

## 本次顺手修的

- `install.sh` 里一句过时注释（说 SSH 不传 OPT_ADDRS）已改成现在的情况，
  并注明了 18/18 一致的验证结果。`bash -n install.sh` 仍通过。
- 分发层实现后，前端测试从 24 项扩到 26 项（新增 ws path /ws、httpupgrade
  path /httpupgrade 断言），全部通过。
