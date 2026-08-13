# crawl4ai-mcp Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build and deploy a localhost-only MCP service that automatically escalates web scraping through seven cost tiers, remembers the cheapest successful tier per domain, and integrates four tools into opencode while enforcing strict browser lifecycle and cgroup memory limits.

**Architecture:** The service treats page acquisition as a set of pluggable providers that return a normalized `FetchResult`. A cascade engine classifies each result, chooses the next tier, and stores per-domain policy in SQLite; crawl4ai remains responsible for HTML-to-markdown processing. A single FastMCP Streamable HTTP daemon owns all provider lifecycles, browser semaphores, and idle cleanup.

**Tech Stack:** Python 3.12, crawl4ai 0.9.2, curl_cffi 0.16.x, FastMCP 3.4.x, aiosqlite, httpx, Playwright, Patchright, Camoufox 0.5.x, pytest, pytest-asyncio, systemd --user, opencode remote MCP.

## Global Constraints

- Run on Linux aarch64; all binary dependencies must have native arm64 builds.
- Bind only to `127.0.0.1:11236`; do not expose the service to LAN, tailnet, or public interfaces.
- Pin `crawl4ai==0.9.2`; do not import code from the sibling upstream clone at runtime.
- Default maximum tier is `firecrawl`; automatic escalation may consume Rayobyte and Firecrawl credits.
- Never escalate `401`, `404`, or `410`; these responses must stop at the current tier.
- Cloudflare challenge responses skip the datacenter-proxy tier.
- Visible-text success threshold defaults to exactly 200 characters and is configurable.
- Browser concurrency is exactly 2; HTTP-only concurrency is exactly 8.
- Chromium idle timeout is 180 seconds; Camoufox idle timeout is 120 seconds.
- systemd limits are `MemoryHigh=1536M`, `MemoryMax=2560M`, and `MemorySwapMax=0`.
- Store secrets only in `.env` with mode `0600`; never commit credentials or proxy URLs.
- Do not add a daily paid-provider quota; rely on domain policy memory and cooldown.
- Camoufox must be independently disableable; unavailable Camoufox skips directly to Rayobyte.
- Use test-driven development and commit after every task.

---

## File Structure

Create the following focused units:

```text
pyproject.toml                         dependency pins, package metadata, pytest config
config.example.toml                   non-secret defaults and tier enable flags
.env.example                          secret variable names only
src/crawl4ai_mcp/__init__.py          package version
src/crawl4ai_mcp/config.py            TOML/environment loading and validation
src/crawl4ai_mcp/models.py            tier enums and normalized result models
src/crawl4ai_mcp/detect.py            block/challenge/result classification
src/crawl4ai_mcp/policy.py            SQLite domain memory and cooldown
src/crawl4ai_mcp/render.py            crawl4ai HTML-to-markdown normalization
src/crawl4ai_mcp/providers/base.py     provider protocol and shared helpers
src/crawl4ai_mcp/providers/http.py     Tier 0 curl_cffi provider
src/crawl4ai_mcp/providers/browser.py  Tiers 1, 2, and 4 browser providers/lifecycles
src/crawl4ai_mcp/providers/camoufox.py Tier 3 provider/lifecycle
src/crawl4ai_mcp/providers/rayobyte.py Tier 5 hosted provider
src/crawl4ai_mcp/providers/firecrawl.py Tier 6 hosted provider
src/crawl4ai_mcp/cascade.py            routing, retries, policy updates, cost metadata
src/crawl4ai_mcp/discovery.py          sitemap/Common Crawl map and bounded crawl
src/crawl4ai_mcp/service.py            application facade and diagnostics
src/crawl4ai_mcp/server.py             four FastMCP tools and health route
src/crawl4ai_mcp/__main__.py           daemon entry point
systemd/crawl4ai-mcp.service           user service and cgroup limits
scripts/install-user-service.sh        idempotent local deployment helper
tests/                                 unit, integration, and acceptance tests
```

The provider boundary is:

```python
class FetchProvider(Protocol):
    tier: Tier
    cost_kind: CostKind

    async def fetch(self, url: str) -> FetchResult:
        raise NotImplementedError

    async def close(self) -> None:
        raise NotImplementedError

    def availability(self) -> ProviderAvailability:
        raise NotImplementedError
```

All downstream tasks consume this exact interface.

---

### Task 1: Package Skeleton, Configuration, and Core Types

**Files:**
- Create: `pyproject.toml`
- Create: `config.example.toml`
- Create: `.env.example`
- Create: `src/crawl4ai_mcp/__init__.py`
- Create: `src/crawl4ai_mcp/config.py`
- Create: `src/crawl4ai_mcp/models.py`
- Create: `src/crawl4ai_mcp/providers/__init__.py`
- Create: `src/crawl4ai_mcp/providers/base.py`
- Test: `tests/test_config.py`
- Test: `tests/test_models.py`

**Interfaces:**
- Produces: `Tier`, `CostKind`, `Decision`, `FetchResult`, `ScrapeResult`, `ProviderAvailability`, `AppConfig`, `load_config()` and `FetchProvider`.
- Consumes: no project interfaces.

- [ ] **Step 1: Write failing core-type tests**

```python
from crawl4ai_mcp.models import CostKind, FetchResult, Tier


def test_tiers_are_strictly_ordered():
    assert Tier.HTTP < Tier.STEALTH < Tier.UNDETECTED < Tier.CAMOUFOX
    assert Tier.CAMOUFOX < Tier.PROXY < Tier.RAYOBYTE < Tier.FIRECRAWL


def test_fetch_result_preserves_raw_response():
    result = FetchResult(
        url="https://example.com",
        tier=Tier.HTTP,
        cost_kind=CostKind.FREE,
        status_code=200,
        html="<main>Hello</main>",
        headers={"content-type": "text/html"},
        elapsed_ms=42,
    )
    assert result.html == "<main>Hello</main>"
    assert result.markdown is None
```

- [ ] **Step 2: Run tests and confirm import failure**

Run: `python3 -m pytest tests/test_models.py -v`

Expected: FAIL with `ModuleNotFoundError: No module named 'crawl4ai_mcp'`.

- [ ] **Step 3: Create `pyproject.toml` with exact runtime/test dependencies**

```toml
[build-system]
requires = ["setuptools>=75", "wheel"]
build-backend = "setuptools.build_meta"

[project]
name = "crawl4ai-mcp"
version = "0.1.0"
requires-python = ">=3.12"
dependencies = [
  "crawl4ai==0.9.2",
  "curl_cffi>=0.16,<0.17",
  "fastmcp>=3.4,<3.5",
  "aiosqlite>=0.20,<0.22",
  "httpx[http2]>=0.27,<1",
  "beautifulsoup4>=4.12,<5",
  "pydantic>=2.10,<3",
  "python-dotenv>=1,<2",
]

[project.optional-dependencies]
camoufox = ["camoufox>=0.5,<0.6"]
test = ["pytest>=8,<9", "pytest-asyncio>=0.24,<1", "respx>=0.22,<1"]

[project.scripts]
crawl4ai-mcp = "crawl4ai_mcp.__main__:main"

[tool.setuptools.packages.find]
where = ["src"]

[tool.pytest.ini_options]
asyncio_mode = "auto"
testpaths = ["tests"]
```

- [ ] **Step 4: Implement the exact models**

```python
from enum import IntEnum, StrEnum
from pydantic import BaseModel, Field


class Tier(IntEnum):
    HTTP = 0
    STEALTH = 1
    UNDETECTED = 2
    CAMOUFOX = 3
    PROXY = 4
    RAYOBYTE = 5
    FIRECRAWL = 6


class CostKind(StrEnum):
    FREE = "free"
    PROXY_BANDWIDTH = "proxy_bandwidth"
    RAYOBYTE_CREDIT = "rayobyte_credit"
    FIRECRAWL_CREDIT = "firecrawl_credit"


class Decision(StrEnum):
    SUCCESS = "success"
    SHORT_STATIC = "short_static"
    NEEDS_JS = "needs_js"
    CLOUDFLARE = "cloudflare"
    RATE_LIMITED = "rate_limited"
    TERMINAL = "terminal"
    RETRYABLE_NETWORK = "retryable_network"
    FAILED = "failed"


class FetchResult(BaseModel):
    url: str
    tier: Tier
    cost_kind: CostKind
    status_code: int | None = None
    html: str = ""
    markdown: str | None = None
    headers: dict[str, str] = Field(default_factory=dict)
    redirected_url: str | None = None
    elapsed_ms: int
    error: str | None = None


class ProviderAvailability(BaseModel):
    enabled: bool
    ready: bool
    reason: str | None = None


class Attempt(BaseModel):
    tier: Tier
    decision: Decision
    cost_kind: CostKind
    status_code: int | None = None
    elapsed_ms: int
    error: str | None = None


class ScrapeResult(BaseModel):
    url: str
    status: str
    content: str = ""
    tier_used: Tier | None = None
    cost_kind: CostKind | None = None
    elapsed_ms: int
    attempts: list[Attempt] = Field(default_factory=list)
    cooldown_until: int | None = None
    error: str | None = None
```

Implement named constructors used by later tasks with these signatures:

```python
@classmethod
def success_from(cls, fetched: FetchResult, markdown: str, attempts: list[Attempt]) -> "ScrapeResult":
    return cls(
        url=fetched.url,
        status="success",
        content=markdown,
        tier_used=fetched.tier,
        cost_kind=fetched.cost_kind,
        elapsed_ms=sum(attempt.elapsed_ms for attempt in attempts),
        attempts=attempts,
    )

@classmethod
def terminal_from(cls, fetched: FetchResult, attempts: list[Attempt]) -> "ScrapeResult":
    return cls(
        url=fetched.url,
        status="terminal",
        tier_used=fetched.tier,
        cost_kind=fetched.cost_kind,
        elapsed_ms=sum(attempt.elapsed_ms for attempt in attempts),
        attempts=attempts,
        error=fetched.error or f"HTTP {fetched.status_code}",
    )

@classmethod
def cooldown(cls, url: str, cooldown_until: int, error: str | None) -> "ScrapeResult":
    return cls(
        url=url,
        status="cooldown",
        elapsed_ms=0,
        cooldown_until=cooldown_until,
        error=error,
    )

@classmethod
def failed(cls, url: str, cooldown_until: int, attempts: list[Attempt]) -> "ScrapeResult":
    return cls(
        url=url,
        status="failed",
        elapsed_ms=sum(attempt.elapsed_ms for attempt in attempts),
        attempts=attempts,
        cooldown_until=cooldown_until,
        error="all tiers failed",
    )
```

- [ ] **Step 5: Write failing configuration tests**

```python
from pathlib import Path
from crawl4ai_mcp.config import load_config


def test_defaults_match_resource_contract(tmp_path: Path):
    path = tmp_path / "config.toml"
    path.write_text("", encoding="utf-8")
    config = load_config(path, env={})
    assert config.http_concurrency == 8
    assert config.browser_concurrency == 2
    assert config.visible_text_threshold == 200
    assert config.chromium_idle_seconds == 180
    assert config.camoufox_idle_seconds == 120
    assert config.bind_host == "127.0.0.1"
    assert config.bind_port == 11236
```

- [ ] **Step 6: Implement validated Pydantic configuration**

`AppConfig` must include exact fields: `bind_host`, `bind_port`, `database_path`, `visible_text_threshold`, `http_concurrency`, `browser_concurrency`, `chromium_idle_seconds`, `camoufox_idle_seconds`, `policy_decay_days`, `cooldown_seconds`, `enabled_tiers`, `webshare_proxies`, `oxylabs_proxies`, `rayobyte_api_url`, `rayobyte_api_key`, and `firecrawl_api_key`. Secret values come from environment and override TOML; reject any `bind_host` other than `127.0.0.1`.

- [ ] **Step 7: Run Task 1 tests**

Run: `python3 -m pytest tests/test_models.py tests/test_config.py -v`

Expected: PASS.

- [ ] **Step 8: Create the virtual environment and install the editable package**

Run:

```bash
python3 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -e '.[test]'
```

Expected: crawl4ai 0.9.2 and curl_cffi arm64 wheels install successfully.

- [ ] **Step 9: Commit**

```bash
git add pyproject.toml config.example.toml .env.example src tests
git commit -m "feat: scaffold crawl4ai mcp service"
```

### Task 2: Response Classification and Escalation Rules

**Files:**
- Create: `src/crawl4ai_mcp/detect.py`
- Test: `tests/test_detect.py`

**Interfaces:**
- Consumes: `FetchResult`, `Decision` from Task 1.
- Produces: `classify(result: FetchResult, visible_text_threshold: int = 200) -> Decision` and `next_tiers(current: Tier, decision: Decision, maximum: Tier) -> list[Tier]`.

- [ ] **Step 1: Write table-driven failing classifier tests**

```python
import pytest
from crawl4ai_mcp.detect import classify
from crawl4ai_mcp.models import CostKind, Decision, FetchResult, Tier


def fetched(status: int, html: str, headers=None):
    return FetchResult(
        url="https://example.com", tier=Tier.HTTP, cost_kind=CostKind.FREE,
        status_code=status, html=html, headers=headers or {}, elapsed_ms=1,
    )


@pytest.mark.parametrize("status", [401, 404, 410])
def test_terminal_statuses_never_escalate(status):
    assert classify(fetched(status, "missing")) == Decision.TERMINAL


def test_short_static_page_is_valid_content():
    assert classify(fetched(200, "<main>Short notice</main>")) == Decision.SHORT_STATIC


def test_short_script_shell_needs_js():
    assert classify(fetched(200, "<div id='app'></div><script src='app.js'></script>")) == Decision.NEEDS_JS


def test_cloudflare_challenge_is_detected_before_length_check():
    html = "<title>Just a moment...</title><script>window.__cf_chl_opt={}</script>"
    assert classify(fetched(200, html)) == Decision.CLOUDFLARE


def test_retry_after_is_rate_limited():
    assert classify(fetched(429, "slow down", {"retry-after": "60"})) == Decision.RATE_LIMITED
```

- [ ] **Step 2: Run tests and verify failure**

Run: `.venv/bin/pytest tests/test_detect.py -v`

Expected: FAIL because `detect.py` does not exist.

- [ ] **Step 3: Implement deterministic classification order**

Implement this exact precedence:

```python
TERMINAL_STATUSES = {401, 404, 410}
CLOUDFLARE_MARKERS = (
    "just a moment", "attention required", "cf-challenge",
    "turnstile", "__cf_chl", "cf-mitigated",
)


def classify(result: FetchResult, visible_text_threshold: int = 200) -> Decision:
    if result.status_code in TERMINAL_STATUSES:
        return Decision.TERMINAL
    haystack = f"{result.html}\n{result.headers}".lower()
    if any(marker in haystack for marker in CLOUDFLARE_MARKERS):
        return Decision.CLOUDFLARE
    if result.status_code in {429, 503} and "retry-after" in {
        key.lower() for key in result.headers
    }:
        return Decision.RATE_LIMITED
    if result.error and result.status_code is None:
        return Decision.RETRYABLE_NETWORK
    text = BeautifulSoup(result.html, "lxml").get_text(" ", strip=True)
    if result.status_code == 200 and len(text) >= visible_text_threshold:
        return Decision.SUCCESS
    if result.status_code == 200 and len(text) < visible_text_threshold:
        return Decision.NEEDS_JS if "<script" in result.html.lower() else Decision.SHORT_STATIC
    return Decision.FAILED
```

- [ ] **Step 4: Write routing tests that lock the paid-tier behavior**

```python
from crawl4ai_mcp.detect import next_tiers
from crawl4ai_mcp.models import Decision, Tier


def test_cloudflare_skips_datacenter_proxy():
    assert next_tiers(Tier.UNDETECTED, Decision.CLOUDFLARE, Tier.FIRECRAWL) == [
        Tier.CAMOUFOX, Tier.RAYOBYTE, Tier.FIRECRAWL,
    ]


def test_rate_limit_prefers_proxy_before_hosted_services():
    assert next_tiers(Tier.UNDETECTED, Decision.RATE_LIMITED, Tier.FIRECRAWL) == [
        Tier.PROXY, Tier.RAYOBYTE, Tier.FIRECRAWL,
    ]


def test_terminal_has_no_next_tier():
    assert next_tiers(Tier.HTTP, Decision.TERMINAL, Tier.FIRECRAWL) == []
```

- [ ] **Step 5: Implement and run all detector tests**

Run: `.venv/bin/pytest tests/test_detect.py -v`

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/crawl4ai_mcp/detect.py tests/test_detect.py
git commit -m "feat: classify scrape failures and escalation routes"
```

### Task 3: SQLite Domain Policy and Cooldown

**Files:**
- Create: `src/crawl4ai_mcp/policy.py`
- Test: `tests/test_policy.py`

**Interfaces:**
- Consumes: `Tier`.
- Produces: `DomainPolicy`, `PolicyStore.open()`, `get_start_tier(url, now)`, `record_success(url, tier, now)`, `record_failure(url, error_kind, now)`, `clear(domain=None)`, and `list_policies(domain=None)`.

- [ ] **Step 1: Write failing persistence and cooldown tests**

```python
import pytest
from crawl4ai_mcp.models import Tier
from crawl4ai_mcp.policy import PolicyStore


@pytest.mark.asyncio
async def test_success_is_reused_for_same_domain(tmp_path):
    store = await PolicyStore.open(tmp_path / "policy.db")
    await store.record_success("https://docs.example.com/a", Tier.UNDETECTED, now=1_000)
    assert await store.get_start_tier("https://docs.example.com/b", now=1_001) == Tier.UNDETECTED
    await store.close()


@pytest.mark.asyncio
async def test_policy_decays_one_tier_after_seven_days(tmp_path):
    store = await PolicyStore.open(tmp_path / "policy.db", decay_days=7)
    await store.record_success("https://example.com/a", Tier.RAYOBYTE, now=1_000)
    eight_days = 1_000 + 8 * 86_400
    assert await store.get_start_tier("https://example.com/b", now=eight_days) == Tier.PROXY


@pytest.mark.asyncio
async def test_failure_backoff_sequence_is_capped_at_24_hours(tmp_path):
    store = await PolicyStore.open(tmp_path / "policy.db")
    expected = [600, 3_600, 21_600, 86_400, 86_400]
    for index, seconds in enumerate(expected, start=1):
        policy = await store.record_failure("https://bad.example/x", "all_failed", now=1_000)
        assert policy.cooldown_until == 1_000 + seconds
```

- [ ] **Step 2: Verify tests fail**

Run: `.venv/bin/pytest tests/test_policy.py -v`

Expected: FAIL because `PolicyStore` is undefined.

- [ ] **Step 3: Implement the exact schema and transactional upserts**

```sql
CREATE TABLE IF NOT EXISTS domain_policy (
    domain TEXT PRIMARY KEY,
    best_tier INTEGER,
    last_success_at INTEGER,
    fail_count INTEGER NOT NULL DEFAULT 0,
    cooldown_until INTEGER,
    last_error_kind TEXT,
    updated_at INTEGER NOT NULL
);
```

Normalize domains with `urlsplit(url).hostname.lower().rstrip('.')`. The backoff sequence is exactly `(600, 3600, 21600, 86400)` and remains capped at 86400.

- [ ] **Step 4: Add a cooldown-query test**

```python
@pytest.mark.asyncio
async def test_active_cooldown_returns_cached_failure(tmp_path):
    store = await PolicyStore.open(tmp_path / "policy.db")
    await store.record_failure("https://bad.example/x", "all_failed", now=1_000)
    policy = await store.get_active_cooldown("https://bad.example/y", now=1_100)
    assert policy is not None
    assert policy.last_error_kind == "all_failed"
```

- [ ] **Step 5: Run policy tests**

Run: `.venv/bin/pytest tests/test_policy.py -v`

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/crawl4ai_mcp/policy.py tests/test_policy.py
git commit -m "feat: remember domain tiers and failure cooldowns"
```

### Task 4: Tier 0 curl_cffi Provider and crawl4ai Rendering

**Files:**
- Create: `src/crawl4ai_mcp/providers/http.py`
- Create: `src/crawl4ai_mcp/render.py`
- Test: `tests/providers/test_http.py`
- Test: `tests/test_render.py`

**Interfaces:**
- Consumes: `FetchProvider`, `FetchResult`, `Tier`, `CostKind`.
- Produces: `HttpProvider.fetch()`, `HttpProvider.close()`, and `render_html(url, html) -> str`.

- [ ] **Step 1: Write a failing provider test with an in-process HTTP server**

Use `pytest` plus a minimal Starlette/Uvicorn fixture that returns status 200, a redirect, and a 404. Assert the provider preserves non-2xx status codes instead of raising, uses Tier HTTP/CostKind FREE, and exposes final redirect URL.

```python
@pytest.mark.asyncio
async def test_http_provider_returns_404_for_classifier(local_server):
    provider = HttpProvider(concurrency=8, timeout_seconds=10)
    result = await provider.fetch(f"{local_server}/missing")
    assert result.status_code == 404
    assert result.tier == Tier.HTTP
    assert result.cost_kind == CostKind.FREE
    await provider.close()
```

- [ ] **Step 2: Verify the test fails**

Run: `.venv/bin/pytest tests/providers/test_http.py -v`

Expected: FAIL because `HttpProvider` does not exist.

- [ ] **Step 3: Implement the curl_cffi provider**

Use one lazy `curl_cffi.requests.AsyncSession(impersonate="chrome131")`, an `asyncio.Semaphore(8)`, a 10-second connect/read timeout, redirects enabled, TLS verification enabled, and browser-like accept headers. Convert connection exceptions into `FetchResult(error=str(exc), status_code=None)`; never raise HTTP status exceptions.

- [ ] **Step 4: Write the failing crawl4ai render test**

```python
@pytest.mark.asyncio
async def test_render_html_uses_crawl4ai_markdown_pipeline():
    markdown = await render_html(
        "https://example.com",
        "<html><nav>Noise</nav><main><h1>Title</h1><p>Body text</p></main></html>",
    )
    assert "# Title" in markdown
    assert "Body text" in markdown
```

- [ ] **Step 5: Implement rendering through crawl4ai, not a second converter**

Construct an `AsyncWebCrawler` with this no-I/O strategy, then call `arun()` with `CrawlerRunConfig(markdown_generator=DefaultMarkdownGenerator())`. Return `result.markdown.fit_markdown or result.markdown.raw_markdown`.

```python
class StaticHtmlStrategy(AsyncCrawlerStrategy):
    def __init__(self, html: str):
        self.html = html

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return None

    async def crawl(self, url: str, **kwargs) -> AsyncCrawlResponse:
        return AsyncCrawlResponse(
            html=self.html,
            response_headers={},
            status_code=200,
        )
```

- [ ] **Step 6: Run provider and render tests**

Run: `.venv/bin/pytest tests/providers/test_http.py tests/test_render.py -v`

Expected: PASS.

- [ ] **Step 7: Run a real static-page smoke test**

Run a short Python script against `https://docs.python.org/3/library/asyncio.html` and print tier, elapsed time, status, and markdown length.

Expected: Tier HTTP, status 200, markdown length greater than 1,000. Treat the 500ms latency target as an acceptance metric, not a deterministic unit-test assertion.

- [ ] **Step 8: Commit**

```bash
git add src/crawl4ai_mcp/providers/http.py src/crawl4ai_mcp/render.py tests
git commit -m "feat: add lightweight tls-impersonating fetch tier"
```

### Task 5: Managed Chromium Providers and Idle Reaping

**Files:**
- Create: `src/crawl4ai_mcp/providers/browser.py`
- Test: `tests/providers/test_browser.py`

**Interfaces:**
- Consumes: `FetchProvider`, `FetchResult`, `AppConfig`.
- Produces: `BrowserProvider(tier, idle_seconds, semaphore, proxy_pool=())`, supporting STEALTH, UNDETECTED, and PROXY tiers.

- [ ] **Step 1: Write lifecycle tests against injected fake crawlers**

```python
@pytest.mark.asyncio
async def test_browser_is_lazy_and_closed_after_idle(fake_clock):
    factory = FakeCrawlerFactory()
    provider = BrowserProvider(
        tier=Tier.STEALTH, factory=factory, idle_seconds=180,
        semaphore=asyncio.Semaphore(2), clock=fake_clock,
    )
    assert factory.created == 0
    await provider.fetch("https://example.com")
    assert factory.created == 1
    fake_clock.advance(181)
    await provider.reap_idle()
    assert factory.closed == 1
```

Also test that two provider calls can enter but a third waits, and that a PROXY provider rotates configured proxies round-robin.

- [ ] **Step 2: Verify lifecycle tests fail**

Run: `.venv/bin/pytest tests/providers/test_browser.py -v`

Expected: FAIL because `BrowserProvider` is undefined.

- [ ] **Step 3: Implement factories for each browser tier**

Use these exact configurations:

```python
# Tier 1
BrowserConfig(
    headless=True, enable_stealth=True, text_mode=True,
    memory_saving_mode=True, max_pages_before_recycle=25,
)

# Tier 2
AsyncPlaywrightCrawlerStrategy(
    browser_config=BrowserConfig(
        headless=True, text_mode=True, memory_saving_mode=True,
        max_pages_before_recycle=25,
    ),
    browser_adapter=UndetectedAdapter(),
)
```

Tier 4 uses Tier 2 plus one `ProxyConfig` chosen by round-robin and held sticky for the duration of a single fetch. Do not combine `enable_stealth=True` with `UndetectedAdapter`; upstream explicitly treats them as mutually exclusive.

- [ ] **Step 4: Implement lifecycle ownership**

The provider owns one lazy `AsyncWebCrawler`, updates `last_used` after each request, and exposes `reap_idle(now=None)`. Reaping calls `crawler.close()` and sets the reference to `None`. All browser tiers share one `asyncio.Semaphore(2)` supplied by the service.

- [ ] **Step 5: Map crawl4ai results to normalized results**

Use `result.html`, `result.status_code`, `result.response_headers`, `result.redirected_url`, and `result.error_message`. Browser providers return HTML; markdown normalization remains in the service/cascade layer.

- [ ] **Step 6: Run browser tests and a real forced-tier smoke test**

Run:

```bash
.venv/bin/pytest tests/providers/test_browser.py -v
.venv/bin/python -m playwright install chromium
```

Then force Tier STEALTH against `https://web-scraping.dev/js-links`. Save this exact target and the expected JS-injected link marker in `tests/acceptance/targets.toml` so the check is reproducible. First fetch the page once manually with a real browser and record the injected anchor URL as the marker; if the fixture contract has changed, update both the marker and a dated note in the TOML rather than silently choosing another site.

Expected: browser starts lazily, marker is present, and no browser process remains after an explicit close.

- [ ] **Step 7: Commit**

```bash
git add src/crawl4ai_mcp/providers/browser.py tests/providers/test_browser.py tests/acceptance/targets.toml
git commit -m "feat: add managed stealth and undetected browser tiers"
```

### Task 6: Cascade Engine, Retry Rules, and Policy Integration

**Files:**
- Create: `src/crawl4ai_mcp/cascade.py`
- Test: `tests/test_cascade.py`

**Interfaces:**
- Consumes: provider registry `dict[Tier, FetchProvider]`, `PolicyStore`, `classify`, `next_tiers`, `render_html`.
- Produces: `CascadeEngine.scrape(url, maximum=Tier.FIRECRAWL, force=None) -> ScrapeResult`.

- [ ] **Step 1: Write failing cascade tests with scripted fake providers**

Required cases:

```python
@pytest.mark.asyncio
async def test_404_stops_without_paid_calls(engine_with_404):
    result = await engine_with_404.scrape("https://example.com/missing")
    assert result.status == "terminal"
    assert engine_with_404.calls == [Tier.HTTP]


@pytest.mark.asyncio
async def test_cloudflare_skips_proxy(engine_with_cloudflare):
    result = await engine_with_cloudflare.scrape("https://protected.example/")
    assert engine_with_cloudflare.calls == [
        Tier.HTTP, Tier.STEALTH, Tier.UNDETECTED,
        Tier.CAMOUFOX, Tier.RAYOBYTE,
    ]
    assert result.tier_used == Tier.RAYOBYTE


@pytest.mark.asyncio
async def test_second_request_starts_at_remembered_tier(engine_with_policy):
    await engine_with_policy.scrape("https://hard.example/a")
    engine_with_policy.calls.clear()
    await engine_with_policy.scrape("https://hard.example/b")
    assert engine_with_policy.calls[0] == Tier.UNDETECTED


@pytest.mark.asyncio
async def test_cooldown_prevents_any_provider_call(engine_in_cooldown):
    result = await engine_in_cooldown.scrape("https://bad.example/x")
    assert result.status == "cooldown"
    assert engine_in_cooldown.calls == []
```

- [ ] **Step 2: Verify cascade tests fail**

Run: `.venv/bin/pytest tests/test_cascade.py -v`

Expected: FAIL because `CascadeEngine` is undefined.

- [ ] **Step 3: Implement the exact cascade algorithm**

```python
async def scrape(self, url, maximum=Tier.FIRECRAWL, force=None):
    if force is None and (cooldown := await self.policy.get_active_cooldown(url)):
        return ScrapeResult.cooldown(url, cooldown.cooldown_until, cooldown.last_error_kind)
    start = force if force is not None else await self.policy.get_start_tier(url)
    queue = [start]
    network_retry_used = False
    while queue:
        tier = queue.pop(0)
        if tier > maximum or tier in attempted:
            continue
        provider = self.providers.get(tier)
        if provider is None or not provider.availability().ready:
            queue.extend(next_tiers(tier, Decision.FAILED, maximum))
            continue
        fetched = await provider.fetch(url)
        decision = classify(fetched, self.threshold)
        if decision in {Decision.SUCCESS, Decision.SHORT_STATIC}:
            markdown = fetched.markdown or await render_html(url, fetched.html)
            await self.policy.record_success(url, tier)
            return ScrapeResult.success_from(fetched, markdown, attempts)
        if decision == Decision.TERMINAL:
            return ScrapeResult.terminal_from(fetched, attempts)
        if decision == Decision.RETRYABLE_NETWORK and not network_retry_used:
            network_retry_used = True
            queue.insert(0, tier)
        else:
            queue.extend(next_tiers(tier, decision, maximum))
    policy = await self.policy.record_failure(url, "all_failed")
    return ScrapeResult.failed(url, policy.cooldown_until, attempts)
```

Deduplicate the queue while preserving order. Every result includes attempt records with tier, decision, elapsed time, status code, error, and cost kind.

- [ ] **Step 4: Run cascade tests**

Run: `.venv/bin/pytest tests/test_cascade.py -v`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/crawl4ai_mcp/cascade.py tests/test_cascade.py
git commit -m "feat: orchestrate tier escalation and policy reuse"
```

### Task 7: Hosted Providers for Rayobyte and Firecrawl

**Files:**
- Create: `src/crawl4ai_mcp/providers/rayobyte.py`
- Create: `src/crawl4ai_mcp/providers/firecrawl.py`
- Create: `docs/provider-contracts/rayobyte.md`
- Test: `tests/providers/test_rayobyte.py`
- Test: `tests/providers/test_firecrawl.py`

**Interfaces:**
- Consumes: `FetchProvider`, `FetchResult`, config secrets.
- Produces: `RayobyteProvider` and `FirecrawlProvider`.

- [ ] **Step 1: Capture the Rayobyte account-specific API contract before coding**

Rayobyte does not publish the Web Scraping API request contract on its public product page. Open the authenticated Rayobyte dashboard, copy its official curl example, redact the credential, and record in `docs/provider-contracts/rayobyte.md`:

- HTTP method and exact endpoint
- API-key location (header/query/body)
- target URL field
- JavaScript rendering field and value
- success response content type and body path
- errors for exhausted credits, rate limit, and invalid target

The document must contain a redacted, executable curl shape. Do not infer field names from another scraping provider. If dashboard access is unavailable during execution, mark only Rayobyte unavailable through `ProviderAvailability`; continue implementing Firecrawl and all other tiers.

- [ ] **Step 2: Turn the captured contract into failing mocked tests**

Use `respx` to assert exact method, URL, auth placement, JSON/query field names, and response extraction. Include 200, credit exhaustion, 429, and malformed-response cases.

- [ ] **Step 3: Implement the minimal Rayobyte provider to satisfy the captured tests**

Requirements independent of the account-specific request shape:

- Lazy shared `httpx.AsyncClient(http2=True, timeout=60)`
- `CostKind.RAYOBYTE_CREDIT`
- `availability().ready` only when endpoint and key are configured
- Return rendered HTML when the API supplies HTML
- Preserve provider error/status without raising into the cascade

- [ ] **Step 4: Write exact Firecrawl v2 contract tests**

```python
@pytest.mark.asyncio
async def test_firecrawl_requests_auto_proxy_and_markdown(respx_mock):
    route = respx_mock.post("https://api.firecrawl.dev/v2/scrape").mock(
        return_value=httpx.Response(200, json={
            "success": True,
            "data": {"markdown": "# Protected", "metadata": {"statusCode": 200}},
        })
    )
    provider = FirecrawlProvider(api_key="fc-test")
    result = await provider.fetch("https://protected.example")
    payload = json.loads(route.calls[0].request.content)
    assert payload == {
        "url": "https://protected.example",
        "formats": ["markdown"],
        "onlyMainContent": True,
        "proxy": "auto",
        "timeout": 60000,
    }
    assert result.markdown == "# Protected"
```

- [ ] **Step 5: Implement Firecrawl v2 exactly as documented**

POST `https://api.firecrawl.dev/v2/scrape`, send `Authorization: Bearer <key>`, and the payload in the test. Parse `data.markdown` and `data.metadata.statusCode`. Map HTTP 402, 429, and 5xx into normalized failed `FetchResult` values. `proxy="auto"` is intentional: Firecrawl may use enhanced proxies and charge up to 5 credits when basic fails.

- [ ] **Step 6: Run hosted-provider tests**

Run: `.venv/bin/pytest tests/providers/test_rayobyte.py tests/providers/test_firecrawl.py -v`

Expected: PASS for every provider whose contract is available; Rayobyte may be explicitly skipped only when the authenticated dashboard contract could not be obtained, with the reason asserted by a test.

- [ ] **Step 7: Run credentialed smoke tests without printing secrets**

Load `.env`, force each hosted tier against a harmless static URL, and print only provider name, status, content length, elapsed time, and cost kind. Never log request headers or full provider responses.

- [ ] **Step 8: Commit**

```bash
git add src/crawl4ai_mcp/providers docs/provider-contracts tests/providers
git commit -m "feat: add hosted scraping provider tiers"
```

### Task 8: Camoufox ARM64 Provider

**Files:**
- Create: `src/crawl4ai_mcp/providers/camoufox.py`
- Test: `tests/providers/test_camoufox.py`

**Interfaces:**
- Consumes: browser semaphore and AppConfig.
- Produces: `CamoufoxProvider.fetch()`, `reap_idle()`, `close()`, and availability reporting.

- [ ] **Step 1: Install and fetch the ARM64 browser artifact**

Run:

```bash
.venv/bin/pip install -e '.[camoufox,test]'
.venv/bin/python -m camoufox fetch
```

Expected: the Linux arm64 Camoufox browser is installed. Verify the executable architecture with `file <resolved-browser-path>` and require `aarch64`/`ARM aarch64` output.

- [ ] **Step 2: Write lifecycle and disabled-mode tests**

Test lazy server launch, shared semaphore use, 120-second idle shutdown, failure-to-launch normalization, and `enabled=false` returning `ProviderAvailability(ready=False, reason="disabled")` without importing/starting Camoufox.

- [ ] **Step 3: Implement Camoufox as an independent Firefox provider**

Use `camoufox.server.launch_server` or its asynchronous process equivalent to start a local WS server lazily, then connect with `playwright.async_api.async_playwright().firefox.connect(endpoint)`. Create one isolated context per request with:

```python
headless=True
humanize=True
block_images=True
block_webrtc=True
```

Navigate using `wait_until="domcontentloaded"`, wait for network idle with a bounded fallback timeout, capture `page.content()`, then close the page/context. Keep the browser server alive until idle reaping.

- [ ] **Step 4: Run unit tests and forced ARM64 smoke test**

Run: `.venv/bin/pytest tests/providers/test_camoufox.py -v`

Then force Camoufox against `https://web-scraping.dev/antibot/easy`, recorded in `tests/acceptance/targets.toml` with the visible success marker observed by a real browser. Also use `https://web-scraping.dev/classify-test/blocked` only to validate Cloudflare-style block classification; it is a deterministic 403 fixture, not a bypass-success target.

Expected: rendered HTML returned; if the upstream ARM64 browser fails, the provider reports unavailable and the cascade skips to Rayobyte rather than failing the request.

- [ ] **Step 5: Commit**

```bash
git add pyproject.toml src/crawl4ai_mcp/providers/camoufox.py tests/providers/test_camoufox.py
git commit -m "feat: add optional camoufox arm64 tier"
```

### Task 9: URL Mapping and Bounded Multi-page Crawl

**Files:**
- Create: `src/crawl4ai_mcp/discovery.py`
- Test: `tests/test_discovery.py`

**Interfaces:**
- Consumes: `AsyncUrlSeeder`, `SeedingConfig`, `CascadeEngine`.
- Produces: `map_urls(url, search=None, limit=100)` and `crawl_site(url, max_pages=10, max_depth=2, include_pattern=None)`.

- [ ] **Step 1: Write mapping tests with an injected fake seeder**

Assert domain extraction, `SeedingConfig(source="sitemap+cc", max_urls=limit, concurrency=20, hits_per_sec=5, query=search, extract_head=bool(search))`, URL deduplication, same-origin filtering, and the exact hard limit of 100.

- [ ] **Step 2: Implement `map_urls`**

Use crawl4ai `AsyncUrlSeeder.urls(domain, config)`, return URL strings only, deduplicate preserving order, and close the seeder in `finally`.

- [ ] **Step 3: Write bounded-crawl tests**

Use a local fixture site with cycles and off-domain links. Verify:

- breadth-first traversal
- no duplicate URLs
- same-origin only
- `max_pages` and `max_depth` enforced
- first successful page determines the remembered domain tier and subsequent pages reuse it
- default browser/HTTP concurrency limits remain owned by providers

- [ ] **Step 4: Implement `crawl_site`**

Extract links from successful HTML using BeautifulSoup, normalize with `urljoin`, remove fragments, apply `include_pattern` with `fnmatch`, and use an explicit queue of `(url, depth)`. Default `max_pages=10`, `max_depth=2`; reject values above 100 pages or 5 depth.

- [ ] **Step 5: Run discovery tests**

Run: `.venv/bin/pytest tests/test_discovery.py -v`

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/crawl4ai_mcp/discovery.py tests/test_discovery.py
git commit -m "feat: add url mapping and bounded site crawl"
```

### Task 10: Service Facade and Four FastMCP Tools

**Files:**
- Create: `src/crawl4ai_mcp/service.py`
- Create: `src/crawl4ai_mcp/server.py`
- Create: `src/crawl4ai_mcp/__main__.py`
- Test: `tests/test_service.py`
- Test: `tests/test_server.py`

**Interfaces:**
- Consumes: all providers, cascade, policy, discovery.
- Produces: `CrawlService.start()`, `close()`, `scrape()`, `crawl()`, `map()`, `diagnose()` and FastMCP tools named exactly `scrape`, `crawl`, `map`, `diagnose`.

- [ ] **Step 1: Write service lifecycle tests**

Assert one shared browser semaphore, lazy provider construction, periodic idle reaping, close order (reaper task → providers → policy DB), and diagnostics containing RSS memory, provider availability, browser state, recent failures, and domain policy.

- [ ] **Step 2: Implement `CrawlService`**

At `start()`, open SQLite and register provider instances but do not start browsers. Start one reaper task that runs every 30 seconds and calls `reap_idle()` on browser providers. At `close()`, cancel the reaper and close providers with `asyncio.gather(*(provider.close() for provider in providers.values()), return_exceptions=True)`.

- [ ] **Step 3: Write MCP schema tests**

Use FastMCP's in-process client to list tools and assert exactly four names and these parameter defaults:

```text
scrape(url, format="markdown", max_tier="firecrawl", force_tier=None)
crawl(url, max_pages=10, max_depth=2, include_pattern=None)
map(url, search=None, limit=100)
diagnose(domain=None)
```

Also call `scrape` through the MCP client with a fake service and assert the structured result preserves `tier_used`, `cost_kind`, `elapsed_ms`, `status`, and `attempts`.

- [ ] **Step 4: Implement FastMCP with Streamable HTTP**

```python
mcp = FastMCP("crawl4ai-mcp")

@mcp.custom_route("/health", methods=["GET"])
async def health(_request):
    return JSONResponse({"status": "ok"})

if __name__ == "__main__":
    mcp.run(
        transport="http",
        host="127.0.0.1",
        port=11236,
        path="/mcp",
        host_origin_protection=True,
        allowed_hosts=["127.0.0.1:11236", "localhost:11236"],
    )
```

Register startup/shutdown hooks that call `CrawlService.start()` and `close()`. Do not enable CORS.

- [ ] **Step 5: Run service and MCP tests**

Run: `.venv/bin/pytest tests/test_service.py tests/test_server.py -v`

Expected: PASS and exactly four tools listed.

- [ ] **Step 6: Run the full unit suite**

Run: `.venv/bin/pytest -v`

Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add src/crawl4ai_mcp/service.py src/crawl4ai_mcp/server.py src/crawl4ai_mcp/__main__.py tests
git commit -m "feat: expose cascade through four mcp tools"
```

### Task 11: systemd Deployment and opencode Configuration

> **Required execution skill:** Invoke `customize-opencode` before reading or modifying opencode configuration or permission files in this task.

**Files:**
- Create: `systemd/crawl4ai-mcp.service`
- Create: `scripts/install-user-service.sh`
- Modify: `.gitignore`
- Modify outside repo: `~/.config/opencode/opencode.jsonc`
- Modify outside repo: `~/.config/opencode/permissions/interactive.jsonc`
- Modify outside repo: `~/.config/opencode/permissions/longrun.jsonc`
- Test: `tests/deployment/test_unit_file.py`

**Interfaces:**
- Consumes: installed `crawl4ai-mcp` entry point and `.env`.
- Produces: auto-starting localhost daemon and opencode MCP entry.

- [ ] **Step 1: Write a unit-file contract test**

Parse `systemd/crawl4ai-mcp.service` and assert exact entries:

```ini
[Service]
Type=exec
WorkingDirectory=%h/Workspace/crawl4ai-mcp
EnvironmentFile=%h/Workspace/crawl4ai-mcp/.env
ExecStart=%h/Workspace/crawl4ai-mcp/.venv/bin/crawl4ai-mcp
Restart=always
RestartSec=5
TimeoutStopSec=30
MemoryHigh=1536M
MemoryMax=2560M
MemorySwapMax=0
KillMode=control-group
```

- [ ] **Step 2: Implement the user unit**

Use `WantedBy=default.target`. Do not use `After=network-online.target` in a user unit. Set `UMask=0077`, `NoNewPrivileges=true`, `PrivateTmp=true`, and `SyslogIdentifier=crawl4ai-mcp`. Do not set `ProtectHome=true` because the service must read its project and browser cache under the user's home.

- [ ] **Step 3: Implement the idempotent installer**

The script must:

1. Refuse to run if `.env` is absent or not mode 600.
2. Create `~/.config/systemd/user` if necessary.
3. Install the unit with `install -m 0644`.
4. Run `systemctl --user daemon-reload`.
5. Run `systemctl --user enable --now crawl4ai-mcp.service`.
6. Poll `http://127.0.0.1:11236/health` for at most 60 seconds.
7. Print `systemctl --user status --no-pager crawl4ai-mcp.service` on failure.

- [ ] **Step 4: Create `.env` securely**

Create the real `.env` with mode 600 and variables:

```dotenv
RAYOBYTE_API_URL=
RAYOBYTE_API_KEY=
FIRECRAWL_API_KEY=
WEBSHARE_PROXIES=
OXYLABS_PROXIES=
```

Populate values interactively from the user's account data; do not read credentials from shell history or commit them. Empty values make that provider unavailable rather than crashing startup.

- [ ] **Step 5: Install and verify the service**

Run:

```bash
scripts/install-user-service.sh
systemctl --user show crawl4ai-mcp.service -p ActiveState -p MemoryHigh -p MemoryMax -p MemorySwapMax
curl -fsS http://127.0.0.1:11236/health
```

Expected: `ActiveState=active`, `MemoryHigh=1610612736`, `MemoryMax=2684354560`, `MemorySwapMax=0`, and health JSON `{"status":"ok"}`.

- [ ] **Step 6: Back up and modify opencode configuration minimally**

Before editing, copy each file to a timestamped backup. Add under `mcp` in `~/.config/opencode/opencode.jsonc`:

```jsonc
"crawl4ai": {
  "type": "remote",
  "url": "http://127.0.0.1:11236/mcp",
  "enabled": true,
  "timeout": 120000
}
```

Add to interactive permissions:

```jsonc
"crawl4ai_*": "ask",
"crawl4ai_scrape": "allow",
"crawl4ai_map": "allow"
```

Add to longrun permissions:

```jsonc
"crawl4ai_*": "allow"
```

Preserve all existing comments and unrelated configuration.

- [ ] **Step 7: Validate opencode config and MCP discovery**

Restart only the opencode user service if its runtime does not hot-reload MCP configuration. Check its journal and use opencode's MCP status/list command available in the installed version. Confirm `crawl4ai` reports connected and exposes exactly `scrape`, `crawl`, `map`, and `diagnose`.

- [ ] **Step 8: Commit repository deployment artifacts**

```bash
git add systemd scripts .gitignore tests/deployment
git commit -m "ops: deploy crawl4ai mcp as bounded user service"
```

Do not commit files under `~/.config/opencode` into this repository.

### Task 12: Full Acceptance, Resource Verification, and Operations Guide

**Files:**
- Create: `tests/acceptance/test_live_tiers.py`
- Create: `tests/acceptance/test_resource_lifecycle.py`
- Create: `docs/operations.md`
- Create: `README.md`

**Interfaces:**
- Consumes: deployed service and credentials.
- Produces: evidence that all 15 design acceptance criteria pass, plus operator procedures.

- [ ] **Step 1: Implement opt-in live tests**

Mark live tests with `pytest.mark.live` and skip unless `CRAWL4AI_MCP_LIVE_TESTS=1`. Load stable URLs and expected markers from `tests/acceptance/targets.toml`; do not hard-code volatile third-party targets in Python.

Cover:

- Tier 0 static document
- Tier 1 JS-rendered page
- Tier 2 forced undetected page
- Tier 3 forced Camoufox page when available
- Tier 4 IP echo showing proxy IP differs from direct IP
- Tier 5 forced Rayobyte when configured
- Tier 6 forced Firecrawl when configured
- a Cloudflare challenge route that skips Tier 4
- a real 404 that consumes no paid tier
- repeated-domain policy reuse
- repeated-failure cooldown

- [ ] **Step 2: Implement resource lifecycle measurement**

Measure cgroup memory from `/sys/fs/cgroup/user.slice/user-$(id -u).slice/user@$(id -u).service/app.slice/crawl4ai-mcp.service/memory.current` when available, with `systemctl --user show -p MemoryCurrent` as fallback.

Assertions:

- after 5 idle minutes: memory below 120 MiB and no service-owned chromium/firefox process
- after Tier 1 request: browser appears
- after 4 idle minutes: browser disappears and memory returns below 120 MiB
- `MemoryMax` equals 2.5 GiB

Process ownership must be determined from the systemd cgroup, not by globally matching all Chromium processes on the host.

- [ ] **Step 3: Verify restart self-healing safely**

Capture `MainPID`, send `kill -9` to that PID, poll for a different active `MainPID` for 30 seconds, then call `/health`.

Expected: new PID within 5-10 seconds and health succeeds.

- [ ] **Step 4: Verify actual opencode tool invocation**

Start a fresh opencode session after configuration reload and request a known static documentation page through `crawl4ai_scrape`. Confirm the returned payload contains markdown, `tier_used`, `cost_kind`, status, elapsed time, and attempts. Then call `crawl4ai_diagnose` and confirm provider availability reflects configured/empty secrets without exposing their values.

- [ ] **Step 5: Write `docs/operations.md`**

Document exact commands for:

- service status, logs, restart, stop/start
- cgroup memory and peak memory inspection
- domain-policy inspection through `diagnose`, plus clearing with the documented local SQLite command while the service is stopped, for example `sqlite3 ~/.local/state/crawl4ai-mcp/policy.db "DELETE FROM domain_policy WHERE domain='example.com';"`; do not add a fifth MCP tool or a destructive `diagnose` parameter
- enabling/disabling Camoufox in `config.toml`
- rotating API keys and proxy lists in `.env`
- recognizing paid-tier usage from `cost_kind`
- handling provider credit exhaustion
- rolling back opencode config from timestamped backups
- uninstalling the user service without deleting the SQLite policy database

- [ ] **Step 6: Update README with concise installation and architecture summary**

Include prerequisites (Python 3.12, aarch64-supported browsers, systemd user linger), installation commands, the four tools, the seven tiers, and links to the design, implementation plan, and operations guide.

- [ ] **Step 7: Run final verification suite**

Run:

```bash
.venv/bin/pytest -v
CRAWL4AI_MCP_LIVE_TESTS=1 .venv/bin/pytest tests/acceptance -v
systemctl --user show crawl4ai-mcp.service -p ActiveState -p MainPID -p MemoryCurrent -p MemoryPeak -p MemoryHigh -p MemoryMax -p MemorySwapMax
git status --short
```

Expected:

- unit/integration suite PASS
- every configured live tier PASS; unavailable optional providers SKIP with explicit reason
- service active and within memory contract
- worktree contains only intentional documentation/test changes before commit

- [ ] **Step 8: Commit**

```bash
git add README.md docs tests/acceptance
git commit -m "test: verify tier escalation and resource bounds"
```

---

## Final Review Checklist

- [ ] All seven tier identifiers exist and are ordered exactly HTTP through FIRECRAWL.
- [ ] `401`, `404`, and `410` never call a later provider.
- [ ] Cloudflare classification never invokes the datacenter proxy tier.
- [ ] A 200 short static page is accepted rather than escalated.
- [ ] Network failure retries the same tier exactly once.
- [ ] Domain success starts the next request at the remembered tier.
- [ ] Policy older than seven days starts one tier cheaper.
- [ ] Failed domains use 10m, 1h, 6h, 24h cooldowns.
- [ ] Provider results always expose `cost_kind` and attempt metadata.
- [ ] No secret is printed, logged, or committed.
- [ ] Browser concurrency is 2 and HTTP concurrency is 8.
- [ ] Chromium and Camoufox are lazy and idle-reaped at 180s/120s.
- [ ] Service is loopback-only on port 11236.
- [ ] systemd cgroup limits exactly match 1.5G/2.5G/0 swap.
- [ ] opencode exposes exactly four `crawl4ai_*` tools.
- [ ] Camoufox and missing hosted credentials degrade by skipping, not startup failure.
