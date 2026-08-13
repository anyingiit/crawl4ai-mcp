from __future__ import annotations

import asyncio
import time
from typing import Awaitable, Callable
from urllib.parse import urljoin

from curl_cffi import CurlOpt

from crawl4ai_mcp.egress import UrlPolicy, UrlPolicyError
from crawl4ai_mcp.models import CostKind, FetchResult, ProviderAvailability, Tier
from crawl4ai_mcp.providers.base import failed_result

BROWSER_HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
    "Upgrade-Insecure-Requests": "1",
}

REDIRECT_STATUSES = {301, 302, 303, 307, 308}


class HttpProvider:
    tier = Tier.HTTP
    cost_kind = CostKind.FREE

    def __init__(
        self,
        policy: UrlPolicy | None = None,
        concurrency: int = 8,
        timeout_seconds: int = 10,
        max_redirects: int = 10,
        session_factory: Callable[..., Awaitable[object]] | None = None,
    ):
        self._policy = policy or UrlPolicy()
        self._semaphore = asyncio.Semaphore(concurrency)
        self._timeout = timeout_seconds
        self._max_redirects = max_redirects
        self._session_factory = session_factory

    async def _open_session(self, pinned: list[str]):
        factory = self._session_factory
        if factory is None:
            from curl_cffi.requests import AsyncSession

            factory = AsyncSession
        return factory(
            impersonate="chrome131",
            timeout=self._timeout,
            trust_env=False,
            allow_redirects=False,
            curl_options={CurlOpt.RESOLVE: pinned},
        )

    async def fetch(self, url: str) -> FetchResult:
        started = time.monotonic()
        async with self._semaphore:
            current = url
            hops = 0
            while True:
                if hops >= self._max_redirects:
                    return failed_result(
                        url,
                        Tier.HTTP,
                        CostKind.FREE,
                        "too_many_redirects",
                        started,
                        network_error="too_many_redirects",
                    )
                try:
                    target = await self._policy.resolve(current)
                except UrlPolicyError as exc:
                    return failed_result(
                        url,
                        Tier.HTTP,
                        CostKind.FREE,
                        "",
                        started,
                        policy_error=exc.reason.value,
                    )
                current = target.url.url
                pinned = [
                    f"{target.host}:{target.port}:{address}"
                    for address in target.addresses
                ]
                session = None
                try:
                    session = await self._open_session(pinned)
                    response = await session.get(
                        target.url.url, headers=BROWSER_HEADERS
                    )
                except Exception as exc:
                    return failed_result(
                        url,
                        Tier.HTTP,
                        CostKind.FREE,
                        str(exc),
                        started,
                        network_error="request_failed",
                    )
                finally:
                    if session is not None:
                        try:
                            await session.close()
                        except Exception:
                            pass
                if (
                    response.status_code in REDIRECT_STATUSES
                    and "location" in {key.lower() for key in response.headers}
                ):
                    location = next(
                        value
                        for key, value in response.headers.items()
                        if key.lower() == "location"
                    )
                    current = urljoin(target.url.url, location)
                    hops += 1
                    continue
                return FetchResult(
                    url=url,
                    tier=Tier.HTTP,
                    cost_kind=CostKind.FREE,
                    target_status_code=response.status_code,
                    html=response.text,
                    headers=dict(response.headers),
                    redirected_url=current if hops > 0 else None,
                    elapsed_ms=int((time.monotonic() - started) * 1000),
                )

    async def close(self) -> None:
        return None

    def availability(self) -> ProviderAvailability:
        return ProviderAvailability(enabled=True, ready=True)
