独立调查结论：抓取可靠性问题属实；目前不能据此给出全量生产请求的成功率，也不能断言最近发生了整体退化。已复现正常正文被拒绝、挑战页被当成成功、浏览器可用性虚报，以及会放大失败的级联与域名冷却策略。

问题跟踪见[问题清单及成功率、效率、成本分类](issues.md)。清单将合并问题拆分编号，标明证据强度、影响条件、待验证风险和验收方向；后续跟踪优先级以该清单为准。

调查时间：2026-09-14 UTC。代码：`6ea99eb`。只增加调查材料，未修改生产源码、配置、浏览器安装或生产策略库，未重启服务。在线抓取使用同目录 `.venv`、部署配置与凭据，以及临时独立策略库；生产 MCP 只调用 `diagnose`。因此，部署诊断与新建实例的实测分别列示，不把新建实例等同于常驻进程的全部历史行为。

调查范围与证据边界：

- 未获得用户所指最近代理报告的原文，仓库内也未找到相应报告或历史验收结果。本结论独立于该报告。
- 检查实现、运行中 MCP 诊断、近期 systemd 日志，执行非联网测试和定向联网复测。`pytest -q -m 'not live'`：**511 passed, 21 deselected，10.60 秒**。未执行会重启服务的完整资源生命周期验收。
- 服务当时为 active，自 2026-09-02 17:57:24 UTC 启动，`NRestarts=0`。诊断的七层全部 `ready=true`，保留了 11 条失败记录，其中包括 Cloudflare 文档、Redis、Laravel。这只是失败记录，不能作为成功率的分母。
- 10 个 URL 来自近期日志/失败诊断，加上 example、Python、JS quotes 控制样本；这是偏向问题复现的小样本，不是随机抽样。先逐 URL 清空临时策略，用 HTTP→STEALTH→UNDETECTED 复测；再对失败 URL 开放完整七层兜底。没有测试 `map`、大规模 `crawl` 的整体完成率。
- 首轮 UNESCO 校验串误设为 Great Mosque；官方页面实际是 Mogao Caves。保留首轮原始结果，在复测中更正，不能把这一处校验错误归咎于工具。

本轮分阶段复测的结果：最初限定前三个免费层，10 页中 6 页返回 success，其中 Nature 为假成功，实际 5 页取得目标正文；对其余 4 个失败页开放完整七层后，Direct Upload 被救回，另 3 页仍失败。合并这轮分阶段结果，**10 页中 6 页取得目标正文、3 页明确失败、1 页假成功**。这是问题样本上的观察值，不是生产总体成功率，也不是 10 页同时独立运行默认配置的统计估计。UNESCO 按更正后的标记和复测计入成功。

| 样本 | 前三层结果 | 开放七层后的追加复测 | 内容判断 |
|---|---|---|---|
| example.com | HTTP success | 无需追加 | 目标正文 |
| Python asyncio | HTTP success | 无需追加 | 目标正文 |
| JS quotes | undetected success；stealth 启动失败 | 无需追加 | 目标正文 |
| Cloudflare Direct Upload | failed；HTTP 正文被误报 | Firecrawl success，21.07 秒 | 目标正文，存在不必要升级 |
| Cloudflare Waiting Room plans | failed；HTTP 正文被误报 | failed，10.30 秒 | 未交付正文 |
| Redis distributed locks 旧 URL | failed | failed，11.88 秒 | 未交付正文 |
| Laravel 12.x Query Builder | failed；HTTP 正文被误报 | failed，39.90 秒 | 未交付正文 |
| Nature 论文 | HTTP success | 因已误报成功，正常路由不会升级 | Client Challenge，假成功 |
| UNESCO Mogao Caves | HTTP success | 更正校验标记后，HTTP success | 目标正文 |
| Kubernetes Disruptions | HTTP success | 无需追加 | 目标正文 |

独立提供商探针：Camoufox、Rayobyte、Firecrawl 均能抓到 example.com 的正确内容；proxy 当次隧道失败。Firecrawl 还能直接拿到 Direct Upload 正文。因此没有证据支持“所有付费供应商失效”或“只能通过加额度解决”。上线服务历史上 9 月 13 日的 Direct Upload 全层失败，在本次新实例中未按原样重现；当天的详细 attempts/响应没有持久化，不能事后确定当时的兜底失败原因。

实测事实与整改优先级如下。

| 优先级 | 已确认问题 | 证据与影响 | 建议及验收条件 |
|---|---|---|---|
| P0 | Cloudflare 检测对普通文本误报 | `detect.py:25` 在完整 HTML 和 headers 字符串里匹配通用词。Direct Upload 的 HTTP 200、9,925 字符可见文本、正确标题和正文均已获得，仅命中 `turnstile` 就判挑战；Waiting Room 和 Laravel 查询文档同样复现。Laravel 正文 78,514 字符仍被拒绝。 | 去掉通用单词“一票否决”；优先检查结构化 `cf-mitigated: challenge`，其他信号结合挑战 DOM、标题、正文质量综合判断。普通文档提到 Turnstile、挑战代码示例、带正常验证组件的页面必须保留；真正挑战页必须继续被识别。 |
| P0 | 挑战页被标成成功 | Nature 论文 URL 的 HTTP 200 页面标题为 `Client Challenge`，可见文本 315 字符，没有论文标题；因超过 200 字符阈值返回 `success`。此时不会触发其他层。 | 成功必须通过内容质量检测，并在生成 Markdown 后再次检查。识别通用挑战、登录墙、空页和 JS shell，保留失败原因并继续合适的兜底。该 Nature 反例必须不再报告正文成功；不能只提高字数阈值。 |
| P0 | 当前 stealth 无法启动，但显示 ready | 用部署虚拟环境实测，Playwright 缺少 `chromium_headless_shell-1223/chrome-linux/headless_shell`。多个 URL 重复失败。`BrowserProvider.availability()` 对 stealth 无条件 ready。独立 proxy 基础探针也出现 `ERR_TUNNEL_CONNECTION_FAILED`，而诊断仍 ready。 | 安装与当前虚拟环境匹配的 Chromium/headless shell，锁定依赖和浏览器产物；验收必须强制只用 stealth 抓取 JS quotes，不能靠 undetected 掩盖故障。区分 configured、binary_present、probe_ok 和 last_error；代理逐出口验证。单次隧道失败不足以确定凭据或供应商的具体根因。 |
| P1 | 错误类别过宽，级联过早停止 | `HttpProvider.fetch()` 把 session 构造及请求的任意异常都标为网络错误；级联同层试两次即停止。离线复现中即使提供健康浏览器，该浏览器调用次数仍为 0。 | 按异常类型区分本地依赖故障、TLS/协议兼容、目标不可达与供应商故障；可恢复错误允许免费层有限兜底。目标确认不可达、安全策略拒绝仍快速停止。付费兜底受显式预算和原有 `max_tier` 约束，避免把修复变成无条件付费重试。 |
| P1 | 单页或供应商失败扩大成整域名冷却 | `policy.py` 按 host 记录失败，600→3,600→21,600→86,400 秒。离线复现中 `/a` 失败后，健康 `/b` 不抓就返回 cooldown。`max_tier=http` 的受限测试也能污染后续全层请求。配置的 `cooldown_seconds` 没有接入此计算。 | 供应商故障熔断供应商，内容失败按 URL/路径归因，只有足够证据的整站限流才冷却域名。受限层测试与正常请求分开计数，允许有预算的恢复探针；接通冷却配置并给出原因。 |
| P1 | 记忆高层失败后不会回探低层 | 级联从记忆层开始，只向更高层追加。离线构造 Firecrawl 配额失败、HTTP 健康，结果仍 failed，HTTP 调用次数为 0。 | 在供应商故障或记忆层不可用时，预算内回探未试过的低成本层；记忆记录健康度、时效和适用路径，避免单个页面永久影响全站。 |
| P2 | 短页、时延与监控口径不完整 | 空 HTML 200 和短拒绝页判 `short_static`；已渲染短正文只因存在 analytics script 仍判 `needs_js`。响应耗时仅累加 provider fetch，不包括渲染和全部策略工作。`recent_failures` 只保存 scrape 的 failed，最多 50 条且不持久化；未涵盖 cooldown、terminal、异常或假成功。 | 增加短正文正反例、渲染稳定性判据和总 deadline；分别统计有效正文率、工具成功率、超时率、冷却率、分层耗时与成本。记录每次请求/尝试的判据、跳过原因和内容质量分，不能只统计 HTTP 200 或 `status=success`。 |

Cloudflare 官方提供了 `cf-mitigated: challenge` 的结构化识别依据；Playwright 官方说明每个版本需要匹配的浏览器二进制，升级后可能需要重新安装。这两项建议有明确上游依据：[Cloudflare 检测挑战响应](https://developers.cloudflare.com/cloudflare-challenges/challenge-types/challenge-pages/detect-response/)、[Playwright 浏览器安装](https://playwright.dev/python/docs/browsers)。这些建议仍需验证供应商是否透传目标响应头，不能把缺少该头等同于没有挑战。

额外的内容与网络适配工作应排在上述 P0 之后：HTTP 重定向当前每跳创建新 session，Cookie 不继承；对依赖 Cookie 的跳转可能不利，但本轮未将其确认为某个真实失败的根因。Redis 旧 URL 返回新地址的壳页，undetected 报结构性反爬失败，需要针对重定向/渲染完成再做专门回归；不能仅据此次表现宣称 Redis 全站不可抓。

建议先修 P0 的判定与部署健康度，再修 P1 的路由和冷却。上线验收应包含本次真实失败样本与稳定的本地正反例：正常文档包含 Turnstile、200 挑战页、合法短页、空页、缺失浏览器、单页失败不冻结兄弟 URL、供应商熔断不污染域名、记忆高层故障后低层恢复。随后建立固定分层样本集，在不同时间重复运行并记录有效正文、延迟和费用。真实 404、需要登录的页面和策略拒绝应单独报告，不能混入可公开抓取页面的成功率。现阶段不建议优先增加第八层或简单购买更多代理额度。

复现与材料：

- [免费层逐页原始证据](live-free.json)：含每次抓取的状态、标题、正文长度、匹配词与响应元数据。
- [完整七层复测](live-full-rechecks.json)、[提供商基础探针](providers-live.json)、[部署诊断摘要](diagnose.json)、[离线反例](synthetic.json)。
- `.venv/bin/python docs/investigations/2026-09-14/reproduce_synthetic.py` 可无网络重现策略反例。
- `.venv/bin/python docs/investigations/2026-09-14/reproduce_free.py` 使用临时策略库，要求本地 MCP 可读诊断，会覆盖同目录本轮证据。
- `CRAWL4AI_MCP_LIVE_TESTS=1 .venv/bin/python docs/investigations/2026-09-14/reproduce_full.py` 和 `reproduce_providers.py` 会调用已配置付费层并覆盖相应证据；不应将其混入默认单元测试。
