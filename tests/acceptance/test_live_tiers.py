"""Opt-in live tests against real providers and targets.

Run with: CRAWL4AI_MCP_LIVE_TESTS=1 scripts/run-acceptance.sh
(or: CRAWL4AI_MCP_LIVE_TESTS=1 .venv/bin/pytest tests/acceptance -v)

Every test uses a fresh temporary policy database so domain memory from
earlier runs cannot mask a failure. The service fixture loads the same
deployment config (config.toml) and environment (.env) as the systemd unit,
overriding only the database path, so `enabled_tiers` in the deployment
config governs which tiers acceptance exercises.

Skip policy (acceptance markers):
- acceptance_required tests must pass; a skip here means incomplete acceptance.
- Camoufox, the datacenter proxy tier, Rayobyte, and Firecrawl are
  acceptance_optional and skip ONLY when intentionally disabled (not in
  config enabled_tiers / provider disabled) or unconfigured (no credentials
  or proxies). When enabled/configured they must assert availability.ready,
  a successful fetch, the exact tier, the exact cost kind, and the target
  marker. Credit-exhaustion and proxied-fetch-failure skips are
  intentionally absent: a configured provider that fails is an acceptance
  failure.
"""

import os
import re
import tomllib
from pathlib import Path

import pytest
from dotenv import dotenv_values
from fastmcp import Client

from crawl4ai_mcp.config import load_config
from crawl4ai_mcp.models import Tier
from crawl4ai_mcp.service import CrawlService

from acceptance_helpers import (
    assert_configured_provider_success,
    configured_provider_skip_reason,
)

LIVE = os.environ.get("CRAWL4AI_MCP_LIVE_TESTS") == "1"

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(not LIVE, reason="set CRAWL4AI_MCP_LIVE_TESTS=1 to run"),
]

ROOT = Path(__file__).resolve().parents[2]
TARGETS = tomllib.loads((ROOT / "tests" / "acceptance" / "targets.toml").read_text())

SERVICE_URL = "http://127.0.0.1:11236/mcp"


@pytest.fixture
async def service(tmp_path):
    config = load_config(
        ROOT / "config.toml",
        env=dotenv_values(ROOT / ".env"),
    )
    config = config.model_copy(update={"database_path": tmp_path / "policy.db"})
    svc = CrawlService(config)
    await svc.start()
    yield svc
    await svc.close()


@pytest.fixture
async def remote_client():
    async with Client(SERVICE_URL) as client:
        yield client


@pytest.mark.acceptance_required
@pytest.mark.asyncio
async def test_tier0_static_document(service):
    result = await service.scrape("https://docs.python.org/3/library/asyncio.html")
    assert result["status"] == "success"
    assert result["tier_used"] == Tier.HTTP.name.lower()
    assert result["cost_kind"] == "free"
    assert len(result["content"]) > 1_000


@pytest.mark.acceptance_required
@pytest.mark.asyncio
async def test_tier1_js_rendered_page(service):
    target = TARGETS["js_quotes"]
    result = await service.scrape(target["url"])
    assert result["status"] == "success"
    assert target["marker"] in result["content"]
    assert result["tier_used"] in (
        Tier.STEALTH.name.lower(),
        Tier.UNDETECTED.name.lower(),
    )


@pytest.mark.acceptance_required
@pytest.mark.asyncio
async def test_tier2_forced_undetected(service):
    target = TARGETS["js_quotes"]
    result = await service.scrape(
        target["url"], max_tier="undetected", force_tier="undetected"
    )
    assert result["status"] == "success"
    assert target["marker"] in result["content"]
    assert result["tier_used"] == Tier.UNDETECTED.name.lower()


@pytest.mark.acceptance_optional
@pytest.mark.asyncio
async def test_tier3_camoufox_when_configured(service):
    tier = Tier.CAMOUFOX
    provider = service.providers.get(tier)
    reason = configured_provider_skip_reason(
        provider, tier, service.config.enabled_tiers
    )
    if reason is not None:
        pytest.skip(reason)
    assert provider.availability().ready, f"{tier.name.lower()} configured but unavailable"
    target = TARGETS["js_quotes"]
    result = await service.scrape(
        target["url"], max_tier="camoufox", force_tier="camoufox"
    )
    assert_configured_provider_success(result, tier, "free", target["marker"])


@pytest.mark.acceptance_optional
@pytest.mark.asyncio
async def test_tier4_proxy_when_configured(service):
    tier = Tier.PROXY
    provider = service.providers.get(tier)
    reason = configured_provider_skip_reason(
        provider, tier, service.config.enabled_tiers
    )
    if reason is not None:
        pytest.skip(reason)
    assert provider.availability().ready, f"{tier.name.lower()} configured but unavailable"
    target = TARGETS["proxy_ip_check"]
    direct = await service.scrape(
        target["url"], max_tier="http", force_tier="http"
    )
    proxied = await service.scrape(
        target["url"], max_tier="proxy", force_tier="proxy"
    )
    assert direct["status"] == "success", direct.get("error")
    assert_configured_provider_success(
        proxied, tier, "proxy_bandwidth", target["marker"], marker_is_regex=True
    )

    def extract_ip(content):
        match = re.search(r'"ip"\s*:\s*"([^"]+)"', content)
        return match.group(1) if match else None

    direct_ip = extract_ip(direct["content"])
    proxied_ip = extract_ip(proxied["content"])
    assert direct_ip is not None and proxied_ip is not None
    assert direct_ip != proxied_ip


@pytest.mark.acceptance_optional
@pytest.mark.asyncio
async def test_tier5_rayobyte_when_configured(service):
    tier = Tier.RAYOBYTE
    provider = service.providers.get(tier)
    reason = configured_provider_skip_reason(
        provider, tier, service.config.enabled_tiers
    )
    if reason is not None:
        pytest.skip(reason)
    assert provider.availability().ready, f"{tier.name.lower()} configured but unavailable"
    result = await service.scrape(
        "https://example.com/", max_tier="rayobyte", force_tier="rayobyte"
    )
    assert_configured_provider_success(result, tier, "rayobyte_credit", "Example Domain")


@pytest.mark.acceptance_optional
@pytest.mark.asyncio
async def test_tier6_firecrawl_when_configured(service):
    tier = Tier.FIRECRAWL
    provider = service.providers.get(tier)
    reason = configured_provider_skip_reason(
        provider, tier, service.config.enabled_tiers
    )
    if reason is not None:
        pytest.skip(reason)
    assert provider.availability().ready, f"{tier.name.lower()} configured but unavailable"
    result = await service.scrape(
        "https://example.com/", max_tier="firecrawl", force_tier="firecrawl"
    )
    assert_configured_provider_success(result, tier, "firecrawl_credit", "Example Domain")


@pytest.mark.acceptance_required
@pytest.mark.asyncio
async def test_live_service_rejects_loopback_and_file_urls(remote_client):
    for url in ("http://127.0.0.1/", "file:///etc/passwd"):
        with pytest.raises(Exception):
            await remote_client.call_tool("scrape", {"url": url})


@pytest.mark.acceptance_required
@pytest.mark.asyncio
async def test_network_failure_does_not_reach_paid_tiers(service):
    result = await service.scrape(TARGETS["unreachable_public"]["url"])
    assert len(result["attempts"]) == 2
    assert result["attempts"][0]["tier"] == result["attempts"][1]["tier"]
    assert not {"rayobyte", "firecrawl"} & {
        attempt["tier"] for attempt in result["attempts"]
    }


@pytest.mark.acceptance_required
@pytest.mark.asyncio
async def test_cloudflare_route_skips_proxy_tier(service):
    target = TARGETS["classify_blocked"]
    service.engine.calls.clear()
    result = await service.scrape(
        target["url"], max_tier="camoufox"
    )
    assert result["status"] == "failed"
    assert Tier.PROXY not in service.engine.calls
    assert Tier.RAYOBYTE not in service.engine.calls
    assert Tier.FIRECRAWL not in service.engine.calls


@pytest.mark.acceptance_required
@pytest.mark.asyncio
async def test_real_404_consumes_no_paid_tier(service):
    service.engine.calls.clear()
    result = await service.scrape("https://example.com/definitely-missing-404")
    assert result["status"] == "terminal"
    assert service.engine.calls == [Tier.HTTP]


@pytest.mark.acceptance_required
@pytest.mark.asyncio
async def test_repeated_domain_reuses_remembered_tier(service):
    target = TARGETS["js_quotes"]
    first = await service.scrape(target["url"])
    assert first["status"] == "success"
    remembered = first["tier_used"]
    calls_so_far = len(service.engine.calls)
    second = await service.scrape(target["url"])
    assert second["status"] == "success"
    assert service.engine.calls[calls_so_far] == Tier[remembered.upper()]


@pytest.mark.acceptance_required
@pytest.mark.asyncio
async def test_repeated_failure_enters_cooldown(service):
    target = TARGETS["server_error"]
    first = await service.scrape(target["url"], max_tier="http")
    assert first["status"] == "failed"
    assert service.engine.calls == [Tier.HTTP]
    service.engine.calls.clear()
    second = await service.scrape(target["url"], max_tier="http")
    assert second["status"] == "cooldown"
    assert service.engine.calls == []
