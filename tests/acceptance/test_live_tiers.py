"""Opt-in live tests against real providers and targets.

Run with: CRAWL4AI_MCP_LIVE_TESTS=1 scripts/run-acceptance.sh
(or: CRAWL4AI_MCP_LIVE_TESTS=1 .venv/bin/pytest tests/acceptance -v)

Every test uses a fresh temporary policy database so domain memory from
earlier runs cannot mask a failure.

Skip policy (acceptance markers):
- acceptance_required tests must pass; a skip here means incomplete acceptance.
- Camoufox, the datacenter proxy tier, Rayobyte, and Firecrawl are
  acceptance_optional and skip ONLY when disabled or unconfigured. When
  enabled/configured they must assert availability.ready, a successful
  fetch, the exact tier, the exact cost kind, and the target marker.
  Credit-exhaustion and proxied-fetch-failure skips are intentionally
  absent: a configured provider that fails is an acceptance failure.
"""

import os
import re
import tomllib
from pathlib import Path

import pytest
from dotenv import dotenv_values
from fastmcp import Client

from crawl4ai_mcp.config import AppConfig
from crawl4ai_mcp.models import Tier
from crawl4ai_mcp.service import CrawlService

LIVE = os.environ.get("CRAWL4AI_MCP_LIVE_TESTS") == "1"

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(not LIVE, reason="set CRAWL4AI_MCP_LIVE_TESTS=1 to run"),
]

ROOT = Path(__file__).resolve().parents[2]
TARGETS = tomllib.loads((ROOT / "tests" / "acceptance" / "targets.toml").read_text())
ENV = dotenv_values(ROOT / ".env")

SERVICE_URL = "http://127.0.0.1:11236/mcp"


@pytest.fixture
async def service(tmp_path):
    config = AppConfig(
        database_path=tmp_path / "policy.db",
        webshare_proxies=[
            p for p in (ENV.get("WEBSHARE_PROXIES") or "").split(",") if p
        ],
        oxylabs_proxies=[
            p for p in (ENV.get("OXYLABS_PROXIES") or "").split(",") if p
        ],
        rayobyte_api_url=ENV.get("RAYOBYTE_API_URL"),
        rayobyte_api_key=ENV.get("RAYOBYTE_API_KEY"),
        firecrawl_api_key=ENV.get("FIRECRAWL_API_KEY"),
    )
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
    provider = service.providers[Tier.CAMOUFOX]
    availability = provider.availability()
    if not availability.enabled:
        pytest.skip(f"camoufox disabled: {availability.reason}")
    assert availability.ready, f"camoufox enabled but unavailable: {availability.reason}"
    target = TARGETS["js_quotes"]
    result = await service.scrape(
        target["url"], max_tier="camoufox", force_tier="camoufox"
    )
    assert result["status"] == "success", result.get("error")
    assert result["tier_used"] == Tier.CAMOUFOX.name.lower()
    assert result["cost_kind"] == "free"
    assert target["marker"] in result["content"]


@pytest.mark.acceptance_optional
@pytest.mark.asyncio
async def test_tier4_proxy_when_configured(service):
    provider = service.providers[Tier.PROXY]
    availability = provider.availability()
    if not availability.ready:
        pytest.skip(f"proxy tier unconfigured: {availability.reason}")
    assert availability.ready, f"proxy tier enabled but unavailable: {availability.reason}"
    target = TARGETS["proxy_ip_check"]
    direct = await service.scrape(
        target["url"], max_tier="http", force_tier="http"
    )
    proxied = await service.scrape(
        target["url"], max_tier="proxy", force_tier="proxy"
    )
    assert direct["status"] == "success", direct.get("error")
    assert proxied["status"] == "success", proxied.get("error")
    assert proxied["tier_used"] == Tier.PROXY.name.lower()
    assert proxied["cost_kind"] == "proxy_bandwidth"
    assert re.search(target["marker"], direct["content"]), "direct IP body malformed"
    assert re.search(target["marker"], proxied["content"]), "proxied IP body malformed"

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
    provider = service.providers[Tier.RAYOBYTE]
    availability = provider.availability()
    if not availability.ready:
        pytest.skip(f"rayobyte unconfigured: {availability.reason}")
    assert availability.ready, f"rayobyte configured but unavailable: {availability.reason}"
    result = await service.scrape(
        "https://example.com/", max_tier="rayobyte", force_tier="rayobyte"
    )
    assert result["status"] == "success", result.get("error")
    assert result["tier_used"] == Tier.RAYOBYTE.name.lower()
    assert result["cost_kind"] == "rayobyte_credit"
    assert "Example Domain" in result["content"]


@pytest.mark.acceptance_optional
@pytest.mark.asyncio
async def test_tier6_firecrawl_when_configured(service):
    provider = service.providers[Tier.FIRECRAWL]
    availability = provider.availability()
    if not availability.ready:
        pytest.skip(f"firecrawl unconfigured: {availability.reason}")
    assert availability.ready, f"firecrawl configured but unavailable: {availability.reason}"
    result = await service.scrape(
        "https://example.com/", max_tier="firecrawl", force_tier="firecrawl"
    )
    assert result["status"] == "success", result.get("error")
    assert result["tier_used"] == Tier.FIRECRAWL.name.lower()
    assert result["cost_kind"] == "firecrawl_credit"
    assert "Example Domain" in result["content"]


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
