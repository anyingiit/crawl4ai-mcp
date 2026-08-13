# crawl4ai-mcp 运维指南

本服务以 systemd user unit 常驻本机，只监听 `127.0.0.1:11236`。本文档覆盖日常运维、资源监控、密钥轮换与回滚。

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

服务被 systemd 限制在独立 cgroup：`MemoryHigh=1536M`（软限）、`MemoryMax=2560M`（硬限，触发 OOM kill）、`MemorySwapMax=0`（禁用 swap）。

```bash
# 当前内存（单位字节）
cat /sys/fs/cgroup/user.slice/user-$(id -u).slice/user@$(id -u).service/app.slice/crawl4ai-mcp.service/memory.current

# systemd 视角
systemctl --user show crawl4ai-mcp.service -p MemoryCurrent -p MemoryPeak -p MemoryHigh -p MemoryMax

# 峰值（自启动以来）
systemctl --user show crawl4ai-mcp.service -p MemoryPeak
```

预期：空闲（无浏览器）约 80-100 MiB；Chromium 抓取时约 250 MiB；Chromium 与 Camoufox 同时运行时约 700 MiB。空闲 180 秒后浏览器应被回收，内存回到 100 MiB 以下。

## 3. 域名策略检查与清理

`diagnose` MCP 工具返回 `domain_policies`、`recent_failures`、`providers` 可用性与 `browsers` 状态。清理某个域名的记忆需停止服务后直接操作 SQLite：

```bash
systemctl --user stop crawl4ai-mcp.service
sqlite3 ~/.local/state/crawl4ai-mcp/policy.db "DELETE FROM domain_policy WHERE domain='example.com';"
systemctl --user start crawl4ai-mcp.service
```

不要新增 MCP 工具或给 `diagnose` 加破坏性参数；清理一律走上述本地命令。

## 4. 启用/禁用 Camoufox

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

## 5. 密钥与代理轮换

全部密钥在 `.env`（权限 600，禁止提交）：

```dotenv
RAYOBYTE_API_URL=https://api.scraping.rayobyte.com/
RAYOBYTE_API_KEY=
FIRECRAWL_API_KEY=
WEBSHARE_PROXIES=
OXYLABS_PROXIES=http://user-<user>-country-US:<password>@dc.oxylabs.io:8000
```

修改后：

```bash
chmod 600 .env
systemctl --user restart crawl4ai-mcp.service
```

- 代理列表用逗号分隔，逐条轮转（PROXY 层）。
- 密钥留空 = 该提供商不可用（`diagnose` 中 ready=false），服务照常启动。
- 更换密钥后旧的失效即可，无需清理策略库。

## 6. 识别付费层用量

`scrape` 结果中的 `cost_kind`：

| cost_kind | 含义 |
|---|---|
| `free` | 本地免费手段（HTTP/浏览器/Camoufox） |
| `proxy_bandwidth` | WebShare/Oxylabs 数据中心代理带宽 |
| `rayobyte_credit` | Rayobyte Web Scraper API 额度 |
| `firecrawl_credit` | Firecrawl 额度 |

`attempts` 数组按顺序记录了每一层尝试的 `tier`、`decision`、`status_code`、`error`。`diagnose` 的 `recent_failures` 保留了最近失败的 URL 与耗时。

## 7. 提供商额度耗尽

- Firecrawl 402「Insufficient credits」、Rayobyte 429/额度类错误被归一化为带错误信息的失败结果；级联会继续尝试（或返回 failed 并进入域名冷却）。
- 额度耗尽不崩溃：相关层 `availability().ready` 仍为 true（配置存在），但每次请求都会失败。运营上建议：在 `config.toml` 的 `enabled_tiers` 中去掉该层，或充值后在 `.env` 轮换密钥并重启。

## 8. opencode 配置回滚

opencode 配置在改动前会生成带时间戳的备份：

```bash
ls -la ~/.config/opencode/opencode.jsonc.bak-* ~/.config/opencode/permissions/*.bak-*
# 回滚示例
cp ~/.config/opencode/opencode.jsonc.bak-20260813-102903 ~/.config/opencode/opencode.jsonc
```

修改权限快照后需切换一次权限模式（`oc-mode interactive` / `oc-mode longrun`）才会合并生效。opencode 配置不是热加载的：改完 `opencode.jsonc` 需重启 opencode 会话（TUI 退出重开，或 `systemctl --user restart opencode`）。

## 9. 卸载（保留策略库）

```bash
systemctl --user disable --now crawl4ai-mcp.service
rm ~/.config/systemd/user/crawl4ai-mcp.service
systemctl --user daemon-reload
# 策略库保留：~/.local/state/crawl4ai-mcp/policy.db
```

浏览器缓存（Chromium/Camoufox）位于 `~/.cache/ms-playwright`、`~/.cache/patchright`、`~/.cache/camoufox`，可单独删除。
