import asyncio
import ipaddress

import pytest
from crawl4ai.async_configs import ProxyConfig

from crawl4ai_mcp.egress import BrowserRequestGuard, UpstreamProxy, UrlPolicy
from crawl4ai_mcp.models import CostKind, Tier
from crawl4ai_mcp.providers.browser import BrowserProvider


def public_policy(address: str = "93.184.216.34") -> UrlPolicy:
    async def resolver(_host: str, _port: int):
        return [ipaddress.ip_address(address)]
    return UrlPolicy(resolver)


def private_policy() -> UrlPolicy:
    async def resolver(_host: str, _port: int):
        return [ipaddress.ip_address("127.0.0.1")]
    return UrlPolicy(resolver)


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


class FakePinnedProxy:
    def __init__(self):
        self.endpoint_calls = []
        self._upstream_ports = {}

    def endpoint(self, upstream=None):
        self.endpoint_calls.append(upstream)
        if upstream is None:
            port = 41000
        else:
            port = self._upstream_ports.setdefault(
                upstream, 41001 + len(self._upstream_ports)
            )
        return ProxyConfig(server=f"http://127.0.0.1:{port}")


class FakeRequestGuard:
    def __init__(self):
        self.contexts = []

    async def install(self, context):
        self.contexts.append(context)


class FakeClock:
    def __init__(self, now: float = 0.0):
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class FakeResult:
    def __init__(self, html: str = "<main>Hello</main>"):
        self.html = html
        self.status_code = 200
        self.response_headers = {"content-type": "text/html"}
        self.redirected_url = None
        self.error_message = None


class FakeContainer:
    def __init__(self, html: str = "<main>Hello</main>"):
        self._results = [FakeResult(html)]


class FakeCrawler:
    def __init__(self, factory, proxy=None):
        self.factory = factory
        self.proxy = proxy
        self.closed = False
        self.configs = []

    async def arun(self, url, config=None):
        self.configs.append(config)
        return FakeContainer()

    async def close(self):
        self.closed = True
        self.factory.closed += 1


class FakeCrawlerFactory:
    def __init__(self):
        self.created = 0
        self.closed = 0
        self.proxies = []
        self.crawlers = []

    def __call__(self, proxy=None):
        self.created += 1
        if proxy is not None:
            self.proxies.append(proxy)
        crawler = FakeCrawler(self, proxy)
        self.crawlers.append(crawler)
        return crawler


@pytest.mark.asyncio
async def test_browser_is_lazy_and_closed_after_idle(fake_clock):
    factory = FakeCrawlerFactory()
    provider = BrowserProvider(
        tier=Tier.STEALTH, factory=factory, idle_seconds=180,
        semaphore=asyncio.Semaphore(2), clock=fake_clock,
        egress_proxy=FakePinnedProxy(), request_guard=FakeRequestGuard(),
    )
    assert factory.created == 0
    await provider.fetch("https://example.com")
    assert factory.created == 1
    fake_clock.advance(181)
    await provider.reap_idle()
    assert factory.closed == 1


@pytest.mark.asyncio
async def test_reuse_crawler_before_idle(fake_clock):
    factory = FakeCrawlerFactory()
    provider = BrowserProvider(
        tier=Tier.STEALTH, factory=factory, idle_seconds=180,
        semaphore=asyncio.Semaphore(2), clock=fake_clock,
        egress_proxy=FakePinnedProxy(), request_guard=FakeRequestGuard(),
    )
    await provider.fetch("https://example.com")
    await provider.fetch("https://example.com/other")
    assert factory.created == 1
    await provider.reap_idle()
    assert factory.closed == 0


@pytest.mark.asyncio
async def test_third_call_waits_for_semaphore(fake_clock):
    factory = FakeCrawlerFactory()
    provider = BrowserProvider(
        tier=Tier.STEALTH, factory=factory, idle_seconds=180,
        semaphore=asyncio.Semaphore(2), clock=fake_clock,
        egress_proxy=FakePinnedProxy(), request_guard=FakeRequestGuard(),
    )
    gate = asyncio.Event()
    original_arun = FakeCrawler.arun
    results = []

    async def blocking_arun(self, url, config=None):
        self.gate_holder = True
        await gate.wait()
        return FakeContainer()

    FakeCrawler.arun = blocking_arun
    try:
        first = asyncio.create_task(provider.fetch("https://example.com/a"))
        second = asyncio.create_task(provider.fetch("https://example.com/b"))
        await asyncio.sleep(0.05)
        third = asyncio.create_task(provider.fetch("https://example.com/c"))
        await asyncio.sleep(0.05)
        assert not third.done()
        gate.set()
        await asyncio.gather(first, second, third)
        assert all(task.done() for task in (first, second, third))
    finally:
        FakeCrawler.arun = original_arun
        await provider.close()


@pytest.mark.asyncio
async def test_browser_guard_allows_public_cross_origin_subresource():
    route = FakeRoute()
    await BrowserRequestGuard(public_policy()).handle(route, FakeRequest("https://cdn.example.net/app.js"))
    assert route.continued and not route.aborted


@pytest.mark.asyncio
async def test_browser_guard_aborts_private_subresource():
    route = FakeRoute()
    await BrowserRequestGuard(public_policy()).handle(route, FakeRequest("http://192.168.1.1/admin"))
    assert route.aborted and not route.continued


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


@pytest.mark.asyncio
async def test_stealth_fetch_uses_direct_pinned_endpoint(fake_clock):
    factory = FakeCrawlerFactory()
    egress = FakePinnedProxy()
    provider = BrowserProvider(
        tier=Tier.STEALTH, factory=factory, idle_seconds=180,
        semaphore=asyncio.Semaphore(2), clock=fake_clock,
        egress_proxy=egress, request_guard=FakeRequestGuard(),
    )
    await provider.fetch("https://example.com")
    config = factory.crawlers[0].configs[0]
    assert config.proxy_config.server == "http://127.0.0.1:41000"
    assert egress.endpoint_calls == [None]
    await provider.close()


@pytest.mark.asyncio
async def test_proxy_provider_unavailable_without_pool(fake_clock):
    provider = BrowserProvider(
        tier=Tier.PROXY, factory=FakeCrawlerFactory(), idle_seconds=180,
        semaphore=asyncio.Semaphore(2), clock=fake_clock, proxy_pool=(),
        egress_proxy=FakePinnedProxy(), request_guard=FakeRequestGuard(),
    )
    assert provider.availability().ready is False
    assert provider.cost_kind == CostKind.PROXY_BANDWIDTH


@pytest.mark.asyncio
async def test_fetch_result_preserves_browser_tier(fake_clock):
    factory = FakeCrawlerFactory()
    provider = BrowserProvider(
        tier=Tier.STEALTH, factory=factory, idle_seconds=180,
        semaphore=asyncio.Semaphore(2), clock=fake_clock,
        egress_proxy=FakePinnedProxy(), request_guard=FakeRequestGuard(),
    )
    result = await provider.fetch("https://example.com")
    assert result.tier == Tier.STEALTH
    assert result.cost_kind == CostKind.FREE
    assert result.status_code == 200
    assert "Hello" in result.html
    await provider.close()
