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

API_URL = "https://api.firecrawl.dev/v2/scrape"


class FirecrawlProvider:
    tier = Tier.FIRECRAWL
    cost_kind = CostKind.FIRECRAWL_CREDIT

    def __init__(self, api_key: str | None = None):
        self.api_key = api_key
        self._client: httpx.AsyncClient | None = None

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(http2=True, timeout=60)
        return self._client

    async def fetch(self, url: str) -> FetchResult:
        started = time.monotonic()
        if not self.api_key:
            return failed_result(
                url, self.tier, self.cost_kind,
                "firecrawl api key not configured", started,
            )
        try:
            response = await self._get_client().post(
                API_URL,
                headers={"Authorization": f"Bearer {self.api_key}"},
                json={
                    "url": url,
                    "formats": ["markdown", "html"],
                    "onlyMainContent": True,
                    "proxy": "auto",
                    "timeout": 60000,
                },
            )
        except httpx.RequestError as exc:
            return failed_result(
                url, self.tier, self.cost_kind, str(exc), started,
                provider_error_kind=ProviderErrorKind.TRANSPORT,
                provider_error=str(exc),
            )
        provider_status = response.status_code
        if provider_status != 200:
            detail = ""
            try:
                detail = (response.json().get("error") or "").strip()
            except Exception:
                pass
            message = detail or f"firecrawl http {provider_status}"
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
                provider_error="invalid firecrawl json",
            )
        if not isinstance(data, dict):
            return failed_result(
                url, self.tier, self.cost_kind, "malformed firecrawl response", started,
                provider_status_code=provider_status,
                provider_error_kind=ProviderErrorKind.MALFORMED_RESPONSE,
                provider_error="malformed firecrawl response",
            )
        payload = data.get("data")
        metadata = payload.get("metadata") if isinstance(payload, dict) else None
        target_status = metadata.get("statusCode") if isinstance(metadata, dict) else None
        if not isinstance(target_status, int):
            return failed_result(
                url, self.tier, self.cost_kind, "malformed firecrawl response", started,
                provider_status_code=provider_status,
                provider_error_kind=ProviderErrorKind.MALFORMED_RESPONSE,
                provider_error="malformed firecrawl response",
            )
        return FetchResult(
            url=url,
            tier=self.tier,
            cost_kind=self.cost_kind,
            target_status_code=target_status,
            provider_status_code=provider_status,
            html=payload.get("html") or "",
            markdown=payload.get("markdown"),
            elapsed_ms=int((time.monotonic() - started) * 1000),
            error=data.get("error") if data.get("success") is False else None,
        )

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    def availability(self) -> ProviderAvailability:
        ready = bool(self.api_key)
        return ProviderAvailability(
            enabled=ready,
            ready=ready,
            reason=None if ready else "firecrawl api key not configured",
        )
