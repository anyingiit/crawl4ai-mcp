import json
import httpx
import pytest
import respx

from crawl4ai_mcp.models import CostKind, ProviderErrorKind, Tier
from crawl4ai_mcp.providers.firecrawl import FirecrawlProvider

API_URL = "https://api.firecrawl.dev/v2/scrape"


def success_payload(markdown="# Protected", html="<main>Protected</main>", target_status=200):
    return {
        "success": True,
        "data": {
            "markdown": markdown,
            "html": html,
            "metadata": {"statusCode": target_status},
        },
    }


def mock_firecrawl_provider_failure(respx_mock, status):
    respx_mock.post(API_URL).mock(
        return_value=httpx.Response(status, json={"success": False, "error": f"http {status}"})
    )


@pytest.mark.asyncio
async def test_firecrawl_requests_html_and_markdown(respx_mock):
    route = respx_mock.post(API_URL).mock(
        return_value=httpx.Response(200, json=success_payload())
    )
    provider = FirecrawlProvider(api_key="fc-test")
    result = await provider.fetch("https://protected.example")
    payload = json.loads(route.calls[0].request.content)
    assert payload == {
        "url": "https://protected.example",
        "formats": ["markdown", "html"],
        "onlyMainContent": True,
        "proxy": "auto",
        "timeout": 60000,
    }
    assert route.calls[0].request.headers["Authorization"] == "Bearer fc-test"
    assert result.markdown == "# Protected"
    assert result.html == "<main>Protected</main>"
    assert result.target_status_code == 200
    assert result.provider_status_code == 200
    assert result.provider_error_kind is None
    assert result.tier == Tier.FIRECRAWL
    assert result.cost_kind == CostKind.FIRECRAWL_CREDIT
    await provider.close()


@pytest.mark.asyncio
async def test_firecrawl_preserves_target_status_code(respx_mock):
    respx_mock.post(API_URL).mock(
        return_value=httpx.Response(200, json=success_payload(target_status=404))
    )
    provider = FirecrawlProvider(api_key="fc-test")
    result = await provider.fetch("https://example.com/missing")
    assert result.target_status_code == 404
    assert result.provider_status_code == 200
    assert result.provider_error_kind is None
    await provider.close()


@pytest.mark.parametrize(
    ("status", "kind"),
    [
        (401, ProviderErrorKind.AUTH),
        (403, ProviderErrorKind.AUTH),
        (402, ProviderErrorKind.QUOTA),
        (429, ProviderErrorKind.RATE_LIMIT),
        (500, ProviderErrorKind.SERVICE),
        (503, ProviderErrorKind.SERVICE),
    ],
)
@pytest.mark.asyncio
async def test_firecrawl_http_error_is_provider_failure(respx_mock, status, kind):
    mock_firecrawl_provider_failure(respx_mock, status)
    result = await FirecrawlProvider("fc-test").fetch("https://example.com/")
    assert result.target_status_code is None
    assert result.provider_status_code == status
    assert result.provider_error_kind == kind
    assert result.provider_error is not None


@pytest.mark.asyncio
async def test_firecrawl_malformed_success_payload_is_malformed_kind(respx_mock):
    respx_mock.post(API_URL).mock(
        return_value=httpx.Response(200, json={"success": True, "data": None})
    )
    result = await FirecrawlProvider("fc-test").fetch("https://example.com/")
    assert result.target_status_code is None
    assert result.provider_status_code == 200
    assert result.provider_error_kind == ProviderErrorKind.MALFORMED_RESPONSE
    assert result.error is not None


@pytest.mark.asyncio
async def test_firecrawl_missing_metadata_is_malformed_kind(respx_mock):
    respx_mock.post(API_URL).mock(
        return_value=httpx.Response(
            200, json={"success": True, "data": {"markdown": "# x", "html": "<p>x</p>"}}
        )
    )
    result = await FirecrawlProvider("fc-test").fetch("https://example.com/")
    assert result.target_status_code is None
    assert result.provider_error_kind == ProviderErrorKind.MALFORMED_RESPONSE


@pytest.mark.asyncio
async def test_firecrawl_invalid_json_is_malformed_kind(respx_mock):
    respx_mock.post(API_URL).mock(
        return_value=httpx.Response(200, text="<html>not json</html>")
    )
    result = await FirecrawlProvider("fc-test").fetch("https://example.com/")
    assert result.target_status_code is None
    assert result.provider_status_code == 200
    assert result.provider_error_kind == ProviderErrorKind.MALFORMED_RESPONSE


@pytest.mark.asyncio
async def test_firecrawl_transport_error_is_transport_kind(respx_mock):
    respx_mock.post(API_URL).mock(
        side_effect=httpx.ConnectError("connection refused")
    )
    result = await FirecrawlProvider("fc-test").fetch("https://example.com/")
    assert result.target_status_code is None
    assert result.provider_status_code is None
    assert result.provider_error_kind == ProviderErrorKind.TRANSPORT
    assert result.provider_error is not None


@pytest.mark.asyncio
async def test_firecrawl_http_error_detail_never_exposes_api_key(respx_mock):
    respx_mock.post(API_URL).mock(
        return_value=httpx.Response(401, json={"error": "invalid token: fc-secret-key-456"})
    )
    result = await FirecrawlProvider("fc-secret-key-456").fetch("https://example.com/")
    assert result.provider_error_kind == ProviderErrorKind.AUTH
    assert "fc-secret-key-456" not in (result.provider_error or "")
    assert "fc-secret-key-456" not in (result.error or "")


@pytest.mark.parametrize("status", [True, False, "200", 200.5, None])
@pytest.mark.asyncio
async def test_firecrawl_non_integer_metadata_status_is_malformed(respx_mock, status):
    respx_mock.post(API_URL).mock(
        return_value=httpx.Response(200, json={
            "success": True,
            "data": {"markdown": "# x", "html": "<p>x</p>", "metadata": {"statusCode": status}},
        })
    )
    result = await FirecrawlProvider("fc-test").fetch("https://example.com/")
    assert result.target_status_code is None
    assert result.provider_error_kind == ProviderErrorKind.MALFORMED_RESPONSE


@pytest.mark.parametrize("content", [{"a": 1}, ["x"], 200, True])
@pytest.mark.asyncio
async def test_firecrawl_non_string_content_is_malformed(respx_mock, content):
    respx_mock.post(API_URL).mock(
        return_value=httpx.Response(200, json={
            "success": True,
            "data": {"markdown": content, "html": content, "metadata": {"statusCode": 200}},
        })
    )
    result = await FirecrawlProvider("fc-test").fetch("https://example.com/")
    assert result.target_status_code is None
    assert result.provider_error_kind == ProviderErrorKind.MALFORMED_RESPONSE


@pytest.mark.asyncio
async def test_firecrawl_nullable_content_is_accepted(respx_mock):
    respx_mock.post(API_URL).mock(
        return_value=httpx.Response(200, json={
            "success": True,
            "data": {"markdown": None, "html": None, "metadata": {"statusCode": 200}},
        })
    )
    result = await FirecrawlProvider("fc-test").fetch("https://example.com/")
    assert result.target_status_code == 200
    assert result.provider_error_kind is None
    assert result.html == ""
    assert result.markdown is None


@pytest.mark.asyncio
async def test_firecrawl_unavailable_without_key():
    provider = FirecrawlProvider(api_key=None)
    availability = provider.availability()
    assert availability.ready is False
    assert availability.reason is not None
    result = await provider.fetch("https://example.com")
    assert result.target_status_code is None
    assert result.provider_status_code is None
    assert result.provider_error_kind is None
    assert result.error is not None
    await provider.close()
