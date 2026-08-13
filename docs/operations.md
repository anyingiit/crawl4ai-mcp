# crawl4ai-mcp 运维指南

本服务以 systemd user unit 常驻本机，只监听 `127.0.0.1:11236`。本文档覆盖日常运维、资源监控、出口安全、提供商模型、完整验收、密钥轮换与回滚。

## 0. 完整部署验收

验收脚本要求显式 opt-in，跑 `tests/acceptance` 全部用例（含真实层级与资源生命周期，约 10 分钟），并把 JUnit XML 与运行日志持久化到 `.superpowers/sdd/acceptance/`（可用 `ACCEPTANCE_ARTIFACT_DIR` 覆盖）：

```bash
CRAWL4AI_MCP_LIVE_TESTS=1 scripts/run-acceptance.sh
```

| 退出码 | 输出 | 含义 |
|---|---|---|
| 0 | `acceptance complete` | 零失败/错误/跳过，部署可认证 |
| 1 | `acceptance failed` | 至少一个失败或错误 |
| 2 | — | 未 opt-in 或环境不可用 |
| 3 | `acceptance incomplete` | 无失败但有跳过（未配置的可选层），**不算通过** |

规则：`acceptance_required` 用例（Tier 0-2、安全拒绝、付费层不可达、Cloudflare 跳过、404、记忆层、冷却、资源生命周期、opencode 发现与调用）必须通过；`acceptance_optional` 用例（Camoufox/代理/Rayobyte/Firecrawl）只有在**禁用或未配置**时才允许跳过——已配置的提供商失败就是验收失败，不再有额度耗尽类跳过。任何跳过都会让脚本以 3 退出，明确区分「完整通过」与「部分验收」。

「禁用/未配置」由部署配置判定：验收的 service fixture 与运行中的服务共用同一份 `config.toml` 与 `.env`（仅策略库换成临时库）。因此 `enabled_tiers` 中不包含的层按「故意禁用」跳过（即使 `.env` 仍配置了密钥）；启用且已配置但不可用或失败的层一律判失败。判定逻辑不依赖网络：pytest 退出状态异常（中断/内部错误/零用例）或 JUnit 中 `tests==0` 时脚本必定以 1 退出，绝不误报成功；无 JUnit 证据时以 2 退出。

前置：`crawl4ai-mcp.service` 已部署运行、opencode 配置含 crawl4ai 远程入口（见 §8）、需要付费层时 `.env` 已配置对应密钥。

## 1. 服务状态、日志与启停

```bash
# 状态
systemctl --user status crawl4ai-mcp.service
systemctl --user show crawl4ai-mcp.service -p ActiveState -p MainPID

# 日志
journalctl --user -u crawl4ai-mcp.service -f            # 跟随
journalctl --user -u crawl4ai-mcp.service -n 200        # 最近 200 行

# 重启 / 停止 / 启动
systemctl --user restart crawl4ai-mcp.service
systemctl --user stop crawl4ai-mcp.service
systemctl --user start crawl4ai-mcp.service

# 健康检查
curl -fsS http://127.0.0.1:11236/health                # {"status":"ok"}
```

`Restart=always` + `RestartSec=5` 保证进程被 `kill -9` 后约 5-10 秒内自愈。如需禁止开机自启：`systemctl --user disable crawl4ai-mcp.service`。

## 2. cgroup 内存监控

服务被 systemd 限制在独立 cgroup：`MemoryHigh=1536M`（软限）、`MemoryMax=2560M`（硬限，触发 OOM kill）、`MemorySwapMax=0`（禁用 swap）、`KillMode=control-group`（重启/停止时连浏览器子进程一起回收）。

```bash
# 当前内存（单位字节）
cat /sys/fs/cgroup/user.slice/user-$(id -u).slice/user@$(id -u).service/app.slice/crawl4ai-mcp.service/memory.current

# systemd 视角
systemctl --user show crawl4ai-mcp.service -p MemoryCurrent -p MemoryPeak -p MemoryHigh -p MemoryMax -p KillMode -p Restart

# 峰值（自启动以来）
systemctl --user show crawl4ai-mcp.service -p MemoryPeak
```

预期：空闲（无浏览器）约 80-100 MiB；Chromium 抓取时约 250 MiB；Chromium 与 Camoufox 同时运行时约 700 MiB。空闲 180 秒后浏览器应被回收，内存回到 100 MiB 以下。验收的资源用例直接读服务自身 cgroup（`cgroup.procs`），不做全局进程匹配；空闲回收不打断进行中的抓取（`reap_idle` 在活动抓取期间直接返回）。

## 3. 出口安全（egress）

- 只允许 `http/https` 公共地址：私网、回环、链路本地、文档段等一律拒绝（`non_global_address`）；`file://` 等非 HTTP scheme 拒绝（`unsupported_scheme`）；userinfo、非常规端口、IPv6 zone id、控制字符同样拒绝
- 域名每请求全量解析并逐地址校验；HTTP 层 `CurlOpt.RESOLVE` 钉住解析结果，curl 关闭自动跳转，重定向逐跳重新解析复核；每一跳实际请求规范化后的 URL（尾点主机名无法绕过 pin）；浏览器子资源与 seeder 流量同策略，非公共地址 abort
- 被拦截的主框架导航在浏览器层归一化为 `policy_error`；本地 pinning 代理只回固定公开原因（`403 blocked by policy` / `502 tunnel failed`），不反射 URL/凭据/内部异常文本
- 上游代理配置仅接受 `http/https`，URL 不得含 userinfo/path/query/fragment（凭据走显式字段），非法端口与缺失主机直接报错
- 同源 = 规范化 scheme/host/有效端口 比较，重定向逃逸即拒绝

## 4. 提供商模型与失败语义

`diagnose` 的 `providers` 给出每层 `{enabled, ready, reason}`：

- 未配置 = 不可用：密钥/代理留空时 `ready=false`，级联跳过该层，`diagnose` 明示原因
- 已配置但失败 = 真失败：级联按规则处理或返回失败，绝不静默跳过

`attempts[]` 逐条记录 `tier`（小写层级名）、`decision`、`cost_kind`、`target_status_code`、`provider_status_code`、`provider_error_kind`、`provider_error`、`error`。提供商失败枚举：

| `provider_error_kind` | 含义 |
|---|---|
| `auth` | 凭证无效 |
| `quota` | 额度耗尽（Rayobyte 429/额度错误、Firecrawl 402 归一化而来） |
| `rate_limit` | 提供商限流 |
| `transport` | 与提供商之间的传输失败 |
| `service` | 提供商服务端错误 |
| `malformed_response` | 响应不符合契约 |

行为要点：

- 目标网络错误（连接拒绝/超时）只**同层重试一次**即停止并进入 `target_network` 冷却，绝不上探付费层
- Cloudflare 挑战一旦出现，本请求内粘性跳过 PROXY 层（`cloudflare_seen` 逐请求重置）
- Tier 4（proxy）每次抓取独立构造 `proxy_config` 逐条轮转（WebShare + Oxylabs 合并列表），不跨请求共享
- 401/404/410 立即终止不升级；429/503 带 `Retry-After` 优先走代理

## 5. 域名策略检查与清理

`diagnose` MCP 工具返回 `domain_policies`、`recent_failures`、`providers` 可用性与 `browsers` 状态；`domain` 参数接受裸主机名（`example.com`，规范化后匹配）或完整 URL，私网/回环字面量与非法主机名会直接报错。清理某个域名的记忆需停止服务后直接操作 SQLite：

```bash
systemctl --user stop crawl4ai-mcp.service
sqlite3 ~/.local/state/crawl4ai-mcp/policy.db "DELETE FROM domain_policy WHERE domain='example.com';"
systemctl --user start crawl4ai-mcp.service
```

不要新增 MCP 工具或给 `diagnose` 加破坏性参数；清理一律走上述本地命令。

## 6. 启用/禁用 Camoufox

Camoufox 可独立关闭。复制示例配置并编辑：

```bash
cp config.example.toml config.toml   # 已在工作目录则直接编辑
systemctl --user restart crawl4ai-mcp.service
```

在 `config.toml` 中：

```toml
enabled_tiers = ["http", "stealth", "undetected", "proxy", "rayobyte", "firecrawl"]  # 去掉 camoufox
```

或在 `config.toml` 保留 `camoufox` 但让 `availability` 报告不可用（浏览器产物缺失时自动发生）。Camoufox 不可用时级联直接跳到 Rayobyte，不影响其余层级。

## 7. 密钥与代理轮换

全部密钥在 `.env`（权限 600，禁止提交）：

```dotenv
RAYOBYTE_API_URL=https://api.scraping.rayobyte.com/
RAYOBYTE_API_KEY=
FIRECRAWL_API_KEY=
WEBSHARE_PROXIES=
WEBSHARE_PROXY_USERNAME=
WEBSHARE_PROXY_PASSWORD=
OXYLABS_PROXIES=http://dc.oxylabs.io:8000
OXYLABS_PROXY_USERNAME=
OXYLABS_PROXY_PASSWORD=
```

修改后：

```bash
chmod 600 .env
systemctl --user restart crawl4ai-mcp.service
```

- 代理列表用逗号分隔，逐条轮转（PROXY 层）。
- 代理 URL 只接受 `http/https`，不得含 userinfo/path/query/fragment；凭据走显式字段：WebShare 池用 `WEBSHARE_PROXY_USERNAME`/`WEBSHARE_PROXY_PASSWORD`，Oxylabs 池用 `OXYLABS_PROXY_USERNAME`/`OXYLABS_PROXY_PASSWORD`，两池互不混淆。URL 内若残留 userinfo（`http://user:pass@host:port`）会被拒绝；用户/密码只填一个视为配置错误（启动即报错，且 `diagnose` 中 PROXY 层 ready=false 并给出明确原因）。
- 无凭据的代理 URL 按未认证代理使用（适合真正免认证的端点）；需要认证的池必须成对提供用户名与密码。
- 密钥留空 = 该提供商不可用（`diagnose` 中 ready=false），服务照常启动。
- 更换密钥后旧的失效即可，无需清理策略库。

## 8. 识别付费层用量

`scrape` 结果中的 `cost_kind`：

| cost_kind | 含义 |
|---|---|
| `free` | 本地免费手段（HTTP/浏览器/Camoufox） |
| `proxy_bandwidth` | WebShare/Oxylabs 数据中心代理带宽 |
| `rayobyte_credit` | Rayobyte Web Scraper API 额度 |
| `firecrawl_credit` | Firecrawl 额度 |

`attempts` 数组按顺序记录了每一层尝试的 `tier`（小写层级名）、`decision`、`cost_kind`、`target_status_code`、`provider_status_code`、`provider_error_kind`、`provider_error`、`error`。`diagnose` 的 `recent_failures` 保留了最近失败的 URL 与耗时。

响应契约：`scrape` → `{url, status, content, tier_used, cost_kind, elapsed_ms, attempts, cooldown_until, error}`（`format` 仅 `markdown`/`html`，`html` 仅成功时返回原始 HTML）；`crawl` → `{pages, stats}`（同源重定向的页面报告规范化最终地址，同源重定向别名去重不二次抓取，跨源逃逸保持请求别名）；`map` → `{urls}`（limit 1..100，条目经完整规范化同源校验）；`diagnose(domain)` → `{rss_bytes, providers, browsers, recent_failures, domain_policies}`（domain 接受裸主机名或 URL）。层级名一律小写字符串。

## 9. 提供商额度耗尽

- Firecrawl 402「Insufficient credits」、Rayobyte 429/额度类错误被归一化为 `provider_error_kind=quota` 的失败结果（`attempts[]` 中可见）；级联会继续尝试（或返回 failed 并进入域名冷却）。
- 额度耗尽不崩溃：相关层 `availability().ready` 仍为 true（配置存在），但每次请求都会失败。**验收视角下这是失败而非跳过**——运营上建议：在 `config.toml` 的 `enabled_tiers` 中去掉该层（使其成为「禁用」从而合法跳过），或充值后在 `.env` 轮换密钥并重启。

## 10. opencode 配置回滚

opencode 配置在改动前会生成带时间戳的备份：

```bash
ls -la ~/.config/opencode/opencode.jsonc.bak-* ~/.config/opencode/permissions/*.bak-*
# 回滚示例
cp ~/.config/opencode/opencode.jsonc.bak-20260813-102903 ~/.config/opencode/opencode.jsonc
```

修改权限快照后需切换一次权限模式（`oc-mode interactive` / `oc-mode longrun`）才会合并生效。opencode 配置不是热加载的：改完 `opencode.jsonc` 需重启 opencode 会话（TUI 退出重开，或 `systemctl --user restart opencode`）。

opencode 侧的 crawl4ai 远程入口（`~/.config/opencode/opencode.jsonc` 的 `mcp` 段）：

```jsonc
"crawl4ai": {
  "type": "remote",
  "url": "http://127.0.0.1:11236/mcp",
  "enabled": true,
  "timeout": 120000
}
```

`opencode mcp list` 应显示 `crawl4ai connected`。若显示失败，先查 `systemctl --user status crawl4ai-mcp.service` 与 `curl http://127.0.0.1:11236/health`。验收会实际调用 `opencode run` 验证发现与抓取（钉在免费 http 层，不产生付费用量）。

## 11. 卸载（保留策略库）

```bash
systemctl --user disable --now crawl4ai-mcp.service
rm ~/.config/systemd/user/crawl4ai-mcp.service
systemctl --user daemon-reload
# 策略库保留：~/.local/state/crawl4ai-mcp/policy.db
```

浏览器缓存（Chromium/Camoufox）位于 `~/.cache/ms-playwright`、`~/.cache/patchright`、`~/.cache/camoufox`，可单独删除。
