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
async def test_rayobyte_http_error_extracts_json_error_detail(respx_mock):
    respx_mock.get(API_URL).mock(
        return_value=httpx.Response(500, json={"error": "Upstream exploded"})
    )
    result = await RayobyteProvider(API_URL, "key").fetch("https://example.com/")
    assert result.target_status_code is None
    assert result.provider_status_code == 500
    assert result.provider_error_kind == ProviderErrorKind.SERVICE
    assert "Upstream exploded" in (result.provider_error or "")
    assert "Upstream exploded" in (result.error or "")


@pytest.mark.asyncio
async def test_rayobyte_http_error_falls_back_to_body_text(respx_mock):
    respx_mock.get(API_URL).mock(
        return_value=httpx.Response(503, text="<html>service unavailable</html>")
    )
    result = await RayobyteProvider(API_URL, "key").fetch("https://example.com/")
    assert result.provider_status_code == 503
    assert result.provider_error_kind == ProviderErrorKind.SERVICE
    assert "service unavailable" in (result.provider_error or "")


@pytest.mark.asyncio
async def test_rayobyte_credit_exhausted_body_is_quota_even_with_non_402_status(respx_mock):
    respx_mock.get(API_URL).mock(
        return_value=httpx.Response(500, json={"error": "credits exhausted, top up account"})
    )
    result = await RayobyteProvider(API_URL, "key").fetch("https://example.com/")
    assert result.provider_status_code == 500
    assert result.provider_error_kind == ProviderErrorKind.QUOTA
    assert "credits exhausted" in (result.provider_error or "")


@pytest.mark.asyncio
async def test_rayobyte_http_error_detail_never_exposes_api_key(respx_mock):
    respx_mock.get(API_URL).mock(
        return_value=httpx.Response(401, json={"error": "Invalid token: rb-secret-key-123"})
    )
    result = await RayobyteProvider(API_URL, "rb-secret-key-123").fetch("https://example.com/")
    assert result.provider_error_kind == ProviderErrorKind.AUTH
    assert "rb-secret-key-123" not in (result.provider_error or "")
    assert "rb-secret-key-123" not in (result.error or "")


@pytest.mark.parametrize("http_code", [True, False, "200", 200.5, None, {"a": 1}, [200]])
@pytest.mark.asyncio
async def test_rayobyte_non_integer_http_code_is_malformed(respx_mock, http_code):
    respx_mock.get(API_URL).mock(
        return_value=httpx.Response(
            200, json={"status": "SUCCESS", "httpCode": http_code, "result": "<main>Hello</main>"}
        )
    )
    result = await RayobyteProvider(API_URL, "key").fetch("https://example.com/")
    assert result.target_status_code is None
    assert result.provider_error_kind == ProviderErrorKind.MALFORMED_RESPONSE


@pytest.mark.parametrize("result_value", [{"a": 1}, ["x"], 200, True, None])
@pytest.mark.asyncio
async def test_rayobyte_non_string_result_is_malformed(respx_mock, result_value):
    respx_mock.get(API_URL).mock(
        return_value=httpx.Response(
            200, json={"status": "SUCCESS", "httpCode": 200, "result": result_value}
        )
    )
    result = await RayobyteProvider(API_URL, "key").fetch("https://example.com/")
    assert result.target_status_code is None
    assert result.provider_error_kind == ProviderErrorKind.MALFORMED_RESPONSE
    assert result.html == ""


@pytest.mark.asyncio
async def test_rayobyte_fail_body_bool_status_code_is_malformed(respx_mock):
    respx_mock.get(API_URL).mock(
        return_value=httpx.Response(
            200, json={"status": "FAIL", "statusCode": True, "error": "boom"}
        )
    )
    result = await RayobyteProvider(API_URL, "key").fetch("https://example.com/")
    assert result.target_status_code is None
    assert result.provider_status_code == 200
    assert result.provider_error_kind == ProviderErrorKind.MALFORMED_RESPONSE


@pytest.mark.asyncio
async def test_rayobyte_fail_body_non_string_error_is_sanitized(respx_mock):
    respx_mock.get(API_URL).mock(
        return_value=httpx.Response(
            200, json={"status": "FAIL", "statusCode": 401, "error": {"message": "secret"}}
        )
    )
    result = await RayobyteProvider(API_URL, "key").fetch("https://example.com/")
    assert result.provider_error_kind == ProviderErrorKind.AUTH
    assert result.error is not None
    assert "secret" not in (result.error or "")
    assert "secret" not in (result.provider_error or "")


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


@pytest.mark.asyncio
async def test_rayobyte_unknown_status_envelope_is_provider_failure(respx_mock):
    respx_mock.get(API_URL).mock(
        return_value=httpx.Response(
            200, json={"status": "ERROR", "statusCode": 500, "error": "upstream boom"}
        )
    )
    result = await RayobyteProvider(API_URL, "key").fetch("https://example.com/")
    assert result.target_status_code is None
    assert result.provider_status_code == 500
    assert result.provider_error_kind == ProviderErrorKind.SERVICE
    assert result.provider_error is not None


@pytest.mark.asyncio
async def test_rayobyte_missing_status_envelope_is_provider_failure(respx_mock):
    respx_mock.get(API_URL).mock(
        return_value=httpx.Response(
            200, json={"httpCode": 200, "result": "<main>Hello</main>"}
        )
    )
    result = await RayobyteProvider(API_URL, "key").fetch("https://example.com/")
    assert result.target_status_code is None
    assert result.provider_status_code == 200
    assert result.provider_error_kind == ProviderErrorKind.MALFORMED_RESPONSE


@pytest.mark.asyncio
async def test_rayobyte_success_status_is_exact_string_required(respx_mock):
    respx_mock.get(API_URL).mock(
        return_value=httpx.Response(
            200, json={"status": "success", "httpCode": 200, "result": "<main>Hello</main>"}
        )
    )
    result = await RayobyteProvider(API_URL, "key").fetch("https://example.com/")
    assert result.target_status_code is None
    assert result.provider_error_kind == ProviderErrorKind.MALFORMED_RESPONSE
