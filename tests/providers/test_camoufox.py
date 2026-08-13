import asyncio
import pytest
from crawl4ai.async_configs import ProxyConfig
from crawl4ai_mcp.models import CostKind, Tier
from crawl4ai_mcp.providers.camoufox import CamoufoxProvider


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


class FakeResponse:
    def __init__(self, status=200, url="https://example.com/"):
        self.status = status
        self.url = url


class FakePage:
    def __init__(self, gate=None):
        self.gate = gate
        self.closed = False

    async def goto(self, url, **kwargs):
        if self.gate is not None:
            await self.gate.wait()
        return FakeResponse()

    async def wait_for_load_state(self, *args, **kwargs):
        pass

    async def content(self):
        return "<main>Camoufox content</main>"

    async def close(self):
        self.closed = True


class FakeContext:
    def __init__(self, gate=None):
        self.gate = gate
        self.closed = False

    async def new_page(self):
        return FakePage(self.gate)

    async def close(self):
        self.closed = True


class FakeBrowser:
    def __init__(self, gate=None):
        self.gate = gate
        self.closed = False
        self.contexts = []

    async def new_context(self, **kwargs):
        context = FakeContext(self.gate)
        context.kwargs = kwargs
        self.contexts.append(context)
        return context

    async def close(self):
        self.closed = True


class FakeSession:
    def __init__(self, browser):
        self.browser = browser
        self.closed = False

    async def new_context(self, **kwargs):
        return await self.browser.new_context(**kwargs)

    async def close(self):
        self.closed = True
        await self.browser.close()


class FakeLauncher:
    def __init__(self, error=None, gate=None):
        self.calls = 0
        self.error = error
        self.gate = gate
        self.sessions = []

    async def __call__(self):
        self.calls += 1
        if self.error is not None:
            raise self.error
        session = FakeSession(FakeBrowser(self.gate))
        self.sessions.append(session)
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
    assert result.status_code is None
    assert result.error is not None
    assert launcher.calls == 0
    await provider.close()


@pytest.mark.asyncio
async def test_camoufox_launch_failure_is_normalized():
    launcher = FakeLauncher(error=RuntimeError("browser artifact missing"))
    provider = make_provider(launcher=launcher)
    result = await provider.fetch("https://example.com")
    assert result.status_code is None
    assert result.error is not None
    assert result.network_error is None
    availability = provider.availability()
    assert availability.ready is False
    assert "artifact missing" in (availability.reason or "")
    await provider.close()


@pytest.mark.asyncio
async def test_camoufox_fetch_preserves_tier_and_cost():
    launcher = FakeLauncher()
    provider = make_provider(launcher=launcher)
    result = await provider.fetch("https://example.com")
    assert result.tier == Tier.CAMOUFOX
    assert result.cost_kind == CostKind.FREE
    assert result.status_code == 200
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
        raise RuntimeError("Timeout 60000ms exceeded.")

    FakePage.goto = timeout_goto
    try:
        result = await provider.fetch("https://slow.example/")
        assert result.network_error is not None
    finally:
        FakePage.goto = original_goto
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
