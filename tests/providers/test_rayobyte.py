import httpx
import pytest
import respx

from crawl4ai_mcp.models import CostKind, ProviderErrorKind, Tier
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


def mock_rayobyte_success(respx_mock, target_status=200):
    respx_mock.get(API_URL).mock(
        return_value=httpx.Response(200, json=success_body(http_code=target_status))
    )


def mock_rayobyte_failure(respx_mock, status, error):
    respx_mock.get(API_URL).mock(
        return_value=httpx.Response(200, json=fail_body(status, error))
    )


@pytest.mark.asyncio
async def test_rayobyte_success_returns_rendered_html(respx_mock):
    route = respx_mock.get(API_URL).mock(
        return_value=httpx.Response(200, json=success_body())
    )
    provider = RayobyteProvider(api_url=API_URL, api_key="rb-test")
    result = await provider.fetch("https://example.com")
    assert result.target_status_code == 200
    assert result.provider_status_code == 200
    assert result.provider_error_kind is None
    assert "<main>Hello</main>" in result.html
    assert result.tier == Tier.RAYOBYTE
    assert result.cost_kind == CostKind.RAYOBYTE_CREDIT
    params = route.calls[0].request.url.params
    assert params["token"] == "rb-test"
    assert params["url"] == "https://example.com"
    await provider.close()


@pytest.mark.asyncio
async def test_rayobyte_invalid_token_is_provider_auth_not_target_401(respx_mock):
    mock_rayobyte_failure(respx_mock, 401, "Invalid token")
    result = await RayobyteProvider(API_URL, "bad").fetch("https://example.com/")
    assert result.target_status_code is None
    assert result.provider_status_code == 401
    assert result.provider_error_kind == ProviderErrorKind.AUTH


@pytest.mark.asyncio
async def test_rayobyte_target_404_remains_target_status(respx_mock):
    mock_rayobyte_success(respx_mock, target_status=404)
    result = await RayobyteProvider(API_URL, "key").fetch("https://example.com/missing")
    assert result.target_status_code == 404
    assert result.provider_status_code == 200
    assert result.provider_error_kind is None
    assert result.error is None


@pytest.mark.parametrize("status", [401, 404, 410])
@pytest.mark.asyncio
async def test_rayobyte_target_statuses_never_occupy_provider_fields(respx_mock, status):
    mock_rayobyte_success(respx_mock, target_status=status)
    result = await RayobyteProvider(API_URL, "key").fetch("https://example.com/")
    assert result.target_status_code == status
    assert result.provider_status_code == 200
    assert result.provider_error_kind is None


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
async def test_rayobyte_http_error_is_provider_failure(respx_mock, status, kind):
    respx_mock.get(API_URL).mock(
        return_value=httpx.Response(status, json={"error": "boom"})
    )
    result = await RayobyteProvider(API_URL, "key").fetch("https://example.com/")
    assert result.target_status_code is None
    assert result.provider_status_code == status
    assert result.provider_error_kind == kind
    assert result.provider_error is not None


@pytest.mark.asyncio
async def test_rayobyte_fail_body_is_provider_failure_not_target(respx_mock):
    mock_rayobyte_failure(respx_mock, 403, "Forbidden")
    result = await RayobyteProvider(API_URL, "key").fetch("https://example.com/")
    assert result.target_status_code is None
    assert result.provider_status_code == 403
    assert result.provider_error_kind == ProviderErrorKind.AUTH


@pytest.mark.asyncio
async def test_rayobyte_fail_body_credit_text_is_quota(respx_mock):
    respx_mock.get(API_URL).mock(
        return_value=httpx.Response(
            200, json=fail_body(200, "You have used up all your credits")
        )
    )
    result = await RayobyteProvider(API_URL, "key").fetch("https://example.com/")
    assert result.target_status_code is None
    assert result.provider_status_code == 200
    assert result.provider_error_kind == ProviderErrorKind.QUOTA


@pytest.mark.asyncio
async def test_rayobyte_fail_body_rate_limit_kind(respx_mock):
    mock_rayobyte_failure(respx_mock, 429, "Rate limit exceeded")
    result = await RayobyteProvider(API_URL, "key").fetch("https://example.com/")
    assert result.target_status_code is None
    assert result.provider_status_code == 429
    assert result.provider_error_kind == ProviderErrorKind.RATE_LIMIT


@pytest.mark.asyncio
async def test_rayobyte_transport_error_is_transport_kind(respx_mock):
    respx_mock.get(API_URL).mock(
        side_effect=httpx.ConnectError("connection refused")
    )
    result = await RayobyteProvider(API_URL, "key").fetch("https://example.com/")
    assert result.target_status_code is None
    assert result.provider_status_code is None
    assert result.provider_error_kind == ProviderErrorKind.TRANSPORT
    assert result.provider_error is not None


@pytest.mark.asyncio
async def test_rayobyte_malformed_shape_is_malformed_kind(respx_mock):
    respx_mock.get(API_URL).mock(
        return_value=httpx.Response(200, json={"unexpected": True})
    )
    result = await RayobyteProvider(API_URL, "key").fetch("https://example.com/")
    assert result.target_status_code is None
    assert result.provider_status_code == 200
    assert result.provider_error_kind == ProviderErrorKind.MALFORMED_RESPONSE


@pytest.mark.asyncio
async def test_rayobyte_invalid_json_is_malformed_kind(respx_mock):
    respx_mock.get(API_URL).mock(
        return_value=httpx.Response(200, text="<html>not json</html>")
    )
    result = await RayobyteProvider(API_URL, "key").fetch("https://example.com/")
    assert result.target_status_code is None
    assert result.provider_status_code == 200
    assert result.provider_error_kind == ProviderErrorKind.MALFORMED_RESPONSE


@pytest.mark.asyncio
async def test_rayobyte_unavailable_without_credentials():
    provider = RayobyteProvider(api_url=None, api_key=None)
    availability = provider.availability()
    assert availability.ready is False
    assert availability.reason is not None
    result = await provider.fetch("https://example.com")
    assert result.target_status_code is None
    assert result.provider_status_code is None
    assert result.provider_error_kind is None
    assert result.error is not None
    await provider.close()
