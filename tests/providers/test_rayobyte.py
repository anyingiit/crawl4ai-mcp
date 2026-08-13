import json
import httpx
import pytest
import respx

from crawl4ai_mcp.models import CostKind, Tier
from crawl4ai_mcp.providers.rayobyte import RayobyteProvider

API_URL = "https://api.scraping.rayobyte.com/"


def success_body(http_code=200, html="<main>Hello</main>"):
    return {
        "status": "SUCCESS",
        "date": "Thu, 13 Aug 2026 10:02:17 GMT",
        "httpCode": http_code,
        "headers": {"user-agent": "Mozilla/5.0"},
        "taskId": "768ce64a-a952-4e24-af2d-d81e78f55725",
        "result": html,
    }


def fail_body(status_code=401, error="Invalid or blocked token: x"):
    return {"status": "FAIL", "statusCode": status_code, "error": error}


@pytest.mark.asyncio
async def test_rayobyte_success_returns_rendered_html(respx_mock):
    route = respx_mock.get(API_URL).mock(
        return_value=httpx.Response(200, json=success_body())
    )
    provider = RayobyteProvider(api_url=API_URL, api_key="rb-test")
    result = await provider.fetch("https://example.com")
    assert result.status_code == 200
    assert "<main>Hello</main>" in result.html
    assert result.tier == Tier.RAYOBYTE
    assert result.cost_kind == CostKind.RAYOBYTE_CREDIT
    params = route.calls[0].request.url.params
    assert params["token"] == "rb-test"
    assert params["url"] == "https://example.com"
    await provider.close()


@pytest.mark.asyncio
async def test_rayobyte_preserves_target_404(respx_mock):
    respx_mock.get(API_URL).mock(
        return_value=httpx.Response(200, json=success_body(http_code=404, html="missing"))
    )
    provider = RayobyteProvider(api_url=API_URL, api_key="rb-test")
    result = await provider.fetch("https://example.com/missing")
    assert result.status_code == 404
    assert result.error is None
    await provider.close()


@pytest.mark.asyncio
async def test_rayobyte_invalid_token_is_normalized(respx_mock):
    respx_mock.get(API_URL).mock(
        return_value=httpx.Response(200, json=fail_body(401))
    )
    provider = RayobyteProvider(api_url=API_URL, api_key="rb-bad")
    result = await provider.fetch("https://example.com")
    assert result.status_code == 401
    assert "Invalid or blocked token" in (result.error or "")
    assert result.tier == Tier.RAYOBYTE
    await provider.close()


@pytest.mark.asyncio
async def test_rayobyte_credit_exhaustion_is_normalized(respx_mock):
    respx_mock.get(API_URL).mock(
        return_value=httpx.Response(200, json=fail_body(429, "Rate limit exceeded"))
    )
    provider = RayobyteProvider(api_url=API_URL, api_key="rb-test")
    result = await provider.fetch("https://example.com")
    assert result.status_code == 429
    assert result.error == "Rate limit exceeded"
    await provider.close()


@pytest.mark.asyncio
async def test_rayobyte_malformed_response_does_not_raise(respx_mock):
    respx_mock.get(API_URL).mock(
        return_value=httpx.Response(200, json={"unexpected": True})
    )
    provider = RayobyteProvider(api_url=API_URL, api_key="rb-test")
    result = await provider.fetch("https://example.com")
    assert result.error is not None
    await provider.close()


@pytest.mark.asyncio
async def test_rayobyte_unavailable_without_credentials():
    provider = RayobyteProvider(api_url=None, api_key=None)
    availability = provider.availability()
    assert availability.ready is False
    assert availability.reason is not None
    result = await provider.fetch("https://example.com")
    assert result.status_code is None
    assert result.error is not None
    await provider.close()
