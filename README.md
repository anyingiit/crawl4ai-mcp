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
- cgroup 硬限：`MemoryHigh=1536M`、`MemoryMax=2560M`、`MemorySwapMax=0`（OOM 时由 cgroup 击杀并自动重启，不波及整机）
- 浏览器并发 = 2，HTTP 并发 = 8；Chromium 空闲 180 秒、Camoufox 空闲 120 秒即完全回收，稳态内存约 80-100 MiB

## 测试

```bash
.venv/bin/pytest -v                          # 单元/集成
CRAWL4AI_MCP_LIVE_TESTS=1 .venv/bin/pytest tests/acceptance/test_live_tiers.py -v        # 真实层级（耗额度）
CRAWL4AI_MCP_LIVE_TESTS=1 .venv/bin/pytest tests/acceptance/test_resource_lifecycle.py -v # 内存/浏览器生命周期（约 10 分钟）
```

## 文档

- [设计文档](docs/superpowers/specs/2026-08-13-crawl4ai-mcp-design.md)
- [实施计划](docs/superpowers/plans/2026-08-13-crawl4ai-mcp-implementation.md)
- [运维指南](docs/operations.md) — 状态/日志/内存/策略清理/密钥轮换/回滚
- [Rayobyte 契约](docs/provider-contracts/rayobyte.md) — 抓取 API 请求/响应格式（实测捕获）
