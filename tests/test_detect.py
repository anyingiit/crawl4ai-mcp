import pytest
from crawl4ai_mcp.detect import classify
from crawl4ai_mcp.models import CostKind, Decision, FetchResult, Tier


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


from crawl4ai_mcp.detect import next_tiers
from crawl4ai_mcp.models import Decision, Tier


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
