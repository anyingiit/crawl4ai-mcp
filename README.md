<!-- Source: Best-README-Template BLANK_README (Unlicense) — https://github.com/othneildrew/Best-README-Template -->
<a id="readme-top"></a>

# crawl4ai-mcp

An MCP server that scrapes and maps web pages over a loopback HTTP endpoint, escalating through progressively heavier fetch tiers only when a lighter one fails.

**English** · [简体中文](README.zh-CN.md)

[![CI](https://github.com/anyingiit/crawl4ai-mcp/actions/workflows/ci.yml/badge.svg)](https://github.com/anyingiit/crawl4ai-mcp/actions/workflows/ci.yml)
[![License](https://img.shields.io/github/license/anyingiit/crawl4ai-mcp)](LICENSE)

[Report a bug](https://github.com/anyingiit/crawl4ai-mcp/issues/new?template=bug_report.yml) · [Request a feature](https://github.com/anyingiit/crawl4ai-mcp/issues/new?template=feature_request.yml)

<details>
  <summary>Table of Contents</summary>
  <ol>
    <li><a href="#about-the-project">About The Project</a></li>
    <li><a href="#getting-started">Getting Started</a></li>
    <li><a href="#usage">Usage</a></li>
    <li><a href="#contributing">Contributing</a></li>
    <li><a href="#license">License</a></li>
    <li><a href="#contact">Contact</a></li>
  </ol>
</details>

## About The Project

crawl4ai-mcp exposes `scrape`, `map` and `diagnose` as MCP tools over HTTP, so an
assistant can fetch a page without the caller choosing how. The choosing is the
point: `src/crawl4ai_mcp/cascade.py` starts at the cheapest tier a URL has
previously worked with and escalates only on failure, through a plain HTTP
client, a headless browser, Camoufox and finally a paid provider, so most pages
cost one request and the expensive path is reserved for the ones that need it.

It binds to `127.0.0.1` and refuses hosts it was not configured for, which makes
it a local tool rather than a service: there is no authentication layer, because
nothing off the machine is meant to reach it.

See the [open issues](https://github.com/anyingiit/crawl4ai-mcp/issues) for planned features and known issues.

## Getting Started

### Prerequisites

- Python 3.12 or newer, the floor declared in `pyproject.toml`
- A browser runtime for the middle tiers: Chromium via `crawl4ai`, plus Camoufox
  if the optional `camoufox` extra is installed
- For the packaged service, a Linux host with systemd user sessions, since
  `systemd/crawl4ai-mcp.service` installs under the user manager rather than the
  system one
- API credentials only if the paid tiers are enabled; see `.env.example`

### Installation

```sh
git clone https://github.com/anyingiit/crawl4ai-mcp.git
cd crawl4ai-mcp
python -m venv .venv && . .venv/bin/activate
pip install -e ".[test]"
```

To run it as a background service on Linux instead, `scripts/install-user-service.sh`
installs the systemd unit and waits for the health endpoint to answer.

## Usage

Start the server in the foreground, then check that it is up:

```sh
crawl4ai-mcp
curl -fsS http://127.0.0.1:11236/health
```

The MCP endpoint is served at `/mcp` on the same port. Point an MCP client at
`http://127.0.0.1:11236/mcp` and call `scrape` with a URL; pass `max_tier` to cap
how far the cascade is allowed to escalate.

## Contributing

Contributions are welcome. Read [CONTRIBUTING.md](CONTRIBUTING.md) for how to open an issue or a pull request, and [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md) for the standards expected of everyone taking part.

Please do not report security issues in public issues or pull requests. [SECURITY.md](SECURITY.md) explains how to report them privately.

## License

Distributed under the MIT License. See [LICENSE](LICENSE) for details.

## Contact

Project link: [https://github.com/anyingiit/crawl4ai-mcp](https://github.com/anyingiit/crawl4ai-mcp)

<p align="right">(<a href="#readme-top">back to top</a>)</p>
