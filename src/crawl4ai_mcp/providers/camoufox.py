from __future__ import annotations

import asyncio
import time
from typing import Callable

from crawl4ai_mcp.egress import BrowserRequestGuard, PinnedEgressProxy
from crawl4ai_mcp.models import CostKind, FetchResult, ProviderAvailability, Tier
from crawl4ai_mcp.providers.base import failed_result
from crawl4ai_mcp.providers.browser_errors import FetchStage, browser_network_error


async def _default_launch():
    from camoufox.async_api import AsyncCamoufox

    session = AsyncCamoufox(
        headless=True,
        humanize=True,
        block_images=True,
        block_webrtc=True,
        i_know_what_im_doing=True,
    )
    await session.__aenter__()
    return _CamoufoxSession(session)


class _CamoufoxSession:
    def __init__(self, session):
        self._session = session
        self.browser = session.browser

    async def new_context(self, **kwargs):
        from camoufox.async_api import AsyncNewContext

        return await AsyncNewContext(self.browser, **kwargs)

    async def close(self) -> None:
        try:
            await self._session.__aexit__(None, None, None)
        except Exception:
            pass


class CamoufoxProvider:
    tier = Tier.CAMOUFOX
    cost_kind = CostKind.FREE

    def __init__(
        self,
        enabled: bool = True,
        idle_seconds: int = 120,
        semaphore=None,
        *,
        launcher: Callable | None = None,
        egress_proxy: PinnedEgressProxy,
        request_guard: BrowserRequestGuard,
        clock: Callable | None = None,
    ):
        self.enabled = enabled
        self.idle_seconds = idle_seconds
        self._semaphore = semaphore or asyncio.Semaphore(2)
        self._launch = launcher or _default_launch
        self.egress_proxy = egress_proxy
        self.request_guard = request_guard
        self._clock = clock or time.monotonic
        self._session = None
        self._last_used = 0.0
        self._broken: str | None = None

    async def fetch(self, url: str) -> FetchResult:
        started = time.monotonic()
        if not self.enabled:
            return failed_result(
                url, self.tier, self.cost_kind, "camoufox disabled", started
            )
        async with self._semaphore:
            if self._session is None:
                try:
                    self._session = await self._launch()
                except Exception as exc:
                    self._broken = str(exc)
                    return failed_result(
                        url, self.tier, self.cost_kind, str(exc), started
                    )

            def fail(exc: Exception, *, network: bool = False) -> FetchResult:
                network_error = (
                    "browser_navigation_failed"
                    if network
                    and browser_network_error(
                        exc, operation=FetchStage.NAVIGATION
                    )
                    else None
                )
                return failed_result(
                    url, self.tier, self.cost_kind, str(exc), started,
                    network_error=network_error,
                )

            context = None
            try:
                try:
                    endpoint = self.egress_proxy.endpoint()
                    context = await self._session.new_context(
                        proxy={"server": endpoint.server}
                    )
                except Exception as exc:
                    return fail(exc)
                try:
                    await self.request_guard.install(context)
                except Exception as exc:
                    return fail(exc)
                try:
                    page = await context.new_page()
                except Exception as exc:
                    return fail(exc)
                try:
                    response = await page.goto(
                        url, wait_until="domcontentloaded", timeout=60_000
                    )
                except Exception as exc:
                    return fail(exc, network=True)
                try:
                    await page.wait_for_load_state("networkidle", timeout=5_000)
                except Exception:
                    pass
                try:
                    html = await page.content()
                except Exception as exc:
                    return fail(exc)
            finally:
                if context is not None:
                    try:
                        await context.close()
                    except Exception:
                        pass
            self._last_used = self._clock()
            return FetchResult(
                url=url,
                tier=self.tier,
                cost_kind=self.cost_kind,
                status_code=response.status if response is not None else None,
                html=html,
                elapsed_ms=int((time.monotonic() - started) * 1000),
            )

    async def reap_idle(self, now: float | None = None) -> None:
        now = self._clock() if now is None else now
        if self._session is not None and now - self._last_used >= self.idle_seconds:
            await self.close()

    async def close(self) -> None:
        if self._session is not None:
            try:
                await self._session.close()
            except Exception:
                pass
            self._session = None

    def is_active(self) -> bool:
        return self._session is not None

    def availability(self) -> ProviderAvailability:
        if not self.enabled:
            return ProviderAvailability(enabled=False, ready=False, reason="disabled")
        if self._broken is not None:
            return ProviderAvailability(
                enabled=True, ready=False, reason=self._broken
            )
        return ProviderAvailability(enabled=True, ready=True)
