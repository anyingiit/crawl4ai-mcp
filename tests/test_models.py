from crawl4ai_mcp.models import (
    AttemptResponse,
    CostKind,
    Decision,
    FetchResult,
    ScrapeOutcome,
    ScrapeResponse,
    Tier,
)


def test_tiers_are_strictly_ordered():
    assert Tier.HTTP < Tier.STEALTH < Tier.UNDETECTED < Tier.CAMOUFOX
    assert Tier.CAMOUFOX < Tier.PROXY < Tier.RAYOBYTE < Tier.FIRECRAWL


def test_fetch_result_preserves_raw_response():
    result = FetchResult(
        url="https://example.com",
        tier=Tier.HTTP,
        cost_kind=CostKind.FREE,
        target_status_code=200,
        html="<main>Hello</main>",
        headers={"content-type": "text/html"},
        elapsed_ms=42,
    )
    assert result.html == "<main>Hello</main>"
    assert result.markdown is None


def test_fetch_result_separates_target_provider_and_policy_errors():
    result = FetchResult(
        url="https://example.com",
        tier=Tier.HTTP,
        cost_kind=CostKind.FREE,
        target_status_code=None,
        provider_status_code=401,
        network_error=None,
        policy_error="non_global_address",
        elapsed_ms=1,
    )
    assert result.target_status_code is None
    assert result.provider_status_code == 401
    assert result.network_error is None
    assert result.policy_error == "non_global_address"
    assert result.status_code is None


def test_deprecated_status_code_keyword_maps_to_target_status():
    result = FetchResult(
        url="https://example.com",
        tier=Tier.HTTP,
        cost_kind=CostKind.FREE,
        status_code=200,
        elapsed_ms=1,
    )
    assert result.target_status_code == 200
    assert result.status_code == 200
    assert "status_code" not in result.model_dump()


def test_scrape_response_serializes_lowercase_tier_names():
    response = ScrapeResponse(
        url="https://example.com",
        status="success",
        content="# ok",
        tier_used="http",
        cost_kind=CostKind.FREE,
        elapsed_ms=3,
        attempts=[
            AttemptResponse(
                tier="undetected",
                decision=Decision.SUCCESS,
                cost_kind=CostKind.FREE,
                status_code=200,
                elapsed_ms=3,
            )
        ],
    )
    payload = response.model_dump(mode="json")
    assert payload["tier_used"] == "http"
    assert payload["attempts"][0]["tier"] == "undetected"


def test_scrape_outcome_is_frozen_and_slotted():
    response = ScrapeResponse(
        url="https://example.com",
        status="success",
        elapsed_ms=1,
    )
    outcome = ScrapeOutcome(
        response=response, raw_html="<main>x</main>", effective_url="https://example.com/"
    )
    assert outcome.raw_html == "<main>x</main>"
    assert outcome.effective_url == "https://example.com/"
    assert not hasattr(outcome, "__dict__")
    try:
        outcome.raw_html = "other"
    except Exception as exc:
        assert isinstance(exc, Exception)
    else:
        raise AssertionError("ScrapeOutcome must be frozen")
