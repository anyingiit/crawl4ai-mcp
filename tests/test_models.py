from crawl4ai_mcp.models import (
    AttemptResponse,
    CostKind,
    Decision,
    FetchResult,
    ProviderErrorKind,
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
        provider_error_kind=ProviderErrorKind.AUTH,
        provider_error="Invalid token",
        network_error=None,
        policy_error="non_global_address",
        elapsed_ms=1,
    )
    assert result.target_status_code is None
    assert result.provider_status_code == 401
    assert result.provider_error_kind == ProviderErrorKind.AUTH
    assert result.provider_error == "Invalid token"
    assert result.network_error is None
    assert result.policy_error == "non_global_address"
    assert "status_code" not in result.model_dump()


def test_fetch_result_has_no_ambiguous_status_code_alias():
    result = FetchResult(
        url="https://example.com",
        tier=Tier.HTTP,
        cost_kind=CostKind.FREE,
        target_status_code=200,
        elapsed_ms=1,
    )
    assert not hasattr(result, "status_code")
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
                target_status_code=200,
                elapsed_ms=3,
            )
        ],
    )
    payload = response.model_dump(mode="json")
    assert payload["tier_used"] == "http"
    assert payload["attempts"][0]["tier"] == "undetected"
    assert payload["attempts"][0]["target_status_code"] == 200


def test_attempt_response_carries_provider_failure_separately():
    attempt = AttemptResponse(
        tier="rayobyte",
        decision=Decision.PROVIDER_FAILURE,
        cost_kind=CostKind.RAYOBYTE_CREDIT,
        target_status_code=None,
        provider_status_code=401,
        provider_error_kind=ProviderErrorKind.AUTH,
        provider_error="Invalid token",
        elapsed_ms=3,
        error="Invalid token",
    )
    assert attempt.target_status_code is None
    assert attempt.provider_status_code == 401
    assert attempt.provider_error_kind == ProviderErrorKind.AUTH
    assert attempt.provider_error == "Invalid token"
    payload = attempt.model_dump(mode="json")
    assert payload["target_status_code"] is None
    assert payload["provider_status_code"] == 401
    assert payload["provider_error_kind"] == "auth"


def successful_outcome(
    url: str = "https://example.com/",
    markdown: str = "# ok",
    raw_html: str = "<main>ok</main>",
    effective_url: str | None = None,
) -> ScrapeOutcome:
    response = ScrapeResponse(
        url=url,
        status="success",
        content=markdown,
        tier_used="http",
        cost_kind=CostKind.FREE,
        elapsed_ms=1,
    )
    return ScrapeOutcome(
        response=response,
        raw_html=raw_html,
        effective_url=effective_url or url,
    )


def test_scrape_outcome_keeps_raw_html_out_of_external_response():
    outcome = successful_outcome(raw_html="<a href='/rendered'>x</a>")
    assert outcome.raw_html
    assert "raw_html" not in outcome.response.model_dump(mode="json")


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
