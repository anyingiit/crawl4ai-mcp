import pytest
from pydantic import ValidationError

from crawl4ai_mcp.models import (
    AttemptResponse,
    BrowserState,
    CostKind,
    Decision,
    DiagnoseDomainPolicy,
    DiagnoseResponse,
    FetchResult,
    MapResponse,
    ProviderAvailability,
    ProviderErrorKind,
    RecentFailure,
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


def test_scrape_response_serializes_tier_names_not_ints():
    response = ScrapeResponse(
        url="https://example.com",
        status="success",
        content="# ok",
        tier_used="undetected",
        cost_kind=CostKind.FREE,
        elapsed_ms=3,
        attempts=[
            AttemptResponse(
                tier="undetected",
                decision=Decision.SUCCESS,
                cost_kind=CostKind.FREE,
                elapsed_ms=3,
            )
        ],
    )
    payload = response.model_dump(mode="json")
    assert payload["tier_used"] == "undetected"
    assert payload["attempts"][0]["tier"] == "undetected"


def test_scrape_response_and_attempt_reject_integer_tiers():
    with pytest.raises(ValidationError):
        ScrapeResponse(
            url="https://example.com", status="success", tier_used=0, elapsed_ms=1
        )
    with pytest.raises(ValidationError):
        AttemptResponse(
            tier=0, decision=Decision.SUCCESS, cost_kind=CostKind.FREE, elapsed_ms=1
        )


def test_map_response_is_typed_urls_list():
    response = MapResponse(urls=["https://example.com/a", "https://example.com/b"])
    assert set(response.model_dump(mode="json")) == {"urls"}
    assert response.urls == ["https://example.com/a", "https://example.com/b"]


def test_diagnose_response_serializes_domain_tier_as_lowercase_name():
    response = DiagnoseResponse(
        rss_bytes=1024,
        providers={"HTTP": ProviderAvailability(enabled=True, ready=True)},
        browsers={
            "HTTP": BrowserState(active=False, active_fetches=0, last_used=0.0)
        },
        recent_failures=[
            RecentFailure(
                url="https://example.com",
                time=1,
                error="all tiers failed",
                attempts=["http", "stealth"],
            )
        ],
        domain_policies=[
            DiagnoseDomainPolicy(domain="example.com", best_tier="stealth", updated_at=2)
        ],
    )
    payload = response.model_dump(mode="json")
    assert payload["domain_policies"][0]["best_tier"] == "stealth"
    assert payload["browsers"]["HTTP"]["active"] is False
    assert payload["recent_failures"][0]["attempts"] == ["http", "stealth"]


def test_recent_failure_attempts_are_validated_tier_names():
    failure = RecentFailure(
        url="https://example.com", time=1, attempts=["http", "rayobyte"]
    )
    assert failure.attempts == ["http", "rayobyte"]
    with pytest.raises(ValidationError):
        RecentFailure(url="https://example.com", time=1, attempts=["bogus"])
    with pytest.raises(ValidationError):
        RecentFailure(url="https://example.com", time=1, attempts=[0])


def test_dead_integer_tier_models_are_removed():
    import crawl4ai_mcp.models as models

    assert not hasattr(models, "ScrapeResult")
    assert not hasattr(models, "Attempt")
