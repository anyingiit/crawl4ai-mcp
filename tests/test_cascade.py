import pytest
from crawl4ai_mcp.cascade import CascadeEngine
from crawl4ai_mcp.models import CostKind, Decision, FetchResult, ProviderAvailability, Tier
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
        url=url, tier=tier, cost_kind=CostKind.FREE, status_code=200,
        html="<main>" + "x" * 300 + "</main>",
        markdown="# Fine", elapsed_ms=5,
    )


def cloudflare(url, tier):
    return FetchResult(
        url=url, tier=tier, cost_kind=CostKind.FREE, status_code=200,
        html="<title>Just a moment...</title><script>window.__cf_chl_opt={}</script>",
        elapsed_ms=5,
    )


def short_js(url, tier):
    return FetchResult(
        url=url, tier=tier, cost_kind=CostKind.FREE, status_code=200,
        html="<div id='app'></div><script src='app.js'></script>", elapsed_ms=5,
    )


def not_found(url, tier):
    return FetchResult(
        url=url, tier=tier, cost_kind=CostKind.FREE, status_code=404,
        html="missing", elapsed_ms=5,
    )


def network_error(url, tier):
    return FetchResult(
        url=url, tier=tier, cost_kind=CostKind.FREE, status_code=None,
        error="Connection refused", elapsed_ms=5,
    )


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


@pytest.mark.asyncio
async def test_network_failure_retries_same_tier_once(tmp_path):
    engine, policy = await make_engine(tmp_path, {
        Tier.HTTP: network_error, Tier.STEALTH: success,
    })
    try:
        result = await engine.scrape("https://flaky.example/")
        assert engine.calls == [Tier.HTTP, Tier.HTTP, Tier.STEALTH]
        assert result.status == "success"
        assert result.tier_used == Tier.STEALTH
    finally:
        await policy.close()


@pytest.mark.asyncio
async def test_short_static_page_is_accepted_without_escalation(tmp_path):
    engine, policy = await make_engine(tmp_path, {Tier.HTTP: not_found})
    provider = engine.providers[Tier.HTTP]

    def short_static(url, tier):
        return FetchResult(
            url=url, tier=tier, cost_kind=CostKind.FREE, status_code=200,
            html="<main>Short notice</main>", markdown="Short notice", elapsed_ms=5,
        )

    provider.handler = short_static
    try:
        result = await engine.scrape("https://example.com/short")
        assert result.status == "success"
        assert engine.calls == [Tier.HTTP]
    finally:
        await policy.close()


@pytest.mark.asyncio
async def test_maximum_tier_bounds_escalation(tmp_path):
    engine, policy = await make_engine(tmp_path, {Tier.HTTP: cloudflare})
    try:
        result = await engine.scrape(
            "https://protected.example/", maximum=Tier.HTTP
        )
        assert result.status == "failed"
        assert engine.calls == [Tier.HTTP]
    finally:
        await policy.close()


@pytest.mark.asyncio
async def test_attempts_carry_full_metadata(engine_with_404):
    result = await engine_with_404.scrape("https://example.com/missing")
    assert len(result.attempts) == 1
    attempt = result.attempts[0]
    assert attempt.tier == Tier.HTTP
    assert attempt.decision == Decision.TERMINAL
    assert attempt.cost_kind == CostKind.FREE
    assert attempt.status_code == 404
    assert attempt.elapsed_ms == 5
    assert attempt.error is None
    assert result.error == "HTTP 404"
