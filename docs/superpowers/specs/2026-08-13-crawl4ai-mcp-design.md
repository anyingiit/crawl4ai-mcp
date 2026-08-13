# crawl4ai-mcp 设计文档

日期：2026-08-13
状态：已批准，待实施

## 1. 目标与问题陈述

为 opencode 提供一个自托管的网页抓取能力，同时满足两个通常互相冲突的要求：

- **强反爬**：遇到有反爬防护的站点仍能拿到内容
- **控制资源开销**：绝大多数页面不应该付出浏览器的代价

核心手段是**成本递增的级联升级**：从零开销的 HTTP 抓取起步，只在被明确拦截时才逐级升级到更贵的手段，并按域名记住结论以避免重复爬梯子。

## 2. 关键决策与依据

### 2.1 不使用官方 Docker 镜像，改为薄封装层

官方 `unclecode/crawl4ai` Docker server 在代码层面锁死了反爬所需的两个核心手段：

- `deploy/docker/egress_broker.py:195-203` — `enforce_egress()` 无条件清空 `proxy_config` 并替换为本地 DNS-pinning 代理。外部代理不可用。
- `deploy/docker/crawler_pool.py:113` — 构造 `AsyncWebCrawler` 时不传 `browser_adapter`，`UndetectedAdapter`（patchright）永远不可达。
- `crawl4ai/async_configs.py:203-217` — 来自网络请求体的配置被禁止设置 `simulate_user`/`override_navigator`/`magic`/`proxy_*`。

绕过这些需要 patch 上游文件，与上游耦合度高于自己写一层封装。此外官方镜像常驻 Redis + Gunicorn + 浏览器池，空载约 1-1.5GB，与「控制资源开销」目标冲突。

**决策**：不用官方 server，把 crawl4ai 当作库使用，自建 MCP 服务。

### 2.2 架构杠杆点：AsyncCrawlerStrategy

`crawl4ai/async_crawler_strategy.py:36-44` 定义的 `AsyncCrawlerStrategy` 是一个只有 `crawl()` 一个方法的抽象基类，返回 `AsyncCrawlResponse`（`crawl4ai/models.py:331-347`，字段：`html`、`response_headers`、`status_code`、`redirected_url` 等）。

这意味着「怎么把 HTML 弄到手」与「怎么把 HTML 变成干净 markdown」彻底解耦。因此：

- 每个 Tier 实现为一个 `AsyncCrawlerStrategy` 子类，只负责获取 HTML
- crawl4ai 的 scraping / markdown / extraction 流水线被所有 Tier 共享复用
- Tier 0 的 curl_cffi 与 Tier 6 的 Firecrawl 输出同样格式的结果，调用方无感

### 2.3 已排除的方案

**CapMonster（不纳入）**：Cloudflare 的 `cf_clearance` cookie 被绑定到解题时使用的 IP + User-Agent + TLS 指纹三元组（Cloudflare 官方文档及 CapMonster 自身文档均确认）。CapMonster 的 "Cloudflare Challenge" 任务类型明确要求自带代理。本项目可用的代理（Webshare、Oxylabs）经确认**全部是数据中心 IP**（Oxylabs 的 5 个 IP 归属 HostRoyale Technologies，connection type 为 Datacenter/hosting），没有住宅 IP，解出的 cookie 拿到本机 IP 上会立即失效并重新挑战。

唯一 proxyless 可用的 `TurnstileTask` 针对的是表单内嵌的验证码 widget（登录/注册场景），而非拦在页面前的 "Just a moment" 拦截页，对抓取用途无价值。

**结论**：CapMonster 在没有住宅代理的前提下对本项目无效。接口预留，待获得住宅代理后再启用。

**Exa（不纳入级联）**：Exa 返回其索引中的缓存正文，完全不访问目标站。语义上与「抓取这个 URL 的当前内容」不同（可能过时、可能未收录）。不放进级联，需要时直接使用已有的搜索工具。

## 3. 系统架构

```
opencode ──MCP(streamable-http)──> crawl4ai-mcp daemon (systemd --user, 常驻)
                                        │
                                        ├─ 域名策略记忆 (SQLite)  ← 决定从哪一 Tier 起跳
                                        │
                                        ├─ 级联执行器 ──> Tier 0..6
                                        │
                                        ├─ 拦截检测器  ← 决定是否升级、升到哪
                                        └─ 浏览器生命周期管理（懒启动 + 空闲回收 + 并发信号量）
```

### 3.1 Tier 定义

Tier 顺序遵循一条原则：**成本严格递增，且每一层解决的是不同种类的失败**。

| Tier | 名称 | 实现方式 | 内存 | 延迟 | 解决什么 |
|---|---|---|---|---|---|
| 0 | `http` | 自写：`curl_cffi.AsyncSession(impersonate="chrome131")` | ~20MB | 50-300ms | TLS/JA3 指纹检测 |
| 1 | `stealth` | crawl4ai 原生 `BrowserConfig(enable_stealth=True)` | ~150MB | 1-3s | JS 渲染 + 基础检测 |
| 2 | `undetected` | `AsyncPlaywrightCrawlerStrategy(browser_adapter=UndetectedAdapter())` | ~200MB | 2-4s | 深度浏览器指纹 |
| 3 | `camoufox` | 自写：`camoufox.server.launch_server` + `firefox.connect(ws)` | ~350MB | 3-6s | 最强免费指纹伪装 |
| 4 | `proxy` | Tier 2 配置 + `proxy_config` 轮转 | ~200MB | 3-8s | 本机 IP 被限速/封禁 |
| 5 | `rayobyte` | 自写：HTTP 调 Rayobyte Scraping API | ~5MB | 3-10s | 托管指纹 + 代理池 |
| 6 | `firecrawl` | 自写：HTTP 调 Firecrawl `/v2/scrape` | ~5MB | 3-15s | 最终兜底 |

分界线：Tier 0-3 是免费本地手段；Tier 3 失败通常说明缺的是「干净的住宅 IP」，本地再怎么伪装都无解，只能依靠托管服务（Tier 5/6）。

### 3.2 各 Tier 实现要点

**Tier 0 是新增能力**。上游 `AsyncHTTPCrawlerStrategy`（`crawl4ai/async_crawler_strategy.py:2466`）是纯 aiohttp 实现（见 `:2548` 的 `aiohttp.TCPConnector`），**没有** TLS 指纹伪装能力。`HTTPCrawlerConfig`（`async_configs.py:1250-1277`）也不含 impersonate 选项。因此必须自写基于 curl_cffi 的策略。curl_cffi 已确认有 aarch64 wheel（`curl_cffi-0.16.0-cp310-abi3-manylinux2014_aarch64`）。

**Tier 1/2 直接复用上游**，零自写抓取代码。Tier 2 仅需在构造 crawler 时传入 `browser_adapter=UndetectedAdapter()`（`async_crawler_strategy.py:75-96` 支持该参数）。patchright 已在 crawl4ai 的 requirements 中（`requirements.txt:12-16`），无新增依赖。

**Tier 3 Camoufox 必须独立进程**。crawl4ai 的 `BrowserManager` 通过 `chromium.connect_over_cdp` 连接（`browser_manager.py:629`），而 Camoufox 是 Firefox，需通过 `firefox.connect(ws://...)` 连接。二者协议不兼容，Camoufox 无法接入 crawl4ai 的 BrowserManager，必须实现为独立的 `AsyncCrawlerStrategy`，以 `launch_server()` 起常驻 WS 服务（懒启动、空闲关闭）。

**Tier 4 的现实定位**：可用代理为 Webshare 10 个数据中心 IP（1GB/月带宽）+ Oxylabs 5 个数据中心 IP。它**不是**用来打 Cloudflare 的（数据中心 IP 的 ASN 会被直接识别），而是解决「同一站点抓取过多导致本机 IP 被 429/封禁」。带宽极其有限，须谨慎使用。

**Tier 5/6 输出归一化**：托管 API 可能直接返回 markdown 或返回 HTML。若返回 HTML 则照常送入 crawl4ai 流水线；若已是 markdown 则直接包装为结果，跳过流水线。

### 3.3 拦截检测器与升级路由

这是整个设计中最容易出错的部分。判定不准会导致该升级时不升级（拿不到内容），或不该升级时升级（浪费额度）。

正文字数阈值定为 **200 字**（去除标签后的可见文本），可在 `config.toml` 中调整。

| 信号 | 判定 | 动作 |
|---|---|---|
| HTTP 200 + 正文 ≥ 200 字 | 成功 | 返回，记忆该 Tier |
| HTTP 200 但正文 < 200 字且存在 `<script>` | JS 未渲染 | 升到下一 Tier |
| HTTP 200 且正文 < 200 字但无 `<script>` | 页面本身就短 | 成功，返回 |
| 标题含 `Just a moment` / `Attention Required`，或页面含 `cf-challenge` / `turnstile` / `__cf_chl` | Cloudflare 挑战 | **跳过 Tier 4**，升级路径为 Tier 2 → 3 → 5 → 6 |
| `403` 且带 Cloudflare 标志 | 同上 | 同上 |
| `429`、`503` 且带 `Retry-After` | 限速 | **优先 Tier 4**（换 IP） |
| `404` / `410` / `401` | 页面本身问题 | **立即停止**，不升级、不消耗额度 |
| DNS 失败 / 连接超时 | 网络问题 | 重试一次，不升级 |

`404` 不触发级联是硬性要求：一个不存在的 URL 绝不应该逐级烧到 Firecrawl credits。

## 4. 资源控制

### 4.1 背景：2026-08-05 事故

本机 `~/.config/systemd/user/opencode.service.d/` 的整改记录显示：opencode 单进程曾涨到 21.09 GiB / 23.3 GiB（90.5%），而 `memory.max=max`，导致应用层故障升级为整机颠簸活锁 2 小时 50 分。处置方案是施加 cgroup 内存硬限。

一个运行 Chromium 的新服务必须从第一天就带同样的护栏，否则是在重演同一事故。

### 4.2 内存模型

| 状态 | 常驻内存 |
|---|---|
| 空载 | ~80MB（仅 Python 进程 + SQLite，无浏览器） |
| Tier 0 抓取中 | ~100MB |
| Tier 1/2 抓取中 | ~250MB |
| Tier 3 抓取中 | 额外 ~350MB（Camoufox 独立 Firefox 进程） |
| Tier 5/6 抓取中 | ~85MB |

### 4.3 三条硬性资源纪律

1. **懒启动 + 空闲回收**：浏览器只在实际升级到 Tier 1+ 时启动；空闲 180 秒无请求即完全关闭（`browser.close()` 且 playwright 进程退出）。Camoufox 空闲 120 秒关闭。稳态下服务应为约 80MB 的纯 Python 进程。

2. **cgroup 硬限**：`MemoryHigh=1.5G`、`MemoryMax=2.5G`、`MemorySwapMax=0`。依据：正常峰值（Chromium 与 Camoufox 同时运行）约 700MB，2.5G 留约 3 倍余量；即使 Chromium 泄漏失控也被 cgroup OOM 击杀而不波及整机。配合 `Restart=always` 自愈。

3. **全局并发信号量**：同时最多 2 个浏览器页面（本机 4 核，且需与 opencode 共存）。Tier 0 可放宽至 8 并发（无浏览器）。避免 agent 批量抓取时开出大量 Chromium。

### 4.4 对上游已知内存泄漏的对策

社区反复报告 crawl4ai 在 Docker 长期运行时内存爬升、chrome 进程残留（issues #1256、#1608、#943、#742）。本设计的空闲即关闭策略从根本上规避该问题：浏览器生命周期以分钟计而非以天计，不存在累积泄漏的窗口。cgroup 硬限为第二道防线。

## 5. 状态存储

SQLite 单文件，一张表：

```sql
domain_policy(
  domain           TEXT PRIMARY KEY,
  best_tier        TEXT,
  last_success_at  INTEGER,
  fail_count       INTEGER,
  cooldown_until   INTEGER,
  last_error_kind  TEXT,
  updated_at       INTEGER
)
```

行为规则：

- **成功** → 记录 `best_tier`。下次同域名**直接从该 Tier 起跳**，不再逐级爬梯。这是省时间和省额度的主要机制。
- **全部 Tier 失败** → 设置 `cooldown_until`，指数退避：10 分钟 → 1 小时 → 6 小时 → 24 小时。冷却期内该域名直接返回缓存的失败结论，**不重跑级联**。
- **记忆衰减**：`best_tier` 超过 7 天未验证则降一级重试，避免站点已放松防护后仍永久锁定在昂贵 Tier。

按用户决策，**不设置每日配额上限**。因此冷却退避是唯一的额度消耗防线，其可靠性是关键，必须在验收中单独验证（见 §8 验收项 10）。

## 6. 部署形态

### 6.1 目录结构

```
~/Workspace/crawl4ai-mcp/          # 独立 git 仓库
├── .venv/                          # 独立虚拟环境，从 PyPI 安装 crawl4ai==0.9.2
├── src/crawl4ai_mcp/
│   ├── server.py                   # FastMCP，streamable-http on 127.0.0.1:11236
│   ├── cascade.py                  # 级联执行器 + 拦截检测
│   ├── policy.py                   # SQLite 域名策略记忆
│   ├── browser.py                  # 浏览器懒启动 / 空闲回收 / 并发信号量
│   └── tiers/                      # 7 个 Tier 策略实现
├── config.toml                     # Tier 开关、各 Tier 超时、正文字数阈值、空闲回收秒数、并发上限、代理列表
├── .env                            # API keys（权限 0600，不进 git）
└── systemd/crawl4ai-mcp.service
```

与 `~/Workspace/crawl4ai`（上游 clone）完全解耦，上游仅作参考阅读。升级 crawl4ai 只需改版本号。

### 6.2 运行时

- **端口 11236**，绑定 `127.0.0.1`（已确认 11235 与 11236 均空闲；避开官方镜像惯用的 11235）。opencode 在同机，无需对外暴露、无需 token 认证。
- **传输选择 streamable-http**：opencode 的 `type:"remote"` 会先尝试 Streamable HTTP 再回落到 SSE（opencode issue #16247 确认此行为），使用现代传输可避开 SSE 的已知挂起问题（#8406、#17168）。
- **API keys 存放于 `.env`（0600）**，由 systemd `EnvironmentFile=` 加载。与现有 `~/.config/opencode/server.env` 的做法一致：不进 git、不进 journal。

### 6.3 opencode 接入

写入 `~/.config/opencode/opencode.jsonc` 的 `mcp` 段：

```jsonc
"crawl4ai": {
  "type": "remote",
  "url": "http://127.0.0.1:11236/mcp",
  "enabled": true
}
```

权限沿用现有模式：`permissions/interactive.jsonc` 中设 `"crawl4ai_*": "ask"`，但 `crawl4ai_scrape` 与 `crawl4ai_map` 放行；`permissions/longrun.jsonc` 中全部放行。

## 7. MCP 工具接口

暴露 4 个工具：

```
scrape(url, format="markdown", max_tier="firecrawl", force_tier=None)
  → { content, tier_used, cost_kind, elapsed_ms, status }
  单页抓取，走完整级联。force_tier 用于调试或绕过策略记忆。

crawl(url, max_pages=10, max_depth=2, include_pattern=None)
  → { pages: [{url, content, tier_used}], stats }
  站内多页抓取。同域共享策略记忆，首页确定 Tier 后续页直接复用。

map(url, search=None, limit=100)
  → { urls: [...] }
  枚举站内 URL（crawl4ai URL seeder：sitemap + Common Crawl）。零抓取成本。

diagnose(domain=None)
  → { domain_policies, browser_state, memory_mb, tier_availability, recent_failures }
  运维视图：域名策略表、浏览器状态、当前内存、各 Tier 可用性（对应 key 是否已配置）。
```

返回值中的 `cost_kind` 取值为 `free` / `proxy_bandwidth` / `rayobyte_credit` / `firecrawl_credit`，使模型与用户都能感知本次抓取的成本类型。

按用户决策，级联**默认全自动升级至顶层（含 Firecrawl）**。`max_tier` 参数允许调用方主动封顶。

## 8. 验收标准

| # | 验证项 | 方法 | 通过标准 |
|---|---|---|---|
| 1 | Tier 0 | 抓静态文档站（如 docs.python.org 某页） | `tier_used=http`，<500ms，正文完整 |
| 2 | Tier 0→1 升级 | 抓纯 JS 渲染的 SPA | 自动升到 `stealth`，拿到渲染后内容 |
| 3 | Tier 1→2 升级 | 抓已知有基础 bot 检测的站 | 自动升到 `undetected` 并成功 |
| 4 | Tier 3 可用性 | 强制 `force_tier=camoufox` | ARM64 上 Camoufox 能启动并返回内容 |
| 5 | Tier 4 代理 | 强制 `force_tier=proxy`，抓 IP 回显站 | 返回的 IP 为代理 IP，非本机 IP |
| 6 | Tier 5/6 | 分别强制 `force_tier=rayobyte` / `firecrawl` | 各自返回内容，`cost_kind` 标注正确 |
| 7 | 完整级联 | 抓 Cloudflare 硬站 | 逐级升级最终成功，或干净失败并记录 cooldown |
| 8 | 404 不烧钱 | 抓一个 404 URL | 立即返回失败，`tier_used=http`，无任何升级 |
| 9 | 策略记忆 | 对同一难站抓两次 | 第二次 `tier_used` 相同且耗时大幅下降 |
| 10 | 冷却退避 | 对必定失败的域名连抓两次 | 第二次直接返回缓存失败，无级联、无额度消耗 |
| 11 | 空载内存 | 服务启动后静置 5 分钟 | RSS < 120MB，`ps` 中无 chromium/firefox 残留 |
| 12 | 浏览器回收 | 抓一次 Tier 1 后静置 4 分钟 | 浏览器进程消失，RSS 回落至空载水平 |
| 13 | cgroup 限制 | `systemctl --user show crawl4ai-mcp -p MemoryMax` | 返回 2.5G |
| 14 | 重启自愈 | `kill -9` 主进程 | systemd 5 秒内拉起，服务恢复 |
| 15 | opencode 接入 | 在 opencode 中实际调用 `crawl4ai_scrape` | 工具可见、调用成功、返回 markdown |

验收项 11 与 12 是「控制资源开销」目标的真正验收点。若空载内存不在约 80MB 量级，或浏览器不回收，则设计判定为失败，必须返工修复而非接受现状。

## 9. 实施顺序

每步可独立验证，避免大爆炸式集成：

1. 项目骨架 + venv + 安装 crawl4ai，跑通一次原生 `arun`
2. Tier 0/1/2（免费本地层）+ 拦截检测器 → 验收 1、2、3、8
3. SQLite 策略记忆 + 冷却退避 → 验收 9、10
4. FastMCP 包装 + 4 个工具 → 本地 curl 验证
5. systemd 单元 + cgroup 限制 + 浏览器回收 → 验收 11、12、13、14
6. opencode 接入 → 验收 15
7. Tier 4/5/6（代理与托管 API）→ 验收 5、6、7
8. Tier 3 Camoufox → 验收 4

Camoufox 排在最后，因为它是唯一有平台风险的组件（见 §10）。即使它在 ARM64 上无法工作，前 7 步交付的功能已完整可用。

## 10. 已知风险

**ARM64 平台风险（Camoufox）**：本机为 aarch64。Camoufox 官方提供 `camoufox-152.0.4-beta.28-lin.arm64.zip`（654MB），但其下载量为 22,298，而 x86_64 版为 232,957 —— arm64 构建的实测覆盖低一个数量级，边缘 bug 风险更高。此外 Camoufox 上游明确声明 "in active development and may not be suitable for a production environment"。

缓解措施：Tier 3 必须支持通过 `config.toml` 一键禁用，禁用或失败时自动跳到 Tier 5。

**指纹一致性风险**：Camoufox 在 ARM 主机上伪装 Windows/macOS 桌面指纹时，某些底层特征可能不匹配。此项须实测确认，已纳入验收项 4。

**代理带宽极小**：Webshare 免费层为 1GB/月。Tier 4 若被频繁触发会迅速耗尽。缓解措施：Tier 4 仅在检测到限速类错误（429/503）时触发，不参与 Cloudflare 挑战的升级路径。

**无每日配额上限**：按用户决策不设配额。冷却退避机制因此成为唯一防线，其正确性由验收项 10 保障。
