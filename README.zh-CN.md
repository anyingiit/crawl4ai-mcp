[English](README.md) · **简体中文**

> 英文版是规范版本。本页与 [README.md](README.md) 不一致时，以英文版为准。

<!-- translation-of: README.md sha256:75435c47e6f88638 -->

<!-- Source: Best-README-Template BLANK_README (Unlicense) — https://github.com/othneildrew/Best-README-Template -->
<a id="readme-top"></a>

# crawl4ai-mcp

一个 MCP 服务器，通过本机回环 HTTP 端点抓取和遍历网页，仅在较轻的抓取层失败时才逐级升级到更重的层。

[![CI](https://github.com/anyingiit/crawl4ai-mcp/actions/workflows/ci.yml/badge.svg)](https://github.com/anyingiit/crawl4ai-mcp/actions/workflows/ci.yml)
[![License](https://img.shields.io/github/license/anyingiit/crawl4ai-mcp)](LICENSE)

[报告问题](https://github.com/anyingiit/crawl4ai-mcp/issues/new?template=bug_report.yml) · [提出需求](https://github.com/anyingiit/crawl4ai-mcp/issues/new?template=feature_request.yml)

<details>
  <summary>目录</summary>
  <ol>
    <li><a href="#about-the-project">关于本项目</a></li>
    <li><a href="#getting-started">开始使用</a></li>
    <li><a href="#usage">用法</a></li>
    <li><a href="#contributing">参与贡献</a></li>
    <li><a href="#license">许可证</a></li>
    <li><a href="#contact">联系方式</a></li>
  </ol>
</details>

## 关于本项目

crawl4ai-mcp 以 MCP 工具的形式通过 HTTP 暴露 `scrape`、`map` 和 `diagnose`，
调用方无需自己决定用哪种方式抓取。这个"替你决定"正是它的要点：
`src/crawl4ai_mcp/cascade.py` 会从该 URL 以往成功过的最廉价一层开始，只在失败时
才逐级升级——从普通 HTTP 客户端，到无头浏览器，到 Camoufox，最后才是付费服务商。
于是大多数页面只花一次请求，昂贵的路径留给真正需要它的页面。

它绑定在 `127.0.0.1`，并拒绝未经配置的主机名，因此它是一个本地工具而不是一项服务：
没有鉴权层，因为本就不打算让机器之外的任何东西访问它。

计划中的功能与已知问题，见 [open issues](https://github.com/anyingiit/crawl4ai-mcp/issues)。

## 开始使用

### 环境要求

- Python 3.12 或更高版本，即 `pyproject.toml` 声明的下限
- 中间层所需的浏览器运行时：由 `crawl4ai` 提供的 Chromium；若安装了可选的
  `camoufox` 附加依赖，还需要 Camoufox
- 若要以打包好的服务方式运行，需要支持 systemd 用户会话的 Linux 主机，因为
  `systemd/crawl4ai-mcp.service` 安装在用户级管理器下而非系统级
- 仅在启用付费层时才需要 API 凭据，参见 `.env.example`

### 安装

```sh
git clone https://github.com/anyingiit/crawl4ai-mcp.git
cd crawl4ai-mcp
python -m venv .venv && . .venv/bin/activate
pip install -e ".[test]"
```

若想在 Linux 上以后台服务方式运行，`scripts/install-user-service.sh`
会安装 systemd 单元并等待健康检查端点作出响应。

## 用法

在前台启动服务，然后确认它已就绪：

```sh
crawl4ai-mcp
curl -fsS http://127.0.0.1:11236/health
```

MCP 端点在同一端口的 `/mcp` 路径上。把 MCP 客户端指向
`http://127.0.0.1:11236/mcp` 并调用 `scrape` 传入一个 URL；用 `max_tier`
限制级联最多允许升级到哪一层。

## 参与贡献

欢迎参与。[CONTRIBUTING.md](CONTRIBUTING.md) 说明如何提交 issue 或 pull request，[CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md) 说明对所有参与者的行为要求。

请不要在公开的 issue 或 pull request 中报告安全问题。[SECURITY.md](SECURITY.md) 说明了私下报告的方式。

## 许可证

以 MIT 许可证分发。详见 [LICENSE](LICENSE)。

## 联系方式

项目地址：[https://github.com/anyingiit/crawl4ai-mcp](https://github.com/anyingiit/crawl4ai-mcp)

<p align="right">(<a href="#readme-top">back to top</a>)</p>
