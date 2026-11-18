Nexus-Argo Worker 动态订阅
部署时只生成原生订阅，Worker 现场裂变，换优选 IP 不用重新部署 VPS。
不走 8080。 面板 8080 只绑 127.0.0.1，不暴露公网，也不给它建隧道路由。Worker 按环境变量直接生成原生节点，不去拉 VPS 的订阅。
部署 Worker
Dashboard 方式（推荐，不用装 wrangler）：
Cloudflare Dashboard → Workers & Pages → Create → Worker
把 worker.js 的内容粘贴进去，Deploy
Settings → Variables → 加这些变量，值换成你自己的：
变量
例子
NODE_UUID
bf4b0e89-5d86-4918-921f-556cd0d5ae88（建议设为 Secret）
NODE_PREFIX
Nexus
XRAY_DOMAIN
xr.ahstshu.eu.cc
XRAY_CONFIGS
xhttp_h1:443,xhttp_h2:443,xhttp_h3:443
SINGBOX_DOMAIN
sb.ahstshu.eu.cc
SINGBOX_CONFIGS
ws:8443,httpupgrade:8443,grpc:8443
只启用一个核心时，另一个的 DOMAIN / CONFIGS 留空即可。
记下 Worker 的域名，例如 nexus-argo-sub.<你的子域名>.workers.dev
Wrangler 方式：改好 wrangler.toml，跑 wrangler deploy，NODE_UUID 同样建议在 Dashboard 里设为 Secret。
订阅地址（给客户端用）
把 <worker域名> 换成上面第 4 步那个，<uuid> 换成你的 UUID：
原生订阅：https://<worker域名>/sub/raw/<uuid>
动态订阅：https://<worker域名>/sub/merged/<uuid>?addrs=cf.xreak.top,cf.3666888.xyz,vps.cheng2001.top
外部订阅裂变：https://<worker域名>/sub/merged?url=<外部订阅地址 URL 编码>&addrs=cf.xreak.top,cf.3666888.xyz
换优选 IP：只改订阅地址里的 ?addrs=，客户端点更新订阅就行。换 UUID / 域名 / 端口：改 Worker 的环境变量，不用动 VPS。
面板怎么访问
面板只绑了 127.0.0.1:8080，VPS 公网 IP:8080 打不开，用 SSH 端口转发：
ssh -L 8080:127.0.0.1:8080 root@<VPS-IP>
然后浏览器打开 http://127.0.0.1:8080。
本地测试过
Worker 的原生链接生成和裂变逻辑已用 Node 测过：3 条原生 × 2 个优选地址 = 6 条，SNI 保留，ws/httpupgrade 的 alpn 是 http/1.1，grpc 是 h2。
