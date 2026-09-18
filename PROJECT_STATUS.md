# crawl4ai-mcp 状态总结

> 本文件由 #WORKSPACE_MANAGEMENT 生成于 2026-09-18（主机：HOST_REDACTED）。可以自由补充修改；
> 以后自动更新时，只会追加缺失或需要补充的章节，不会覆盖已有内容。

## 用途

自托管的 localhost-only MCP 抓取服务：从零开销的 TLS 指纹模拟 HTTP 请求起步，遇到拦截逐级升级到浏览器、反检测浏览器、数据中心代理，直至托管 API（Rayobyte / Firecrawl），并按域名记住「哪一层能成功」，避免反复烧钱爬梯子。通过 `scrape`/`crawl`/`map`/`diagnose` 四个 MCP 工具接入 opencode，供其在对话中抓取网页内容（依据 README）。

## 技术栈

- 语言与框架：Python 3.12（README 前置要求），核心基于 crawl4ai 抓取框架，以 MCP streamable-http 协议对外提供服务，通过 systemd --user 常驻运行
- 主要依赖：依据 README 描述，包括 curl_cffi（TLS 指纹模拟）、crawl4ai（含 stealth Chromium）、patchright（反检测浏览器 UndetectedAdapter）、playwright（Chromium）、camoufox（可选，独立 Firefox 进程）；付费兜底层为 Rayobyte、Firecrawl 两个托管 API。依赖声明文件为 `pyproject.toml`，但本次事实包只给出文件名、未提供其内容，以上清单取自 README 正文而非该文件本身

## 运行方式

依据 README，安装前置为 Linux aarch64、Python 3.12、systemd user linger，步骤如下：

```bash
git clone <repo> && cd crawl4ai-mcp
python3 -m venv .venv
.venv/bin/pip install -e '.[test]'          # 可选: '.[camoufox,test]'
.venv/bin/python -m playwright install chromium
.venv/bin/python -m patchright install chromium
.venv/bin/python -m camoufox fetch          # 可选

cp .env.example .env && chmod 600 .env      # 需要自行在 .env 中填入密钥，本文件不记录任何真实凭据
./scripts/install-user-service.sh           # 安装 systemd --user 服务 + 启动 + 健康检查
```

验证：`curl http://127.0.0.1:11236/health` 预期返回 `{"status":"ok"}`；随后在 opencode 配置的 `mcp` 段加入指向 `http://127.0.0.1:11236/mcp` 的远程入口。付费层（Rayobyte/Firecrawl/数据中心代理）需要另外配置对应凭据才能启用，未配置时级联会跳过这些层。

当前主机 HOST_REDACTED 上已有该服务以 systemd --user 方式运行，工作目录为 `/home/ubuntu/Workspace/crawl4ai-mcp`。**需注意**：本次可运行性评估只执行了 `ps -p <pid>` 确认关联进程存活（service-status 方法），没有执行构建、编译、测试，也没有实际请求 `/health` 端点，因此不代表功能已被验证可用。

## 当前状态

可运行性：可运行
评估日期：2026-09-18

- 检查方式：service-status，关键命令：`ps -p <pid>`（对 9 个关联进程逐一验证存活；未执行构建、编译、测试或 HTTP 健康检查）
- 最近提交：2026-09-02 test: cover stale http policy decay at service boundary
- 服务由主机 HOST_REDACTED 的 systemd --user 管理，工作目录 `/home/ubuntu/Workspace/crawl4ai-mcp`；本次评估未记录外部依赖缺口（external_requirements 为空）

## 已知问题与下一步

- 本次「可运行」结论仅基于进程存活检查（service-status），未验证服务是否真正响应请求；建议后续执行 `curl http://127.0.0.1:11236/health` 或 `.venv/bin/pytest -v` 以取得功能层面的验证证据
- 完整验收测试（`CRAWL4AI_MCP_LIVE_TESTS=1 scripts/run-acceptance.sh`）依赖已配置的付费层（Rayobyte/Firecrawl/代理）且本次未执行；如需更高置信度的可运行性结论，可安排在具备相应凭据的环境中运行一次
- 事实包中本次未见诊断记录、外部依赖缺口或修复建议（diagnosis/external_requirements/fix_candidate 均为空），暂无更具体的已知问题可列
