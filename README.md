# crawl4ai-mcp

自托管的 localhost-only MCP 抓取服务：从零开销的 TLS 指纹模拟 HTTP 请求起步，被拦截时逐级升级到浏览器、反检测浏览器、数据中心代理，直至托管 API（Rayobyte / Firecrawl），并按域名记住「哪一层能成功」，避免反复烧钱爬梯子。通过四个 MCP 工具接入 opencode。

## 架构与七层阶梯

```
opencode ──MCP(streamable-http)──> crawl4ai-mcp daemon (systemd --user)
                                        ├─ 域名策略记忆 (SQLite)
                                        ├─ 级联执行器（拦截检测 + 升级路由）
                                        └─ 浏览器生命周期（懒启动 + 空闲回收 + 并发信号量）
```

| Tier | 名称 | 手段 | 解决什么 | cost_kind |
|---|---|---|---|---|
| 0 | `http` | curl_cffi TLS 指纹模拟（chrome131） | TLS/JA3 指纹检测 | free |
| 1 | `stealth` | crawl4ai 原生 stealth Chromium | JS 渲染 + 基础检测 | free |
| 2 | `undetected` | patchright UndetectedAdapter | 深度浏览器指纹 | free |
| 3 | `camoufox` | Camoufox (Firefox) 独立进程 | 最强免费指纹伪装 | free |
| 4 | `proxy` | Tier 2 + 数据中心代理轮转 | 本机 IP 被限速/封禁 | proxy_bandwidth |
| 5 | `rayobyte` | Rayobyte Web Scraper API | 托管指纹 + 代理池 | rayobyte_credit |
| 6 | `firecrawl` | Firecrawl v2 `/scrape` | 最终兜底 | firecrawl_credit |

关键规则：`401/404/410` 立即停止不升级；Cloudflare 挑战跳过数据中心代理层；429/503 带 `Retry-After` 优先走代理；短静态页直接接受；网络错误只重试一次；正文阈值默认 200 字（`config.toml` 可调）。

## 四个 MCP 工具

- `scrape(url, format="markdown", max_tier="firecrawl", force_tier=None)` — 级联抓取，返回 markdown + tier/cost/attempts 元数据
- `crawl(url, max_pages=10, max_depth=2, include_pattern=None)` — 同源广度优先爬站
- `map(url, search=None, limit=100)` — sitemap + Common Crawl 发现 URL
- `diagnose(domain=None)` — 内存、各层可用性、浏览器状态、最近失败、域名策略

## 安装

**前置**：Linux aarch64、Python 3.12、systemd user linger（`loginctl enable-linger ubuntu`）、`crawl4ai` 的 chromium 与 patchright、camoufox（可选）。

```bash
git clone <repo> && cd crawl4ai-mcp
python3 -m venv .venv
.venv/bin/pip install -e '.[test]'          # 可选: '.[camoufox,test]'
.venv/bin/python -m playwright install chromium
.venv/bin/python -m patchright install chromium
.venv/bin/python -m camoufox fetch          # 可选

cp .env.example .env && chmod 600 .env      # 填入密钥（见 .env.example）
./scripts/install-user-service.sh           # 安装 + 启动 + 健康检查
```

验证：`curl http://127.0.0.1:11236/health` → `{"status":"ok"}`。在 `~/.config/opencode/opencode.jsonc` 的 `mcp` 段添加远程入口后重启 opencode 会话：

```jsonc
"crawl4ai": {
  "type": "remote",
  "url": "http://127.0.0.1:11236/mcp",
  "enabled": true,
  "timeout": 120000
}
```

## 资源护栏

- 仅监听 `127.0.0.1:11236`，拒绝其他 bind host
- cgroup 硬限：`MemoryHigh=1536M`、`MemoryMax=2560M`、`MemorySwapMax=0`（OOM 时由 cgroup 击杀并自动重启，不波及整机）；`KillMode=control-group`、`Restart=always`
- 浏览器并发 = 2，HTTP 并发 = 8；Chromium 空闲 180 秒、Camoufox 空闲 120 秒即完全回收，稳态内存约 80-100 MiB

## 出口安全（egress）

- 只允许 `http/https` 公共地址：私网、回环（127.0.0.1 等）、链路本地、文档段（TEST-NET）一律 `non_global_address` 拒绝；`file://` 等非 HTTP scheme 直接 `unsupported_scheme` 拒绝；含 userinfo、非常规端口、IPv6 zone id、控制字符的 URL 同样拒绝
- 域名每次请求都做全量 DNS 解析并逐地址校验（防 DNS rebinding）；HTTP 层用 `CurlOpt.RESOLVE` 把解析结果钉住再发包，重定向逐跳重新解析校验（curl 关闭自动跳转，手动跟随并复核），每一跳实际请求的是规范化后的 URL（尾点主机名无法绕过 pin）
- 浏览器子资源（`**/*`）与 crawl4ai seeder 流量同样走 URL 策略，非公共地址一律 abort；被拦截的主框架导航由浏览器层上报 `policy_error`，不会误报为普通失败
- 同源判断比较规范化后的 scheme/host/有效端口；重定向逃逸同源范围即拒绝
- 本地 pinning 代理对客户端只返回固定公开原因短语（`403 blocked by policy` / `502 tunnel failed`），不反射被拒 URL、凭据或内部 socket/DNS 异常文本
- 上游代理配置只接受 `http/https`，URL 内不得携带 userinfo/path/query/fragment（凭据走显式字段），非法端口与缺失主机直接拒绝
- 代理池凭据按池注入：`WEBSHARE_PROXY_USERNAME`/`WEBSHARE_PROXY_PASSWORD` 与 `OXYLABS_PROXY_USERNAME`/`OXYLABS_PROXY_PASSWORD`（见 `.env.example`），两池互不混淆；用户/密码只填一个即配置错误，`diagnose` 中 PROXY 层 `ready=false` 并给出明确原因，认证 CONNECT 携带正确的 `Proxy-Authorization`

## 提供商模型与失败语义

- 未配置 = 不可用：密钥/代理留空时 `diagnose` 中对应层 `ready=false`，级联直接跳过该层
- 已配置但失败 = 真失败：不静默跳过；额度耗尽（Rayobyte 429/额度错误、Firecrawl 402）归一化为 `quota` 类错误，级联继续尝试或返回失败并进入域名冷却
- 提供商失败枚举：`auth` / `quota` / `rate_limit` / `transport` / `service` / `malformed_response`，随 attempts 逐条记录 `provider_error_kind` 与 `provider_error`
- 目标网络错误（连接拒绝/超时）只同层重试一次即停止（`target_network` 冷却），绝不自动升级到付费层
- Cloudflare 挑战一旦出现即粘性跳过 PROXY 层（本次请求内永久过滤）
- Tier 4（proxy）每次抓取独立构造 `proxy_config` 轮转，不跨请求共享

## MCP 契约（响应形状）

- 七个层级名一律小写字符串：`http` / `stealth` / `undetected` / `camoufox` / `proxy` / `rayobyte` / `firecrawl`
- `scrape` 返回 `{url, status, content, tier_used, cost_kind, elapsed_ms, attempts, cooldown_until, error}`，`format` 仅 `markdown`（默认）或 `html`（仅成功时返回原始 HTML，失败置空）
- `crawl` 返回 `{pages, stats}`：同源重定向后 `pages[].url` 报告规范化后的最终地址（`effective_url`），跨源逃逸时保持请求别名；同源重定向别名直接去重，绝不二次抓取（避免重复付费）
- `map` 返回 `{urls}`（limit 1..100，超出拒绝），条目经完整规范化同源校验（scheme/port/凭据/非 HTTP 一律剔除）
- `diagnose(domain)` 接受裸主机名（`example.com`）或完整 URL；私网/回环字面量与非法主机名拒绝
- 恰好四个工具：`scrape` / `crawl` / `map` / `diagnose`；raw HTML 始终内部持有，除非显式请求 html 格式

## 测试

```bash
.venv/bin/pytest -v                          # 单元/集成（非 live）
.venv/bin/pytest tests/deployment/test_unit_file.py -v   # systemd 契约
CRAWL4AI_MCP_LIVE_TESTS=1 scripts/run-acceptance.sh      # 完整部署验收（付费层需要配置）
```

完整验收命令（Step 9）需要显式 opt-in（`CRAWL4AI_MCP_LIVE_TESTS=1`），跑 `tests/acceptance` 全部用例、落 JUnit/日志证据，并按此判定：

| 退出码 | 含义 |
|---|---|
| 0 | `acceptance complete`：零失败/错误/跳过 |
| 1 | `acceptance failed`：至少一个失败或错误 |
| 2 | 未 opt-in 或环境不可用 |
| 3 | `acceptance incomplete`：无失败但存在跳过（例如未配置的付费/可选层） |

已配置的提供商（Camoufox/代理/Rayobyte/Firecrawl）失败不再跳过；只有禁用/未配置的可选层才允许跳过。资源生命周期用例约需 10 分钟（5 分钟空闲 + 4 分钟回收 + 重启自愈）。

## 文档

- [设计文档](docs/superpowers/specs/2026-08-13-crawl4ai-mcp-design.md)
- [实施计划](docs/superpowers/plans/2026-08-13-crawl4ai-mcp-implementation.md)
- [运维指南](docs/operations.md) — 状态/日志/内存/策略清理/密钥轮换/回滚
- [Rayobyte 契约](docs/provider-contracts/rayobyte.md) — 抓取 API 请求/响应格式（实测捕获）
