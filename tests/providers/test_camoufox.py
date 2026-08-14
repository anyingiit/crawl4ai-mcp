import asyncio
import ipaddress

import pytest
from crawl4ai.async_configs import ProxyConfig
from playwright._impl._errors import TimeoutError as PlaywrightTimeoutError

from crawl4ai_mcp.egress import BrowserRequestGuard, UrlPolicy
from crawl4ai_mcp.models import CostKind, Tier
from crawl4ai_mcp.providers.camoufox import CamoufoxProvider


def public_policy(address: str = "93.184.216.34") -> UrlPolicy:
    async def resolver(_host: str, _port: int):
        return [ipaddress.ip_address(address)]

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


class FakeNavigationRequest:
    def __init__(self, url, frame):
        self.url = url
        self.frame = frame

    def is_navigation_request(self):
        return True


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


class FakeResponse:
    def __init__(self, status=200, url="https://example.com/"):
        self.status = status
        self.url = url


class FakePage:
    def __init__(self, gate=None, final_url=None):
        self.gate = gate
        self.final_url = final_url
        self.url = "about:blank"
        self.closed = False

    async def goto(self, url, **kwargs):
        if self.gate is not None:
            await self.gate.wait()
        self.url = self.final_url or url
        return FakeResponse()

    async def wait_for_load_state(self, *args, **kwargs):
        pass

    async def content(self):
        return "<main>Camoufox content</main>"

    async def close(self):
        self.closed = True


class FakeContext:
    def __init__(self, gate=None, final_url=None):
        self.gate = gate
        self.final_url = final_url
        self.closed = False
        self.route_calls = 0

    async def new_page(self):
        return FakePage(self.gate, self.final_url)

    async def route(self, pattern, handler):
        self.route_calls += 1

    async def close(self):
        self.closed = True


class FakeBrowser:
    def __init__(self, gate=None, final_url=None):
        self.gate = gate
        self.final_url = final_url
        self.closed = False
        self.contexts = []

    async def new_context(self, **kwargs):
        context = FakeContext(self.gate, self.final_url)
        context.kwargs = kwargs
        self.contexts.append(context)
        return context

    async def close(self):
        self.closed = True


class FakeSession:
    def __init__(self, browser, launcher):
        self.browser = browser
        self.launcher = launcher
        self.closed = False

    async def new_context(self, **kwargs):
        return await self.browser.new_context(**kwargs)

    async def close(self):
        self.launcher.close_started.set()
        if self.launcher.close_gate is not None:
            await self.launcher.close_gate.wait()
        self.closed = True
        await self.browser.close()


class FakeLauncher:
    def __init__(self, error=None, gate=None, close_gate=None, final_url=None):
        self.calls = 0
        self.error = error
        self.gate = gate
        self.close_gate = close_gate
        self.final_url = final_url
        self.sessions = []
        self.started = asyncio.Event()
        self.close_started = asyncio.Event()

    async def __call__(self):
        self.calls += 1
        if self.error is not None:
            raise self.error
        session = FakeSession(FakeBrowser(self.gate, self.final_url), self)
        self.sessions.append(session)
        self.started.set()
        return session


class FakeClock:
    def __init__(self, now: float = 0.0):
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def make_provider(enabled=True, launcher=None, clock=None, semaphore=None):
    return CamoufoxProvider(
        enabled=enabled,
        idle_seconds=120,
        semaphore=semaphore or asyncio.Semaphore(2),
        launcher=launcher or FakeLauncher(),
        clock=clock or FakeClock(),
        egress_proxy=FakePinnedProxy(),
        request_guard=FakeRequestGuard(),
    )


@pytest.fixture
def gate():
    return asyncio.Event()


@pytest.fixture
def clock():
    return FakeClock()


@pytest.fixture
def provider(gate, clock):
    launcher = FakeLauncher(gate=gate)
    p = CamoufoxProvider(
        enabled=True,
        idle_seconds=120,
        semaphore=asyncio.Semaphore(2),
        launcher=launcher,
        clock=clock,
        egress_proxy=FakePinnedProxy(),
        request_guard=FakeRequestGuard(),
    )
    p.launcher = launcher
    return p


@pytest.mark.asyncio
async def test_camoufox_reaper_does_not_close_active_session(provider, gate, clock):
    fetch = asyncio.create_task(provider.fetch("https://example.com/slow"))
    await provider.launcher.started.wait()
    clock.advance(121)
    await provider.reap_idle()
    assert provider.launcher.sessions[0].closed is False
    gate.set()
    await fetch


@pytest.mark.asyncio
async def test_camoufox_close_waits_for_active_fetch(provider, gate):
    fetch = asyncio.create_task(provider.fetch("https://example.com/slow"))
    await provider.launcher.started.wait()
    close = asyncio.create_task(provider.close())
    await asyncio.sleep(0)
    assert not close.done()
    gate.set()
    await fetch
    await close
    assert provider.launcher.sessions[0].closed is True


@pytest.mark.asyncio
async def test_concurrent_camoufox_first_fetches_launch_once():
    launcher = FakeLauncher()
    provider = make_provider(launcher=launcher)
    await asyncio.gather(
        provider.fetch("https://example.com/a"), provider.fetch("https://example.com/b")
    )
    assert launcher.calls == 1
    await provider.close()


@pytest.mark.asyncio
async def test_camoufox_fetch_cancel_decrements_active_count(provider, gate):
    fetch = asyncio.create_task(provider.fetch("https://example.com/slow"))
    await provider.launcher.started.wait()
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
async def test_camoufox_repeated_close_is_idempotent(provider, gate):
    fetch = asyncio.create_task(provider.fetch("https://example.com/slow"))
    await provider.launcher.started.wait()
    gate.set()
    await fetch
    await provider.close()
    await provider.close()
    assert provider.launcher.sessions[0].closed is True


@pytest.mark.asyncio
async def test_camoufox_concurrent_closes_do_not_deadlock(provider, gate):
    fetch = asyncio.create_task(provider.fetch("https://example.com/slow"))
    await provider.launcher.started.wait()
    first = asyncio.create_task(provider.close())
    second = asyncio.create_task(provider.close())
    await asyncio.sleep(0)
    assert not first.done()
    assert not second.done()
    gate.set()
    await fetch
    await asyncio.wait_for(asyncio.gather(first, second), timeout=5)
    assert provider.launcher.sessions[0].closed is True


@pytest.mark.asyncio
async def test_camoufox_fetch_during_shutdown_gets_provider_closing():
    launcher = FakeLauncher()
    provider = make_provider(launcher=launcher)
    await provider.fetch("https://example.com/a")
    launcher.close_gate = asyncio.Event()
    close = asyncio.create_task(provider.close())
    await launcher.close_started.wait()
    result = await provider.fetch("https://example.com/b")
    assert result.error is not None and "closing" in (result.error or "")
    assert launcher.calls == 1
    launcher.close_gate.set()
    await close
    assert launcher.sessions[0].closed is True


@pytest.mark.asyncio
async def test_camoufox_concurrent_closes_wait_for_session_close(provider, gate):
    fetch = asyncio.create_task(provider.fetch("https://example.com/slow"))
    await provider.launcher.started.wait()
    gate.set()
    await fetch
    provider.launcher.close_gate = asyncio.Event()
    first = asyncio.create_task(provider.close())
    second = asyncio.create_task(provider.close())
    await provider.launcher.close_started.wait()
    await asyncio.sleep(0)
    assert not first.done()
    assert not second.done()
    provider.launcher.close_gate.set()
    await asyncio.wait_for(asyncio.gather(first, second), timeout=5)
    assert provider.launcher.sessions[0].closed is True


@pytest.mark.asyncio
async def test_camoufox_close_caller_cancel_keeps_cleanup_owned(provider, gate):
    fetch = asyncio.create_task(provider.fetch("https://example.com/slow"))
    await provider.launcher.started.wait()
    gate.set()
    await fetch
    provider.launcher.close_gate = asyncio.Event()
    first = asyncio.create_task(provider.close())
    await provider.launcher.close_started.wait()
    first.cancel()
    try:
        await first
        raise AssertionError("first close was not cancelled")
    except asyncio.CancelledError:
        pass
    assert provider.launcher.sessions[0].closed is False
    second = asyncio.create_task(provider.close())
    provider.launcher.close_gate.set()
    await asyncio.wait_for(second, timeout=5)
    assert provider.launcher.sessions[0].closed is True


@pytest.mark.asyncio
async def test_camoufox_lifecycle_methods_report_activity(provider, gate, clock):
    fetch = asyncio.create_task(provider.fetch("https://example.com/slow"))
    await provider.launcher.started.wait()
    assert provider.is_active() is True
    assert provider.active_fetch_count() == 1
    clock.advance(121)
    await provider.reap_idle()
    assert provider.launcher.sessions[0].closed is False
    gate.set()
    await fetch
    assert provider.active_fetch_count() == 0
    assert provider.last_used() == clock.now
    await provider.close()
    assert provider.is_active() is False


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


@pytest.mark.asyncio
async def test_camoufox_is_lazy_and_reaped_after_idle():
    clock = FakeClock()
    launcher = FakeLauncher()
    provider = make_provider(launcher=launcher, clock=clock)
    assert launcher.calls == 0
    await provider.fetch("https://example.com")
    assert launcher.calls == 1
    clock.advance(121)
    await provider.reap_idle()
    assert launcher.sessions[0].closed is True
    await provider.close()


@pytest.mark.asyncio
async def test_camoufox_uses_shared_semaphore():
    gate = asyncio.Event()
    launcher = FakeLauncher(gate=gate)
    provider = make_provider(launcher=launcher, semaphore=asyncio.Semaphore(1))
    first = asyncio.create_task(provider.fetch("https://example.com/a"))
    await asyncio.sleep(0.05)
    second = asyncio.create_task(provider.fetch("https://example.com/b"))
    await asyncio.sleep(0.05)
    assert not second.done()
    gate.set()
    await asyncio.gather(first, second)
    assert launcher.calls == 1
    await provider.close()


@pytest.mark.asyncio
async def test_camoufox_disabled_reports_unavailable():
    launcher = FakeLauncher()
    provider = make_provider(enabled=False, launcher=launcher)
    availability = provider.availability()
    assert availability.ready is False
    assert availability.reason == "disabled"
    result = await provider.fetch("https://example.com")
    assert result.target_status_code is None
    assert result.error is not None
    assert launcher.calls == 0
    await provider.close()


@pytest.mark.asyncio
async def test_camoufox_launch_failure_is_normalized():
    launcher = FakeLauncher(error=RuntimeError("browser artifact missing"))
    provider = make_provider(launcher=launcher)
    result = await provider.fetch("https://example.com")
    assert result.target_status_code is None
    assert result.error is not None
    assert result.network_error is None
    availability = provider.availability()
    assert availability.ready is False
    assert "artifact missing" in (availability.reason or "")
    await provider.close()


@pytest.mark.asyncio
async def test_camoufox_records_no_redirect_when_final_url_unchanged():
    launcher = FakeLauncher()
    provider = make_provider(launcher=launcher)
    try:
        result = await provider.fetch("https://example.com/")
        assert result.redirected_url is None
    finally:
        await provider.close()


@pytest.mark.asyncio
async def test_camoufox_records_redirected_url_after_page_redirect():
    launcher = FakeLauncher(final_url="https://cdn.example.com/landed")
    provider = make_provider(launcher=launcher)
    try:
        result = await provider.fetch("https://example.com/start")
        assert result.url == "https://example.com/start"
        assert result.redirected_url == "https://cdn.example.com/landed"
        assert result.target_status_code == 200
    finally:
        await provider.close()


@pytest.mark.asyncio
async def test_camoufox_fetch_preserves_tier_and_cost():
    launcher = FakeLauncher()
    provider = make_provider(launcher=launcher)
    result = await provider.fetch("https://example.com")
    assert result.tier == Tier.CAMOUFOX
    assert result.cost_kind == CostKind.FREE
    assert result.target_status_code == 200
    assert "Camoufox content" in result.html
    assert launcher.sessions[0].browser.contexts[0].closed is True
    await provider.close()


@pytest.mark.asyncio
async def test_camoufox_navigation_network_error_is_classified():
    launcher = FakeLauncher()
    provider = make_provider(launcher=launcher)
    original_goto = FakePage.goto

    async def failing_goto(self, url, **kwargs):
        raise RuntimeError(
            "net::ERR_CONNECTION_REFUSED at https://example.com:443"
        )

    FakePage.goto = failing_goto
    try:
        result = await provider.fetch("https://example.com/")
        assert result.network_error is not None
        assert result.target_status_code is None
    finally:
        FakePage.goto = original_goto
        await provider.close()


@pytest.mark.asyncio
async def test_camoufox_navigation_timeout_is_classified():
    launcher = FakeLauncher()
    provider = make_provider(launcher=launcher)
    original_goto = FakePage.goto

    async def timeout_goto(self, url, **kwargs):
        raise PlaywrightTimeoutError("Timeout 60000ms exceeded.")

    FakePage.goto = timeout_goto
    try:
        result = await provider.fetch("https://slow.example/")
        assert result.network_error is not None
    finally:
        FakePage.goto = original_goto
        await provider.close()


@pytest.mark.asyncio
async def test_camoufox_context_creation_timeout_is_provider_failure():
    launcher = FakeLauncher()
    provider = make_provider(launcher=launcher)
    original_new_context = FakeSession.new_context

    async def timeout_new_context(self, **kwargs):
        raise PlaywrightTimeoutError("Timeout 60000ms exceeded.")

    FakeSession.new_context = timeout_new_context
    try:
        result = await provider.fetch("https://example.com/")
        assert result.network_error is None
        assert result.error is not None
    finally:
        FakeSession.new_context = original_new_context
        await provider.close()


@pytest.mark.asyncio
async def test_camoufox_guard_install_timeout_is_provider_failure():
    launcher = FakeLauncher()
    provider = make_provider(launcher=launcher)
    original_install = FakeRequestGuard.install

    async def timeout_install(self, context):
        raise PlaywrightTimeoutError("Timeout 60000ms exceeded.")

    FakeRequestGuard.install = timeout_install
    try:
        result = await provider.fetch("https://example.com/")
        assert result.network_error is None
        assert result.error is not None
    finally:
        FakeRequestGuard.install = original_install
        await provider.close()


@pytest.mark.asyncio
async def test_camoufox_page_creation_timeout_is_provider_failure():
    launcher = FakeLauncher()
    provider = make_provider(launcher=launcher)
    original_new_page = FakeContext.new_page

    async def timeout_new_page(self):
        raise PlaywrightTimeoutError("Timeout 60000ms exceeded.")

    FakeContext.new_page = timeout_new_page
    try:
        result = await provider.fetch("https://example.com/")
        assert result.network_error is None
        assert result.error is not None
    finally:
        FakeContext.new_page = original_new_page
        await provider.close()


@pytest.mark.asyncio
async def test_camoufox_content_timeout_is_provider_failure():
    launcher = FakeLauncher()
    provider = make_provider(launcher=launcher)
    original_content = FakePage.content

    async def timeout_content(self):
        raise PlaywrightTimeoutError("Timeout 60000ms exceeded.")

    FakePage.content = timeout_content
    try:
        result = await provider.fetch("https://example.com/")
        assert result.network_error is None
        assert result.error is not None
    finally:
        FakePage.content = original_content
        await provider.close()


@pytest.mark.asyncio
async def test_camoufox_context_failure_is_not_network_error():
    launcher = FakeLauncher()
    provider = make_provider(launcher=launcher)
    original_new_page = FakeContext.new_page

    async def failing_new_page(self):
        raise RuntimeError("Target page, context or browser has been closed")

    FakeContext.new_page = failing_new_page
    try:
        result = await provider.fetch("https://example.com/")
        assert result.network_error is None
        assert result.error is not None
    finally:
        FakeContext.new_page = original_new_page
        await provider.close()


@pytest.mark.asyncio
async def test_camoufox_reap_cancellation_propagates_and_close_continues_owned(provider, gate, clock):
    fetch = asyncio.create_task(provider.fetch("https://example.com/a"))
    await provider.launcher.started.wait()
    gate.set()
    await fetch
    clock.advance(121)
    provider.launcher.close_gate = asyncio.Event()
    reap = asyncio.create_task(provider.reap_idle())
    await provider.launcher.close_started.wait()
    reap.cancel()
    with pytest.raises(asyncio.CancelledError):
        await reap
    assert provider.launcher.sessions[0].closed is False
    assert provider._close_tasks
    provider.launcher.close_gate.set()
    await asyncio.wait_for(provider.close(), timeout=5)
    assert provider.launcher.sessions[0].closed is True
    assert provider._close_tasks == set()


@pytest.mark.asyncio
async def test_camoufox_close_after_reap_waits_and_stays_closed(provider, gate, clock):
    fetch = asyncio.create_task(provider.fetch("https://example.com/a"))
    await provider.launcher.started.wait()
    gate.set()
    await fetch
    clock.advance(121)
    provider.launcher.close_gate = asyncio.Event()
    reap = asyncio.create_task(provider.reap_idle())
    await provider.launcher.close_started.wait()
    close = asyncio.create_task(provider.close())
    await asyncio.sleep(0)
    assert not close.done()
    provider.launcher.close_gate.set()
    await asyncio.wait_for(asyncio.gather(reap, close), timeout=5)
    assert provider.launcher.sessions[0].closed is True
    result = await provider.fetch("https://example.com/b")
    assert result.error is not None and "closed" in (result.error or "")
    assert provider.launcher.calls == 1


@pytest.mark.asyncio
async def test_camoufox_fetch_after_terminal_close_rejects_without_launching(provider, gate):
    fetch = asyncio.create_task(provider.fetch("https://example.com/a"))
    await provider.launcher.started.wait()
    gate.set()
    await fetch
    await provider.close()
    result = await provider.fetch("https://example.com/b")
    assert result.error is not None and "closed" in (result.error or "")
    assert provider.launcher.calls == 1
    await provider.close()
    assert provider.launcher.sessions[0].closed is True


@pytest.mark.asyncio
async def test_camoufox_reap_after_terminal_close_is_noop(provider, gate, clock):
    fetch = asyncio.create_task(provider.fetch("https://example.com/a"))
    await provider.launcher.started.wait()
    gate.set()
    await fetch
    await provider.close()
    clock.advance(121)
    await provider.reap_idle()
    assert provider.launcher.sessions[0].closed is True


@pytest.mark.asyncio
async def test_camoufox_blocked_main_frame_navigation_is_policy_error():
    launcher = FakeLauncher()
    guard = BrowserRequestGuard(public_policy())
    provider = CamoufoxProvider(
        enabled=True,
        idle_seconds=120,
        semaphore=asyncio.Semaphore(2),
        launcher=launcher,
        egress_proxy=FakePinnedProxy(),
        request_guard=guard,
    )
    original_goto = FakePage.goto

    async def blocked_goto(self, url, **kwargs):
        frame = FakeFrame(self)
        self.main_frame = frame
        request = FakeNavigationRequest("https://10.0.0.5/secret", frame)
        route = FakeRoute()
        await guard.handle(route, request)
        assert route.aborted and not route.fell_back
        raise RuntimeError("Page.goto: navigation to page was aborted")

    FakePage.goto = blocked_goto
    try:
        result = await provider.fetch("https://example.com/start")
        assert result.policy_error == "non_global_address"
        assert result.network_error is None
    finally:
        FakePage.goto = original_goto
        await provider.close()


@pytest.mark.asyncio
async def test_camoufox_subresource_block_alone_is_not_policy_error():
    launcher = FakeLauncher()
    guard = BrowserRequestGuard(public_policy())
    provider = CamoufoxProvider(
        enabled=True,
        idle_seconds=120,
        semaphore=asyncio.Semaphore(2),
        launcher=launcher,
        egress_proxy=FakePinnedProxy(),
        request_guard=guard,
    )
    original_goto = FakePage.goto

    async def subresource_goto(self, url, **kwargs):
        frame = FakeFrame(self)
        self.main_frame = frame
        route = FakeRoute()
        request = FakeNavigationRequest("https://10.0.0.5/tracker.js", frame)
        request.is_navigation_request = lambda: False
        await guard.handle(route, request)
        assert route.aborted
        return FakeResponse()

    FakePage.goto = subresource_goto
    try:
        result = await provider.fetch("https://example.com/")
        assert result.policy_error is None
        assert result.target_status_code == 200
    finally:
        FakePage.goto = original_goto
        await provider.close()


@pytest.mark.asyncio
async def test_camoufox_recorder_is_cleaned_up_after_fetch():
    launcher = FakeLauncher()
    guard = BrowserRequestGuard(public_policy())
    provider = CamoufoxProvider(
        enabled=True,
        idle_seconds=120,
        semaphore=asyncio.Semaphore(2),
        launcher=launcher,
        egress_proxy=FakePinnedProxy(),
        request_guard=guard,
    )
    try:
        result = await provider.fetch("https://example.com/")
        assert result.target_status_code == 200
        assert guard._pages == {}
        assert guard._by_task == {}
    finally:
        await provider.close()


@pytest.mark.asyncio
async def test_camoufox_goto_failure_is_never_target_success():
    from crawl4ai_mcp.detect import classify
    from crawl4ai_mcp.models import Decision

    launcher = FakeLauncher()
    provider = make_provider(launcher=launcher)
    original_goto = FakePage.goto

    async def generic_failing_goto(self, url, **kwargs):
        raise RuntimeError("browser session crashed")

    FakePage.goto = generic_failing_goto
    try:
        result = await provider.fetch("https://example.com/")
        assert result.target_status_code is None
        assert result.network_error is None
        assert classify(result) == Decision.FAILED
        assert classify(result) not in {Decision.SUCCESS, Decision.SHORT_STATIC}
    finally:
        FakePage.goto = original_goto
        await provider.close()
