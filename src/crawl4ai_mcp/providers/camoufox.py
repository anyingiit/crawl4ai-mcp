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
        self._lifecycle = asyncio.Condition()
        self._active_fetches = 0
        self._closing = False
        self._closed = False
        self._close_task: asyncio.Task | None = None
        self._close_tasks: set[asyncio.Task] = set()

    async def fetch(self, url: str) -> FetchResult:
        started = time.monotonic()
        if not self.enabled:
            return failed_result(
                url, self.tier, self.cost_kind, "camoufox disabled", started
            )
        async with self._semaphore:
            async with self._lifecycle:
                if self._closed:
                    return failed_result(
                        url, self.tier, self.cost_kind, "provider closed", started
                    )
                if self._closing:
                    return failed_result(
                        url, self.tier, self.cost_kind, "provider closing", started
                    )
                if self._session is None:
                    try:
                        self._session = await self._launch()
                    except Exception as exc:
                        self._broken = str(exc)
                        return failed_result(
                            url, self.tier, self.cost_kind, str(exc), started
                        )
                session = self._session
                self._active_fetches += 1
            recorder = self.request_guard.begin_fetch()
            try:
                def fail(exc: Exception, *, network: bool = False) -> FetchResult:
                    blocked = recorder.blocked()
                    policy_error = blocked[0][1] if blocked else None
                    network_error = None
                    if policy_error is None and network and browser_network_error(
                        exc, operation=FetchStage.NAVIGATION
                    ):
                        network_error = "browser_navigation_failed"
                    return failed_result(
                        url, self.tier, self.cost_kind, str(exc), started,
                        network_error=network_error,
                        policy_error=policy_error,
                    )

                context = None
                try:
                    try:
                        endpoint = self.egress_proxy.endpoint()
                        context = await session.new_context(
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
                    self.request_guard.bind_page(page)
                    try:
                        response = await page.goto(
                            url, wait_until="domcontentloaded", timeout=60_000
                        )
                    except Exception as exc:
                        return fail(exc, network=True)
                    final_url = page.url
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
                return FetchResult(
                    url=url,
                    tier=self.tier,
                    cost_kind=self.cost_kind,
                    target_status_code=response.status if response is not None else None,
                    html=html,
                    redirected_url=final_url if final_url != url else None,
                    elapsed_ms=int((time.monotonic() - started) * 1000),
                )
            finally:
                recorder.close()
                async with self._lifecycle:
                    self._active_fetches -= 1
                    self._last_used = self._clock()
                    self._lifecycle.notify_all()

    async def reap_idle(self, now: float | None = None) -> None:
        now = self._clock() if now is None else now
        async with self._lifecycle:
            if self._closed or self._closing or self._active_fetches > 0:
                return
            if self._session is None or now - self._last_used < self.idle_seconds:
                return
            session = self._session
            self._session = None
            task = asyncio.create_task(self._close_session(session))
            self._close_tasks.add(task)
            task.add_done_callback(self._close_tasks.discard)
        # The session close task is provider-owned and tracked in
        # _close_tasks; shielding keeps it alive when the reaper's
        # caller is cancelled. Cancellation must propagate from here so
        # the service reaper exits promptly; provider.close() joins the
        # shared close task.
        await asyncio.shield(task)

    async def _close_session(self, session) -> None:
        try:
            await session.close()
        except Exception:
            pass

    async def close(self) -> None:
        async with self._lifecycle:
            if self._close_task is not None:
                task = self._close_task
            elif self._closed:
                task = None
            else:
                self._closing = True
                task = asyncio.create_task(self._close_resource())
                self._close_task = task
        if task is not None:
            await asyncio.shield(task)
        pending = list(self._close_tasks)
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

    async def _close_resource(self) -> None:
        try:
            async with self._lifecycle:
                while self._active_fetches > 0:
                    await self._lifecycle.wait()
                session = self._session
                self._session = None
            if session is not None:
                try:
                    await session.close()
                except Exception:
                    pass
        finally:
            async with self._lifecycle:
                self._closing = False
                self._closed = True
                self._close_task = None
                self._lifecycle.notify_all()

    def active_fetch_count(self) -> int:
        return self._active_fetches

    def last_used(self) -> float:
        return self._last_used

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
