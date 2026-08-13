from __future__ import annotations

import time

import httpx

from crawl4ai_mcp.models import CostKind, FetchResult, ProviderAvailability, Tier
from crawl4ai_mcp.providers.base import failed_result

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
                    "formats": ["markdown"],
                    "onlyMainContent": True,
                    "proxy": "auto",
                    "timeout": 60000,
                },
            )
            if response.status_code in {402, 429} or response.status_code >= 500:
                detail = ""
                try:
                    detail = (response.json().get("error") or "").strip()
                except Exception:
                    pass
                message = detail or f"firecrawl http {response.status_code}"
                return failed_result(
                    url, self.tier, self.cost_kind, message,
                    started,
                    status_code=response.status_code,
                )
            data = response.json()
            payload = data.get("data") or {}
            metadata = payload.get("metadata") or {}
            return FetchResult(
                url=url,
                tier=self.tier,
                cost_kind=self.cost_kind,
                status_code=metadata.get("statusCode"),
                markdown=payload.get("markdown"),
                elapsed_ms=int((time.monotonic() - started) * 1000),
                error=data.get("error") if data.get("success") is False else None,
            )
        except Exception as exc:
            return failed_result(
                url, self.tier, self.cost_kind, str(exc), started
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
