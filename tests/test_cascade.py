import asyncio
import ipaddress

import pytest
from crawl4ai_mcp.cascade import CascadeEngine, CascadeInputError
from crawl4ai_mcp.models import (
    CostKind,
    Decision,
    FetchResult,
    ProviderAvailability,
    ProviderErrorKind,
    Tier,
)
from crawl4ai_mcp.policy import PolicyStore


class FakeClock:
    def __init__(self, now: float = 0.0):
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def success(url, tier):
    return FetchResult(
        url=url, tier=tier, cost_kind=CostKind.FREE, target_status_code=200,
        html="<main>" + "x" * 300 + "</main>",
        markdown="# Fine", elapsed_ms=5,
    )


def cloudflare(url, tier):
    return FetchResult(
        url=url, tier=tier, cost_kind=CostKind.FREE, target_status_code=200,
        html="<title>Just a moment...</title><script>window.__cf_chl_opt={}</script>",
        elapsed_ms=5,
    )


def short_js(url, tier):
    return FetchResult(
        url=url, tier=tier, cost_kind=CostKind.FREE, target_status_code=200,
        html="<div id='app'></div><script src='app.js'></script>", elapsed_ms=5,
    )


def not_found(url, tier):
    return FetchResult(
        url=url, tier=tier, cost_kind=CostKind.FREE, target_status_code=404,
        html="missing", elapsed_ms=5,
    )


def network_error(url, tier):
    return FetchResult(
        url=url, tier=tier, cost_kind=CostKind.FREE, target_status_code=None,
        network_error="connection_refused", error="Connection refused", elapsed_ms=5,
    )


def generic_error(url, tier):
    return FetchResult(
        url=url, tier=tier, cost_kind=CostKind.FREE, target_status_code=None,
        error="Invalid token", elapsed_ms=5,
    )


def provider_auth_failure(url, tier):
    return FetchResult(
        url=url, tier=tier, cost_kind=CostKind.RAYOBYTE_CREDIT,
        target_status_code=None, provider_status_code=401,
        provider_error_kind=ProviderErrorKind.AUTH,
        provider_error="Invalid token", error="Invalid token", elapsed_ms=5,
    )


def rate_limited(url, tier):
    return FetchResult(
        url=url, tier=tier, cost_kind=CostKind.FREE, target_status_code=429,
        html="slow down", headers={"retry-after": "60"}, elapsed_ms=5,
    )


def policy_rejected(url, tier):
    return FetchResult(
        url=url, tier=tier, cost_kind=CostKind.FREE, target_status_code=None,
        policy_error="non_global_address", error="blocked by policy", elapsed_ms=5,
    )


def route(url, tier):
    if "hard.example" in url:
        if tier == Tier.RAYOBYTE:
            if "/missing" in url:
                return not_found(url, tier)
            return provider_auth_failure(url, tier)
        return success(url, tier)
    if "flaky.example" in url:
        return network_error(url, tier)
    if "protected.example" in url:
        if tier in {Tier.RAYOBYTE, Tier.FIRECRAWL}:
            return success(url, tier)
        if tier == Tier.CAMOUFOX:
            return rate_limited(url, tier)
        return cloudflare(url, tier)
    return success(url, tier)


class ScriptedProvider:
    def __init__(self, tier, handler=None):
        self.tier = tier
        self.handler = handler
        self.calls = []

    async def fetch(self, url):
        self.calls.append(self.tier)
        if self.handler is None:
            return success(url, self.tier)
        return self.handler(url, self.tier)

    async def close(self):
        pass

    def availability(self):
        return ProviderAvailability(enabled=True, ready=True)


async def make_engine(tmp_path, handlers, now=1_000):
    providers = {tier: ScriptedProvider(tier, handlers.get(tier)) for tier in Tier}
    policy = await PolicyStore.open(tmp_path / "policy.db")
    return CascadeEngine(
        providers, policy, threshold=200, clock=FakeClock(now)
    ), policy


@pytest.fixture
async def policy(tmp_path):
    store = await PolicyStore.open(tmp_path / "policy.db")
    yield store
    await store.close()


@pytest.fixture
async def engine(tmp_path, policy):
    providers = {tier: ScriptedProvider(tier, route) for tier in Tier}
    engine = CascadeEngine(
        providers, policy, threshold=200, clock=FakeClock(1_000)
    )
    await policy.record_success("https://hard.example/", Tier.RAYOBYTE, now=1_000)
    return engine


@pytest.fixture
async def engine_with_404(tmp_path):
    engine, policy = await make_engine(tmp_path, {Tier.HTTP: not_found})
    yield engine
    await policy.close()


@pytest.fixture
async def engine_with_cloudflare(tmp_path):
    engine, policy = await make_engine(tmp_path, {
        Tier.HTTP: cloudflare, Tier.STEALTH: cloudflare, Tier.UNDETECTED: cloudflare,
        Tier.CAMOUFOX: cloudflare, Tier.RAYOBYTE: success, Tier.FIRECRAWL: success,
    })
    yield engine
    await policy.close()


@pytest.fixture
async def engine_with_policy(tmp_path):
    engine, policy = await make_engine(tmp_path, {
        Tier.HTTP: short_js, Tier.STEALTH: short_js, Tier.UNDETECTED: success,
    })
    yield engine
    await policy.close()


@pytest.fixture
async def engine_in_cooldown(tmp_path):
    engine, policy = await make_engine(tmp_path, {})
    await policy.record_failure("https://bad.example/x", "all_failed", now=1_000)
    yield engine
    await policy.close()


@pytest.mark.asyncio
async def test_404_stops_without_paid_calls(engine_with_404):
    outcome = await engine_with_404.scrape("https://example.com/missing")
    assert outcome.response.status == "terminal"
    assert engine_with_404.calls == [Tier.HTTP]


@pytest.mark.asyncio
async def test_cloudflare_skips_proxy(engine_with_cloudflare):
    outcome = await engine_with_cloudflare.scrape("https://protected.example/")
    assert engine_with_cloudflare.calls == [
        Tier.HTTP, Tier.STEALTH, Tier.UNDETECTED,
        Tier.CAMOUFOX, Tier.RAYOBYTE,
    ]
    assert outcome.response.tier_used == "rayobyte"


@pytest.mark.asyncio
async def test_second_request_starts_at_remembered_tier(engine_with_policy):
    await engine_with_policy.scrape("https://hard.example/a")
    engine_with_policy.calls.clear()
    outcome = await engine_with_policy.scrape("https://hard.example/b")
    assert engine_with_policy.calls[0] == Tier.UNDETECTED
    assert outcome.response.status == "success"


@pytest.mark.asyncio
async def test_cooldown_prevents_any_provider_call(engine_in_cooldown):
    outcome = await engine_in_cooldown.scrape("https://bad.example/x")
    assert outcome.response.status == "cooldown"
    assert engine_in_cooldown.calls == []


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
async def test_already_queued_proxy_is_excluded_once_cloudflare_seen(tmp_path):
    engine, policy = await make_engine(tmp_path, {
        Tier.HTTP: short_js, Tier.STEALTH: cloudflare, Tier.UNDETECTED: cloudflare,
        Tier.CAMOUFOX: cloudflare, Tier.PROXY: success, Tier.RAYOBYTE: success,
    })
    try:
        outcome = await engine.scrape("https://protected.example/")
        assert Tier.PROXY not in engine.calls
        assert engine.calls == [
            Tier.HTTP, Tier.STEALTH, Tier.UNDETECTED,
            Tier.CAMOUFOX, Tier.RAYOBYTE,
        ]
        assert outcome.response.tier_used == "rayobyte"
    finally:
        await policy.close()


@pytest.mark.asyncio
async def test_untyped_provider_error_falls_back_to_next_available_tier(tmp_path):
    engine, policy = await make_engine(tmp_path, {
        Tier.RAYOBYTE: generic_error, Tier.FIRECRAWL: success,
    })
    await policy.record_success("https://hard.example/a", Tier.RAYOBYTE, now=1000)
    try:
        outcome = await engine.scrape("https://hard.example/b")
        assert engine.calls == [Tier.RAYOBYTE, Tier.FIRECRAWL]
        assert outcome.response.status == "success"
        assert outcome.response.attempts[0].decision == Decision.FAILED
    finally:
        await policy.close()


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


@pytest.mark.asyncio
async def test_provider_auth_failure_attempt_carries_provider_details(engine):
    outcome = await engine.scrape("https://hard.example/")
    attempt = outcome.response.attempts[0]
    assert attempt.decision == Decision.PROVIDER_FAILURE
    assert attempt.target_status_code is None
    assert attempt.provider_status_code == 401
    assert attempt.provider_error_kind == ProviderErrorKind.AUTH
    assert attempt.provider_error == "Invalid token"


@pytest.mark.asyncio
async def test_provider_auth_failure_on_last_paid_tier_stops_without_duplicate_credit_waste(tmp_path):
    engine, policy = await make_engine(tmp_path, {
        Tier.RAYOBYTE: provider_auth_failure, Tier.FIRECRAWL: provider_auth_failure,
    })
    await policy.record_success("https://hard.example/a", Tier.RAYOBYTE, now=1000)
    try:
        outcome = await engine.scrape("https://hard.example/b")
        assert engine.calls == [Tier.RAYOBYTE, Tier.FIRECRAWL]
        assert outcome.response.status == "failed"
        assert [
            attempt.decision for attempt in outcome.response.attempts
        ] == [Decision.PROVIDER_FAILURE, Decision.PROVIDER_FAILURE]
    finally:
        await policy.close()


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
    assert await policy.list_policies("https://example.com/") == []


@pytest.mark.asyncio
async def test_policy_rejection_returns_without_cooldown_mutation(tmp_path):
    engine, policy = await make_engine(tmp_path, {Tier.HTTP: policy_rejected})
    try:
        outcome = await engine.scrape("https://blocked.example/")
        assert engine.calls == [Tier.HTTP]
        assert outcome.response.status == "failed"
        assert outcome.response.cooldown_until is None
        assert await policy.list_policies() == []
    finally:
        await policy.close()


@pytest.mark.asyncio
async def test_short_static_page_is_accepted_without_escalation(tmp_path):
    engine, policy = await make_engine(tmp_path, {Tier.HTTP: not_found})
    provider = engine.providers[Tier.HTTP]

    def short_static(url, tier):
        return FetchResult(
            url=url, tier=tier, cost_kind=CostKind.FREE, target_status_code=200,
            html="<main>Short notice</main>", markdown="Short notice", elapsed_ms=5,
        )

    provider.handler = short_static
    try:
        outcome = await engine.scrape("https://example.com/short")
        assert outcome.response.status == "success"
        assert engine.calls == [Tier.HTTP]
    finally:
        await policy.close()


@pytest.mark.asyncio
async def test_maximum_tier_bounds_escalation(tmp_path):
    engine, policy = await make_engine(tmp_path, {Tier.HTTP: cloudflare})
    try:
        outcome = await engine.scrape(
            "https://protected.example/", maximum=Tier.HTTP
        )
        assert outcome.response.status == "failed"
        assert engine.calls == [Tier.HTTP]
    finally:
        await policy.close()


@pytest.mark.asyncio
async def test_success_outcome_carries_private_html_and_effective_url(tmp_path):
    engine, policy = await make_engine(tmp_path, {Tier.HTTP: success})
    try:
        outcome = await engine.scrape("https://example.com/")
        assert outcome.raw_html == "<main>" + "x" * 300 + "</main>"
        assert outcome.effective_url == "https://example.com/"
        assert "raw_html" not in outcome.response.model_dump(mode="json")
    finally:
        await policy.close()


@pytest.mark.asyncio
async def test_redirect_success_outcome_reports_effective_url(tmp_path):
    def redirected(url, tier):
        fetched = success(url, tier)
        fetched.redirected_url = "https://cdn.example.com/page"
        return fetched

    engine, policy = await make_engine(tmp_path, {Tier.HTTP: redirected})
    try:
        outcome = await engine.scrape("https://example.com/start")
        assert outcome.effective_url == "https://cdn.example.com/page"
        assert outcome.raw_html == "<main>" + "x" * 300 + "</main>"
    finally:
        await policy.close()


@pytest.mark.asyncio
async def test_attempts_carry_full_metadata(engine_with_404):
    outcome = await engine_with_404.scrape("https://example.com/missing")
    assert len(outcome.response.attempts) == 1
    attempt = outcome.response.attempts[0]
    assert attempt.tier == "http"
    assert attempt.decision == Decision.TERMINAL
    assert attempt.cost_kind == CostKind.FREE
    assert attempt.target_status_code == 404
    assert attempt.provider_status_code is None
    assert attempt.provider_error_kind is None
    assert attempt.elapsed_ms == 5
    assert attempt.error is None
    assert outcome.response.error == "HTTP 404"


class _FakeResult:
    def __init__(self, html="<main>Hello</main>", error_message=None, success=True,
                 status_code=None):
        self.html = html
        self.status_code = (
            status_code if status_code is not None else (200 if success else None)
        )
        self.response_headers = {}
        self.redirected_url = None
        self.error_message = error_message
        self.success = success


class _FakeContainer:
    def __init__(self, **kwargs):
        self._results = [_FakeResult(**kwargs)]


class _FakeCrawler:
    def __init__(self, factory):
        self.factory = factory
        self.closed = False
        self.configs = []

    async def arun(self, url, config=None):
        self.configs.append(config)
        return self.factory.container_factory()

    async def close(self):
        self.closed = True
        self.factory.closed += 1


class _FakeCrawlerFactory:
    def __init__(self, container_factory):
        self.created = 0
        self.closed = 0
        self.container_factory = container_factory

    def __call__(self):
        self.created += 1
        return _FakeCrawler(self)


class _FakeEgressProxy:
    def endpoint(self, upstream=None):
        from crawl4ai.async_configs import ProxyConfig

        return ProxyConfig(server="http://127.0.0.1:41000")


class _FakeGuard:
    def __init__(self):
        self.recorders = []

    async def install(self, context):
        pass

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


def make_browser_provider(container_factory, guard=None):
    from crawl4ai_mcp.providers.browser import BrowserProvider

    return BrowserProvider(
        tier=Tier.STEALTH,
        idle_seconds=180,
        semaphore=asyncio.Semaphore(2),
        egress_proxy=_FakeEgressProxy(),
        request_guard=guard or _FakeGuard(),
        factory=_FakeCrawlerFactory(container_factory),
    )


@pytest.mark.asyncio
async def test_returned_browser_network_failure_never_reaches_paid_tiers(tmp_path):
    from crawl4ai_mcp.egress import BrowserRequestGuard, UrlPolicy

    async def resolver(_host, _port):
        return [ipaddress.ip_address("93.184.216.34")]

    guard = BrowserRequestGuard(UrlPolicy(resolver))
    provider = make_browser_provider(
        lambda: _FakeContainer(
            error_message=(
                "Failed on navigating ACS-GOTO:\n"
                "net::ERR_CONNECTION_RESET at https://flaky.example/"
            ),
            success=False,
        ),
        guard=guard,
    )
    paid_calls = []
    providers = {
        Tier.STEALTH: provider,
        Tier.RAYOBYTE: ScriptedProvider(
            Tier.RAYOBYTE,
            lambda url, tier: paid_calls.append(tier) or success(url, tier),
        ),
        Tier.FIRECRAWL: ScriptedProvider(
            Tier.FIRECRAWL,
            lambda url, tier: paid_calls.append(tier) or success(url, tier),
        ),
    }
    policy = await PolicyStore.open(tmp_path / "policy.db")
    engine = CascadeEngine(providers, policy, threshold=200, clock=FakeClock(1000))
    try:
        outcome = await engine.scrape("https://flaky.example/", force=Tier.STEALTH)
        assert [attempt.tier for attempt in outcome.response.attempts] == [
            "stealth", "stealth",
        ]
        assert outcome.response.attempts[-1].decision == Decision.TARGET_NETWORK
        assert paid_calls == []
        assert outcome.response.status == "failed"
    finally:
        await provider.close()
        await policy.close()


@pytest.mark.asyncio
async def test_returned_browser_failure_200_falls_to_next_tier_without_empty_success(tmp_path):
    from crawl4ai_mcp.egress import BrowserRequestGuard, UrlPolicy

    async def resolver(_host, _port):
        return [ipaddress.ip_address("93.184.216.34")]

    guard = BrowserRequestGuard(UrlPolicy(resolver))

    async def failed_200_arun(self, url, config=None):
        return _FakeContainer(
            html="",
            error_message="scrape pipeline crashed after load",
            success=False,
            status_code=200,
        )

    provider = make_browser_provider(lambda: _FakeContainer(), guard=guard)
    paid_calls = []
    providers = {
        Tier.STEALTH: provider,
        Tier.RAYOBYTE: ScriptedProvider(
            Tier.RAYOBYTE,
            lambda url, tier: paid_calls.append(tier) or success(url, tier),
        ),
    }
    policy = await PolicyStore.open(tmp_path / "policy.db")
    engine = CascadeEngine(providers, policy, threshold=200, clock=FakeClock(1000))
    original_arun = _FakeCrawler.arun
    _FakeCrawler.arun = failed_200_arun
    try:
        outcome = await engine.scrape("https://example.com/", force=Tier.STEALTH)
        assert [attempt.tier for attempt in outcome.response.attempts] == [
            "stealth", "rayobyte",
        ]
        assert outcome.response.attempts[0].decision == Decision.PROVIDER_FAILURE
        assert outcome.response.attempts[0].provider_error_kind == ProviderErrorKind.SERVICE
        assert outcome.response.status == "success"
        assert outcome.response.content == "# Fine"
        assert paid_calls == [Tier.RAYOBYTE]
    finally:
        _FakeCrawler.arun = original_arun
        await provider.close()
        await policy.close()


@pytest.mark.asyncio
async def test_public_to_private_browser_redirect_is_policy_error_without_paid_tiers(
    tmp_path,
):
    from crawl4ai_mcp.egress import BrowserRequestGuard, UrlPolicy

    async def resolver(host, _port):
        if host == "private.example":
            return [ipaddress.ip_address("10.0.0.5")]
        return [ipaddress.ip_address("93.184.216.34")]

    guard = BrowserRequestGuard(UrlPolicy(resolver))
    page = _FakePage()
    frame = _FakeFrame(page)
    page.main_frame = frame

    async def blocked_arun(self, url, config=None):
        guard.bind_page(page)
        route = _FakeRoute()
        request = _FakeNavigationRequest("https://private.example/secret", frame)
        await guard.handle(route, request)
        assert route.aborted and not route.fell_back
        return _FakeContainer(
            error_message="Navigation to https://private.example/secret was aborted",
            success=False,
        )

    provider = make_browser_provider(lambda: _FakeContainer(), guard=guard)
    paid_calls = []
    providers = {
        Tier.STEALTH: provider,
        Tier.RAYOBYTE: ScriptedProvider(
            Tier.RAYOBYTE,
            lambda url, tier: paid_calls.append(tier) or success(url, tier),
        ),
        Tier.FIRECRAWL: ScriptedProvider(
            Tier.FIRECRAWL,
            lambda url, tier: paid_calls.append(tier) or success(url, tier),
        ),
    }
    policy = await PolicyStore.open(tmp_path / "policy.db")
    engine = CascadeEngine(providers, policy, threshold=200, clock=FakeClock(1000))
    original_arun = _FakeCrawler.arun
    _FakeCrawler.arun = blocked_arun
    try:
        outcome = await engine.scrape("https://example.com/start", force=Tier.STEALTH)
        assert outcome.response.attempts[-1].decision == Decision.POLICY_REJECTED
        assert paid_calls == []
        assert outcome.response.status == "failed"
        assert outcome.response.cooldown_until is None
        assert await policy.list_policies() == []
    finally:
        _FakeCrawler.arun = original_arun
        await provider.close()
        await policy.close()


class _FakeRoute:
    def __init__(self):
        self.fell_back = False
        self.aborted = False

    async def fallback(self):
        self.fell_back = True

    async def abort(self, _reason="blockedbyclient"):
        self.aborted = True


class _FakeFrame:
    def __init__(self, page):
        self.page = page


class _FakePage:
    def __init__(self, main_frame=None):
        self.main_frame = main_frame


class _FakeNavigationRequest:
    def __init__(self, url, frame=None):
        self.url = url
        self.frame = frame

    def is_navigation_request(self):
        return True


class ExplodingProvider:
    def __init__(self, tier):
        self.tier = tier
        self.cost_kind = CostKind.FREE

    async def fetch(self, url):
        raise RuntimeError("provider exploded unexpectedly")

    async def close(self):
        pass

    def availability(self):
        return ProviderAvailability(enabled=True, ready=True)


@pytest.mark.asyncio
async def test_unexpected_provider_exception_becomes_service_failure_and_escalates(tmp_path):
    providers = {
        Tier.RAYOBYTE: ExplodingProvider(Tier.RAYOBYTE),
        Tier.FIRECRAWL: ScriptedProvider(Tier.FIRECRAWL),
    }
    policy = await PolicyStore.open(tmp_path / "policy.db")
    engine = CascadeEngine(providers, policy, threshold=200, clock=FakeClock(1000))
    await policy.record_success("https://hard.example/a", Tier.RAYOBYTE, now=1000)
    try:
        outcome = await engine.scrape("https://hard.example/b")
        assert outcome.response.status == "success"
        assert engine.calls == [Tier.RAYOBYTE, Tier.FIRECRAWL]
        assert outcome.response.attempts[0].decision == Decision.PROVIDER_FAILURE
        assert outcome.response.attempts[0].provider_error_kind == ProviderErrorKind.SERVICE
        assert "exploded" in (outcome.response.attempts[0].provider_error or "")
    finally:
        await policy.close()


@pytest.mark.asyncio
async def test_cascade_does_not_swallow_cancelled_error(tmp_path):
    class CancelProvider(ExplodingProvider):
        async def fetch(self, url):
            raise asyncio.CancelledError()

    providers = {Tier.HTTP: CancelProvider(Tier.HTTP)}
    policy = await PolicyStore.open(tmp_path / "policy.db")
    engine = CascadeEngine(providers, policy, threshold=200, clock=FakeClock(1000))
    try:
        with pytest.raises(asyncio.CancelledError):
            await engine.scrape("https://example.com/")
    finally:
        await policy.close()


@pytest.mark.asyncio
async def test_unexpected_exception_on_only_tier_records_cooldown(tmp_path):
    providers = {Tier.HTTP: ExplodingProvider(Tier.HTTP)}
    policy = await PolicyStore.open(tmp_path / "policy.db")
    engine = CascadeEngine(providers, policy, threshold=200, clock=FakeClock(1000))
    try:
        outcome = await engine.scrape("https://example.com/")
        assert outcome.response.status == "failed"
        assert [a.decision for a in outcome.response.attempts] == [
            Decision.PROVIDER_FAILURE
        ]
        assert outcome.response.cooldown_until == 1000 + 600
    finally:
        await policy.close()
