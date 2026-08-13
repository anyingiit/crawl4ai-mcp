import json

import pytest
from fastmcp import Client

from crawl4ai_mcp.server import create_server


class FakeService:
    async def scrape(self, url, max_tier="firecrawl", force_tier=None):
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
        return []

    async def map(self, url, search=None, limit=100):
        return []

    async def diagnose(self, domain=None):
        return {"providers": {}}


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
