from crawl4ai_mcp.models import CostKind, FetchResult, Tier


def test_tiers_are_strictly_ordered():
    assert Tier.HTTP < Tier.STEALTH < Tier.UNDETECTED < Tier.CAMOUFOX
    assert Tier.CAMOUFOX < Tier.PROXY < Tier.RAYOBYTE < Tier.FIRECRAWL


def test_fetch_result_preserves_raw_response():
    result = FetchResult(
        url="https://example.com",
        tier=Tier.HTTP,
        cost_kind=CostKind.FREE,
        status_code=200,
        html="<main>Hello</main>",
        headers={"content-type": "text/html"},
        elapsed_ms=42,
    )
    assert result.html == "<main>Hello</main>"
    assert result.markdown is None
