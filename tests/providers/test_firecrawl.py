import json
import httpx
import pytest
import respx

from crawl4ai_mcp.models import CostKind, Tier
from crawl4ai_mcp.providers.firecrawl import FirecrawlProvider

API_URL = "https://api.firecrawl.dev/v2/scrape"


@pytest.mark.asyncio
async def test_firecrawl_requests_auto_proxy_and_markdown(respx_mock):
    route = respx_mock.post(API_URL).mock(
        return_value=httpx.Response(200, json={
            "success": True,
            "data": {"markdown": "# Protected", "metadata": {"statusCode": 200}},
        })
    )
    provider = FirecrawlProvider(api_key="fc-test")
    result = await provider.fetch("https://protected.example")
    payload = json.loads(route.calls[0].request.content)
    assert payload == {
        "url": "https://protected.example",
        "formats": ["markdown"],
        "onlyMainContent": True,
        "proxy": "auto",
        "timeout": 60000,
    }
    assert route.calls[0].request.headers["Authorization"] == "Bearer fc-test"
    assert result.markdown == "# Protected"
    assert result.status_code == 200
    assert result.tier == Tier.FIRECRAWL
    assert result.cost_kind == CostKind.FIRECRAWL_CREDIT
    await provider.close()


@pytest.mark.asyncio
async def test_firecrawl_preserves_target_status_code(respx_mock):
    respx_mock.post(API_URL).mock(
        return_value=httpx.Response(200, json={
            "success": True,
            "data": {"markdown": "", "metadata": {"statusCode": 404}},
        })
    )
    provider = FirecrawlProvider(api_key="fc-test")
    result = await provider.fetch("https://example.com/missing")
    assert result.status_code == 404
    await provider.close()


@pytest.mark.parametrize("status", [402, 429, 500, 503])
@pytest.mark.asyncio
async def test_firecrawl_http_errors_are_normalized(respx_mock, status):
    respx_mock.post(API_URL).mock(return_value=httpx.Response(status, json={
        "success": False, "error": f"http {status}",
    }))
    provider = FirecrawlProvider(api_key="fc-test")
    result = await provider.fetch("https://example.com")
    assert result.status_code == status
    assert result.error is not None
    await provider.close()


@pytest.mark.asyncio
async def test_firecrawl_malformed_response_does_not_raise(respx_mock):
    respx_mock.post(API_URL).mock(
        return_value=httpx.Response(200, json={"success": True, "data": None})
    )
    provider = FirecrawlProvider(api_key="fc-test")
    result = await provider.fetch("https://example.com")
    assert result.error is None
    assert result.status_code is None
    await provider.close()


@pytest.mark.asyncio
async def test_firecrawl_unavailable_without_key():
    provider = FirecrawlProvider(api_key=None)
    availability = provider.availability()
    assert availability.ready is False
    assert availability.reason is not None
    result = await provider.fetch("https://example.com")
    assert result.status_code is None
    assert result.error is not None
    await provider.close()
