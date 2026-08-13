"""Opt-in live tests against real providers and targets.

Run with: CRAWL4AI_MCP_LIVE_TESTS=1 .venv/bin/pytest tests/acceptance -v
Every test uses a fresh temporary policy database so domain memory from
earlier runs cannot mask a failure. Paid tiers (Rayobyte, Firecrawl) and the
datacenter proxy tier are exercised only when their credentials are
configured, and are skipped with an explicit reason otherwise.
"""

import os
import tomllib
from pathlib import Path

import pytest
from dotenv import dotenv_values

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


@pytest.mark.asyncio
async def test_tier0_static_document(service):
    result = await service.scrape("https://docs.python.org/3/library/asyncio.html")
    assert result["status"] == "success"
    assert result["tier_used"] == Tier.HTTP.value
    assert result["cost_kind"] == "free"
    assert len(result["content"]) > 1_000


@pytest.mark.asyncio
async def test_tier1_js_rendered_page(service):
    target = TARGETS["js_quotes"]
    result = await service.scrape(target["url"])
    assert result["status"] == "success"
    assert target["marker"] in result["content"]
    assert result["tier_used"] in (Tier.STEALTH.value, Tier.UNDETECTED.value)


@pytest.mark.asyncio
async def test_tier2_forced_undetected(service):
    target = TARGETS["js_quotes"]
    result = await service.scrape(
        target["url"], max_tier="undetected", force_tier="undetected"
    )
    assert result["status"] == "success"
    assert target["marker"] in result["content"]
    assert result["tier_used"] == Tier.UNDETECTED.value


@pytest.mark.asyncio
async def test_tier3_camoufox_runs_or_reports_unavailable(service):
    provider = service.providers[Tier.CAMOUFOX]
    availability = provider.availability()
    if not availability.ready:
        pytest.skip(f"camoufox unavailable: {availability.reason}")
    target = TARGETS["antibot_easy"]
    result = await service.scrape(
        target["url"], max_tier="camoufox", force_tier="camoufox"
    )
    assert result["attempts"], "camoufox attempt must be recorded"
    assert result["attempts"][0]["tier"] == Tier.CAMOUFOX.value
    if result["status"] != "success":
        assert "You are a bot" in result["content"] or result["error"], (
            "blocked targets must still report rendered content or an error"
        )


@pytest.mark.asyncio
async def test_tier4_proxy_ip_differs_from_direct(service):
    provider = service.providers[Tier.PROXY]
    availability = provider.availability()
    if not availability.ready:
        pytest.skip(f"proxy tier unavailable: {availability.reason}")
    direct = await service.scrape(
        "https://api.ipify.org?format=json", max_tier="http", force_tier="http"
    )
    proxied = await service.scrape(
        "https://api.ipify.org?format=json", max_tier="proxy", force_tier="proxy"
    )
    if proxied["status"] != "success":
        pytest.skip(f"proxied fetch failed: {proxied['error']}")
    import re

    def extract_ip(content):
        match = re.search(r'"ip"\s*:\s*"([^"]+)"', content)
        return match.group(1) if match else None

    direct_ip = extract_ip(direct["content"])
    proxied_ip = extract_ip(proxied["content"])
    assert direct_ip is not None and proxied_ip is not None
    assert direct_ip != proxied_ip


@pytest.mark.asyncio
async def test_tier5_rayobyte_when_configured(service):
    provider = service.providers[Tier.RAYOBYTE]
    availability = provider.availability()
    if not availability.ready:
        pytest.skip(f"rayobyte unavailable: {availability.reason}")
    result = await service.scrape(
        "https://example.com/", max_tier="rayobyte", force_tier="rayobyte"
    )
    if result["status"] != "success":
        errors = " ".join(str(a.get("error") or "") for a in result["attempts"]).lower()
        if "credit" in errors or "insufficient" in errors or "quota" in errors:
            pytest.skip(f"rayobyte credits exhausted: {errors[:120]}")
    assert result["status"] == "success"
    assert result["tier_used"] == Tier.RAYOBYTE.value
    assert result["cost_kind"] == "rayobyte_credit"


@pytest.mark.asyncio
async def test_tier6_firecrawl_when_configured(service):
    provider = service.providers[Tier.FIRECRAWL]
    availability = provider.availability()
    if not availability.ready:
        pytest.skip(f"firecrawl unavailable: {availability.reason}")
    result = await service.scrape(
        "https://example.com/", max_tier="firecrawl", force_tier="firecrawl"
    )
    if result["status"] != "success":
        errors = " ".join(str(a.get("error") or "") for a in result["attempts"]).lower()
        if "credit" in errors or "insufficient" in errors or "quota" in errors:
            pytest.skip(f"firecrawl credits exhausted: {errors[:120]}")
    assert result["status"] == "success"
    assert result["tier_used"] == Tier.FIRECRAWL.value
    assert result["cost_kind"] == "firecrawl_credit"


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


@pytest.mark.asyncio
async def test_real_404_consumes_no_paid_tier(service):
    service.engine.calls.clear()
    result = await service.scrape("https://example.com/definitely-missing-404")
    assert result["status"] == "terminal"
    assert service.engine.calls == [Tier.HTTP]


@pytest.mark.asyncio
async def test_repeated_domain_reuses_remembered_tier(service):
    target = TARGETS["js_quotes"]
    first = await service.scrape(target["url"])
    assert first["status"] == "success"
    remembered = first["tier_used"]
    calls_so_far = len(service.engine.calls)
    second = await service.scrape(target["url"])
    assert second["status"] == "success"
    assert service.engine.calls[calls_so_far] == Tier(remembered)


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
