import pytest
from crawl4ai_mcp.cascade import CascadeEngine, CascadeInputError
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


def rate_limited(url, tier):
    return FetchResult(
        url=url, tier=tier, cost_kind=CostKind.FREE, status_code=429,
        html="slow down", headers={"retry-after": "60"}, elapsed_ms=5,
    )


def policy_rejected(url, tier):
    return FetchResult(
        url=url, tier=tier, cost_kind=CostKind.FREE, status_code=None,
        policy_error="non_global_address", error="blocked by policy", elapsed_ms=5,
    )


def route(url, tier):
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
    return CascadeEngine(
        providers, policy, threshold=200, clock=FakeClock(1_000)
    )


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
            url=url, tier=tier, cost_kind=CostKind.FREE, status_code=200,
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
async def test_attempts_carry_full_metadata(engine_with_404):
    outcome = await engine_with_404.scrape("https://example.com/missing")
    assert len(outcome.response.attempts) == 1
    attempt = outcome.response.attempts[0]
    assert attempt.tier == "http"
    assert attempt.decision == Decision.TERMINAL
    assert attempt.cost_kind == CostKind.FREE
    assert attempt.status_code == 404
    assert attempt.elapsed_ms == 5
    assert attempt.error is None
    assert outcome.response.error == "HTTP 404"
