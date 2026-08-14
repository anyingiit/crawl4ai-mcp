import asyncio
import ipaddress

import pytest

from crawl4ai_mcp.config import AppConfig
from crawl4ai_mcp.egress import UrlPolicy, UrlPolicyError, UrlPolicyReason
from crawl4ai_mcp.models import (
    CostKind,
    FetchResult,
    ProviderAvailability,
    ScrapeOutcome,
    ScrapeResponse,
    Tier,
)
from crawl4ai_mcp.service import CrawlService


def public_policy(address: str = "93.184.216.34") -> UrlPolicy:
    async def resolver(_host: str, _port: int):
        return [ipaddress.ip_address(address)]

    return UrlPolicy(resolver)


class StubProvider:
    def __init__(self, tier, semaphore=None, close_log=None, reap_log=None):
        self.tier = tier
        self.semaphore = semaphore
        self.close_log = close_log
        self.reap_log = reap_log
        self.closed = False
        self.reason = None

    async def fetch(self, url):
        return FetchResult(
            url=url, tier=self.tier, cost_kind=CostKind.FREE,
            target_status_code=200, html="<main>" + "x" * 300 + "</main>",
            markdown="# Ok", elapsed_ms=1,
        )

    async def close(self):
        self.closed = True
        if self.close_log is not None:
            self.close_log.append(self.tier.name)

    async def reap_idle(self):
        if self.reap_log is not None:
            self.reap_log.append(self.tier.name)

    def availability(self):
        return ProviderAvailability(enabled=True, ready=True, reason=self.reason)

    def is_active(self):
        return False

    def active_fetch_count(self):
        return 0

    def last_used(self):
        return 0.0


@pytest.fixture
def config(tmp_path):
    return AppConfig(database_path=tmp_path / "policy.db")


async def make_service(config, providers=None, reaper_interval=30.0):
    service = CrawlService(config, providers=providers, reaper_interval=reaper_interval)
    await service.start()
    return service


@pytest.mark.asyncio
async def test_browser_providers_share_one_semaphore(config):
    service = await make_service(config)
    stealth_sem = service.providers[Tier.STEALTH]._semaphore
    undetected_sem = service.providers[Tier.UNDETECTED]._semaphore
    proxy_sem = service.providers[Tier.PROXY]._semaphore
    assert stealth_sem is undetected_sem
    assert stealth_sem is proxy_sem
    await service.close()


@pytest.mark.asyncio
async def test_providers_are_lazy_after_start(config):
    service = await make_service(config)
    assert service.providers[Tier.STEALTH].is_active() is False
    assert service.providers[Tier.CAMOUFOX].is_active() is False
    await service.close()


@pytest.mark.asyncio
async def test_reaper_runs_periodically(config):
    reaped = []
    providers = {
        Tier.STEALTH: StubProvider(Tier.STEALTH, reap_log=reaped),
    }
    service = await make_service(config, providers=providers, reaper_interval=0.05)
    await asyncio.sleep(0.15)
    assert reaped
    await service.close()


@pytest.mark.asyncio
async def test_close_order_reaper_then_providers_then_egress_then_policy(config):
    close_log = []
    providers = {
        Tier.STEALTH: StubProvider(Tier.STEALTH, close_log=close_log),
    }
    service = await make_service(config, providers=providers, reaper_interval=0.05)
    await service.close()
    assert service._close_events == [
        "_reaper_cancelled", "_providers_closed", "_egress_closed", "_policy_closed",
    ]
    assert close_log == ["STEALTH"]
    assert service.providers[Tier.STEALTH].closed is True
    assert service.policy is None


class _ServiceFakeClock:
    def __init__(self, now: float = 0.0):
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class _ServiceFakeCrawler:
    def __init__(self, factory):
        self.factory = factory
        self.closed = False

    async def arun(self, url, config=None):
        return _ServiceFakeContainer()

    async def close(self):
        self.factory.close_started.set()
        if self.factory.close_gate is not None:
            await self.factory.close_gate.wait()
        self.closed = True
        self.factory.closed += 1


class _ServiceFakeContainer:
    def __init__(self):
        self._results = [
            _ServiceFakeResult(
                html="<main>" + "x" * 300 + "</main>",
                success=True,
            )
        ]


class _ServiceFakeResult:
    def __init__(self, html, success):
        self.html = html
        self.status_code = 200
        self.response_headers = {"content-type": "text/html"}
        self.redirected_url = None
        self.error_message = None
        self.success = success


class _ServiceFakeCrawlerFactory:
    def __init__(self):
        self.created = 0
        self.closed = 0
        self.close_started = asyncio.Event()
        self.close_gate = None

    def __call__(self):
        self.created += 1
        return _ServiceFakeCrawler(self)


class _ServiceFakeEgressProxy:
    def endpoint(self, upstream=None):
        return None


class _ServiceFakeGuard:
    async def install(self, context):
        pass

    def begin_fetch(self):
        return _ServiceFakeRecorder()

    def bind_page(self, page):
        pass


class _ServiceFakeRecorder:
    def blocked(self):
        return []

    def close(self):
        pass


@pytest.mark.asyncio
async def test_service_close_cancels_reaper_stuck_in_idle_reap(config):
    from crawl4ai_mcp.providers.browser import BrowserProvider

    clock = _ServiceFakeClock()
    factory = _ServiceFakeCrawlerFactory()
    provider = BrowserProvider(
        tier=Tier.STEALTH, factory=factory, idle_seconds=180,
        semaphore=asyncio.Semaphore(2), clock=clock,
        egress_proxy=_ServiceFakeEgressProxy(), request_guard=_ServiceFakeGuard(),
    )
    await provider.fetch("https://example.com/a")
    clock.advance(181)
    factory.close_gate = asyncio.Event()
    service = CrawlService(
        config,
        providers={Tier.STEALTH: provider},
        reaper_interval=0.01,
    )
    await service.start()
    close_task = None
    try:
        await asyncio.wait_for(factory.close_started.wait(), timeout=2)
        close_task = asyncio.create_task(service.close())
        await asyncio.sleep(0.05)
        assert not close_task.done()
        factory.close_gate.set()
        await asyncio.wait_for(close_task, timeout=2)
        assert factory.closed == 1
        assert provider._close_tasks == set()
        assert service._close_events[0] == "_reaper_cancelled"
        result = await provider.fetch("https://example.com/b")
        assert result.error is not None and "closed" in (result.error or "")
    finally:
        if close_task is not None and not close_task.done():
            close_task.cancel()
        if service._reaper_task is not None and not service._reaper_task.done():
            service._reaper_task.cancel()
        await asyncio.gather(
            *(
                task
                for task in (service._reaper_task, close_task)
                if task is not None
            ),
            return_exceptions=True,
        )
        factory.close_gate.set()


@pytest.mark.asyncio
async def test_service_shares_one_url_policy_egress_proxy_and_guard(config):
    service = await make_service(config)
    try:
        assert service.providers[Tier.STEALTH].egress_proxy is service._egress_proxy
        assert service.providers[Tier.UNDETECTED].egress_proxy is service._egress_proxy
        assert service.providers[Tier.PROXY].egress_proxy is service._egress_proxy
        assert service.providers[Tier.CAMOUFOX].egress_proxy is service._egress_proxy
        assert service.providers[Tier.HTTP]._policy is service._url_policy
        guard = service._request_guard
        assert service.providers[Tier.STEALTH].request_guard is guard
        assert service.providers[Tier.UNDETECTED].request_guard is guard
        assert service.providers[Tier.PROXY].request_guard is guard
        assert service.providers[Tier.CAMOUFOX].request_guard is guard
        assert service._url_policy is not None
        assert service._egress_proxy is not None
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_scrape_success_returns_structured_result(config):
    providers = {Tier.HTTP: StubProvider(Tier.HTTP)}
    service = await make_service(config, providers=providers)
    service._url_policy = public_policy()
    try:
        payload = await service.scrape("https://example.com")
        assert payload["status"] == "success"
        assert payload["tier_used"] == "http"
        assert payload["cost_kind"] == "free"
        assert isinstance(payload["elapsed_ms"], int)
        assert payload["content"] == "# Ok"
        assert len(payload["attempts"]) == 1
        assert payload["attempts"][0]["decision"] == "success"
        assert list(service._recent_failures) == []
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_service_rejects_private_url_before_policy_lookup(config):
    class BoomEngine:
        def __init__(self):
            self.calls = []

        async def scrape(self, url, maximum=Tier.FIRECRAWL, force=None):
            self.calls.append(url)
            raise AssertionError("engine must not run for rejected url")

    engine = BoomEngine()
    service = CrawlService(
        config,
        providers={Tier.HTTP: StubProvider(Tier.HTTP)},
        engine=engine,
    )
    await service.start()
    try:
        with pytest.raises(UrlPolicyError) as exc:
            await service.scrape("http://127.0.0.1/")
        assert exc.value.reason == UrlPolicyReason.NON_GLOBAL_ADDRESS
        assert engine.calls == []
        assert await service.policy.list_policies() == []
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_service_crawl_returns_pages_and_stats_without_raw_html(config):
    providers = {Tier.HTTP: StubProvider(Tier.HTTP)}
    service = await make_service(config, providers=providers)
    service._url_policy = public_policy()
    try:
        payload = await service.crawl("https://example.com/", max_pages=2, max_depth=1)
        assert set(payload) == {"pages", "stats"}
        stats = payload["stats"]
        assert stats["attempted_pages"] == 1
        assert stats["successful_pages"] == 1
        assert stats["failed_pages"] == 0
        assert stats["max_depth_reached"] == 0
        assert stats["elapsed_ms"] >= 0
        assert payload["pages"][0]["url"] == "https://example.com/"
        assert payload["pages"][0]["response"]["status"] == "success"
        assert "raw_html" not in str(payload)
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_service_scrape_html_returns_exact_successful_raw_html(config):
    providers = {Tier.HTTP: StubProvider(Tier.HTTP)}
    service = await make_service(config, providers=providers)
    service._url_policy = public_policy()
    try:
        payload = await service.scrape("https://example.com/", format="html")
        assert payload["status"] == "success"
        assert payload["content"] == "<main>" + "x" * 300 + "</main>"
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_service_scrape_markdown_default_returns_rendered_markdown_not_html(config):
    providers = {Tier.HTTP: StubProvider(Tier.HTTP)}
    service = await make_service(config, providers=providers)
    service._url_policy = public_policy()
    try:
        payload = await service.scrape("https://example.com/")
        assert payload["status"] == "success"
        assert payload["content"] == "# Ok"
        assert payload["content"] != "<main>" + "x" * 300 + "</main>"
    finally:
        await service.close()


class StubOutcomeEngine:
    def __init__(self, outcome):
        self.outcome = outcome

    async def scrape(self, url, maximum=Tier.FIRECRAWL, force=None):
        return self.outcome


def markdown_only_success(url="https://example.com/"):
    response = ScrapeResponse(
        url=url,
        status="success",
        content="# Title",
        tier_used="http",
        cost_kind=CostKind.FREE,
        elapsed_ms=1,
    )
    return ScrapeOutcome(response=response, raw_html=None, effective_url=url)


@pytest.mark.asyncio
async def test_service_scrape_html_without_raw_html_fails(config):
    service = CrawlService(
        config,
        providers={Tier.HTTP: StubProvider(Tier.HTTP)},
        engine=StubOutcomeEngine(markdown_only_success()),
    )
    await service.start()
    service._url_policy = public_policy()
    try:
        payload = await service.scrape("https://example.com/", format="html")
        assert payload["status"] == "failed"
        assert payload["error"] == "successful provider did not return html"
        assert payload["content"] == ""
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_service_scrape_html_preserves_non_success_status(config):
    response = ScrapeResponse(
        url="https://example.com/",
        status="cooldown",
        elapsed_ms=0,
        cooldown_until=999,
        error="cooldown",
    )
    outcome = ScrapeOutcome(
        response=response, raw_html=None, effective_url="https://example.com/"
    )
    service = CrawlService(
        config,
        providers={Tier.HTTP: StubProvider(Tier.HTTP)},
        engine=StubOutcomeEngine(outcome),
    )
    await service.start()
    service._url_policy = public_policy()
    try:
        payload = await service.scrape("https://example.com/", format="html")
        assert payload["status"] == "cooldown"
        assert payload["cooldown_until"] == 999
        assert payload["error"] == "cooldown"
    finally:
        await service.close()


class SpyPolicy(UrlPolicy):
    def __init__(self):
        super().__init__(
            lambda _host, _port: [ipaddress.ip_address("93.184.216.34")]
        )
        self.resolve_calls = []

    async def resolve(self, url):
        self.resolve_calls.append(url)


@pytest.mark.asyncio
async def test_service_scrape_rejects_unknown_format_before_policy_or_engine(config):
    class BoomEngine:
        async def scrape(self, url, maximum=Tier.FIRECRAWL, force=None):
            raise AssertionError("engine must not run for rejected format")

    policy = SpyPolicy()
    service = CrawlService(
        config,
        providers={Tier.HTTP: StubProvider(Tier.HTTP)},
        engine=BoomEngine(),
    )
    service._url_policy = policy
    with pytest.raises(ValueError, match="unknown format"):
        await service.scrape("https://example.com/", format="text")
    assert policy.resolve_calls == []


@pytest.mark.asyncio
async def test_diagnose_reports_expected_sections(config):
    providers = {Tier.HTTP: StubProvider(Tier.HTTP)}
    service = await make_service(config, providers=providers)
    try:
        report = await service.diagnose()
        assert report["rss_bytes"] > 0
        assert report["providers"]["HTTP"]["ready"] is True
        assert report["browsers"]["HTTP"]["active"] is False
        assert report["browsers"]["HTTP"]["active_fetches"] == 0
        assert report["browsers"]["HTTP"]["last_used"] == 0.0
        assert report["recent_failures"] == []
        assert report["domain_policies"] == []
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_diagnose_accepts_bare_hostname(config):
    service = await make_service(config)
    try:
        await service.policy.record_success(
            "https://example.com/a", Tier.HTTP, now=1_000
        )
        report = await service.diagnose(domain="example.com")
        assert [row["domain"] for row in report["domain_policies"]] == ["example.com"]
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_diagnose_still_accepts_full_url(config):
    service = await make_service(config)
    try:
        await service.policy.record_success(
            "https://example.com/a", Tier.HTTP, now=1_000
        )
        report = await service.diagnose(domain="https://example.com/b?q=1")
        assert [row["domain"] for row in report["domain_policies"]] == ["example.com"]
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_diagnose_rejects_private_literal_hostname(config):
    service = await make_service(config)
    try:
        with pytest.raises(UrlPolicyError):
            await service.diagnose(domain="127.0.0.1")
    finally:
        await service.close()


from crawl4ai_mcp.service import parse_upstream_proxy


def test_parse_upstream_proxy_accepts_http_and_https():
    proxy = parse_upstream_proxy("http://proxy.example:8080")
    assert proxy.server == "http://proxy.example:8080"
    assert proxy.username is None
    assert proxy.password is None
    proxy = parse_upstream_proxy("https://proxy.example:8443")
    assert proxy.server == "https://proxy.example:8443"
    proxy = parse_upstream_proxy("proxy.example:8080")
    assert proxy.server == "http://proxy.example:8080"


def test_parse_upstream_proxy_rejects_unsupported_schemes():
    for url in ("ftp://proxy.example:21", "socks5://proxy.example:1080"):
        with pytest.raises(ValueError, match="scheme"):
            parse_upstream_proxy(url)


def test_parse_upstream_proxy_rejects_userinfo():
    with pytest.raises(ValueError, match="userinfo"):
        parse_upstream_proxy("http://user:pass@proxy.example:8080")


def test_parse_upstream_proxy_rejects_path_query_and_fragment():
    for url in (
        "http://proxy.example:8080/path",
        "http://proxy.example:8080/?q=1",
        "http://proxy.example:8080/#frag",
    ):
        with pytest.raises(ValueError):
            parse_upstream_proxy(url)


def test_parse_upstream_proxy_rejects_invalid_ports_cleanly():
    with pytest.raises(ValueError, match="port"):
        parse_upstream_proxy("http://proxy.example:notaport")
    with pytest.raises(ValueError, match="port"):
        parse_upstream_proxy("http://proxy.example:99999")


def test_parse_upstream_proxy_canonicalizes_host():
    proxy = parse_upstream_proxy("HTTP://ExAmPlE.COM.:8080")
    assert proxy.server == "http://example.com:8080"


def test_parse_upstream_proxy_rejects_missing_host():
    with pytest.raises(ValueError):
        parse_upstream_proxy("http://:8080")
    with pytest.raises(ValueError):
        parse_upstream_proxy("http://user@:8080")


def test_parse_upstream_proxy_accepts_explicit_credentials():
    proxy = parse_upstream_proxy("http://proxy.example:8080", "user", "pass")
    assert proxy.server == "http://proxy.example:8080"
    assert proxy.username == "user"
    assert proxy.password == "pass"


@pytest.mark.parametrize("username,password", [("user", None), (None, "pass")])
def test_parse_upstream_proxy_rejects_partial_credentials(username, password):
    with pytest.raises(ValueError, match="username and password"):
        parse_upstream_proxy("http://proxy.example:8080", username, password)


def test_parse_upstream_proxy_userinfo_still_rejected_with_credentials():
    with pytest.raises(ValueError, match="userinfo"):
        parse_upstream_proxy("http://user:pass@proxy.example:8080", "u", "p")


@pytest.mark.asyncio
async def test_proxy_pool_entries_carry_pool_specific_credentials(config):
    config = config.model_copy(
        update={
            "webshare_proxies": ["http://ws.proxy.example:8080"],
            "oxylabs_proxies": ["http://dc.oxylabs.example:8000"],
            "webshare_proxy_username": "ws-user",
            "webshare_proxy_password": "ws-pass",
            "oxylabs_proxy_username": "ox-user",
            "oxylabs_proxy_password": "ox-pass",
        }
    )
    service = await make_service(config)
    try:
        pool = service.providers[Tier.PROXY].proxy_pool
        assert [entry.server for entry in pool] == [
            "http://ws.proxy.example:8080",
            "http://dc.oxylabs.example:8000",
        ]
        assert pool[0].username == "ws-user"
        assert pool[0].password == "ws-pass"
        assert pool[1].username == "ox-user"
        assert pool[1].password == "ox-pass"
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_partial_credential_pool_marks_proxy_unavailable(config):
    config = config.model_copy(
        update={
            "webshare_proxies": ["http://ws.proxy.example:8080"],
            "webshare_proxy_username": "only-user",
        }
    )
    service = await make_service(config)
    try:
        availability = service.providers[Tier.PROXY].availability()
        assert availability.ready is False
        assert "webshare" in availability.reason
        assert "username and password" in availability.reason
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_credential_free_pool_stays_valid(config):
    config = config.model_copy(
        update={"oxylabs_proxies": ["http://dc.oxylabs.example:8000"]}
    )
    service = await make_service(config)
    try:
        availability = service.providers[Tier.PROXY].availability()
        assert availability.ready is True
        entry = service.providers[Tier.PROXY].proxy_pool[0]
        assert entry.username is None
        assert entry.password is None
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_pool_credentials_reach_connect_authorization(config):
    import base64

    from crawl4ai_mcp.egress import build_upstream_connect

    config = config.model_copy(
        update={
            "webshare_proxies": ["http://ws.proxy.example:8080"],
            "webshare_proxy_username": "ws-user",
            "webshare_proxy_password": "ws-pass",
        }
    )
    service = await make_service(config)
    try:
        upstream = service.providers[Tier.PROXY].proxy_pool[0]
        request = build_upstream_connect("93.184.216.34", 443, upstream)
        token = base64.b64encode(b"ws-user:ws-pass").decode()
        assert f"Proxy-Authorization: Basic {token}".encode() in request
    finally:
        await service.close()
