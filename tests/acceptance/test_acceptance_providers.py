"""Unpaid tests for the configured-provider acceptance contract.

These never touch the network: they exercise the same skip/assert helpers the
live tier tests use, and the cascade pipeline with an injected fake provider
that returns a quota failure.

Contract under test (from the acceptance review):
- a tier disabled in the deployment config skips even when its key is set;
- an enabled, configured provider that fails (e.g. quota exhaustion) is an
  acceptance failure, never a skip;
- an unconfigured optional provider may skip;
- an enabled, configured but unavailable provider must NOT skip.
"""

import pytest

from crawl4ai_mcp.config import AppConfig, load_config
from crawl4ai_mcp.models import (
    CostKind,
    FetchResult,
    ProviderAvailability,
    ProviderErrorKind,
    Tier,
)
from crawl4ai_mcp.service import CrawlService

from acceptance_helpers import (
    assert_configured_provider_success,
    configured_provider_skip_reason,
)

PUBLIC_IP_URL = "http://93.184.216.34/"


class FakeOptionalProvider:
    tier: Tier
    cost_kind: CostKind
    enabled = True
    ready = True
    reason = None

    def availability(self) -> ProviderAvailability:
        return ProviderAvailability(
            enabled=self.enabled, ready=self.ready, reason=self.reason
        )

    async def close(self) -> None:
        return None


class FakeFirecrawl(FakeOptionalProvider):
    tier = Tier.FIRECRAWL
    cost_kind = CostKind.FIRECRAWL_CREDIT

    def __init__(self, ready=True, reason=None, enabled=True):
        self.ready = ready
        self.reason = reason
        self.enabled = enabled

    async def fetch(self, url: str) -> FetchResult:
        return FetchResult(
            url=url,
            tier=self.tier,
            cost_kind=self.cost_kind,
            provider_status_code=402,
            provider_error_kind=ProviderErrorKind.QUOTA,
            provider_error="Insufficient credits",
            error="Insufficient credits",
            elapsed_ms=1,
        )


def test_firecrawl_disabled_in_config_skips_despite_key():
    config = AppConfig(
        firecrawl_api_key="configured-key",
        enabled_tiers=[tier for tier in Tier if tier != Tier.FIRECRAWL],
    )
    reason = configured_provider_skip_reason(
        None, Tier.FIRECRAWL, config.enabled_tiers
    )
    assert reason is not None
    assert "disabled" in reason


def test_disabled_provider_skips_even_when_ready():
    provider = FakeFirecrawl()
    reason = configured_provider_skip_reason(
        provider, Tier.FIRECRAWL, [Tier.HTTP]
    )
    assert reason is not None
    assert "disabled" in reason


def test_unconfigured_optional_provider_may_skip():
    provider = FakeFirecrawl(ready=False, reason="firecrawl api key not configured")
    reason = configured_provider_skip_reason(
        provider, Tier.FIRECRAWL, list(Tier)
    )
    assert reason is not None
    assert "unconfigured" in reason


def test_enabled_configured_unavailable_does_not_skip():
    provider = FakeFirecrawl(ready=False, reason="firecrawl service unreachable")
    assert configured_provider_skip_reason(
        provider, Tier.FIRECRAWL, list(Tier)
    ) is None


@pytest.mark.asyncio
async def test_fixture_loads_deployment_config_and_env(tmp_path):
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        'enabled_tiers = ["http", "stealth", "undetected", "camoufox",'
        ' "proxy", "rayobyte"]\n',
        encoding="utf-8",
    )
    config = load_config(
        cfg,
        env={
            "FIRECRAWL_API_KEY": "configured-key",
            "RAYOBYTE_API_KEY": "configured-key",
            "RAYOBYTE_API_URL": "https://api.scraping.rayobyte.com/",
        },
    )
    config = config.model_copy(update={"database_path": tmp_path / "policy.db"})
    svc = CrawlService(config)
    await svc.start()
    try:
        assert Tier.FIRECRAWL not in svc.providers
        assert svc.config.firecrawl_api_key == "configured-key"
        assert svc.providers[Tier.RAYOBYTE].availability().ready
    finally:
        await svc.close()


@pytest.mark.asyncio
async def test_enabled_configured_quota_remains_failure_without_network(tmp_path):
    config = AppConfig(
        database_path=tmp_path / "policy.db",
        firecrawl_api_key="configured",
        enabled_tiers=[Tier.FIRECRAWL],
    )
    provider = FakeFirecrawl()
    svc = CrawlService(config, providers={Tier.FIRECRAWL: provider})
    await svc.start()
    try:
        assert (
            configured_provider_skip_reason(
                provider, Tier.FIRECRAWL, svc.config.enabled_tiers
            )
            is None
        )
        result = await svc.scrape(
            PUBLIC_IP_URL, max_tier="firecrawl", force_tier="firecrawl"
        )
        assert result["status"] == "failed"
        attempt = result["attempts"][0]
        assert attempt["tier"] == "firecrawl"
        assert attempt["provider_error_kind"] == "quota"
        with pytest.raises(AssertionError):
            assert_configured_provider_success(
                result, Tier.FIRECRAWL, "firecrawl_credit", "Example Domain"
            )
    finally:
        await svc.close()
