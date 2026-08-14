# crawl4ai-mcp Review Remediation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Repair the reviewed `crawl4ai-mcp` implementation so all outbound fetches enforce one SSRF-safe egress policy, cascade and paid-credit invariants match the approved design, browser resources are race-safe, crawl discovery uses the successful rendered response, hosted-provider failures remain distinct from target HTTP statuses, and the four MCP tools expose their approved validated contracts.

**Architecture:** Add one shared URL policy and a small in-process pinning forward proxy. Tier 0 retains curl_cffi TLS impersonation while pinning each manually validated redirect hop through `CURLOPT_RESOLVE`; browser, Patchright, Camoufox, and URL-seeder traffic use the forward proxy as the hard DNS-pinning boundary, with browser request interception as defense in depth. Keep the existing provider/cascade/service architecture, adding explicit internal fetch outcomes, provider failure kinds, request-scoped cascade state, synchronized browser lifecycles, and typed MCP response models.

**Tech Stack:** Python 3.12, crawl4ai 0.9.2, curl_cffi 0.16.x with `CurlOpt.RESOLVE`, FastMCP 3.4.x, aiosqlite, httpx, Playwright, Patchright `UndetectedAdapter`, Camoufox 0.5.x, Pydantic 2, pytest, pytest-asyncio, respx, systemd --user, opencode remote MCP.

## Global Constraints

- Work from `/home/ubuntu/Workspace/crawl4ai-mcp/.worktrees/crawl4ai-mcp` on branch `feat/crawl4ai-mcp` and review against `bf1feb4029909ceda60486cf704b59a744ccedc7`.
- Preserve exactly four MCP tools: `scrape`, `crawl`, `map`, and `diagnose`.
- Preserve all seven tiers in order: `http`, `stealth`, `undetected`, `camoufox`, `proxy`, `rayobyte`, `firecrawl`.
- Keep `crawl4ai==0.9.2`; do not patch or import the sibling upstream checkout at runtime.
- Accept only absolute `http` and `https` target URLs without username/password.
- Reject every non-global destination, including embedded-address IPv6 transition forms; reject a hostname if any DNS answer is non-global.
- Do not rely on resolve-then-discard validation: the validated address must be the address actually dialed, including every redirect hop.
- Browser public cross-origin subresources remain allowed; non-global subresources are blocked.
- A target-network failure retries the same tier once, then stops and records cooldown; it never escalates.
- Once Cloudflare is observed, Tier 4 remains forbidden for that request.
- Only target `401`, `404`, and `410` are terminal. Provider failures are never target statuses.
- Browser concurrency remains 2; HTTP concurrency remains 8. Chromium idle timeout remains 180 seconds; Camoufox remains 120 seconds.
- Bind only to validated `127.0.0.1`; use configured port, default `11236`.
- Preserve `MemoryHigh=1536M`, `MemoryMax=2560M`, and `MemorySwapMax=0`.
- Keep automatic paid fallback except where target-network or provider-failure rules explicitly stop or skip.
- Do not expose raw HTML unless `format="html"` is explicitly requested.
- Use TDD and end every task with an independently reviewable commit.
- Do not run live or paid-provider requests before the final opt-in acceptance task.

---

## File Structure And Change Map

```text
src/crawl4ai_mcp/egress.py              shared URL/origin/DNS policy, browser guard, pinning proxy
src/crawl4ai_mcp/models.py              provider failures, internal outcome, explicit MCP models
src/crawl4ai_mcp/providers/base.py      typed failure constructors
src/crawl4ai_mcp/providers/http.py      pinned curl dialing and manual redirects
src/crawl4ai_mcp/providers/browser.py   browser egress, lifecycle locking, per-fetch proxy rotation
src/crawl4ai_mcp/providers/camoufox.py  Camoufox egress and lifecycle locking
src/crawl4ai_mcp/providers/rayobyte.py  provider/target status separation
src/crawl4ai_mcp/providers/firecrawl.py provider/target status separation and HTML retrieval
src/crawl4ai_mcp/detect.py              typed classification
src/crawl4ai_mcp/cascade.py             retry, Cloudflare, max-tier, provider-fallback invariants
src/crawl4ai_mcp/discovery.py           exact-response link extraction and normalized origins
src/crawl4ai_mcp/service.py             shared egress ownership and typed facade
src/crawl4ai_mcp/server.py              exact four-tool schemas
src/crawl4ai_mcp/__main__.py            configured bind host/port
tests/test_egress.py                     SSRF, DNS, transition IPv6, proxy and browser guard tests
tests/providers/*.py                    provider regressions
tests/test_{detect,cascade,discovery,models,service,server,main}.py
tests/acceptance/*.py                    strict provider/resource/opencode acceptance
scripts/run-acceptance.sh                pass/skip/incomplete accounting
README.md, docs/operations.md            repaired contracts and operator procedure
```

## Shared Test Fixture Contract

When a task's test snippets use these names, add these exact local helpers to the named test module in that task. They are test-only interfaces, not production APIs:

```python
def public_policy(address: str = "93.184.216.34") -> UrlPolicy:
    async def resolver(_host: str, _port: int):
        return [ipaddress.ip_address(address)]
    return UrlPolicy(resolver)


def private_policy() -> UrlPolicy:
    async def resolver(_host: str, _port: int):
        return [ipaddress.ip_address("127.0.0.1")]
    return UrlPolicy(resolver)


def two_host_public_policy() -> UrlPolicy:
    async def resolver(host: str, _port: int):
        address = "93.184.216.34" if host == "example.com" else "93.184.216.35"
        return [ipaddress.ip_address(address)]
    return UrlPolicy(resolver)
```

Use these concrete fakes where referenced:

```python
class FakePinnedProxy:
    def __init__(self):
        self.endpoint_calls = []

    def endpoint(self, upstream=None):
        self.endpoint_calls.append(upstream)
        port = 41000 + len(self.endpoint_calls) - 1
        return ProxyConfig(server=f"http://127.0.0.1:{port}")


class FakeRoute:
    def __init__(self):
        self.continued = False
        self.aborted = False

    async def continue_(self):
        self.continued = True

    async def abort(self, _reason="blockedbyclient"):
        self.aborted = True


class FakeRequest:
    def __init__(self, url: str):
        self.url = url


class SeederFactory:
    def __init__(self, entries=()):
        self.entries = list(entries)
        self.clients = []
        self.calls = []

    def __call__(self, client):
        self.clients.append(client)
        seeder = FakeSeeder(self.entries)
        self.calls.append(seeder)
        return seeder
```

Browser and Camoufox fake factories record creation, closure, run configs/context kwargs, expose `started = asyncio.Event()`, and await an injected gate inside `arun()`/`goto()` when supplied. Add these assertions directly to the fake methods:

```python
async def arun(self, url, config=None):
    self.configs.append(config)
    self.factory.started.set()
    if self.factory.gate is not None:
        await self.factory.gate.wait()
    return FakeContainer()

async def goto(self, url, **kwargs):
    self.context.factory.started.set()
    if self.context.factory.gate is not None:
        await self.context.factory.gate.wait()
    return FakeResponse(status=200, url=url)
```

Use this exact discovery engine helper:

```python
class ScriptedEngine:
    def __init__(self, outcomes: dict[str, ScrapeOutcome]):
        self.outcomes = outcomes
        self.calls = []

    async def scrape(self, url, maximum=Tier.FIRECRAWL, force=None):
        self.calls.append(url)
        return self.outcomes[url]


def successful_outcome(
    url="https://example.com/",
    markdown="# ok",
    raw_html="<main>ok</main>",
    tier="http",
    effective_url=None,
):
    response = ScrapeResponse(
        url=url,
        status="success",
        content=markdown,
        tier_used=tier,
        cost_kind=CostKind.FREE,
        elapsed_ms=1,
        attempts=[],
    )
    return ScrapeOutcome(
        response=response,
        raw_html=raw_html,
        effective_url=effective_url or url,
    )


def rendered_root_and_child_outcomes():
    return {
        "https://example.com/": successful_outcome(
            raw_html='<main><a href="/rendered">Rendered</a></main>',
            tier="stealth",
        ),
        "https://example.com/rendered": successful_outcome(
            url="https://example.com/rendered",
            raw_html="<main>child</main>",
            tier="stealth",
        ),
    }


def cross_origin_redirect_outcome():
    return {
        "https://example.com/": successful_outcome(
            raw_html='<a href="/escape-child">child</a>',
            effective_url="https://other.example/",
        )
    }
```

Hosted-provider test modules use literal response helpers:

```python
def mock_rayobyte_failure(respx_mock, status, message):
    return respx_mock.get(API_URL).mock(return_value=httpx.Response(
        200,
        json={"status": "FAIL", "statusCode": status, "error": message},
    ))


def mock_rayobyte_success(respx_mock, target_status=200, html="<main>Hello</main>"):
    return respx_mock.get(API_URL).mock(return_value=httpx.Response(
        200,
        json={"status": "SUCCESS", "httpCode": target_status, "result": html},
    ))


def mock_firecrawl_provider_failure(respx_mock, status):
    return respx_mock.post(API_URL).mock(return_value=httpx.Response(
        status,
        json={"success": False, "error": f"provider {status}"},
    ))
```

### Task 1: Shared URL, Address, And Origin Policy

**Files:**
- Create: `src/crawl4ai_mcp/egress.py`
- Modify: `src/crawl4ai_mcp/policy.py`
- Create: `tests/test_egress.py`
- Modify: `tests/test_policy.py`

**Interfaces:**
- Produces `UrlPolicyReason`, `UrlPolicyError`, `Origin`, `ValidatedUrl`, `ResolvedTarget`, `parse_public_url(url: str) -> ValidatedUrl`, `normalized_origin(url: str) -> Origin`, `same_origin(left: str, right: str) -> bool`, `is_allowed_address(address: IPv4Address | IPv6Address) -> bool`, and `UrlPolicy.resolve(url: str) -> ResolvedTarget`.
- `UrlPolicy.__init__(resolver: Callable[[str, int], Awaitable[Sequence[IPv4Address | IPv6Address]]] | None = None)` uses `getaddrinfo` by default.
- `PolicyStore` consumes `parse_public_url(url).host`; invalid input cannot create an empty-domain row.

- [ ] **Step 1: Write failing syntax and origin tests**

```python
@pytest.mark.parametrize("url", [
    "file:///etc/passwd", "raw:<h1>x</h1>", "ftp://example.com/a",
    "https://user@example.com/a", "https://user:pass@example.com/a",
    "https:///missing-host", "https://example.com:99999/a",
])
def test_parse_public_url_rejects_unsafe_syntax(url):
    with pytest.raises(UrlPolicyError):
        parse_public_url(url)

def test_parse_public_url_normalizes_host_port_and_fragment():
    parsed = parse_public_url("HTTPS://ExAmPlE.COM.:443/a?q=1#fragment")
    assert parsed.url == "https://example.com/a?q=1"
    assert parsed.origin == Origin("https", "example.com", 443)

def test_same_origin_includes_scheme_and_effective_port():
    assert same_origin("https://example.com/a", "https://example.com:443/b")
    assert not same_origin("http://example.com/a", "https://example.com/a")
    assert not same_origin("https://example.com/a", "https://example.com:444/a")
```

- [ ] **Step 2: Run syntax tests**

Run: `.venv/bin/pytest tests/test_egress.py -k 'parse_public_url or same_origin' -v`

Expected: FAIL with `ModuleNotFoundError: No module named 'crawl4ai_mcp.egress'`.

- [ ] **Step 3: Implement exact URL normalization**

`parse_public_url` lowercases the scheme, accepts only `http`/`https`, rejects credentials before hostname use, IDNA-normalizes and strips the trailing dot, catches invalid ports, applies effective port 80/443, strips fragments, preserves path/query, and emits bracketed IPv6 authorities. `UrlPolicyError` stores `reason`, `url`, and `detail`.

- [ ] **Step 4: Write failing address tests**

```python
@pytest.mark.parametrize("literal", [
    "127.0.0.1", "10.0.0.1", "169.254.169.254", "::1", "fe80::1",
    "::ffff:127.0.0.1", "::127.0.0.1", "2002:7f00:1::",
    "2001:0000:4136:e378:8000:63bf:3fff:fdd2", "64:ff9b::7f00:1",
    "64:ff9b:1::7f00:1", "2001:db8:0:1:0:5efe:127.0.0.1",
])
def test_is_allowed_address_rejects_non_global_and_transition_addresses(literal):
    assert is_allowed_address(ipaddress.ip_address(literal)) is False

@pytest.mark.parametrize("literal", ["8.8.8.8", "1.1.1.1", "2606:4700:4700::1111"])
def test_is_allowed_address_accepts_global_addresses(literal):
    assert is_allowed_address(ipaddress.ip_address(literal)) is True
```

- [ ] **Step 5: Implement embedded-address rejection**

Require `address.is_global`, recursively validate `ipv4_mapped`, `sixtofour`, and both Teredo endpoints, inspect `::/96`, `64:ff9b::/96`, `64:ff9b:1::/48`, and recognize ISATAP interface identifiers `00:00:5e:fe`/`02:00:5e:fe`. Any embedded non-global IPv4 rejects the IPv6 address.

- [ ] **Step 6: Write and satisfy DNS all-answer tests**

```python
@pytest.mark.asyncio
async def test_url_policy_rejects_mixed_global_and_private_dns_answers():
    async def resolver(_host, _port):
        return [ipaddress.ip_address("93.184.216.34"), ipaddress.ip_address("127.0.0.1")]
    with pytest.raises(UrlPolicyError) as exc:
        await UrlPolicy(resolver).resolve("https://example.com/")
    assert exc.value.reason == UrlPolicyReason.NON_GLOBAL_ADDRESS

@pytest.mark.asyncio
async def test_url_policy_returns_all_validated_global_answers():
    async def resolver(_host, _port):
        return [ipaddress.ip_address("93.184.216.34"), ipaddress.ip_address("2606:2800:220:1:248:1893:25c8:1946")]
    target = await UrlPolicy(resolver).resolve("https://example.com/")
    assert tuple(map(str, target.addresses)) == (
        "93.184.216.34", "2606:2800:220:1:248:1893:25c8:1946",
    )
```

Default resolution is `getaddrinfo(host, port, type=SOCK_STREAM)`, deduplicated in order; empty results are `DNS_FAILED` and one unsafe answer rejects the hostname.

- [ ] **Step 7: Update policy normalization and run tests**

Run: `.venv/bin/pytest tests/test_egress.py tests/test_policy.py -v`

Expected: PASS; `normalize_domain("file:///etc/passwd")` raises `UrlPolicyError` and no empty-domain row is written.

- [ ] **Step 8: Commit**

```bash
git add src/crawl4ai_mcp/egress.py src/crawl4ai_mcp/policy.py tests/test_egress.py tests/test_policy.py
git commit -m "fix: enforce shared public url policy"
```

### Task 2: Pinned HTTP Dialing And Secure Forward Proxy

**Files:**
- Modify: `src/crawl4ai_mcp/egress.py`
- Modify: `src/crawl4ai_mcp/models.py`
- Modify: `src/crawl4ai_mcp/providers/base.py`
- Modify: `src/crawl4ai_mcp/providers/http.py`
- Modify: `tests/test_egress.py`
- Modify: `tests/providers/test_http.py`

**Interfaces:**
- `FetchResult` replaces ambiguous `status_code` with `target_status_code`, `provider_status_code`, `network_error`, and `policy_error`.
- Produces external `AttemptResponse` and `ScrapeResponse` now, so all later tasks use one name and serialization contract. `AttemptResponse.tier` and `ScrapeResponse.tier_used` are lowercase strings; provider-specific fields are added in Task 7 without renaming them.
- Produces internal frozen slotted `ScrapeOutcome(response: ScrapeResponse, raw_html: str | None, effective_url: str)`. Initially cascade may set `raw_html=None`; Task 6 populates and consumes it.
- `HttpProvider(policy: UrlPolicy, concurrency: int = 8, timeout_seconds: int = 10, max_redirects: int = 10, session_factory: Callable | None = None)`.
- `UpstreamProxy(server: str, username: str | None = None, password: str | None = None)` and `PinnedEgressProxy.start()`, `endpoint(upstream: UpstreamProxy | None = None) -> ProxyConfig`, `close()`.

- [ ] **Step 1: Write failing curl pinning tests**

```python
@pytest.mark.asyncio
async def test_http_provider_pins_validated_address_with_curl_resolve():
    factory = FakeSessionFactory([FakeResponse(status_code=200)])
    provider = HttpProvider(public_policy("93.184.216.34"), session_factory=factory)
    result = await provider.fetch("https://example.com/")
    assert result.target_status_code == 200
    assert factory.sessions[0].kwargs["trust_env"] is False
    assert factory.sessions[0].kwargs["allow_redirects"] is False
    assert factory.sessions[0].kwargs["curl_options"][CurlOpt.RESOLVE] == [
        "example.com:443:93.184.216.34"
    ]

@pytest.mark.asyncio
async def test_http_provider_validates_and_repins_every_redirect_hop():
    factory = FakeSessionFactory([
        FakeResponse(302, headers={"location": "https://www.example.net/final"}),
        FakeResponse(200),
    ])
    provider = HttpProvider(two_host_public_policy(), session_factory=factory)
    result = await provider.fetch("https://example.com/start")
    assert len(factory.sessions) == 2
    assert factory.sessions[1].kwargs["curl_options"][CurlOpt.RESOLVE] == [
        "www.example.net:443:93.184.216.35"
    ]
    assert result.redirected_url == "https://www.example.net/final"
```

- [ ] **Step 2: Run curl tests**

Run: `.venv/bin/pytest tests/providers/test_http.py -k 'pins_validated or repins_every' -v`

Expected: FAIL because `HttpProvider` lacks policy injection and follows redirects automatically.

- [ ] **Step 3: Implement manual pinned hops**

For each hop resolve through `UrlPolicy`, create a fresh `AsyncSession(impersonate="chrome131", trust_env=False, allow_redirects=False, curl_options={CurlOpt.RESOLVE: [f"{host}:{port}:{address}" ...]})`, close it in `finally`, and follow only 301/302/303/307/308 with `urljoin`. This preserves hostname Host/SNI while libcurl dials only validated IPs. Return policy failure before a private redirect dial and `network_error="too_many_redirects"` after 10 hops.

- [ ] **Step 4: Write failing proxy pinning tests**

```python
@pytest.mark.asyncio
async def test_pinning_proxy_connects_to_resolved_ip_not_hostname():
    connected = []
    async def connect(host, port, **_kwargs):
        connected.append((host, port)); return FakeReader(), FakeWriter()
    proxy = PinnedEgressProxy(public_policy("93.184.216.34"), connect=connect)
    await proxy._open_direct_tunnel("example.com", 443)
    assert connected == [("93.184.216.34", 443)]

def test_upstream_connect_uses_pinned_ip_and_proxy_auth():
    request = build_upstream_connect(
        "93.184.216.34", 443, UpstreamProxy("http://proxy.example:8080", "u", "p")
    )
    assert request.startswith(b"CONNECT 93.184.216.34:443 HTTP/1.1\r\n")
    assert b"Proxy-Authorization: Basic dTpw\r\n" in request
```

- [ ] **Step 5: Implement the in-process proxy**

Bind listeners to `127.0.0.1` ephemeral ports; cap headers at 64 KiB; support CONNECT and absolute-form HTTP; validate every target; dial a validated IP; tunnel TLS unchanged; rewrite direct HTTP to origin form while preserving Host; for Tier 4 send `CONNECT <pinned-ip>:<port>` to the configured upstream proxy; strip caller proxy authorization; try validated addresses in order; cancel both copy tasks and close both sides. Keep one direct endpoint and one endpoint per `UpstreamProxy`.

- [ ] **Step 6: Run Task 2 tests**

Run: `.venv/bin/pytest tests/test_egress.py tests/providers/test_http.py tests/test_models.py tests/test_detect.py -v`

Expected: PASS, including private redirect rejection before a second session is created.

- [ ] **Step 7: Commit**

```bash
git add src/crawl4ai_mcp/egress.py src/crawl4ai_mcp/models.py src/crawl4ai_mcp/providers/base.py src/crawl4ai_mcp/providers/http.py tests/test_egress.py tests/providers/test_http.py tests/test_models.py tests/test_detect.py
git commit -m "fix: pin http and proxy egress addresses"
```

### Task 3: Browser, Camoufox, Seeder Egress, And Per-Fetch Proxy Rotation

**Files:**
- Modify: `src/crawl4ai_mcp/providers/browser.py`
- Modify: `src/crawl4ai_mcp/providers/camoufox.py`
- Modify: `src/crawl4ai_mcp/discovery.py`
- Modify: `src/crawl4ai_mcp/service.py`
- Modify: `tests/providers/test_browser.py`
- Modify: `tests/providers/test_camoufox.py`
- Modify: `tests/test_discovery.py`
- Modify: `tests/test_service.py`

**Interfaces:**
- `BrowserRequestGuard(policy: UrlPolicy).install(context)` registers `context.route("**/*", handler)`; syntax/credentials are intercepted, while the pinning proxy enforces DNS/dial safety.
- `BrowserProvider(..., egress_proxy: PinnedEgressProxy, request_guard: BrowserRequestGuard, proxy_pool: Sequence[UpstreamProxy] = ())`.
- `CamoufoxProvider(..., egress_proxy: PinnedEgressProxy, request_guard: BrowserRequestGuard)`.
- `map_urls(..., policy: UrlPolicy, proxy: PinnedEgressProxy, seeder_factory: Callable | None = None)`.

- [ ] **Step 1: Write browser guard and rotation tests**

```python
@pytest.mark.asyncio
async def test_browser_guard_allows_public_cross_origin_subresource():
    route = FakeRoute()
    await BrowserRequestGuard(public_policy()).handle(route, FakeRequest("https://cdn.example.net/app.js"))
    assert route.continued and not route.aborted

@pytest.mark.asyncio
async def test_proxy_provider_rotates_on_consecutive_fetches_without_reap(fake_clock):
    factory = FakeCrawlerFactory()
    egress = FakePinnedProxy()
    provider = BrowserProvider(
        tier=Tier.PROXY,
        idle_seconds=180,
        semaphore=asyncio.Semaphore(2),
        egress_proxy=egress,
        request_guard=FakeRequestGuard(),
        proxy_pool=(
            UpstreamProxy("http://proxy-one:8080"),
            UpstreamProxy("http://proxy-two:8080"),
        ),
        factory=factory,
        clock=fake_clock,
    )
    await provider.fetch("https://example.com/a")
    await provider.fetch("https://example.com/b")
    assert factory.created == 1
    assert [c.proxy_config.server for c in factory.crawlers[0].configs] == [
        "http://127.0.0.1:41001", "http://127.0.0.1:41002",
    ]
```

- [ ] **Step 2: Run browser tests**

Run: `.venv/bin/pytest tests/providers/test_browser.py -k 'guard or rotates_on_consecutive' -v`

Expected: FAIL because current Tier 4 selects a proxy only at crawler creation and passes `config=None`.

- [ ] **Step 3: Implement crawl4ai 0.9.2-compatible browser policy**

Register the inspected `on_page_context_created` strategy hook and install the route guard. For every fetch pass `CrawlerRunConfig(proxy_config=egress_proxy.endpoint(selected_upstream), cache_mode=CacheMode.BYPASS)`. Tier 4 selects and increments its upstream index per fetch. `BrowserManager.create_browser_context()` includes `CrawlerRunConfig.proxy_config` in its context signature, and both Playwright and Patchright expose `BrowserContext.route`, so one crawler process can safely reuse/create proxy-specific contexts.

- [ ] **Step 4: Write and satisfy Camoufox policy test**

```python
@pytest.mark.asyncio
async def test_camoufox_context_uses_pinning_proxy_and_route_guard():
    launcher = FakeLauncher()
    guard = FakeRequestGuard()
    provider = CamoufoxProvider(
        enabled=True,
        idle_seconds=120,
        semaphore=asyncio.Semaphore(2),
        launcher=launcher,
        egress_proxy=FakePinnedProxy(),
        request_guard=guard,
    )
    await provider.fetch("https://example.com/")
    context = launcher.sessions[0].browser.contexts[0]
    assert context.kwargs["proxy"]["server"] == "http://127.0.0.1:41000"
    assert guard.contexts == [context]
```

Use inspected `camoufox.async_api.AsyncNewContext(browser, proxy=...)`, then install the same route guard before `new_page()`.

- [ ] **Step 5: Write and satisfy secure seeder tests**

```python
@pytest.mark.asyncio
async def test_map_urls_validates_root_before_constructing_seeder():
    factory = SeederFactory()
    with pytest.raises(UrlPolicyError):
        await map_urls("http://127.0.0.1/", policy=private_policy(), proxy=FakePinnedProxy(), seeder_factory=factory)
    assert factory.calls == []

@pytest.mark.asyncio
async def test_map_urls_routes_seeder_client_through_pinning_proxy():
    factory = SeederFactory()
    await map_urls("https://example.com/", policy=public_policy(), proxy=FakePinnedProxy(), seeder_factory=factory)
    assert factory.clients[0].proxy_url == "http://127.0.0.1:41000"
```

Construct `httpx.AsyncClient(proxy=endpoint.server, trust_env=False, follow_redirects=True, timeout=30)` and inject it into `AsyncUrlSeeder(client=client)`; close seeder and client in `finally`.

- [ ] **Step 6: Make service own shared egress**

`CrawlService.start()` creates one `UrlPolicy`, one started `PinnedEgressProxy`, and one `BrowserRequestGuard`, injects them everywhere, and `close()` closes providers before the proxy.

- [ ] **Step 7: Run Task 3 tests**

Run: `.venv/bin/pytest tests/test_egress.py tests/providers/test_browser.py tests/providers/test_camoufox.py tests/test_discovery.py tests/test_service.py -v`

Expected: PASS.

- [ ] **Step 8: Commit**

```bash
git add src/crawl4ai_mcp/egress.py src/crawl4ai_mcp/providers/browser.py src/crawl4ai_mcp/providers/camoufox.py src/crawl4ai_mcp/discovery.py src/crawl4ai_mcp/service.py tests
git commit -m "fix: secure browser and discovery egress"
```

### Task 4: Cascade And Paid-Credit Invariants

**Files:**
- Modify: `src/crawl4ai_mcp/detect.py`
- Modify: `src/crawl4ai_mcp/cascade.py`
- Modify: `src/crawl4ai_mcp/service.py`
- Modify: `tests/test_detect.py`
- Modify: `tests/test_cascade.py`
- Modify: `tests/test_service.py`

**Interfaces:**
- Decisions include `TARGET_NETWORK`, `PROVIDER_FAILURE`, and `POLICY_REJECTED`.
- `CascadeInputError(ValueError)` rejects force above maximum.
- Consumes Task 2 `ScrapeOutcome` and `ScrapeResponse`.
- `CascadeEngine.scrape(url: str, maximum: Tier = Tier.FIRECRAWL, force: Tier | None = None) -> ScrapeOutcome`.

- [ ] **Step 1: Write the four failing regressions**

```python
@pytest.mark.asyncio
async def test_target_network_failure_retries_same_tier_once_then_stops(engine):
    outcome = await engine.scrape("https://flaky.example/")
    assert engine.calls == [Tier.HTTP, Tier.HTTP]
    assert outcome.response.status == "failed"
    assert outcome.response.attempts[-1].decision == Decision.TARGET_NETWORK

@pytest.mark.asyncio
async def test_cloudflare_seen_once_permanently_forbids_proxy(engine):
    outcome = await engine.scrape("https://protected.example/")
    assert Tier.PROXY not in engine.calls
    assert outcome.response.tier_used == "rayobyte"

@pytest.mark.asyncio
async def test_remembered_tier_above_maximum_starts_at_maximum(engine, policy):
    await policy.record_success("https://hard.example/a", Tier.FIRECRAWL, now=1000)
    outcome = await engine.scrape("https://hard.example/b", maximum=Tier.UNDETECTED)
    assert engine.calls == [Tier.UNDETECTED]
    assert outcome.response.status == "success"

@pytest.mark.asyncio
async def test_force_above_maximum_is_rejected_without_policy_mutation(engine, policy):
    with pytest.raises(CascadeInputError):
        await engine.scrape("https://example.com/", maximum=Tier.HTTP, force=Tier.FIRECRAWL)
    assert engine.calls == []
    assert await policy.list_policies() == []
```

- [ ] **Step 2: Run regressions**

Run: `.venv/bin/pytest tests/test_cascade.py -k 'target_network_failure or permanently_forbids or remembered_tier_above or force_above' -v`

Expected: FAIL under current escalation, non-sticky Cloudflare, zero-attempt max-tier, and force-tier behavior.

- [ ] **Step 3: Implement request-scoped state**

Reject `force > maximum` before cooldown/policy access. Use `start = force if force is not None else min(await policy.get_start_tier(url, now), maximum)`. Maintain `attempt_counts: dict[Tier, int]` and `cloudflare_seen`. First `TARGET_NETWORK` requeues the same tier; second records `error_kind="target_network"` and returns. Once `cloudflare_seen=True`, filter Tier 4 from every later candidate list. `POLICY_REJECTED` returns without cooldown mutation.

- [ ] **Step 4: Validate service input before policy mutation**

Add `test_service_rejects_private_url_before_policy_lookup`; `CrawlService.scrape()` resolves the URL and validates force/max before invoking the engine.

- [ ] **Step 5: Run Task 4 tests**

Run: `.venv/bin/pytest tests/test_detect.py tests/test_cascade.py tests/test_service.py -v`

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/crawl4ai_mcp/detect.py src/crawl4ai_mcp/cascade.py src/crawl4ai_mcp/service.py tests/test_detect.py tests/test_cascade.py tests/test_service.py
git commit -m "fix: preserve cascade cost invariants"
```

### Task 5: Browser Lifecycle Synchronization

**Files:**
- Modify: `src/crawl4ai_mcp/providers/browser.py`
- Modify: `src/crawl4ai_mcp/providers/camoufox.py`
- Modify: `src/crawl4ai_mcp/service.py`
- Modify: `tests/providers/test_browser.py`
- Modify: `tests/providers/test_camoufox.py`
- Modify: `tests/test_service.py`

**Interfaces:**
- Both browser providers maintain `_lifecycle: asyncio.Condition`, `_active_fetches: int`, `_closing: bool`, and expose `active_fetch_count() -> int`, `last_used() -> float`, `is_active() -> bool`.

- [ ] **Step 1: Write active-reap, shutdown, and first-launch race tests**

```python
@pytest.mark.asyncio
async def test_browser_reaper_does_not_close_active_crawler(provider, gate, clock):
    fetch = asyncio.create_task(provider.fetch("https://example.com/slow"))
    await provider.factory.started.wait(); clock.advance(181)
    await provider.reap_idle()
    assert provider.factory.closed == 0
    gate.set(); await fetch

@pytest.mark.asyncio
async def test_browser_close_waits_for_active_fetch_then_closes_once(provider, gate):
    fetch = asyncio.create_task(provider.fetch("https://example.com/slow"))
    await provider.factory.started.wait()
    close = asyncio.create_task(provider.close()); await asyncio.sleep(0)
    assert not close.done()
    gate.set(); await fetch; await close
    assert provider.factory.closed == 1

@pytest.mark.asyncio
async def test_concurrent_first_fetches_create_one_crawler(provider):
    await asyncio.gather(provider.fetch("https://example.com/a"), provider.fetch("https://example.com/b"))
    assert provider.factory.created == 1
```

- [ ] **Step 2: Run lifecycle tests**

Run: `.venv/bin/pytest tests/providers/test_browser.py -k 'reaper_does_not or close_waits or concurrent_first' -v`

Expected: FAIL because reaping and creation are unsynchronized.

- [ ] **Step 3: Implement detached-close synchronization**

Under the condition, reject new fetches while closing, lazily create once, increment active count, and capture the resource. In fetch `finally`, decrement, update last-used, and notify. Reap only when active count is zero; detach the resource under lock and close outside it. `close()` sets closing, waits for zero active fetches, detaches, and closes once outside the condition.

- [ ] **Step 4: Apply and test identical Camoufox semantics**

Add `test_camoufox_reaper_does_not_close_active_session`, `test_camoufox_close_waits_for_active_fetch`, and `test_concurrent_camoufox_first_fetches_launch_once` with the same expected behavior.

- [ ] **Step 5: Extend diagnostics and run tests**

Run: `.venv/bin/pytest tests/providers/test_browser.py tests/providers/test_camoufox.py tests/test_service.py -v`

Expected: PASS; diagnose reports `active`, `active_fetches`, and `last_used` through public methods.

- [ ] **Step 6: Commit**

```bash
git add src/crawl4ai_mcp/providers/browser.py src/crawl4ai_mcp/providers/camoufox.py src/crawl4ai_mcp/service.py tests/providers/test_browser.py tests/providers/test_camoufox.py tests/test_service.py
git commit -m "fix: synchronize browser lifecycle shutdown"
```

### Task 6: Preserve Successful HTML And Correct Crawl Origins

**Files:**
- Modify: `src/crawl4ai_mcp/models.py`
- Modify: `src/crawl4ai_mcp/cascade.py`
- Modify: `src/crawl4ai_mcp/discovery.py`
- Modify: `src/crawl4ai_mcp/service.py`
- Modify: `tests/test_models.py`
- Modify: `tests/test_cascade.py`
- Modify: `tests/test_discovery.py`
- Modify: `tests/test_service.py`

**Interfaces:**
- Consumes Task 2 `ScrapeOutcome(response: ScrapeResponse, raw_html: str | None, effective_url: str)`; this task begins populating `raw_html` and using it for discovery. It remains private and is never directly returned by MCP.
- External `CrawlPage`, `CrawlStats(attempted_pages, successful_pages, failed_pages, max_depth_reached, elapsed_ms)`, and `CrawlResponse(pages, stats)`.

- [ ] **Step 1: Write raw-HTML privacy and no-refetch tests**

```python
def test_scrape_outcome_keeps_raw_html_out_of_external_response():
    outcome = successful_outcome(raw_html="<a href='/rendered'>x</a>")
    assert outcome.raw_html
    assert "raw_html" not in outcome.response.model_dump(mode="json")

@pytest.mark.asyncio
async def test_crawl_extracts_links_from_successful_rendered_response_without_refetch():
    engine = ScriptedEngine(rendered_root_and_child_outcomes())
    crawl = await crawl_site("https://example.com/", engine=engine, policy=public_policy(), max_depth=1)
    assert [page.url for page in crawl.pages] == [
        "https://example.com/", "https://example.com/rendered",
    ]
    assert engine.calls == ["https://example.com/", "https://example.com/rendered"]
```

- [ ] **Step 2: Write origin and redirect escape tests**

```python
def test_extract_links_rejects_scheme_port_and_non_http_changes():
    html = '<a href="https://example.com:443/ok">ok</a><a href="http://example.com/no">no</a><a href="https://example.com:444/no">no</a><a href="file:///etc/passwd">no</a>'
    assert extract_links(html, "https://example.com/root", Origin("https", "example.com", 443)) == [
        "https://example.com/ok"
    ]

@pytest.mark.asyncio
async def test_crawl_does_not_follow_links_after_cross_origin_redirect():
    engine = ScriptedEngine(cross_origin_redirect_outcome())
    crawl = await crawl_site("https://example.com/", engine=engine, policy=public_policy())
    assert len(crawl.pages) == 1
```

- [ ] **Step 3: Run crawl regressions**

Run: `.venv/bin/pytest tests/test_discovery.py -k 'rendered_response or scheme_port or cross_origin_redirect' -v`

Expected: FAIL because current crawl performs `_page_html()` through Tier 0 and compares hostname only.

- [ ] **Step 4: Implement exact-response discovery**

Delete `_page_html()`. Cascade success stores fetched HTML privately and `effective_url=fetched.redirected_url or fetched.url`. Crawl extracts from that HTML using the effective URL as base, the normalized root `Origin` as boundary, and stops link discovery when the effective URL escaped origin. Validate candidates with `parse_public_url` before queueing.

- [ ] **Step 5: Produce crawl stats and run tests**

Run: `.venv/bin/pytest tests/test_models.py tests/test_cascade.py tests/test_discovery.py tests/test_service.py -v`

Expected: PASS; provider call counts prove no second fetch.

- [ ] **Step 6: Commit**

```bash
git add src/crawl4ai_mcp/models.py src/crawl4ai_mcp/cascade.py src/crawl4ai_mcp/discovery.py src/crawl4ai_mcp/service.py tests
git commit -m "fix: crawl from successful rendered html"
```

### Task 7: Hosted Provider Error Model

**Files:**
- Modify: `src/crawl4ai_mcp/models.py`
- Modify: `src/crawl4ai_mcp/providers/base.py`
- Modify: `src/crawl4ai_mcp/providers/rayobyte.py`
- Modify: `src/crawl4ai_mcp/providers/firecrawl.py`
- Modify: `src/crawl4ai_mcp/detect.py`
- Modify: `src/crawl4ai_mcp/cascade.py`
- Modify: `tests/providers/test_rayobyte.py`
- Modify: `tests/providers/test_firecrawl.py`
- Modify: `tests/test_detect.py`
- Modify: `tests/test_cascade.py`

**Interfaces:**
- `ProviderErrorKind`: `AUTH`, `QUOTA`, `RATE_LIMIT`, `TRANSPORT`, `SERVICE`, `MALFORMED_RESPONSE`.
- `FetchResult` and `AttemptResponse` carry target status, provider status, provider error kind, and provider error separately.
- Every provider failure skips once to the next available tier; none receives target-network retry treatment. Only target 401/404/410 is terminal.

- [ ] **Step 1: Write Rayobyte separation tests**

```python
@pytest.mark.asyncio
async def test_rayobyte_invalid_token_is_provider_auth_not_target_401(respx_mock):
    mock_rayobyte_failure(respx_mock, 401, "Invalid token")
    result = await RayobyteProvider(API_URL, "bad").fetch("https://example.com/")
    assert result.target_status_code is None
    assert result.provider_status_code == 401
    assert result.provider_error_kind == ProviderErrorKind.AUTH

@pytest.mark.asyncio
async def test_rayobyte_target_404_remains_target_status(respx_mock):
    mock_rayobyte_success(respx_mock, target_status=404)
    result = await RayobyteProvider(API_URL, "key").fetch("https://example.com/missing")
    assert result.target_status_code == 404
    assert result.provider_status_code == 200
```

Map provider 401/403 to AUTH, 402/credit text to QUOTA, 429 to RATE_LIMIT, 5xx to SERVICE, `httpx.RequestError` to TRANSPORT, and invalid JSON/shape to MALFORMED_RESPONSE.

- [ ] **Step 2: Write Firecrawl separation tests**

```python
@pytest.mark.parametrize(("status", "kind"), [
    (401, ProviderErrorKind.AUTH), (402, ProviderErrorKind.QUOTA),
    (429, ProviderErrorKind.RATE_LIMIT), (500, ProviderErrorKind.SERVICE),
])
@pytest.mark.asyncio
async def test_firecrawl_http_error_is_provider_failure(respx_mock, status, kind):
    mock_firecrawl_provider_failure(respx_mock, status)
    result = await FirecrawlProvider("fc-test").fetch("https://example.com/")
    assert result.target_status_code is None
    assert result.provider_status_code == status
    assert result.provider_error_kind == kind
```

Request `formats: ["markdown", "html"]`; map `data.html`, `data.markdown`, and `data.metadata.statusCode`; malformed successful payloads are MALFORMED_RESPONSE.

- [ ] **Step 3: Write cascade provider-fallback tests**

```python
@pytest.mark.asyncio
async def test_provider_auth_failure_falls_back_without_terminal_or_network_retry(engine):
    outcome = await engine.scrape("https://hard.example/")
    assert engine.calls == [Tier.RAYOBYTE, Tier.FIRECRAWL]
    assert outcome.response.status == "success"

@pytest.mark.asyncio
async def test_target_404_from_hosted_provider_is_terminal(engine):
    outcome = await engine.scrape("https://hard.example/missing")
    assert engine.calls == [Tier.RAYOBYTE]
    assert outcome.response.status == "terminal"
```

- [ ] **Step 4: Run regressions**

Run: `.venv/bin/pytest tests/providers/test_rayobyte.py tests/providers/test_firecrawl.py tests/test_detect.py tests/test_cascade.py -v`

Expected before implementation: FAIL because provider codes occupy target `status_code`. Expected after exact mappings/classification: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/crawl4ai_mcp/models.py src/crawl4ai_mcp/providers src/crawl4ai_mcp/detect.py src/crawl4ai_mcp/cascade.py tests
git commit -m "fix: separate provider and target failures"
```

### Task 8: MCP Contracts, Input Bounds, Format, And Runtime Binding

**Files:**
- Modify: `src/crawl4ai_mcp/models.py`
- Modify: `src/crawl4ai_mcp/discovery.py`
- Modify: `src/crawl4ai_mcp/service.py`
- Modify: `src/crawl4ai_mcp/server.py`
- Modify: `src/crawl4ai_mcp/__main__.py`
- Modify: `tests/test_models.py`
- Modify: `tests/test_discovery.py`
- Modify: `tests/test_service.py`
- Modify: `tests/test_server.py`
- Create: `tests/test_main.py`

**Interfaces:**
- `ScrapeFormat = Literal["markdown", "html"]`; constrained `MapLimit` is 1..100, `MaxPages` 1..100, `MaxDepth` 1..5.
- Consumes Task 2 `AttemptResponse` and `ScrapeResponse`, Task 6 `CrawlPage`/`CrawlStats`/`CrawlResponse`, and adds `MapResponse(urls: list[str])` and `DiagnoseResponse` without renaming prior types.
- External tiers are lowercase strings, never integers.
- `run_server(config: AppConfig, service: CrawlService | None = None) -> None`.

- [ ] **Step 1: Write model and format tests**

```python
def test_scrape_response_serializes_tier_names_not_ints():
    payload = scrape_response(tier_used="undetected").model_dump(mode="json")
    assert payload["tier_used"] == "undetected"
    assert payload["attempts"][0]["tier"] == "undetected"

@pytest.mark.asyncio
async def test_service_scrape_html_returns_exact_successful_raw_html(service):
    service.engine.result = successful_outcome(markdown="# Title", raw_html="<h1>Title</h1>")
    result = await service.scrape("https://example.com/", format="html")
    assert result.content == "<h1>Title</h1>"
```

Markdown returns rendered markdown. HTML returns exact private successful HTML. A markdown-only success requested as HTML returns status `failed` and error `successful provider did not return html`.

- [ ] **Step 2: Write exact MCP schema tests**

```python
@pytest.mark.asyncio
async def test_exactly_four_tools_with_explicit_output_schemas(client):
    tools = {tool.name: tool for tool in await client.list_tools()}
    assert set(tools) == {"scrape", "crawl", "map", "diagnose"}
    assert tools["scrape"].inputSchema["properties"]["format"]["enum"] == ["markdown", "html"]
    assert set(tools["crawl"].outputSchema["properties"]) == {"pages", "stats"}
    assert set(tools["map"].outputSchema["properties"]) == {"urls"}
    assert tools["map"].inputSchema["properties"]["limit"]["minimum"] == 1
    assert tools["map"].inputSchema["properties"]["limit"]["maximum"] == 100
```

- [ ] **Step 3: Write map and binding regressions**

```python
@pytest.mark.parametrize("limit", [0, -1, 101])
@pytest.mark.asyncio
async def test_map_urls_rejects_limit_outside_one_to_one_hundred(limit):
    with pytest.raises(ValueError, match="limit must be between 1 and 100"):
        await map_urls("https://example.com/", limit=limit, policy=public_policy(), proxy=FakePinnedProxy())

def test_run_server_uses_validated_bind_config(monkeypatch, tmp_path):
    config = AppConfig(bind_host="127.0.0.1", bind_port=12345, database_path=tmp_path / "p.db")
    fake = FakeMCP(); monkeypatch.setattr(main_module, "create_server", lambda *_a, **_k: fake)
    main_module.run_server(config, service=FakeService())
    assert fake.run_kwargs["host"] == "127.0.0.1"
    assert fake.run_kwargs["port"] == 12345
    assert fake.run_kwargs["allowed_hosts"] == ["127.0.0.1:12345", "localhost:12345"]
```

- [ ] **Step 4: Run regressions**

Run: `.venv/bin/pytest tests/test_models.py tests/test_discovery.py tests/test_service.py tests/test_server.py tests/test_main.py -v`

Expected before implementation: FAIL on output shapes, ignored format, lower map bound, and hard-coded runtime bind. Expected after implementation: PASS.

- [ ] **Step 5: Implement exact tool signatures and configured run**

Annotate tool returns with explicit models; keep only four decorated functions. Pass format to service. Return `CrawlResponse` and `MapResponse`. Validate map before `SeedingConfig`. `main()` loads once and calls `run_server`; create one MCP instance and run with configured host/port, `/mcp`, strict origin protection, and derived allowed hosts.

- [ ] **Step 6: Commit**

```bash
git add src/crawl4ai_mcp/models.py src/crawl4ai_mcp/discovery.py src/crawl4ai_mcp/service.py src/crawl4ai_mcp/server.py src/crawl4ai_mcp/__main__.py tests
git commit -m "fix: enforce typed mcp contracts and inputs"
```

### Task 9: Acceptance, Deployment, And Opencode Verification

**Files:**
- Modify: `pyproject.toml`
- Modify: `tests/acceptance/test_live_tiers.py`
- Modify: `tests/acceptance/test_resource_lifecycle.py`
- Create: `tests/acceptance/test_opencode_mcp.py`
- Create: `scripts/run-acceptance.sh`
- Modify: `README.md`
- Modify: `docs/operations.md`
- Verify: `systemd/crawl4ai-mcp.service`
- Verify: `tests/deployment/test_unit_file.py`

**Interfaces:**
- Markers: `live`, `acceptance_required`, `acceptance_optional`.
- `scripts/run-acceptance.sh` requires opt-in, writes JUnit XML, and exits 0 only for zero failures/errors/skips; exits 3 for otherwise successful but incomplete skipped acceptance.

- [ ] **Step 1: Make configured providers fail rather than skip**

For Camoufox, proxy, Rayobyte, and Firecrawl: skip only when disabled/unconfigured. If enabled/configured, assert `availability.ready`, successful fetch, exact tier, exact cost kind, and target marker. Remove credit-exhaustion and proxied-fetch-failure skips.

- [ ] **Step 2: Add required security and cascade acceptance**

```python
@pytest.mark.acceptance_required
@pytest.mark.asyncio
async def test_live_service_rejects_loopback_and_file_urls(remote_client):
    for url in ("http://127.0.0.1/", "file:///etc/passwd"):
        with pytest.raises(Exception):
            await remote_client.call_tool("scrape", {"url": url})

@pytest.mark.acceptance_required
@pytest.mark.asyncio
async def test_network_failure_does_not_reach_paid_tiers(service):
    result = await service.scrape(TARGETS["unreachable_public"]["url"])
    assert len(result.attempts) == 2
    assert result.attempts[0].tier == result.attempts[1].tier
    assert not {"rayobyte", "firecrawl"} & {a.tier for a in result.attempts}
```

Record a dated, stable global-address test target in `tests/acceptance/targets.toml`; it must fail by connection refusal/timeout, not by private-address policy.

- [ ] **Step 3: Strengthen resource acceptance**

Require cgroup-owned process checks and exact `MemoryHigh`, `MemoryMax`, `MemorySwapMax`, `KillMode=control-group`, and `Restart=always`. Add an isolated short-timeout test proving reap does not close an active fetch. Mark idle memory, browser appearance/disappearance, limits, and restart as required.

- [ ] **Step 4: Add actual opencode discovery and invocation**

```python
def test_opencode_lists_connected_crawl4ai_server():
    completed = subprocess.run(["opencode", "mcp", "list"], text=True, capture_output=True, check=True, timeout=60)
    output = completed.stdout + completed.stderr
    assert "crawl4ai" in output and "connected" in output.lower()

def test_opencode_invokes_crawl4ai_scrape():
    prompt = 'Use crawl4ai_scrape exactly once for https://example.com/ with format="markdown", max_tier="http", force_tier="http".'
    completed = subprocess.run(["opencode", "run", "--format", "json", "--auto", "--dir", str(ROOT), prompt], text=True, capture_output=True, check=True, timeout=180)
    assert "crawl4ai_scrape" in completed.stdout
    assert "Example Domain" in completed.stdout
    assert '"tier_used":"http"' in completed.stdout.replace(" ", "")
```

- [ ] **Step 5: Add acceptance accounting script**

Run pytest with `--junitxml`, parse tests/failures/errors/skips, print counts, exit 1 on failure/error, exit 3 on any skip, and print `acceptance complete` only when all criteria pass. This preserves safe optional live tests while preventing skipped criteria from being reported as passed acceptance.

- [ ] **Step 6: Update operator documentation**

Document URL restrictions, curl pinning, browser/Camoufox/seeder proxying, public subresource allowance, private blocking, Tier 4 per-fetch rotation, target-network stop, sticky Cloudflare skip, provider failure mappings, formats, response shapes, and the complete command `CRAWL4AI_MCP_LIVE_TESTS=1 scripts/run-acceptance.sh`.

- [ ] **Step 7: Run non-live verification**

Run: `.venv/bin/pytest -m "not live" -v`

Expected: PASS with no unit, integration, or deployment failures.

- [ ] **Step 8: Run deployment contract verification**

Run: `.venv/bin/pytest tests/deployment/test_unit_file.py -v`

Expected: PASS with limits `1536M`, `2560M`, and `0` unchanged.

- [ ] **Step 9: Run authorized opt-in acceptance**

Run: `CRAWL4AI_MCP_LIVE_TESTS=1 scripts/run-acceptance.sh`

Expected complete deployment: exit 0, zero skips/failures/errors, and `acceptance complete`. Expected with intentionally unavailable optional providers: exit 3 and `acceptance incomplete`; do not treat it as a pass.

- [ ] **Step 10: Commit**

```bash
git add pyproject.toml tests/acceptance scripts/run-acceptance.sh README.md docs/operations.md
git commit -m "test: require complete deployment acceptance"
```

---

## Final Verification

- [ ] Run `.venv/bin/pytest -m "not live" -v`; expected PASS.
- [ ] Run `.venv/bin/pytest tests/test_egress.py tests/providers/test_http.py tests/providers/test_browser.py tests/providers/test_camoufox.py -v`; expected PASS.
- [ ] Run `.venv/bin/pytest tests/test_detect.py tests/test_cascade.py tests/providers/test_rayobyte.py tests/providers/test_firecrawl.py -v`; expected PASS.
- [ ] Run `.venv/bin/pytest tests/test_discovery.py tests/test_models.py tests/test_service.py tests/test_server.py tests/test_main.py -v`; expected PASS with exactly four tools.
- [ ] Run `git diff --check bf1feb4029909ceda60486cf704b59a744ccedc7..HEAD`; expected no output.
- [ ] Run `git status --short`; expected clean after the final implementation commit.
- [ ] Run `git log --oneline bf1feb4029909ceda60486cf704b59a744ccedc7..HEAD`; expected one independently reviewable commit per task.
- [ ] In an authorized deployment run `CRAWL4AI_MCP_LIVE_TESTS=1 scripts/run-acceptance.sh`; expected exit 0 only with no skips.

## Review Finding Coverage

- [ ] **1. SSRF/local-file reads:** Tasks 1-3 cover syntax, credentials, transition IPv6, all-answer DNS checks, pinned curl redirects, browser/Patchright/Camoufox egress, subresources, and seeder traffic.
- [ ] **2. Network retry paid escalation:** Task 4 retries the same tier once, stops, and records `target_network` cooldown.
- [ ] **3. Cloudflare Tier 4 stickiness:** Task 4 retains request-scoped `cloudflare_seen` and filters Tier 4 permanently.
- [ ] **4. Remembered/forced tier bounds:** Task 4 clamps remembered start and rejects force above max without mutation.
- [ ] **5. Active browser reaping:** Task 5 adds synchronized active counts and safe shutdown.
- [ ] **6. Tier 4 per-fetch rotation:** Task 3 uses per-fetch `CrawlerRunConfig.proxy_config` with one crawler process.
- [ ] **7. Crawl Tier 0 refetch:** Task 6 carries private raw HTML and removes `_page_html()`.
- [ ] **8. Hostname-only origin:** Tasks 1 and 6 compare normalized scheme, host, and effective port and handle redirect escape.
- [ ] **9. Provider/target error conflation:** Task 7 defines exact enums, fields, mappings, and fallback behavior.
- [ ] **10. MCP contract/format mismatch:** Task 8 adds lowercase tier names, explicit models, format handling, `{pages, stats}`, and `{urls}`.
- [ ] **11. Map non-positive limit:** Task 8 enforces 1..100 before `SeedingConfig` and in MCP schema.
- [ ] **12. Weak acceptance:** Task 9 strengthens configured-provider, markers, resources, actual opencode invocation, and skip accounting.
- [ ] **13. Ignored bind config:** Task 8 passes configured host/port and derives allowed hosts.
- [ ] Exactly four tools and seven tiers remain.
- [ ] Loopback-only deployment and systemd limits remain unchanged.
- [ ] Domain memory, decay, cooldown, and approved automatic paid fallback remain active.
- [ ] Raw HTML remains internal unless explicitly requested.
