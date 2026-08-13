import pytest
from crawl4ai_mcp.detect import classify, next_tiers
from crawl4ai_mcp.models import (
    CostKind,
    Decision,
    FetchResult,
    ProviderErrorKind,
    Tier,
)


def fetched(status: int, html: str, headers=None):
    return FetchResult(
        url="https://example.com", tier=Tier.HTTP, cost_kind=CostKind.FREE,
        target_status_code=status, html=html, headers=headers or {}, elapsed_ms=1,
    )


@pytest.mark.parametrize("status", [401, 404, 410])
def test_terminal_statuses_never_escalate(status):
    assert classify(fetched(status, "missing")) == Decision.TERMINAL


def test_short_static_page_is_valid_content():
    assert classify(fetched(200, "<main>Short notice</main>")) == Decision.SHORT_STATIC


def test_short_script_shell_needs_js():
    assert classify(fetched(200, "<div id='app'></div><script src='app.js'></script>")) == Decision.NEEDS_JS


def test_cloudflare_challenge_is_detected_before_length_check():
    html = "<title>Just a moment...</title><script>window.__cf_chl_opt={}</script>"
    assert classify(fetched(200, html)) == Decision.CLOUDFLARE


def test_retry_after_is_rate_limited():
    assert classify(fetched(429, "slow down", {"retry-after": "60"})) == Decision.RATE_LIMITED


def test_network_error_is_target_network_decision():
    result = FetchResult(
        url="https://example.com", tier=Tier.HTTP, cost_kind=CostKind.FREE,
        target_status_code=None, network_error="request_failed",
        error="Connection refused", elapsed_ms=1,
    )
    assert classify(result) == Decision.TARGET_NETWORK


def test_untyped_error_without_status_remains_failed():
    result = FetchResult(
        url="https://example.com", tier=Tier.RAYOBYTE,
        cost_kind=CostKind.RAYOBYTE_CREDIT,
        target_status_code=None, error="Invalid token", elapsed_ms=1,
    )
    assert classify(result) == Decision.FAILED


def test_policy_error_is_policy_rejected_decision():
    result = FetchResult(
        url="https://example.com", tier=Tier.HTTP, cost_kind=CostKind.FREE,
        target_status_code=None, policy_error="non_global_address",
        error="non_global_address: https://example.com", elapsed_ms=1,
    )
    assert classify(result) == Decision.POLICY_REJECTED


def test_provider_failure_is_distinct_from_target_status():
    result = FetchResult(
        url="https://example.com", tier=Tier.RAYOBYTE,
        cost_kind=CostKind.RAYOBYTE_CREDIT,
        target_status_code=None, provider_status_code=401,
        error="Invalid token", elapsed_ms=1,
    )
    assert classify(result) == Decision.PROVIDER_FAILURE


@pytest.mark.parametrize("kind", list(ProviderErrorKind))
def test_every_provider_error_kind_is_provider_failure(kind):
    result = FetchResult(
        url="https://example.com", tier=Tier.RAYOBYTE,
        cost_kind=CostKind.RAYOBYTE_CREDIT,
        target_status_code=None, provider_status_code=429,
        provider_error_kind=kind, provider_error="boom", elapsed_ms=1,
    )
    assert classify(result) == Decision.PROVIDER_FAILURE


def test_provider_rate_limit_is_not_target_rate_limited():
    result = FetchResult(
        url="https://example.com", tier=Tier.RAYOBYTE,
        cost_kind=CostKind.RAYOBYTE_CREDIT,
        target_status_code=None, provider_status_code=429,
        provider_error_kind=ProviderErrorKind.RATE_LIMIT,
        provider_error="rate limited", error="rate limited", elapsed_ms=1,
    )
    assert classify(result) == Decision.PROVIDER_FAILURE
    assert next_tiers(Tier.RAYOBYTE, Decision.PROVIDER_FAILURE, Tier.FIRECRAWL) == [
        Tier.FIRECRAWL,
    ]


def test_cloudflare_skips_datacenter_proxy():
    assert next_tiers(Tier.UNDETECTED, Decision.CLOUDFLARE, Tier.FIRECRAWL) == [
        Tier.CAMOUFOX, Tier.RAYOBYTE, Tier.FIRECRAWL,
    ]


def test_rate_limit_prefers_proxy_before_hosted_services():
    assert next_tiers(Tier.UNDETECTED, Decision.RATE_LIMITED, Tier.FIRECRAWL) == [
        Tier.PROXY, Tier.RAYOBYTE, Tier.FIRECRAWL,
    ]


def test_terminal_has_no_next_tier():
    assert next_tiers(Tier.HTTP, Decision.TERMINAL, Tier.FIRECRAWL) == []


def test_target_network_never_escalates():
    assert next_tiers(Tier.HTTP, Decision.TARGET_NETWORK, Tier.FIRECRAWL) == []
    assert next_tiers(Tier.PROXY, Decision.TARGET_NETWORK, Tier.FIRECRAWL) == []


def test_policy_rejected_has_no_next_tier():
    assert next_tiers(Tier.STEALTH, Decision.POLICY_REJECTED, Tier.FIRECRAWL) == []


def test_provider_failure_falls_back_to_next_available_tier():
    assert next_tiers(Tier.RAYOBYTE, Decision.PROVIDER_FAILURE, Tier.FIRECRAWL) == [
        Tier.FIRECRAWL,
    ]
