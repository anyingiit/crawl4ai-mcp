from __future__ import annotations

import time

import httpx

from crawl4ai_mcp.models import (
    CostKind,
    FetchResult,
    ProviderAvailability,
    ProviderErrorKind,
    Tier,
)
from crawl4ai_mcp.providers.base import (
    classify_provider_error,
    extract_error_detail,
    failed_result,
    safe_error_detail,
)

DEFAULT_API_URL = "https://api.scraping.rayobyte.com/"


class RayobyteProvider:
    tier = Tier.RAYOBYTE
    cost_kind = CostKind.RAYOBYTE_CREDIT

    def __init__(self, api_url: str | None = None, api_key: str | None = None):
        self.api_url = api_url
        self.api_key = api_key
        self._client: httpx.AsyncClient | None = None

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(http2=True, timeout=60)
        return self._client

    async def fetch(self, url: str) -> FetchResult:
        started = time.monotonic()
        if not self.api_url or not self.api_key:
            return failed_result(
                url, self.tier, self.cost_kind,
                "rayobyte endpoint or api key not configured", started,
            )
        try:
            response = await self._get_client().get(
                self.api_url,
                params={"token": self.api_key, "url": url},
            )
        except httpx.RequestError as exc:
            return failed_result(
                url, self.tier, self.cost_kind, str(exc), started,
                provider_error_kind=ProviderErrorKind.TRANSPORT,
                provider_error=str(exc),
            )
        provider_status = response.status_code
        if provider_status != 200:
            detail = extract_error_detail(response, [self.api_key] if self.api_key else None)
            message = detail or f"rayobyte http {provider_status}"
            return failed_result(
                url, self.tier, self.cost_kind, message, started,
                provider_status_code=provider_status,
                provider_error_kind=classify_provider_error(provider_status, detail),
                provider_error=message,
            )
        try:
            data = response.json()
        except Exception as exc:
            return failed_result(
                url, self.tier, self.cost_kind, str(exc), started,
                provider_status_code=provider_status,
                provider_error_kind=ProviderErrorKind.MALFORMED_RESPONSE,
                provider_error="invalid rayobyte json",
            )
        if not isinstance(data, dict):
            return failed_result(
                url, self.tier, self.cost_kind, "malformed rayobyte response", started,
                provider_status_code=provider_status,
                provider_error_kind=ProviderErrorKind.MALFORMED_RESPONSE,
                provider_error="malformed rayobyte response",
            )
        status = data.get("status")
        if status != "SUCCESS":
            detail = safe_error_detail(
                data.get("error"), [self.api_key] if self.api_key else None
            )
            message = detail or "rayobyte request failed"
            provider_code = data.get("statusCode")
            if (
                isinstance(provider_code, int)
                and not isinstance(provider_code, bool)
            ):
                return failed_result(
                    url, self.tier, self.cost_kind, message, started,
                    provider_status_code=provider_code,
                    provider_error_kind=classify_provider_error(provider_code, detail),
                    provider_error=message,
                )
            return failed_result(
                url, self.tier, self.cost_kind, message, started,
                provider_status_code=provider_status,
                provider_error_kind=ProviderErrorKind.MALFORMED_RESPONSE,
                provider_error=message,
            )
        if "httpCode" not in data or "result" not in data:
            return failed_result(
                url, self.tier, self.cost_kind, "malformed rayobyte response", started,
                provider_status_code=provider_status,
                provider_error_kind=ProviderErrorKind.MALFORMED_RESPONSE,
                provider_error="malformed rayobyte response",
            )
        http_code = data.get("httpCode")
        result = data.get("result")
        if not isinstance(http_code, int) or isinstance(http_code, bool) or not isinstance(result, str):
            return failed_result(
                url, self.tier, self.cost_kind, "malformed rayobyte response", started,
                provider_status_code=provider_status,
                provider_error_kind=ProviderErrorKind.MALFORMED_RESPONSE,
                provider_error="malformed rayobyte response",
            )
        return FetchResult(
            url=url,
            tier=self.tier,
            cost_kind=self.cost_kind,
            target_status_code=http_code,
            provider_status_code=provider_status,
            html=result,
            elapsed_ms=int((time.monotonic() - started) * 1000),
            error=safe_error_detail(data.get("error"), [self.api_key] if self.api_key else None) or None,
        )

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    def availability(self) -> ProviderAvailability:
        ready = bool(self.api_url and self.api_key)
        return ProviderAvailability(
            enabled=ready,
            ready=ready,
            reason=None if ready else "rayobyte endpoint or api key not configured",
        )
