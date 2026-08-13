from __future__ import annotations

import asyncio
import time

from crawl4ai_mcp.models import CostKind, FetchResult, ProviderAvailability, Tier
from crawl4ai_mcp.providers.base import failed_result

BROWSER_HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
    "Upgrade-Insecure-Requests": "1",
}


class HttpProvider:
    tier = Tier.HTTP
    cost_kind = CostKind.FREE

    def __init__(self, concurrency: int = 8, timeout_seconds: int = 10):
        self._semaphore = asyncio.Semaphore(concurrency)
        self._timeout = timeout_seconds
        self._session = None

    async def fetch(self, url: str) -> FetchResult:
        started = time.monotonic()
        async with self._semaphore:
            try:
                from curl_cffi.requests import AsyncSession

                if self._session is None:
                    self._session = AsyncSession(
                        impersonate="chrome131", timeout=self._timeout
                    )
                response = await self._session.get(url, headers=BROWSER_HEADERS)
                final_url = str(response.url)
                return FetchResult(
                    url=url,
                    tier=Tier.HTTP,
                    cost_kind=CostKind.FREE,
                    status_code=response.status_code,
                    html=response.text,
                    headers=dict(response.headers),
                    redirected_url=final_url if final_url != url else None,
                    elapsed_ms=int((time.monotonic() - started) * 1000),
                )
            except Exception as exc:
                return failed_result(
                    url, Tier.HTTP, CostKind.FREE, str(exc), started
                )

    async def close(self) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None

    def availability(self) -> ProviderAvailability:
        return ProviderAvailability(enabled=True, ready=True)
