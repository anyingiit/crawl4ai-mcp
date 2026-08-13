import asyncio
import ipaddress

import pytest
from crawl4ai.async_configs import ProxyConfig
from playwright._impl._errors import TimeoutError as PlaywrightTimeoutError

from crawl4ai_mcp.egress import BrowserRequestGuard, UpstreamProxy, UrlPolicy
from crawl4ai_mcp.models import CostKind, ProviderErrorKind, Tier
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
        self.fell_back = False
        self.aborted = False

    async def fallback(self):
        self.fell_back = True

    async def abort(self, _reason="blockedbyclient"):
        self.aborted = True


class FakeFrame:
    def __init__(self, page):
        self.page = page


class FakePage:
    def __init__(self, main_frame=None):
        self.main_frame = main_frame


class FakeRequest:
    def __init__(self, url: str, frame=None, navigation: bool = False):
        self.url = url
        self.frame = frame
        self.is_navigation_request = lambda: navigation


class FakeRouteContext:
    def __init__(self):
        self.route_calls = 0
        self.raise_on_route = False

    async def route(self, pattern, handler):
        self.route_calls += 1
        if self.raise_on_route:
            raise RuntimeError("route registration failed")


class GatedRouteContext(FakeRouteContext):
    def __init__(self, gate=None):
        super().__init__()
        self.gate = gate

    async def route(self, pattern, handler):
        self.route_calls += 1
        if self.gate is not None:
            await self.gate.wait()
        if self.raise_on_route:
            raise RuntimeError("route registration failed")


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
        self.recorders = []

    async def install(self, context):
        self.contexts.append(context)

    def begin_fetch(self):
        recorder = _FakeRecorder()
        self.recorders.append(recorder)
        return recorder

    def bind_page(self, page):
        pass


class _FakeRecorder:
    def __init__(self):
        self._events = []

    def blocked(self):
        return list(self._events)

    def close(self):
        pass


class FakeClock:
    def __init__(self, now: float = 0.0):
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class FakeResult:
    def __init__(
        self,
        html: str = "<main>Hello</main>",
        error_message: str | None = None,
        success: bool = True,
    ):
        self.html = html
        self.status_code = 200 if success else None
        self.response_headers = {"content-type": "text/html"}
        self.redirected_url = None
        self.error_message = error_message
        self.success = success


class FakeContainer:
    def __init__(
        self,
        html: str = "<main>Hello</main>",
        error_message: str | None = None,
        success: bool = True,
    ):
        self._results = [FakeResult(html, error_message, success)]


class FakeCrawler:
    def __init__(self, factory, proxy=None):
        self.factory = factory
        self.proxy = proxy
        self.closed = False
        self.configs = []

    async def arun(self, url, config=None):
        self.configs.append(config)
        self.factory.started.set()
        if url.endswith("/slow") and self.factory.gate is not None:
            await self.factory.gate.wait()
        return FakeContainer()

    async def close(self):
        self.factory.close_started.set()
        if self.factory.close_gate is not None:
            await self.factory.close_gate.wait()
        self.closed = True
        self.factory.closed += 1


class FakeCrawlerFactory:
    def __init__(self, gate=None):
        self.created = 0
        self.closed = 0
        self.proxies = []
        self.crawlers = []
        self.started = asyncio.Event()
        self.gate = gate
        self.close_started = asyncio.Event()
        self.close_gate = None

    def __call__(self, proxy=None):
        self.created += 1
        if proxy is not None:
            self.proxies.append(proxy)
        crawler = FakeCrawler(self, proxy)
        self.crawlers.append(crawler)
        return crawler


@pytest.fixture
def gate():
    return asyncio.Event()


@pytest.fixture
def clock():
    return FakeClock()


@pytest.fixture
def provider(gate, clock):
    factory = FakeCrawlerFactory(gate=gate)
    p = BrowserProvider(
        tier=Tier.STEALTH, factory=factory, idle_seconds=180,
        semaphore=asyncio.Semaphore(2), clock=clock,
        egress_proxy=FakePinnedProxy(), request_guard=FakeRequestGuard(),
    )
    p.factory = factory
    return p


@pytest.mark.asyncio
async def test_browser_reaper_does_not_close_active_crawler(provider, gate, clock):
    fetch = asyncio.create_task(provider.fetch("https://example.com/slow"))
    await provider.factory.started.wait()
    clock.advance(181)
    await provider.reap_idle()
    assert provider.factory.closed == 0
    gate.set()
    await fetch


@pytest.mark.asyncio
async def test_browser_close_waits_for_active_fetch_then_closes_once(provider, gate):
    fetch = asyncio.create_task(provider.fetch("https://example.com/slow"))
    await provider.factory.started.wait()
    close = asyncio.create_task(provider.close())
    await asyncio.sleep(0)
    assert not close.done()
    gate.set()
    await fetch
    await close
    assert provider.factory.closed == 1


@pytest.mark.asyncio
async def test_concurrent_first_fetches_create_one_crawler(provider):
    await asyncio.gather(
        provider.fetch("https://example.com/a"), provider.fetch("https://example.com/b")
    )
    assert provider.factory.created == 1


@pytest.mark.asyncio
async def test_browser_fetch_cancel_decrements_active_count(provider, gate):
    fetch = asyncio.create_task(provider.fetch("https://example.com/slow"))
    await provider.factory.started.wait()
    assert provider.active_fetch_count() == 1
    fetch.cancel()
    try:
        await fetch
        raise AssertionError("fetch was not cancelled")
    except asyncio.CancelledError:
        pass
    assert provider.active_fetch_count() == 0
    await provider.close()


@pytest.mark.asyncio
async def test_browser_repeated_close_is_idempotent(provider, gate):
    fetch = asyncio.create_task(provider.fetch("https://example.com/slow"))
    await provider.factory.started.wait()
    gate.set()
    await fetch
    await provider.close()
    await provider.close()
    assert provider.factory.closed == 1


@pytest.mark.asyncio
async def test_browser_concurrent_closes_do_not_deadlock(provider, gate):
    fetch = asyncio.create_task(provider.fetch("https://example.com/slow"))
    await provider.factory.started.wait()
    first = asyncio.create_task(provider.close())
    second = asyncio.create_task(provider.close())
    await asyncio.sleep(0)
    assert not first.done()
    assert not second.done()
    gate.set()
    await fetch
    await asyncio.wait_for(asyncio.gather(first, second), timeout=5)
    assert provider.factory.closed == 1


@pytest.mark.asyncio
async def test_browser_fetch_during_shutdown_gets_provider_closing(provider):
    await provider.fetch("https://example.com/a")
    provider.factory.close_gate = asyncio.Event()
    close = asyncio.create_task(provider.close())
    await provider.factory.close_started.wait()
    result = await provider.fetch("https://example.com/b")
    assert result.error is not None and "closing" in (result.error or "")
    assert provider.factory.created == 1
    provider.factory.close_gate.set()
    await close
    assert provider.factory.closed == 1


@pytest.mark.asyncio
async def test_browser_concurrent_closes_wait_for_resource_close(provider, gate):
    fetch = asyncio.create_task(provider.fetch("https://example.com/slow"))
    await provider.factory.started.wait()
    gate.set()
    await fetch
    provider.factory.close_gate = asyncio.Event()
    first = asyncio.create_task(provider.close())
    second = asyncio.create_task(provider.close())
    await provider.factory.close_started.wait()
    await asyncio.sleep(0)
    assert not first.done()
    assert not second.done()
    provider.factory.close_gate.set()
    await asyncio.wait_for(asyncio.gather(first, second), timeout=5)
    assert provider.factory.closed == 1


@pytest.mark.asyncio
async def test_browser_close_caller_cancel_keeps_cleanup_owned(provider, gate):
    fetch = asyncio.create_task(provider.fetch("https://example.com/slow"))
    await provider.factory.started.wait()
    gate.set()
    await fetch
    provider.factory.close_gate = asyncio.Event()
    first = asyncio.create_task(provider.close())
    await provider.factory.close_started.wait()
    first.cancel()
    try:
        await first
        raise AssertionError("first close was not cancelled")
    except asyncio.CancelledError:
        pass
    assert provider.factory.closed == 0
    second = asyncio.create_task(provider.close())
    provider.factory.close_gate.set()
    await asyncio.wait_for(second, timeout=5)
    assert provider.factory.closed == 1


@pytest.mark.asyncio
async def test_browser_lifecycle_methods_report_activity(provider, gate, clock):
    fetch = asyncio.create_task(provider.fetch("https://example.com/slow"))
    await provider.factory.started.wait()
    assert provider.is_active() is True
    assert provider.active_fetch_count() == 1
    clock.advance(181)
    await provider.reap_idle()
    assert provider.factory.closed == 0
    gate.set()
    await fetch
    assert provider.active_fetch_count() == 0
    assert provider.last_used() == clock.now
    await provider.close()
    assert provider.is_active() is False


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
    assert route.fell_back and not route.aborted


@pytest.mark.asyncio
async def test_browser_guard_aborts_private_subresource():
    route = FakeRoute()
    await BrowserRequestGuard(public_policy()).handle(route, FakeRequest("http://192.168.1.1/admin"))
    assert route.aborted and not route.fell_back


@pytest.mark.asyncio
async def test_browser_guard_install_is_idempotent_per_context():
    guard = BrowserRequestGuard(public_policy())
    context = FakeRouteContext()
    await guard.install(context)
    await guard.install(context)
    assert context.route_calls == 1


@pytest.mark.asyncio
async def test_browser_guard_install_rolls_back_on_route_registration_failure():
    guard = BrowserRequestGuard(public_policy())
    context = FakeRouteContext()
    context.raise_on_route = True
    with pytest.raises(RuntimeError):
        await guard.install(context)
    context.raise_on_route = False
    await guard.install(context)
    assert context.route_calls == 2


@pytest.mark.asyncio
async def test_browser_guard_install_waits_for_inflight_registration():
    guard = BrowserRequestGuard(public_policy())
    gate = asyncio.Event()
    context = GatedRouteContext(gate=gate)
    first = asyncio.create_task(guard.install(context))
    await asyncio.sleep(0.02)
    second = asyncio.create_task(guard.install(context))
    await asyncio.sleep(0.02)
    assert context.route_calls == 1
    assert not second.done()
    gate.set()
    await asyncio.gather(first, second)
    assert context.route_calls == 1
    await guard.install(context)
    assert context.route_calls == 1


@pytest.mark.asyncio
async def test_browser_guard_install_failure_shared_and_retryable():
    guard = BrowserRequestGuard(public_policy())
    context = FakeRouteContext()
    context.raise_on_route = True
    results = await asyncio.gather(
        guard.install(context), guard.install(context), return_exceptions=True
    )
    assert all(isinstance(result, RuntimeError) for result in results)
    assert context.route_calls == 1
    context.raise_on_route = False
    await guard.install(context)
    assert context.route_calls == 2


@pytest.mark.asyncio
async def test_browser_guard_install_cancel_does_not_cancel_shared_registration():
    guard = BrowserRequestGuard(public_policy())
    gate = asyncio.Event()
    context = GatedRouteContext(gate=gate)
    first = asyncio.create_task(guard.install(context))
    await asyncio.sleep(0.02)
    second = asyncio.create_task(guard.install(context))
    await asyncio.sleep(0.02)
    assert not second.done()
    first.cancel()
    try:
        await first
        raise AssertionError("first waiter was not cancelled")
    except asyncio.CancelledError:
        pass
    await asyncio.sleep(0.02)
    assert context.route_calls == 1
    gate.set()
    await second
    assert context.route_calls == 1
    await guard.install(context)
    assert context.route_calls == 1


async def _run_blocked_fetch(guard, page, url):
    recorder = guard.begin_fetch()
    frame = FakeFrame(page)
    page.main_frame = frame
    guard.bind_page(page)
    route = FakeRoute()
    request = FakeRequest(url, frame=frame, navigation=True)
    await guard.handle(route, request)
    assert route.aborted and not route.fell_back
    blocked = recorder.blocked()
    recorder.close()
    return blocked


@pytest.mark.asyncio
async def test_guard_recorders_are_request_scoped():
    guard = BrowserRequestGuard(public_policy())
    page_a = FakePage()
    page_b = FakePage()
    results = await asyncio.gather(
        _run_blocked_fetch(guard, page_a, "https://10.0.0.5/secret-a"),
        _run_blocked_fetch(guard, page_b, "https://10.0.0.5/secret-b"),
    )
    assert results[0] == [("https://10.0.0.5/secret-a", "non_global_address")]
    assert results[1] == [("https://10.0.0.5/secret-b", "non_global_address")]


@pytest.mark.asyncio
async def test_guard_recorders_are_removed_after_close():
    guard = BrowserRequestGuard(public_policy())
    recorder = guard.begin_fetch()
    page = FakePage()
    guard.bind_page(page)
    assert guard._pages
    assert guard._by_task
    recorder.close()
    assert guard._pages == {}
    assert guard._by_task == {}


@pytest.mark.asyncio
async def test_guard_does_not_record_blocked_subresource_on_bound_page():
    guard = BrowserRequestGuard(public_policy())
    recorder = guard.begin_fetch()
    page = FakePage()
    frame = FakeFrame(page)
    page.main_frame = frame
    guard.bind_page(page)
    route = FakeRoute()
    request = FakeRequest("https://10.0.0.5/tracker.js", frame=frame, navigation=False)
    await guard.handle(route, request)
    assert route.aborted and not route.fell_back
    assert recorder.blocked() == []
    recorder.close()


@pytest.mark.asyncio
async def test_guard_does_not_record_blocked_subframe_navigation():
    guard = BrowserRequestGuard(public_policy())
    recorder = guard.begin_fetch()
    page = FakePage()
    subframe = FakeFrame(page)
    page.main_frame = FakeFrame(page)
    guard.bind_page(page)
    route = FakeRoute()
    request = FakeRequest("https://10.0.0.5/frame", frame=subframe, navigation=True)
    await guard.handle(route, request)
    assert route.aborted and not route.fell_back
    assert recorder.blocked() == []
    recorder.close()


@pytest.mark.asyncio
async def test_concurrent_browser_fetches_never_cross_attribute_blocked_navigation(fake_clock):
    guard = BrowserRequestGuard(public_policy())
    factory = FakeCrawlerFactory()
    provider = BrowserProvider(
        tier=Tier.STEALTH, factory=factory, idle_seconds=180,
        semaphore=asyncio.Semaphore(2), clock=fake_clock,
        egress_proxy=FakePinnedProxy(), request_guard=guard,
    )
    blocked_seen = asyncio.Event()
    page_a = FakePage()
    page_b = FakePage()
    frame_a = FakeFrame(page_a)
    frame_b = FakeFrame(page_b)
    page_a.main_frame = frame_a
    page_b.main_frame = frame_b
    original_arun = FakeCrawler.arun

    async def blocked_arun_a(self, url, config=None):
        self.configs.append(config)
        guard.bind_page(page_a)
        route = FakeRoute()
        request = FakeRequest("https://10.0.0.5/secret", frame=frame_a, navigation=True)
        await guard.handle(route, request)
        blocked_seen.set()
        return FakeContainer(
            error_message="navigation to blocked address aborted", success=False
        )

    async def generic_failure_arun_b(self, url, config=None):
        self.configs.append(config)
        guard.bind_page(page_b)
        await blocked_seen.wait()
        return FakeContainer(error_message="scrape pipeline crashed", success=False)

    async def dispatch_arun(self, url, config=None):
        if url == "https://example.com/a":
            return await blocked_arun_a(self, url, config)
        return await generic_failure_arun_b(self, url, config)

    FakeCrawler.arun = dispatch_arun
    try:
        task_a = asyncio.create_task(provider.fetch("https://example.com/a"))
        task_b = asyncio.create_task(provider.fetch("https://example.com/b"))
        result_a, result_b = await asyncio.gather(task_a, task_b)
        assert result_a.policy_error == "non_global_address"
        assert result_a.network_error is None
        assert result_b.policy_error is None
    finally:
        FakeCrawler.arun = original_arun
        await provider.close()


@pytest.mark.asyncio
async def test_concurrent_same_url_fetches_never_cross_attribute_blocked_navigation(fake_clock):
    guard = BrowserRequestGuard(public_policy())
    factory = FakeCrawlerFactory()
    provider = BrowserProvider(
        tier=Tier.STEALTH, factory=factory, idle_seconds=180,
        semaphore=asyncio.Semaphore(2), clock=fake_clock,
        egress_proxy=FakePinnedProxy(), request_guard=guard,
    )
    blocked_seen = asyncio.Event()
    page_a = FakePage()
    page_b = FakePage()
    frame_a = FakeFrame(page_a)
    frame_b = FakeFrame(page_b)
    page_a.main_frame = frame_a
    page_b.main_frame = frame_b
    original_arun = FakeCrawler.arun

    async def blocked_arun_a(self, url, config=None):
        self.configs.append(config)
        guard.bind_page(page_a)
        route = FakeRoute()
        request = FakeRequest("https://10.0.0.5/secret", frame=frame_a, navigation=True)
        await guard.handle(route, request)
        blocked_seen.set()
        return FakeContainer(
            error_message="navigation to blocked address aborted", success=False
        )

    async def generic_failure_arun_b(self, url, config=None):
        self.configs.append(config)
        guard.bind_page(page_b)
        await blocked_seen.wait()
        return FakeContainer(error_message="scrape pipeline crashed", success=False)

    async def dispatch_arun(self, url, config=None):
        if asyncio.current_task() is task_a:
            guard.bind_page(page_a)
            route = FakeRoute()
            request = FakeRequest("https://10.0.0.5/secret", frame=frame_a, navigation=True)
            await guard.handle(route, request)
            blocked_seen.set()
            return FakeContainer(
                error_message="navigation to blocked address aborted", success=False
            )
        guard.bind_page(page_b)
        await blocked_seen.wait()
        return FakeContainer(error_message="scrape pipeline crashed", success=False)

    FakeCrawler.arun = dispatch_arun
    try:
        url = "https://example.com/same"
        task_a = asyncio.create_task(provider.fetch(url))
        task_b = asyncio.create_task(provider.fetch(url))
        result_a, result_b = await asyncio.gather(task_a, task_b)
        assert result_a.policy_error == "non_global_address"
        assert result_a.network_error is None
        assert result_b.policy_error is None
    finally:
        FakeCrawler.arun = original_arun
        await provider.close()


@pytest.mark.asyncio
async def test_fetch_recorders_cleaned_up_on_success(fake_clock):
    guard = BrowserRequestGuard(public_policy())
    factory = FakeCrawlerFactory()
    provider = BrowserProvider(
        tier=Tier.STEALTH, factory=factory, idle_seconds=180,
        semaphore=asyncio.Semaphore(2), clock=fake_clock,
        egress_proxy=FakePinnedProxy(), request_guard=guard,
    )
    result = await provider.fetch("https://example.com/")
    assert result.target_status_code == 200
    assert guard._pages == {}
    assert guard._by_task == {}
    await provider.close()
    assert guard._pages == {}
    assert guard._by_task == {}


@pytest.mark.asyncio
async def test_fetch_recorders_cleaned_up_on_arun_error(fake_clock):
    guard = BrowserRequestGuard(public_policy())
    factory = FakeCrawlerFactory()
    provider = BrowserProvider(
        tier=Tier.STEALTH, factory=factory, idle_seconds=180,
        semaphore=asyncio.Semaphore(2), clock=fake_clock,
        egress_proxy=FakePinnedProxy(), request_guard=guard,
    )
    original_arun = FakeCrawler.arun

    async def exploding_arun(self, url, config=None):
        raise RuntimeError("Executable doesn't exist at /usr/lib/chromium/chrome")

    FakeCrawler.arun = exploding_arun
    try:
        result = await provider.fetch("https://example.com/")
        assert result.error is not None
        assert guard._pages == {}
        assert guard._by_task == {}
    finally:
        FakeCrawler.arun = original_arun
        await provider.close()


@pytest.mark.asyncio
async def test_fetch_recorders_cleaned_up_on_cancellation(fake_clock, gate):
    guard = BrowserRequestGuard(public_policy())
    factory = FakeCrawlerFactory(gate=gate)
    provider = BrowserProvider(
        tier=Tier.STEALTH, factory=factory, idle_seconds=180,
        semaphore=asyncio.Semaphore(2), clock=fake_clock,
        egress_proxy=FakePinnedProxy(), request_guard=guard,
    )
    fetch = asyncio.create_task(provider.fetch("https://example.com/slow"))
    await factory.started.wait()
    fetch.cancel()
    try:
        await fetch
        raise AssertionError("fetch was not cancelled")
    except asyncio.CancelledError:
        pass
    assert guard._pages == {}
    assert guard._by_task == {}
    await provider.close()


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
    assert result.target_status_code == 200
    assert "Hello" in result.html
    await provider.close()


@pytest.mark.asyncio
async def test_navigation_network_error_is_classified(fake_clock):
    factory = FakeCrawlerFactory()
    provider = BrowserProvider(
        tier=Tier.STEALTH, factory=factory, idle_seconds=180,
        semaphore=asyncio.Semaphore(2), clock=fake_clock,
        egress_proxy=FakePinnedProxy(), request_guard=FakeRequestGuard(),
    )
    original_arun = FakeCrawler.arun

    async def failing_arun(self, url, config=None):
        raise RuntimeError(
            "Failed on navigating ACS-GOTO:\n"
            "net::ERR_NAME_NOT_RESOLVED at https://nonexistent.example/"
        )

    FakeCrawler.arun = failing_arun
    try:
        result = await provider.fetch("https://nonexistent.example/")
        assert result.network_error is not None
        assert result.target_status_code is None
    finally:
        FakeCrawler.arun = original_arun
        await provider.close()


@pytest.mark.asyncio
async def test_navigation_timeout_is_classified(fake_clock):
    factory = FakeCrawlerFactory()
    provider = BrowserProvider(
        tier=Tier.STEALTH, factory=factory, idle_seconds=180,
        semaphore=asyncio.Semaphore(2), clock=fake_clock,
        egress_proxy=FakePinnedProxy(), request_guard=FakeRequestGuard(),
    )
    original_arun = FakeCrawler.arun

    async def timeout_arun(self, url, config=None):
        raise RuntimeError(
            "Failed on navigating ACS-GOTO:\nTimeout 60000ms exceeded."
        )

    FakeCrawler.arun = timeout_arun
    try:
        result = await provider.fetch("https://slow.example/")
        assert result.network_error is not None
    finally:
        FakeCrawler.arun = original_arun
        await provider.close()


@pytest.mark.asyncio
async def test_launch_failure_is_not_network_error(fake_clock):
    factory = FakeCrawlerFactory()
    provider = BrowserProvider(
        tier=Tier.STEALTH, factory=factory, idle_seconds=180,
        semaphore=asyncio.Semaphore(2), clock=fake_clock,
        egress_proxy=FakePinnedProxy(), request_guard=FakeRequestGuard(),
    )
    original_arun = FakeCrawler.arun

    async def launch_arun(self, url, config=None):
        raise RuntimeError("Executable doesn't exist at /usr/lib/chromium/chrome")

    FakeCrawler.arun = launch_arun
    try:
        result = await provider.fetch("https://example.com/")
        assert result.network_error is None
        assert result.error is not None
    finally:
        FakeCrawler.arun = original_arun
        await provider.close()


@pytest.mark.asyncio
async def test_bare_timeout_from_arun_is_not_network_error(fake_clock):
    factory = FakeCrawlerFactory()
    provider = BrowserProvider(
        tier=Tier.STEALTH, factory=factory, idle_seconds=180,
        semaphore=asyncio.Semaphore(2), clock=fake_clock,
        egress_proxy=FakePinnedProxy(), request_guard=FakeRequestGuard(),
    )
    original_arun = FakeCrawler.arun

    async def timeout_arun(self, url, config=None):
        raise RuntimeError("Timeout 60000ms exceeded.")

    FakeCrawler.arun = timeout_arun
    try:
        result = await provider.fetch("https://slow.example/")
        assert result.network_error is None
        assert result.error is not None
    finally:
        FakeCrawler.arun = original_arun
        await provider.close()


@pytest.mark.asyncio
async def test_bare_playwright_timeout_from_arun_is_not_network_error(fake_clock):
    factory = FakeCrawlerFactory()
    provider = BrowserProvider(
        tier=Tier.STEALTH, factory=factory, idle_seconds=180,
        semaphore=asyncio.Semaphore(2), clock=fake_clock,
        egress_proxy=FakePinnedProxy(), request_guard=FakeRequestGuard(),
    )
    original_arun = FakeCrawler.arun

    async def timeout_arun(self, url, config=None):
        raise PlaywrightTimeoutError("Timeout 60000ms exceeded.")

    FakeCrawler.arun = timeout_arun
    try:
        result = await provider.fetch("https://slow.example/")
        assert result.network_error is None
        assert result.error is not None
    finally:
        FakeCrawler.arun = original_arun
        await provider.close()


@pytest.mark.asyncio
async def test_returned_result_network_failure_is_classified(fake_clock):
    factory = FakeCrawlerFactory()
    provider = BrowserProvider(
        tier=Tier.STEALTH, factory=factory, idle_seconds=180,
        semaphore=asyncio.Semaphore(2), clock=fake_clock,
        egress_proxy=FakePinnedProxy(), request_guard=FakeRequestGuard(),
    )
    original_arun = FakeCrawler.arun

    async def failing_arun(self, url, config=None):
        self.configs.append(config)
        return FakeContainer(
            error_message=(
                "Failed on navigating ACS-GOTO:\n"
                "net::ERR_NAME_NOT_RESOLVED at https://nonexistent.example/"
            ),
            success=False,
        )

    FakeCrawler.arun = failing_arun
    try:
        result = await provider.fetch("https://nonexistent.example/")
        assert result.network_error is not None
        assert result.target_status_code is None
    finally:
        FakeCrawler.arun = original_arun
        await provider.close()


@pytest.mark.asyncio
async def test_returned_result_network_timeout_is_classified(fake_clock):
    factory = FakeCrawlerFactory()
    provider = BrowserProvider(
        tier=Tier.STEALTH, factory=factory, idle_seconds=180,
        semaphore=asyncio.Semaphore(2), clock=fake_clock,
        egress_proxy=FakePinnedProxy(), request_guard=FakeRequestGuard(),
    )
    original_arun = FakeCrawler.arun

    async def timeout_arun(self, url, config=None):
        self.configs.append(config)
        return FakeContainer(
            error_message="Failed on navigating ACS-GOTO:\nTimeout 60000ms exceeded.",
            success=False,
        )

    FakeCrawler.arun = timeout_arun
    try:
        result = await provider.fetch("https://slow.example/")
        assert result.network_error is not None
    finally:
        FakeCrawler.arun = original_arun
        await provider.close()


@pytest.mark.asyncio
async def test_returned_result_launch_error_is_not_network_or_policy(fake_clock):
    factory = FakeCrawlerFactory()
    provider = BrowserProvider(
        tier=Tier.STEALTH, factory=factory, idle_seconds=180,
        semaphore=asyncio.Semaphore(2), clock=fake_clock,
        egress_proxy=FakePinnedProxy(), request_guard=FakeRequestGuard(),
    )
    original_arun = FakeCrawler.arun

    async def failed_arun(self, url, config=None):
        self.configs.append(config)
        return FakeContainer(
            error_message="Executable doesn't exist at /usr/lib/chromium/chrome",
            success=False,
        )

    FakeCrawler.arun = failed_arun
    try:
        result = await provider.fetch("https://example.com/")
        assert result.network_error is None
        assert result.policy_error is None
        assert result.error is not None
    finally:
        FakeCrawler.arun = original_arun
        await provider.close()


@pytest.mark.asyncio
async def test_returned_result_with_blocked_main_frame_redirect_is_policy_error(fake_clock):
    guard = BrowserRequestGuard(public_policy())
    factory = FakeCrawlerFactory()
    provider = BrowserProvider(
        tier=Tier.STEALTH, factory=factory, idle_seconds=180,
        semaphore=asyncio.Semaphore(2), clock=fake_clock,
        egress_proxy=FakePinnedProxy(), request_guard=guard,
    )
    original_arun = FakeCrawler.arun
    page = FakePage()
    frame = FakeFrame(page)
    page.main_frame = frame

    async def aborted_arun(self, url, config=None):
        self.configs.append(config)
        guard.bind_page(page)
        route = FakeRoute()
        request = FakeRequest("https://10.0.0.5/secret", frame=frame, navigation=True)
        await guard.handle(route, request)
        assert route.aborted and not route.fell_back
        return FakeContainer(
            error_message="Navigation to https://10.0.0.5/secret was aborted",
            success=False,
        )

    FakeCrawler.arun = aborted_arun
    try:
        result = await provider.fetch("https://example.com/start")
        assert result.policy_error == "non_global_address"
        assert result.network_error is None
    finally:
        FakeCrawler.arun = original_arun
        await provider.close()


@pytest.mark.asyncio
async def test_aborted_subresource_alone_is_not_main_frame_policy_error(fake_clock):
    guard = BrowserRequestGuard(public_policy())
    factory = FakeCrawlerFactory()
    provider = BrowserProvider(
        tier=Tier.STEALTH, factory=factory, idle_seconds=180,
        semaphore=asyncio.Semaphore(2), clock=fake_clock,
        egress_proxy=FakePinnedProxy(), request_guard=guard,
    )
    original_arun = FakeCrawler.arun
    page = FakePage()
    frame = FakeFrame(page)
    page.main_frame = frame

    async def subresource_arun(self, url, config=None):
        self.configs.append(config)
        guard.bind_page(page)
        route = FakeRoute()
        request = FakeRequest("https://10.0.0.5/tracker.js", frame=frame, navigation=False)
        await guard.handle(route, request)
        assert route.aborted
        return FakeContainer()

    FakeCrawler.arun = subresource_arun
    try:
        result = await provider.fetch("https://example.com/")
        assert result.policy_error is None
        assert result.target_status_code == 200
    finally:
        FakeCrawler.arun = original_arun
        await provider.close()


@pytest.mark.asyncio
async def test_browser_reap_cancellation_does_not_leak_crawler(provider, clock):
    await provider.fetch("https://example.com/a")
    clock.advance(181)
    provider.factory.close_gate = asyncio.Event()
    reap = asyncio.create_task(provider.reap_idle())
    await provider.factory.close_started.wait()
    reap.cancel()
    await reap
    assert provider.factory.closed == 0
    provider.factory.close_gate.set()
    await asyncio.wait_for(provider.close(), timeout=5)
    assert provider.factory.closed == 1


@pytest.mark.asyncio
async def test_browser_close_after_reap_waits_and_stays_closed(provider, clock):
    await provider.fetch("https://example.com/a")
    clock.advance(181)
    provider.factory.close_gate = asyncio.Event()
    reap = asyncio.create_task(provider.reap_idle())
    await provider.factory.close_started.wait()
    close = asyncio.create_task(provider.close())
    await asyncio.sleep(0)
    assert not close.done()
    provider.factory.close_gate.set()
    await asyncio.wait_for(asyncio.gather(reap, close), timeout=5)
    assert provider.factory.closed == 1
    result = await provider.fetch("https://example.com/b")
    assert result.error is not None and "closed" in (result.error or "")
    assert provider.factory.created == 1


@pytest.mark.asyncio
async def test_browser_fetch_after_terminal_close_rejects_without_creating(provider):
    await provider.fetch("https://example.com/a")
    await provider.close()
    result = await provider.fetch("https://example.com/b")
    assert result.error is not None and "closed" in (result.error or "")
    assert provider.factory.created == 1
    await provider.close()
    assert provider.factory.closed == 1


@pytest.mark.asyncio
async def test_browser_reap_after_terminal_close_is_noop(provider, clock):
    await provider.fetch("https://example.com/a")
    await provider.close()
    clock.advance(181)
    await provider.reap_idle()
    assert provider.factory.closed == 1


@pytest.mark.asyncio
async def test_browser_factory_exception_is_service_failure(fake_clock):
    def failing_factory():
        raise RuntimeError("chromium launch exploded")

    provider = BrowserProvider(
        tier=Tier.STEALTH, factory=failing_factory, idle_seconds=180,
        semaphore=asyncio.Semaphore(2), clock=fake_clock,
        egress_proxy=FakePinnedProxy(), request_guard=FakeRequestGuard(),
    )
    try:
        result = await provider.fetch("https://example.com/")
        assert result.target_status_code is None
        assert result.network_error is None
        assert result.policy_error is None
        assert result.provider_error_kind == ProviderErrorKind.SERVICE
        assert provider.is_active() is False
    finally:
        await provider.close()


@pytest.mark.asyncio
async def test_browser_run_config_exception_is_service_failure(fake_clock):
    class ExplodingEgress:
        def endpoint(self, upstream=None):
            raise RuntimeError("proxy endpoint exploded")

    factory = FakeCrawlerFactory()
    provider = BrowserProvider(
        tier=Tier.STEALTH, factory=factory, idle_seconds=180,
        semaphore=asyncio.Semaphore(2), clock=fake_clock,
        egress_proxy=ExplodingEgress(), request_guard=FakeRequestGuard(),
    )
    try:
        result = await provider.fetch("https://example.com/")
        assert result.provider_error_kind == ProviderErrorKind.SERVICE
        assert result.target_status_code is None
    finally:
        await provider.close()
