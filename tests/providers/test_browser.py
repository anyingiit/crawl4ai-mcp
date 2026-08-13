import asyncio
import pytest
from crawl4ai_mcp.models import CostKind, Tier
from crawl4ai_mcp.providers.browser import BrowserProvider


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

    async def arun(self, url, config=None):
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
async def test_proxy_provider_rotates_round_robin(fake_clock):
    factory = FakeCrawlerFactory()
    provider = BrowserProvider(
        tier=Tier.PROXY, factory=factory, idle_seconds=180,
        semaphore=asyncio.Semaphore(2), clock=fake_clock,
        proxy_pool=("proxy-one", "proxy-two"),
    )
    await provider.fetch("https://example.com")
    assert factory.proxies == ["proxy-one"]
    fake_clock.advance(181)
    await provider.reap_idle()
    await provider.fetch("https://example.com")
    assert factory.proxies == ["proxy-one", "proxy-two"]
    await provider.close()


@pytest.mark.asyncio
async def test_proxy_provider_unavailable_without_pool(fake_clock):
    provider = BrowserProvider(
        tier=Tier.PROXY, factory=FakeCrawlerFactory(), idle_seconds=180,
        semaphore=asyncio.Semaphore(2), clock=fake_clock, proxy_pool=(),
    )
    assert provider.availability().ready is False
    assert provider.cost_kind == CostKind.PROXY_BANDWIDTH


@pytest.mark.asyncio
async def test_fetch_result_preserves_browser_tier(fake_clock):
    factory = FakeCrawlerFactory()
    provider = BrowserProvider(
        tier=Tier.STEALTH, factory=factory, idle_seconds=180,
        semaphore=asyncio.Semaphore(2), clock=fake_clock,
    )
    result = await provider.fetch("https://example.com")
    assert result.tier == Tier.STEALTH
    assert result.cost_kind == CostKind.FREE
    assert result.status_code == 200
    assert "Hello" in result.html
    await provider.close()
