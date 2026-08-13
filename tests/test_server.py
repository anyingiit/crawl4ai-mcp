import json

import pytest
from fastmcp import Client

from crawl4ai_mcp.server import create_server


class FakeService:
    async def scrape(self, url, format="markdown", max_tier="firecrawl", force_tier=None):
        return {
            "url": url,
            "status": "success",
            "content": "# Protected",
            "tier_used": "http",
            "cost_kind": "free",
            "elapsed_ms": 12,
            "attempts": [
                {"tier": "http", "decision": "success", "cost_kind": "free",
                 "elapsed_ms": 12}
            ],
        }

    async def crawl(self, url, max_pages=10, max_depth=2, include_pattern=None):
        return {
            "pages": [],
            "stats": {
                "attempted_pages": 0,
                "successful_pages": 0,
                "failed_pages": 0,
                "max_depth_reached": 0,
                "elapsed_ms": 0,
            },
        }

    async def map(self, url, search=None, limit=100):
        return []

    async def diagnose(self, domain=None):
        return {
            "rss_bytes": 1,
            "providers": {},
            "browsers": {},
            "recent_failures": [],
            "domain_policies": [],
        }


@pytest.fixture
async def client():
    mcp = create_server(FakeService())
    client = Client(mcp)
    async with client:
        yield client


@pytest.mark.asyncio
async def test_exactly_four_tools_with_defaults(client):
    tools = await client.list_tools()
    names = {tool.name for tool in tools}
    assert names == {"scrape", "crawl", "map", "diagnose"}

    by_name = {tool.name: tool for tool in tools}

    scrape_props = by_name["scrape"].inputSchema["properties"]
    assert "url" in scrape_props and scrape_props["url"].get("type") == "string"
    assert scrape_props["format"].get("default") == "markdown"
    assert scrape_props["max_tier"].get("default") == "firecrawl"
    assert scrape_props["force_tier"].get("default") is None

    crawl_props = by_name["crawl"].inputSchema["properties"]
    assert crawl_props["max_pages"].get("default") == 10
    assert crawl_props["max_depth"].get("default") == 2
    assert crawl_props["include_pattern"].get("default") is None

    map_props = by_name["map"].inputSchema["properties"]
    assert map_props["limit"].get("default") == 100
    assert map_props["search"].get("default") is None

    diagnose_props = by_name["diagnose"].inputSchema["properties"]
    assert "domain" in diagnose_props


@pytest.mark.asyncio
async def test_exactly_four_tools_with_explicit_output_schemas(client):
    tools = {tool.name: tool for tool in await client.list_tools()}
    assert set(tools) == {"scrape", "crawl", "map", "diagnose"}
    assert tools["scrape"].inputSchema["properties"]["format"]["enum"] == ["markdown", "html"]
    assert set(tools["crawl"].outputSchema["properties"]) == {"pages", "stats"}
    assert set(tools["map"].outputSchema["properties"]) == {"urls"}
    assert tools["map"].inputSchema["properties"]["limit"]["minimum"] == 1
    assert tools["map"].inputSchema["properties"]["limit"]["maximum"] == 100


@pytest.mark.asyncio
async def test_tier_strings_and_bounds_constrained_in_schema(client):
    tools = {tool.name: tool for tool in await client.list_tools()}
    expected_tiers = ["http", "stealth", "undetected", "camoufox", "proxy", "rayobyte", "firecrawl"]
    scrape_props = tools["scrape"].inputSchema["properties"]
    assert scrape_props["max_tier"]["enum"] == expected_tiers
    force_enum = scrape_props["force_tier"]["anyOf"][0]["enum"]
    assert force_enum == expected_tiers
    crawl_props = tools["crawl"].inputSchema["properties"]
    assert crawl_props["max_pages"]["minimum"] == 1
    assert crawl_props["max_pages"]["maximum"] == 100
    assert crawl_props["max_depth"]["minimum"] == 1
    assert crawl_props["max_depth"]["maximum"] == 5


@pytest.mark.asyncio
async def test_scrape_via_mcp_client_preserves_structure(client):
    result = await client.call_tool(
        "scrape", {"url": "https://protected.example"}
    )
    text_content = result.content[0].text if hasattr(result.content[0], "text") else ""
    payload = json.loads(text_content)
    assert payload["status"] == "success"
    assert payload["tier_used"] == "http"
    assert payload["cost_kind"] == "free"
    assert payload["elapsed_ms"] == 12
    assert payload["content"] == "# Protected"
    assert payload["attempts"][0]["tier"] == "http"
