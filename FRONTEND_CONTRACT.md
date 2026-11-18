# Nexus-Argo — frontend contract extracted from web/index.html (v3.6)
# This file is the source of truth for install.sh. If the frontend changes,
# update this file first, then install.sh.

## Identity
- Title: `Nexus-Argo v3.6 自动化部署与运维系统`
- GitHub: `Sid-x2026/Nexus-Argo`, install entry:
  `bash <(curl -Ls https://raw.githubusercontent.com/Sid-x2026/Nexus-Argo/main/install.sh)`
  and uninstall: same URL + ` uninstall`
- Panel: Tailwind via CDN (`cdn.jsdelivr.net/npm/@tailwindcss/browser@4`),
  Font Awesome 6.4.0 via CDN. No bundled CSS/JS — offline / restricted
  networks will break styling.

## Inputs (IDs) and defaults
| Field | ID | Default |
|---|---|---|
| Xray enabled | `enableXray` | checked |
| Xray protocols | `xrayProto` | `xhttp_h1, xhttp_h2, xhttp_h3` (all checked, port 443 each) |
| Xray origin port | `xrayPort` / `displayXrayPort` | `8001` |
| Xray domain | `xrayDomain` | `xray.yourdomain.com` |
| Sing-box enabled | `enableSb` | checked |
| Sing-box protocols | `sbProto` | `httpupgrade, ws, grpc` (all checked, port 443 each) |
| Sing-box origin port | `sbPort` / `displaySbPort` | `8002` |
| Sing-box domain | `sbDomain` | `singbox.yourdomain.com` |
| Argo Token | `argoToken` | empty (allowed — preview without token) |
| UUID | `nodeUuid` | `98a9eeea-e34d-4a0b-a151-39ec761d83f6` (example — MUST be replaced) |
| Node prefix | `nodePrefix` | `优选JP` |
| Opt addresses | `optAddress` (textarea, one per line) | empty → `['cloudflare.com']` |

## SSH command generated (updateOutputs)
Env vars, in order:
`NODE_UUID, ARGO_TOKEN, ENABLE_XRAY, XRAY_PORT, XRAY_DOMAIN, XRAY_CONFIGS,
ENABLE_SINGBOX, SINGBOX_PORT, SINGBOX_DOMAIN, SINGBOX_CONFIGS`
where `XRAY_CONFIGS="xhttp_h1:443,xhttp_h2:443,..."` and
`SINGBOX_CONFIGS="httpupgrade:443,ws:443,grpc:443"`, then:
`bash <(curl -Ls https://raw.githubusercontent.com/Sid-x2026/Nexus-Argo/main/install.sh)`
`install.sh` MUST parse exactly these names. It does in this repo.

## Node-link generation (actual code, not the intro text)
- Xray, per protocol × per opt-address:
  `vless://<uuid>@<addr>:<port>/?encryption=none&security=tls&sni=<xrayDomain>&type=xhttp&mode=<auto|packet-up>&path=%2F&alpn=<http/1.1|h2|h3>#<prefix>丨Xray丨<proto>`
  - `xhttp_h1`: alpn `http/1.1`, mode `auto`
  - `xhttp_h2`: alpn `h2`, mode `auto`
  - `xhttp_h3`: alpn `h3`, mode `packet-up`
  - **path is `/` for all three** — there is no `/xhttp-h1` etc. in the code.
- Sing-box, per protocol × per opt-address:
  `vless://<uuid>@<addr>:<port>/?encryption=none&security=tls&sni=<sbDomain>&type=<proto>&path=%2F&alpn=h2,http/1.1#<prefix>丨Singbox丨<proto>`
  - `grpc` instead uses `&serviceName=grpc` (no `path=`)
  - **no `quic` protocol in the uploaded frontend** (the intro mentioned it;
    the code has only httpupgrade / ws / grpc).

## Subscription URLs generated
- Dynamic (merged): `https://<activeDomain>/sub/merged/<uuid>`
- Static (raw): `https://<activeDomain>/sub/raw/<uuid>`
- `activeDomain` = Xray domain if Xray enabled, else Sing-box domain.
  Both alert and abort if neither core is enabled.
- **These endpoints do not exist in the frontend** — something server-side
  must serve them. This repo serves `merged.txt` / `raw.txt` via nginx
  (see web/nginx.conf). That is a v1 approximation, not the frontend's
  opt-address裂变 (see below).

## Discrepancies / gaps found (fix before calling this production-ready)
1. **Path mapping is not distinct.** Intro said `/xhttp-h1`, `/` for grpc,
   etc. Code uses `path=/` for every Xray protocol and every non-gRPC
   Sing-box protocol. A backend cannot route by path on that basis.
   This repo renders one unified inbound per core to match the code.
2. **No `quic`.** Intro lists Sing-box quic; code does not. Add it to
   `sbProto` in the frontend if you want it, then add it to install.sh.
3. **No port-conflict check.** `validatePort()` only clamps 1–65535.
   It does not check Xray origin == Sing-box origin, or origin == 8080.
   `install.sh` adds those checks; the frontend should too.
4. **SSH command does not pass `NODE_PREFIX` or `OPT_ADDRS`.**
   The server therefore cannot reproduce the frontend's node pool or
   dynamic subscription. Either add them to the generated command or
   accept that `/sub/*` is domain-only (this repo's v1 choice).
5. **`generateUUID()` uses `Math.random()`.** Use `crypto.randomUUID()`
   (or `crypto.getRandomValues`) for a real UUID.
6. **No auth on the panel.** Anyone who can reach :8080 sees the panel
   and can copy the generated command / UUID. Do not expose 8080 publicly;
   bind to localhost / firewall / Cloudflare Access.
7. **Clipboard + `alert()`:** `navigator.clipboard` needs a secure
   context; on plain http://<VPS-IP>:8080 copy may fail in some browsers.
8. **BBR button** runs a third-party remote script
   (`chiakge/Linux-NetSpeed` tcp.sh) via curl — review before running.
9. **Default UUID is hard-coded** in the HTML. Generate a fresh one on
   load instead of shipping the same example UUID to every deployment.
