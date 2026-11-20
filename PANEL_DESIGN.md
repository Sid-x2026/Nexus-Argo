# Nexus-Argo 面板 — 界面功能逻辑框架（v1）

整理：2026-11-19。先定框架，再做图、再做功能。

**第 1 步只做精简版**：面板只保留核心部署功能 + 自定义订阅地址；裂变 / 优选生成器不在面板里做（手动用别的工具完成）。订阅生成器方案保留（本地文件 `web/sub-generator.html`、`web/sub-server.py` 及 `SUBSCRIPTION_V3.md` 完整保留，预览也保留），后面可能还要拿出来用。一键 SSH 面板部署：一条命令装面板；面板里填好设置点部署，自动完成部署，带安装日志 + 部署检查。

## 总体流程

- 首次：登录 → 总览（未部署，引导去部署）→ 部署安装（一键部署）→ 订阅中心（拿订阅、改订阅地址）→ 日常使用。
- 日常：总览看状态 → 节点管理查节点 / 复制 → 订阅中心换优选地址、复制订阅。
- 改配置：部署页改配置 → 重新部署（覆盖安装）→ 节点和订阅自动更新。
- 数据：部署配置是唯一来源（Token、UUID、域名、端口、部署方式、协议选择、优选地址、订阅后缀）；节点和订阅都由它派生，不单独存。
- 安全终态：面板装好后，把面板绑到隧道（panel 子域名 → 面板端口），验证隧道访问可用后关闭 SSH 端口 22；此后面板、节点、订阅全走隧道，VPS 对外零开放端口；关 SSH 后仅剩服务商控制台作为后备。

## 安装流程（先装面板，再选装隧道）

面板只有一个版本：隧道版与端口版功能完全一样，区别只在访问方式（端口直连 vs 隧道域名），不在功能。安装顺序只有一种：都是先装面板；区别只在面板怎么连：绑定隧道（走隧道域名进）vs IP 直连（走 VPS IP:端口进）。隧道绑定可以在装面板时一起做，也可以在装完后补做。

1. 一条命令装面板（默认，不需要 Token），装完面板就能用。
2. 装完提示：是否安装隧道版（绑定面板到隧道）？
   - 是：提供 Token + 路由域名 + 端口号 → 装 cloudflared、绑面板到隧道（路由在 Zero Trust 里加一步，加完立即生效）。
   - 否（只用直连协议）：跳过，直接用面板（端口访问）装直连协议。
3. 想装 Argo 就在 SSH 里直接装隧道，装完走隧道域名进面板。
4. 进面板部署节点（隧道 / 直连）；部署时面板列出还需要在 Zero Trust 里加的路由（xr、sb、sub）。
5. 验证隧道域名能进面板、节点能用，然后关 SSH 端口 22。
6. 之后全部走隧道域名进面板部署、管理。

## 第 1 步实现（2026-11-19，已本地验证，未上 VPS 实测）

文件：`panel-install.sh`（一键 SSH 安装 / 卸载面板）、`panel/panel-server.py`
（后端，Python 标准库）、`panel/panel.html`（前端，单文件，无外部依赖）、
`panel/Dockerfile`、`panel/README.md`；另改 `install.sh`（直连模式 +
SUB_PATH + 卸载保留面板）和 `web/sub-server.py`（SUB_PATH + 直连节点进订阅）。

关键设计：面板容器 `--network host` + 挂 `/var/run/docker.sock` 和
`/opt/nexus-argo`，在 VPS 本地执行部署，和 SSH 里跑命令一样；部署是后台
任务，浏览器断开不影响；部署配置是唯一来源（`panel/config.json`）；
订阅路径默认 `sub`，改路径只重启 nexus-sub，不用重部署；直连用自签名证书
（客户端 allowInsecure=1），v1 时 QUIC 走 UDP 可与 TLS 同端口（直连 QUIC 已于 2026-11-19 删除）；`install.sh
uninstall` 保留面板（容器 + `panel/` 目录）。

本地验证：bash -n / py_compile / node --check 全过；install.sh 渲染块
三组实测（隧道+直连 9 原生 / 21 动态、纯隧道 6/18、纯直连 2/2）；
sub-server 实测（SUB_PATH=mysub：raw 9、merged 2 addrs 15、singbox 9、
clash 14 个代理且无 QUIC、旧 /sub/ 路径 404、错 UUID 404）；
panel-server 实测（登录 401/200、配置校验 400、部署 200、重复部署 409、
无 Docker 时部署失败且日志有原因、改 sub_path 生效、退出后 401）。
未验证：VPS 上真实部署（Docker 构建、容器启动、隧道、节点连通、改路径后
nexus-sub 重启）—— 需 SHU 在 VPS 上跑一次。

## 分步实施（一步步来，后续功能逐步加）

- 第 1 步（现在做）：精简面板——只保留核心部署功能 + 自定义订阅地址；一键 SSH 面板部署（一条命令装面板）；面板里填好设置点部署，自动完成部署，带安装日志 + 部署检查。裂变 / 优选生成器不在面板里做，手动用别的工具完成。
- 以后：节点管理、订阅中心（完整版）、设置（隧道绑定、关 SSH、备份 / 恢复、卸载）一步步加。

## 1. 登录页

- 账号 + 密码登录；面板首次部署时设定（默认账号 admin，密码随机生成，部署命令输出里给）。
- 未登录访问任何页都跳回登录页；登出回登录页。

## 2. 总览

- 状态卡：Argo 隧道、Xray、Sing-box、订阅服务（运行中 / 未安装 / 异常），点卡片跳到对应页。
- 数字：节点数（隧道 X 个 / 直连 Y 个）、订阅数。
- 快捷入口：去部署、订阅生成器、节点管理。
- 未部署时：显示未部署，引导去部署页。

## 3. 部署安装（核心页）

- 部署方式：Argo 隧道 / 直连节点，可多选（两种都要也行），默认 Argo。
- 协议（各自独立勾选）：隧道：Xray xhttp h1 / h2 / h3、Sing-box ws / httpupgrade / grpc；直连：VLESS + TLS 等（直连 QUIC 已于 2026-11-19 删除，最新清单见“协议清单”一节）。
- 连接信息：Argo Token（隧道需要）、UUID（一键生成）、Xray 域名 / 回源端口 / 客户端端口、Sing-box 域名 / 回源端口 / 客户端端口；直连：VPS 公网 IP 或域名、直连端口。
- 订阅设置：订阅域名、订阅端口、自定义订阅地址后缀。
- 优选地址：默认优选地址（可空，用默认）。
- 点「开始部署」：校验（必填、端口冲突、UUID 格式）→ 面板在 VPS 上执行安装 → 步骤进度 + 日志（拉取镜像 → 渲染配置 → 启动容器 → 健康检查）→ 完成：各服务状态 + 原生订阅地址 + 去订阅生成器的入口。部署在后台任务里跑，跟浏览器连面板的方式无关（走隧道连面板也能部署）；部署时保留面板自身的隧道路由，cloudflared 重启导致页面短暂断开时自动重连，部署不中断。
- 已部署后：本页显示当前配置，提供「重新部署」（改配置后覆盖安装）和「卸载」。

## 4. 节点管理

- 列表：全部已部署节点（隧道 + 直连），筛选：模式（隧道 / 直连）、核心（Xray / Sing-box）、协议。
- 每行：名称、协议、地址、端口、模式、状态（状态 = 所属服务运行状态；单节点延迟测试以后再说）。
- 操作：复制单个节点链接。改节点（增减协议、换优选地址）回部署页改完重新部署，不在这里直接改。

## 5. 订阅中心

- 订阅列表：原生订阅（部署时定好，只展示，不可改）、裂变订阅（通用 / V2Ray / Sing-box / Clash，各带复制）。
- 自定义订阅地址：改后缀，所有订阅地址实时更新显示，改后缀不用重新部署。
- 逻辑：订阅统一拉取隧道节点（裂变后）+ 直连节点（原样），一个订阅里全有。
- 第 1 步只做自定义订阅地址；裂变 / 优选生成器不在面板里做（手动用别的工具完成）。

## 6. 设置

- 改面板账号密码、改面板端口（改端口需重启面板）。
- 面板访问：隧道绑定（panel 子域名 → 面板端口，现有隧道加一条路由，或单独再建一个隧道）+ 关闭 SSH 端口 22（面板里做，防火墙规则；必须先验证隧道域名能打开面板并登录，验证通过才允许关；关后仅剩服务商控制台作为后备）。
- 配置备份 / 恢复（导出 / 导入部署配置）。
- 卸载：停掉并删除全部 Nexus-Argo 容器和文件。
- 关于：版本。

## 第 2 步：全协议（2026-11-19）

### 部署页新布局（2026-11-19，已实现）

部署页按功能分块重排为 5 步：①部署方式 → ②协议选择 → ③配置参数（直连在前、Argo 在后）→ ④节点与订阅 → ⑤开始部署。协议行里不放额外参数，参数统一挪到③；③里只显示已选协议需要的参数，直连在前、Argo 在后，部署时直接调取。只显示当前模式相关的区（不选隧道收起隧道区，不选直连收起直连区和证书区）；证书区只在直连勾选了需要证书的协议时才出现；Reality SNI 参数设置只在勾选 Reality 协议时才显示（见 Reality 一节）。

实施状态（以 panel.html / panel-server.py / install.sh / web/sub-server.py 实际代码为准）：
- ①部署方式：已实现（Argo 隧道 / 直连节点，可多选，默认 Argo）。
- ②协议选择：已实现——隧道协议与直连协议在同一处，Argo 组 / 直连组左右并排（宽屏两栏、窄屏上下，`.proto-wrap` flex，≤900px 上下堆叠），可折叠、带已选计数，只勾一种方式时另一组整个隐藏；每条协议行只留勾选 + 协议名 + 端口。
- ③配置参数：已实现——只显示已选协议需要的参数，直连配置在前、隧道配置在后，部署时直接调取；直连区含直连域名、直连节点前缀、证书配置、Reality 配置，隧道区含 Token、UUID、隧道节点前缀、Xray / Sing-box 子域名与回源端口、订阅子域名与端口、优选地址（单行，默认 cloudflare.com）；两块用不同样式区分（直连绿框、隧道蓝紫框），直连区内再分 4 张安全配置子卡（见“第 3 步”）。
- ④节点与订阅：已实现——单个 UUID + 订阅路径 + 优选地址；节点名称前缀已移到③里按隧道 / 直连分别设置。
- ⑤开始部署：已实现——按钮与部署日志、检查结果；按钮顺序见下。

### 部署页后续调整（2026-11-19，用户反馈）
- **③ 配置参数里直连在前、隧道在后**：两个方式都选时，直连配置在前、隧道配置在后。两块用不同样式区分：直连区是绿框（绿色左框线、绿色标题），隧道区是蓝紫框（蓝紫左框线、蓝紫标题），一眼能分开。
- **② 协议选择是同一处**：隧道协议与直连协议放在同一个协议选择区里，各自有 Argo 组 / 直连组小标题（可折叠、带已选计数），每条协议后面都有自己的端口设置。只勾一种方式时，另一组的协议整个隐藏，不显示；两组都选时，一处勾完所有协议。
- **UUID 只有一个**：不给每个方式各设一个 UUID。同一个 UUID 就是 Xray、Sing-box、隧道和直连全部协议的客户端身份，订阅地址也只用这一个 UUID（`/sub/raw/<uuid>`）；分开设会把订阅拆成两份、多一倍管理，UUID 本身是标识不是密码，分开也没有安全收益。UUID 字段 + 安全生成按钮在步骤 ③ 的 Argo 安全配置卡片里（一个 UUID 通用于隧道和直连全部协议），步骤 ④ 只剩订阅路径与优选订阅生成器入口。
- **总览 Argo 隧道状态修复**：之前总览只查节点隧道容器 `nexus-argo`，面板自身的隧道容器叫 `nexus-panel-argo`，所以用户明明已通过隧道进面板，总览仍显示灰色。修复：后端 `_argo_tunnel_state()` 取两者任一在跑即运行中（`nexus-argo` 或 `nexus-panel-argo`），总览的 Argo 隧道卡片用这个合并值；服务列表里两者都列出。总览登录时自动刷新一次（`showApp` 调用 `loadStatus`），15 秒轮询一次，刷新状态按钮已加粗显示并带“上次刷新”时间；前端按服务单独取值，某项缺失不让整个总览变空白。

### 节点名称前缀（已实现）

Argo 配置与直连配置里各一个前缀：隧道节点前缀默认 `Argo`（`argo_prefix` / `NODE_PREFIX_ARGO`），直连节点前缀默认 `直连`（`direct_prefix` / `NODE_PREFIX_DIRECT`），都可自己改。节点名格式：隧道“前缀丨核心丨协议”（如 `Argo丨Xray丨xhttp_h2`），直连“前缀丨直连丨名称”（如 `直连丨Xray-TLS`）。旧配置兼容：只有单个 `node_prefix` 时把它当隧道前缀读入，直连前缀用默认值；新配置以 `argo_prefix` 为准，`node_prefix` 跟着它走。部署渲染（install.sh）与订阅服务（web/sub-server.py）都按隧道 / 直连分别取前缀。

### Reality 额外参数（已实现）

Reality 配置在③配置参数的直连区里（`realityCard`），只在勾选 Reality 协议时显示；地址 = 自己的域名（直连域名），SNI = 大厂域名。

- 目标域名（大厂域名）：必填，带端口（如 www.cloudflare.com:443）；另有“扫描”与“查找目标”两个按钮（POST `/api/reality-scan`）：输入可填域名或 IP/CIDR，留空用常用候选（samsung / amd / intel / microsoft / amazon / cloudflare / sony / nvidia / aws）；结果按 3X-UI 样式排成表格（目标 / 状态 / TLS / ALPN / 密钥交换 / 证书 / 延迟），按延迟排序，每行“使用”按钮一键填入目标与 SNI；单个目标超时只标不可用，不阻塞部署。
- SNI：选填，留空默认与目标域名相同，也可填自己的域名。
- 指纹：下拉选 chrome / firefox / safari，默认 chrome。
- flow：默认 `xtls-rprx-vision`，填 `none` 表示不带 flow。
- 加密方式：默认 `mlkem768x25519plus.native`（另接受 `.xorpub` / `.random` 后缀，其他值警告后回退 native）；填 `none` 表示不启用 VLESS 加密层。启用时 install.sh 用 openssl 生成 VLESS 加密密钥对，拼成客户端加密串（`REALITY_ENC_CLIENT`）。
- spiderX：默认 `/`，不是 `/` 时才带进订阅链接。
- publicKey 与 shortId 自动生成，Short IDs 支持多个（列表可增可删可重新生成，旧的单个 short_id 当第一个沿用），全部参数（密钥、指纹、flow、加密方式、spiderX、最大时间差 / 最小 / 最大客户端版本、VLESS 加密密钥）一起存 `$WORKDIR/direct/reality.json`，环境变量优先、缺失复用，重部署不变。公钥只读显示，私钥永不返回前端；“获取新证书”重新生成密钥对，“清除”清空密钥。
- 订阅链接（install.sh 部署渲染与 web/sub-server.py 同一套规则，改一处要改另一处）：`encryption=<客户端加密串>`、`security=reality`、`pbk` / `sid` / `sni`、`fp=<指纹>`，flow 非 none 时带 `&flow=`，spiderX 非 `/` 时带 `&spiderX=`。服务端 realitySettings 为 network tcp、security reality，dest 为“目标域名:443”，serverNames 为 SNI；transport 固定 raw / none，不用填。

### 端口占用冲突校验（前后端都已实现）

前端实时检查与后端 `validate_config` 用同一套规则，只校验已启用的端口。前端在输入 / 开关一变时就重新检查（`checkPortConflicts`，注册在全局 input / change 监听上），有冲突就标红字段并在③配置参数底部（`portConflictOut`）显示提示，不等点部署；后端在保存配置与开始部署时再校验一遍，报错点名具体字段。

不能冲突的：面板端口（前端从 `/api/status` 取当前面板端口，默认 2053；另写死 8080）、订阅端口、Xray / Sing-box 回源端口及 Sing-box 分发器回源+1/+2/+3、直连 TCP 协议端口之间及与上述端口之间、系统端口 22 / 80。

可以重复、不算冲突的：同一核心的隧道协议共用端口（Xray h1 / h2 / h3 同 443、Sing-box ws / httpupgrade / grpc 同 8443）、TCP 与 UDP 同号（如 TCP 443 与 Hysteria2 UDP 9446 各自独立）。

已实现的校验细节（panel.html `computePortConflicts` / panel-server.py `validate_config`；install.sh 另有 `check_port` 做范围 1–65535 与部署前复核）：
- Xray 回源端口与 Sing-box 回源端口必须不同，且都不能是 8080；Sing-box 回源端口 ≤ 65532；Sing-box 分发器内部端口（回源+1/+2/+3）不能撞上 Xray 回源端口、8080、订阅端口。
- 订阅端口不能是 8080，也不能与回源端口相同；也不能是面板端口、22、80。
- 直连 TCP 协议端口之间不能重复，也不能撞上 8080 / 面板端口 / 22 / 80 / 回源端口 / 订阅端口 / Sing-box 分发器内部端口；只校验已勾选的协议，未勾选的协议端口不拦保存。
- Hysteria2 走 UDP，只校验 1–65535 范围，不参与 TCP 冲突检查。（直连 VLESS+QUIC 已删除，见下。）
### 按钮顺序（已实现）

⑤开始部署的按钮从左到右：只保存配置 / 开始部署 / 清除部署（清除部署靠右，`margin-left:auto`）。清除部署旁备注：只删除本次部署的节点容器和生成的配置文件，面板本身和已保存的部署配置保留，改完可一键重新部署；连面板一起彻底清理用 VPS 上的 cleanup.sh。

### 安装完成显示与安装内容汇总（panel-install.sh，2026-11-19）

安装完成后先打居中完成提示块，再打安装内容与卸载方式汇总框。提示逻辑本身未改：隧道绑定 y/n 必须明确选择、Token / 域名 / 端口二次确认（回车默认 y）、改端口后重启面板容器。

- 居中完成提示块：`====` 分隔线 + ★ 标题 + 面板地址 / 隧道地址（选了隧道才显示）/ 账号 + 密码三项居中显示；密码只显示这一次（黄色加粗，标“只显示这一次，请立即保存”，另存于 `/opt/nexus-argo/panel/password.txt`，权限 600）。居中规则：终端宽度取 `tput cols` → `stty size`（/dev/tty）→ `$COLUMNS`，宽度 < 60 列或拿不到时退回左对齐；颜色只在接终端时生效（`[ -t 1 ]`，`NO_COLOR=1` 可关），管道输出为纯文本、无颜色码；显示代码不读 stdin，`bash <(curl ...)` 下可用。
- 安装内容与卸载方式汇总框（`====` 分隔）：列出本次装好的内容——管理面板容器 nexus-panel、面板隧道容器 nexus-panel-argo（仅选了隧道时）、面板镜像 nexus-panel、面板文件 `/opt/nexus-argo/panel/`（panel-server.py / panel.html / Dockerfile / auth.json / password.txt；部署配置 panel/config.json 由面板保存时生成）、部署脚本与订阅服务 `/opt/nexus-argo/install.sh`、`/opt/nexus-argo/web/sub-server.py`（web/sub-generator.html 为可选文件）；并说明本次只装了面板、节点尚未部署，节点在面板里部署。
- 卸载命令（三条，均真实存在，均不动 Cloudflare 隧道 / Token / 域名 / Zero Trust 路由）：只拆面板用 `bash panel-install.sh uninstall`（等价于 `docker rm -f nexus-panel nexus-panel-argo; docker rmi nexus-panel; rm -rf /opt/nexus-argo/panel`）；只清节点用 `bash /opt/nexus-argo/install.sh uninstall`（保留面板，或在面板里点「清除部署」）；全部清掉用 cleanup.sh 一键清理（`bash <(curl -Ls https://raw.githubusercontent.com/Sid-x2026/Nexus-Argo/main/cleanup.sh)`，清完可直接重跑 panel-install.sh）。

### 第 3 步：直连安全配置卡片 + 变量安装 + 出站页（2026-11-19，已实现，本地验证）

- **③ 配置参数改成安全配置卡片**：直连区改名“直连安全配置”，用与②协议选择相同的 `.proto-wrap` 并排卡片样式（宽屏两栏、窄屏上下、浅色卡片），拆成 4 张子卡——直连域名与前缀、证书配置、Reality 配置、其他安全参数（Trojan / SS 密码就是 UUID，不再另设密码字段；SS 加密方式固定 aes-256-gcm）。隧道区改名“Argo 安全配置”（Token、UUID、Xray / Sing-box 子域名与回源端口、订阅子域名与端口、优选地址）。协议在②只管勾选 + 端口，部署时直接调这里的安全配置。
- **UUID 移入③**：UUID 字段 + 安全生成按钮从④移到③ Argo 安全配置卡片（一个 UUID 通用于隧道和直连全部协议）；④只剩订阅路径 + 优选订阅生成器入口说明。
- **优选地址（单行）**：直连节点不需要优选地址，不再给直连设；Argo 安全配置里一个单行输入框，默认值已填 `cloudflare.com`，可改，不改就按默认生成。④的多行优选地址框已删除，改为提示“多地址裂变请在优选订阅生成器中完成”；后端 `opt_addrs` 支持保留，单值与订阅 URL `?addrs=` 传入的多地址列表都继续工作。
- **Reality 扫描（3X-UI 样式）**：目标输入框带“扫描”与“查找目标”按钮，输入域名或 IP/CIDR，留空用常用候选；结果为表格（目标 / 状态 / TLS / ALPN / 密钥交换 / 证书 / 延迟），按延迟排序，每行“使用”填入目标 + SNI。Reality 字段按 3X-UI：目标（带端口）、SNI、最大时间差（默认 0）、最小客户端版本（默认 1.0.0）、最大客户端版本（默认 x.y.z）、Short IDs（多个，可增可删可重新生成）、SpiderX、公钥 / 私钥（公钥只读显示，私钥不返回前端；“获取新证书”重新生成，“清除”清空）。后端 `/api/reality-scan` 用 openssl s_client 探 TLS 版本 / ALPN / 密钥交换 / 证书 CN + curl 测延迟，逐目标超时、失败标不可用；`/api/reality-keys` 管密钥的读取 / 重新生成 / 清除。
- **变量一键安装（panel-install.sh）**：两种装法。① SSH 面板安装（交互，默认）：原样不变，一步步确认，装完 IP:端口进面板。② Argo 绑定安装（变量一键）：命令里同时给 `ARGO_TOKEN` 与 `PANEL_DOMAIN`（可另给 `PANEL_PORT`，默认 2053），直接按变量装，不做二次确认，装完走隧道域名进面板；变量缺了就回落到交互安装。示例参数用双引号 + 占位值，见 README.md。两种装法共用 `do_bind_tunnel`（改端口重启面板 + 起 cloudflared + 打印路由），居中完成提示块与 `====` 安装内容汇总框保持不变。
- **出站页**：面板新增“出站”页（3X-UI 样式），默认两行 direct（freedom）与 blocked（blackhole），列表显示标签 / 地址 / 出口 / 国家 / 流量 / 延迟，支持新增 / 编辑 / 删除与“全部测试”延迟测试，页顶提示“此处的所有更改都需要保存并重启面板才能生效”。第一版只做管理 + 测试，家宽对接以后再说，页面已预留自定义出站。出站存在面板配置 `outbounds.json` 里（GET / POST `/api/outbounds`，POST `/api/outbounds/test`）；部署渲染暂不接出站（只存 + 显示 + 测试），待以后接线。

### 协议清单（全部可单独勾选，只装选中的，只进订阅选中的）
- **[Argo] 隧道**（6 个，不变；优选地址裂变只对这 6 个生效）：Xray xhttp_h1 / h2 / h3，Sing-box ws / httpupgrade / grpc。
- **[直连] VLESS 组**：VLESS+TCP（8082，明文）、VLESS+TLS（443）、VLESS+Reality（9447）、VLESS+WS（8083，明文，path /ws）、VLESS+gRPC（8084，TLS）。（VLESS+QUIC 已删除：最新 Xray 26.11.9 移除了独立 QUIC 传输，配置校验不通过；HTTP/2 系传输 gRPC / TLS 为替代，无需新增协议。）
- **[直连] Trojan / VMess 组**：Trojan+TLS（9443，密码 = UUID）、VMess+WS（9444，TLS，alterId 0）、Trojan+TLS（Sing-box，9445）。
- **[直连] Shadowsocks / Hysteria2 组**：Shadowsocks（8388，aes-256-gcm，密码 = UUID）、Hysteria2（9446，UDP，密码 = UUID，alpn h3）。
- 直连节点地址一律是 DIRECT_DOMAIN（与隧道域名分开的子域名；同域名两边共用会互相暴露：直连 DNS 漏 VPS IP，隧道 SNI 漏域名），原样进订阅，永不裂变。端口默认值如上，面板可改；TCP 端口冲突校验覆盖全部直连 TCP 端口，Hysteria2 走 UDP，只校验范围。直连共 11 个协议（Xray VLESS TCP / TLS / Reality / WS / gRPC、Trojan、VMess-WS；Sing-box VLESS-TLS、Trojan、SS、Hysteria2），隧道 6 个，两边都选共 17 个。

### 证书（直连）
- 三种模式：自签名（默认，openssl 10 年证书，无需续期，客户端需 allowInsecure）、上传自己的证书（面板贴 cert_pem / key_pem，点部署时才写到 $WORKDIR/direct/cert.pem / key.pem，install.sh 永不覆盖已上传的证书）、ACME（域名须解析到 VPS、端口 80 放行，Let's Encrypt 90 天，到期前 30 天自动续期，续完重载核心；申请失败自动回退自签名，部署不中断）。
- 面板总览与证书区显示证书状态（GET /api/cert）：类型、CN、到期日、剩余天数、自动续期状态。
- Reality 不需要大厂域名的证书：面板“扫描大厂域名”（POST /api/reality-scan，测 www.microsoft.com / apple / google / amazon / cloudflare / bing / dl.google.com 的延迟，返回排序与最低延迟）选出目标域名后，自动填 SNI 与目标地址；install.sh 生成 X25519 密钥对与 shortId，存 $WORKDIR/direct/reality.json（private_key / public_key / short_id / dest，另有 fingerprint / flow / encryption / spiderx / VLESS 加密密钥对），重部署复用。

### 订阅路径
- SUB_PATH 只改中间一段（后缀 / 文件名）：https://&lt;订阅域名&gt;/&lt;SUB_PATH&gt;/raw/&lt;uuid&gt;，域名与后面的 /raw/&lt;uuid&gt; 固定。面板以模板形式显示，只允许编辑中间段，正则 ^[A-Za-z0-9._-]{1,32}$，保留 api / config.json / help.txt，不可带 /。

### 必填与选填
- 必填（红 *）：隧道模式下 Argo Token、节点 UUID、已启用核心的子域名 + 回源端口、每个已启用核心至少勾一个协议；直连模式下至少勾一个直连协议、Reality 勾选时的大厂域名、ACME 模式下的域名。直连域名标注“选填”（直连域名（SNI / 证书域名），留空用 VPS 公网 IP），留空不拦截（ACME 模式除外，ACME 模式下域名必填）。
- 选填：节点名称前缀（隧道 / 直连各一个）、优选地址、订阅路径（默认 sub）、隧道传输（auto）。
- 点“开始部署”前前端先校验，缺的字段标红并定位到第一个；后端 validate_config 的报错指到具体字段。

### 未做 / 已知限制
- ACME 真实发证未在 VPS 上实测（需域名解析到 VPS 且 80 端口可达）；失败回退自签名已验证。
- 无 Docker 环境下未做真实 Xray / Sing-box 握手测试；渲染与订阅格式已本地验证。

## 待定 / 以后再说

- 单节点延迟测试、流量统计（总览 / 节点页以后可加，第一版不做）。

### 第 4 步：布局 + Reality + 出站路由 + 订阅 UUID 在前（2026-11-20，已实现，本地验证）
- **面板布局**：宽度放宽到 1440px；左边加一排侧边图标按钮导航（总览 / 部署安装 / 出站 / 订阅地址），点图标切换页面；页签顺序订阅永远最后。放宽后 Argo 组与直连组左右对齐，Reality 配置可与上方并排，Reality 块内部位置调整、逻辑清楚。
- **直连协议分组**：共用组（VLESS+TLS、Trojan+TLS，Xray 与 Sing-box 都有）放一起加备注；Xray 组（TCP / Reality / WS / gRPC / VMess-WS）；Sing-box 组（Shadowsocks、Hysteria2，仅 Sing-box，Xray 服务端不支持 Hysteria2，客户端如 V2RayNG 能连）。
- **客户端选择**：协议选择上方加客户端选择（自动最优 / 全部客户端 / V2Ray / Sing-box / Clash），两种方式：按客户端选最适合的所有协议，或按客户端推荐的协议；自动帮用户选最优。
- **证书默认 ACME**：原来默认自签名，现默认 ACME 自动申请（90 天自动续，失败回退自签名）。
- **直连地址与 SNI 拆开**：address 默认 VPS 公网 IP，也可填域名；SNI 单独填；ACME 必须填域名，填 IP 申请不了。
- **节点前缀挪到④**：Argo / 直连前缀都放节点与订阅那一块，部署时可见、想改就改。
- **订阅路径 UUID 在前**：https://订阅域名/<UUID>/<自定义>/raw，旧地址 /<自定义>/raw/<uuid> 兼容保留。
- **Reality 扫描**：目标输入框右侧加扫描/查找目标，点「使用」自动填入目标栏和 SNI 栏、自动关闭扫描框、自动随机生成 3 个 Short ID；扫描按钮未点黑色、点了灰色；延迟改测 TLS 握手时间（3X-UI 式），不再测完整 HTTP 请求。Short IDs 挪到扫描栏下面，点「添加」随机生成一个，可手动输入、可删除；每次换目标域名后重新随机生成 3 个；用自己域名不扫描时也自动生成。Short ID 与域名无关。
- **出站 + 路由**：出站页加路由规则（GET / POST /api/routes，存 routes.json），出站定义走哪条路，路由按域名 / IP / 端口定义什么流量走它，成一对。
- **安装完成框**：不重要的提示放前面，重要内容最后显示，用 =========== 分隔；关键框只留关键内容，密码文件提示挪到框前面；地址、账号、密码对齐，隧道域名地址在前、IP 地址在后；卸载方式简单明了，放在账号密码下方。
- **安装命令**：占位改英文（your-token / your-panel-domain），域名名字叫「隧道面板域名」；README 加卸载/一键清理一节。

## 第 5 步 (2026-11-20)
- 面板整体 1680px，侧边导航收窄，页面默认铺满、超过部分隐藏；Argo/直连组左右对齐。
- 路由独立成左侧导航「路由」，第一步先加出站、第二步加路由，带例子。
- Reality 目标栏按 3X-UI：扫描/查找按钮放目标输入框右侧；扫描扫输入框域名；查找弹 REALITY 目标扫描器弹窗，表格含证书有效期/证书链；选「使用」自动填目标+SNI、生成 3 个 Short ID，并显示结果卡片。
- Reality 配置收紧：公钥+获取新证书放上面、SNI 一起；次要项（最大时间差/最小/最大客户端版本/指纹/flow/加密/spiderX）做短放末尾；最大时间差有说明（时间差容忍值，防重放，默认 0 不限）。
- 面板后缀默认 /Nexus，安装时提示、回车即默认；`bash panel-install.sh info` 呼出面板信息（登录地址/隧道面板地址/域名/后缀/端口/账号，存 panel-info.json）。
- 安装完成框：「隧道地址」改成「隧道面板地址」，重要地址绿色加粗、密码黄色加粗。
- 总览页：状态卡片渐变加高，卡片铺满。

## 第 6 步 (2026-11-20)
- 部署页顺序：①部署方式 → ②代理客户端（单独一块，可多选 V2Ray/Sing-box/Clash/iOS）→ ③协议选择 → ④配置参数 → ⑤节点与订阅。
- 代理客户端：全选是把能适合所有客户端的协议全选中；推荐是按客户端推荐的协议搭配；也可以全选，不强制按客户端限制；iOS 按 Clash 订阅格式走。
- 协议选择横向排：Argo 里 Xray/Sing-box 横向；直连下面横向一分为三（共用组/Xray组/Sing-box组），左右对齐。
- 证书顺序：ACME 自动申请 / 自签名 / 上传自己的证书。
- 直连域名加备注（不是面板域名，CF 加 A 记录到 VPS IP，DNS only 灰云）；直连 SNI 自动填入直连地址。
- Bug 修：客户端推荐改勾选后触发 updateRealityFields，Reality 版块不再不出。
- Argo 安全配置：UUID 和优选地址一排，优选地址只填一个域名默认 cloudflare.com；Token 在上、Xray/Sing-box 隧道设置在下；订阅域名/端口挪到⑤节点与订阅。
- 侧边导航：fixed 全高悬浮不占版面，内容区铺满；顺序路由在前、出站在后。
- 面板后缀真接上：panel-server.py 认 /Nexus（PANEL_PATH 环境变量），/ 跳到 /Nexus，/api 不变。
- 完成框对齐：值列对齐，隧道面板地址后同一行加「（需在 Zero Trust 中添加对应路由后方可访问）」；加速说明：重装别跑 cleanup 清镜像，RAW_BASE 可改 jsDelivr。

## 第 7 步 (2026-11-20)
- 部署页：只勾一种方式时只显示该方式的协议组（修复 details display:flex 导致已隐藏的组还留空白框）。
- 代理客户端去掉「自动最优」「全部客户端」，默认 V2Ray，只留 V2Ray/Sing-box/Clash/iOS（可多选）。
- 推荐协议挪到③协议选择里，改名「推荐协议」（默认）/「全部适配协议」。
- 协议选择自适应：Argo 组窄、直连组宽（grid 0.8fr / 1.7fr，单组时占满），直连三列 minmax 170px，端口框收窄。
- Argo 配置：UUID 与优选地址移到版块底部，输入框高度拉平（36px），按钮改成「生成」。
- 配置参数一分为二：勾 Reality 时 Reality 版块单独在上面先显示，再显示直连域名与证书；不勾只显示直连配置；版块顶部有说明。
- 刷新慢：/api/status 原来 8 次 docker inspect 串行，改成一次 docker ps + 3 秒缓存，刷新约 4-5 秒降到 1 秒左右；前端 showApp 并行加载。
