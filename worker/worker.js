// Nexus-Argo — Cloudflare Worker 动态订阅 (v2, 不走 8080)
//
// 部署时只生成原生订阅, Worker 现场裂变, 换优选 IP 不用重新部署 VPS。
// 和 v1 不同: Worker 不去拉 VPS 面板 8080 的订阅, 而是按环境变量
// 直接生成原生节点。面板 8080 只绑 127.0.0.1, 不暴露公网, 也不用
// 再给它建一条隧道路由。订阅即使面板容器挂了也能用, 只要核心在跑。
//
// 环境变量 (Dashboard > Workers > 该 Worker > Settings > Variables):
//   NODE_UUID        你的 UUID, 例如 bf4b0e89-5d86-4918-921f-556cd0d5ae88
//   NODE_PREFIX      节点名前缀, 默认 Nexus
//   XRAY_DOMAIN      例如 xr.ahstshu.eu.cc
//   XRAY_CONFIGS     例如 xhttp_h1:443,xhttp_h2:443,xhttp_h3:443
//   SINGBOX_DOMAIN   例如 sb.ahstshu.eu.cc
//   SINGBOX_CONFIGS  例如 ws:8443,httpupgrade:8443,grpc:8443
//   (只启用一个核心时, 另一个的 DOMAIN / CONFIGS 留空即可)
//   换 UUID / 域名 / 端口时, 只改这里的环境变量, 不用动 VPS。
//
// 路由:
//   GET /sub/raw/<uuid>
//       原生订阅, 1 协议 = 1 节点, 地址 = 隧道域名, 返回 base64
//   GET /sub/merged/<uuid>?addrs=a,b,c
//       动态订阅: 原生节点按 addrs 现场裂变, 返回 base64
//       不填 addrs: 默认 addrs=cloudflare.com
//   GET /sub/merged?url=<外部订阅地址, URL 编码>&addrs=a,b,c
//       外部订阅裂变: 拉外部订阅 (原生或已裂变过的都行), 按
//       addrs 裂变后返回。输入可能是 base64 也可能是明文链接。
//   GET /
//       说明页
//
// 输出一律是标准 base64 订阅, v2rayN / 客户端都能直接更新。

// ---------- 原生链接生成 (和 install.sh / 前端同一套逻辑) ----------

function xrayLink(uuid, prefix, domain, proto, port) {
  const alpn = { xhttp_h1: "http/1.1", xhttp_h2: "h2", xhttp_h3: "h3" }[proto] || "h2";
  const mode = proto === "xhttp_h3" ? "packet-up" : "auto";
  return (
    `vless://${uuid}@${domain}:${port}/?encryption=none&security=tls` +
    `&sni=${domain}&type=xhttp&mode=${mode}&path=%2F&alpn=${alpn}` +
    `#${prefix}丨Xray丨${proto}`
  );
}

function singboxLink(uuid, prefix, domain, proto, port) {
  // ALPN: ws/httpupgrade 只能走 http/1.1, grpc 只能走 h2
  // (VPS 实测 2026-11-18: ws 用 h2,http/1.1 会协商 h2 导致握手失败)
  const alpn = proto === "grpc" ? "h2" : "http/1.1";
  let svc = "&path=%2Fws";
  if (proto === "httpupgrade") svc = "&path=%2Fhttpupgrade";
  else if (proto === "grpc") svc = "&serviceName=grpc";
  return (
    `vless://${uuid}@${domain}:${port}/?encryption=none&security=tls` +
    `&sni=${domain}&type=${proto}${svc}&alpn=${alpn}` +
    `#${prefix}丨Singbox丨${proto}`
  );
}

function parseConfigs(text) {
  // "xhttp_h1:443,xhttp_h2:443" -> [{proto, port}]
  return (text || "")
    .split(",")
    .map((s) => s.trim())
    .filter(Boolean)
    .map((s) => {
      const [proto, port] = s.split(":");
      return { proto, port: port || "443" };
    });
}

function nativeLinks(env) {
  const uuid = (env.NODE_UUID || "").trim();
  const prefix = (env.NODE_PREFIX || "Nexus").trim() || "Nexus";
  const links = [];
  const xrayDomain = (env.XRAY_DOMAIN || "").trim();
  for (const c of parseConfigs(env.XRAY_CONFIGS)) {
    if (xrayDomain) links.push(xrayLink(uuid, prefix, xrayDomain, c.proto, c.port));
  }
  const sbDomain = (env.SINGBOX_DOMAIN || "").trim();
  for (const c of parseConfigs(env.SINGBOX_CONFIGS)) {
    if (sbDomain) links.push(singboxLink(uuid, prefix, sbDomain, c.proto, c.port));
  }
  return links;
}

// ---------- 订阅编解码 / 裂变 ----------

function decodeSubscription(text) {
  const trimmed = (text || "").trim();
  if (!trimmed) return [];
  if (trimmed.includes("://")) {
    return trimmed.split(/\r?\n/).map((l) => l.trim()).filter(Boolean);
  }
  try {
    const cleaned = trimmed.replace(/\s+/g, "");
    const std = cleaned.replace(/-/g, "+").replace(/_/g, "/");
    const padded = std + "=".repeat((4 - (std.length % 4)) % 4);
    const decoded = atob(padded);
    const bytes = Uint8Array.from(decoded, (c) => c.charCodeAt(0));
    const utf8 = new TextDecoder("utf-8").decode(bytes);
    const lines = utf8.split(/\r?\n/).map((l) => l.trim()).filter(Boolean);
    if (lines.length && lines.some((l) => l.includes("://"))) return lines;
  } catch (_) {}
  return trimmed.split(/\r?\n/).map((l) => l.trim()).filter(Boolean);
}

function encodeSubscription(lines) {
  const text = lines.join("\n") + "\n";
  const bytes = new TextEncoder().encode(text);
  let latin1 = "";
  bytes.forEach((b) => (latin1 += String.fromCharCode(b)));
  return btoa(latin1);
}

// vless://uuid@host:port/...#remark, 只换 host, 其余原样保留
function fissionLink(link, addr) {
  const m = link.match(/^([a-zA-Z0-9+.-]+:\/\/[^@]+@)([^:/?#]+)(:\d+.*)$/);
  if (!m) return null;
  return m[1] + addr + m[3];
}

function fission(lines, addrs) {
  const out = [];
  for (const link of lines) {
    for (const addr of addrs) {
      const f = fissionLink(link, addr);
      if (f) out.push(f);
    }
  }
  return out;
}

function parseAddrs(searchParams) {
  const addrs = (searchParams.get("addrs") || "")
    .split(",")
    .map((a) => a.trim())
    .filter(Boolean);
  return addrs.length ? addrs : ["cloudflare.com"];
}

function subResponse(lines) {
  return new Response(encodeSubscription(lines), {
    headers: {
      "content-type": "text/plain; charset=utf-8",
      "cache-control": "no-store",
      "access-control-allow-origin": "*",
    },
  });
}

function textResponse(text, status = 200) {
  return new Response(text, {
    status,
    headers: { "content-type": "text/plain; charset=utf-8" },
  });
}

// ---------- 主入口 ----------

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    const path = url.pathname;

    if (path === "/" || path === "") {
      return textResponse(
        "Nexus-Argo Worker 动态订阅\n\n" +
          "GET /sub/raw/<uuid>\n" +
          "    原生订阅\n\n" +
          "GET /sub/merged/<uuid>?addrs=a,b,c\n" +
          "    动态订阅: 原生节点按 addrs 现场裂变\n\n" +
          "GET /sub/merged?url=<外部订阅地址>&addrs=a,b,c\n" +
          "    外部订阅裂变\n\n" +
          "配置: 环境变量 NODE_UUID / NODE_PREFIX /\n" +
          "XRAY_DOMAIN / XRAY_CONFIGS /\n" +
          "SINGBOX_DOMAIN / SINGBOX_CONFIGS\n" +
          "不走 VPS 8080, 面板只绑 127.0.0.1\n"
      );
    }

    const uuid = (env.NODE_UUID || "").trim();
    if (!uuid) {
      return textResponse("Worker 未配置 NODE_UUID 环境变量", 500);
    }

    // 原生订阅: /sub/raw/<uuid>
    const rawMatch = path.match(/^\/sub\/raw\/(.+)$/);
    if (rawMatch) {
      if (rawMatch[1] !== uuid) return textResponse("uuid 不匹配", 404);
      const links = nativeLinks(env);
      if (!links.length) return textResponse("未配置任何核心", 500);
      return subResponse(links);
    }

    // 动态订阅: /sub/merged/<uuid>?addrs=... 或 /sub/merged?url=...&addrs=...
    const mergedMatch = path.match(/^\/sub\/merged(?:\/(.+))?$/);
    if (mergedMatch) {
      const addrs = parseAddrs(url.searchParams);
      let lines = [];
      const externalUrl = url.searchParams.get("url");
      if (externalUrl) {
        const res = await fetch(externalUrl);
        if (!res.ok) {
          return textResponse(`拉取外部订阅失败: HTTP ${res.status}`, 502);
        }
        lines = decodeSubscription(await res.text());
      } else {
        if (!mergedMatch[1] || mergedMatch[1] !== uuid) {
          return textResponse("uuid 不匹配: /sub/merged/<uuid>?addrs=a,b,c", 404);
        }
        lines = nativeLinks(env);
      }
      if (!lines.length) return textResponse("订阅为空", 502);
      return subResponse(fission(lines, addrs));
    }

    return textResponse("404: 未知路径, 访问 / 看说明", 404);
  },
};
