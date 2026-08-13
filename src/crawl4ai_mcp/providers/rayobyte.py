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
from crawl4ai_mcp.providers.base import classify_provider_error, failed_result

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
            message = f"rayobyte http {provider_status}"
            return failed_result(
                url, self.tier, self.cost_kind, message, started,
                provider_status_code=provider_status,
                provider_error_kind=classify_provider_error(provider_status),
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
        if data.get("status") == "FAIL":
            provider_code = data.get("statusCode")
            detail = data.get("error") or ""
            if not isinstance(provider_code, int):
                return failed_result(
                    url, self.tier, self.cost_kind, detail or "rayobyte request failed",
                    started,
                    provider_status_code=provider_status,
                    provider_error_kind=ProviderErrorKind.MALFORMED_RESPONSE,
                    provider_error=detail or "rayobyte request failed",
                )
            return failed_result(
                url, self.tier, self.cost_kind, detail or "rayobyte request failed",
                started,
                provider_status_code=provider_code,
                provider_error_kind=classify_provider_error(provider_code, detail),
                provider_error=detail or "rayobyte request failed",
            )
        if "httpCode" not in data or "result" not in data:
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
            target_status_code=data.get("httpCode"),
            provider_status_code=provider_status,
            html=data.get("result") or "",
            elapsed_ms=int((time.monotonic() - started) * 1000),
            error=data.get("error"),
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
