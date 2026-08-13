from __future__ import annotations

import asyncio
import time
from typing import Callable, Sequence

from crawl4ai import AsyncWebCrawler
from crawl4ai.async_configs import (
    BrowserConfig,
    CacheMode,
    CrawlerRunConfig,
)
from crawl4ai.async_crawler_strategy import AsyncPlaywrightCrawlerStrategy
from crawl4ai.browser_adapter import UndetectedAdapter

from crawl4ai_mcp.egress import BrowserRequestGuard, PinnedEgressProxy, UpstreamProxy
from crawl4ai_mcp.models import (
    CostKind,
    FetchResult,
    ProviderAvailability,
    ProviderErrorKind,
    Tier,
)
from crawl4ai_mcp.providers.base import failed_result
from crawl4ai_mcp.providers.browser_errors import FetchStage, browser_network_error


def _stealth_config() -> BrowserConfig:
    return BrowserConfig(
        headless=True, enable_stealth=True, text_mode=True,
        memory_saving_mode=True, max_pages_before_recycle=25,
    )


def _undetected_config() -> BrowserConfig:
    return BrowserConfig(
        headless=True, text_mode=True, memory_saving_mode=True,
        max_pages_before_recycle=25,
    )


def default_factory(tier: Tier) -> AsyncWebCrawler:
    if tier == Tier.STEALTH:
        config = _stealth_config()
        return AsyncWebCrawler(
            crawler_strategy=AsyncPlaywrightCrawlerStrategy(browser_config=config)
        )
    config = _undetected_config()
    return AsyncWebCrawler(
        crawler_strategy=AsyncPlaywrightCrawlerStrategy(
            browser_config=config,
            browser_adapter=UndetectedAdapter(),
        )
    )


class BrowserProvider:
    tier: Tier
    cost_kind: CostKind

    def __init__(
        self,
        tier: Tier,
        idle_seconds: int,
        semaphore,
        *,
        egress_proxy: PinnedEgressProxy,
        request_guard: BrowserRequestGuard,
        proxy_pool: Sequence[UpstreamProxy] = (),
        factory: Callable | None = None,
        clock: Callable | None = None,
    ):
        assert tier in {Tier.STEALTH, Tier.UNDETECTED, Tier.PROXY}
        self.tier = tier
        self.idle_seconds = idle_seconds
        self._semaphore = semaphore
        self.egress_proxy = egress_proxy
        self.request_guard = request_guard
        self.proxy_pool = list(proxy_pool)
        self._proxy_index = 0
        self._factory = factory or (lambda: default_factory(self.tier))
        self._clock = clock or time.monotonic
        self.cost_kind = (
            CostKind.PROXY_BANDWIDTH if tier == Tier.PROXY else CostKind.FREE
        )
        self._crawler = None
        self._last_used = 0.0
        self._lifecycle = asyncio.Condition()
        self._active_fetches = 0
        self._closing = False
        self._close_task: asyncio.Task | None = None

    def _next_upstream(self) -> UpstreamProxy | None:
        if self.tier != Tier.PROXY or not self.proxy_pool:
            return None
        upstream = self.proxy_pool[self._proxy_index % len(self.proxy_pool)]
        self._proxy_index += 1
        return upstream

    def _run_config(self) -> CrawlerRunConfig:
        upstream = self._next_upstream()
        return CrawlerRunConfig(
            proxy_config=self.egress_proxy.endpoint(upstream),
            cache_mode=CacheMode.BYPASS,
        )

    def _install_guard(self) -> None:
        strategy = getattr(self._crawler, "crawler_strategy", None)
        set_hook = getattr(strategy, "set_hook", None)
        if set_hook is None:
            return
        try:
            set_hook("on_page_context_created", self._on_page_context_created)
        except ValueError:
            pass

    async def _on_page_context_created(self, page, *, context=None, config=None):
        if context is not None:
            await self.request_guard.install(context)

    def _blocked_policy_error(self, marker: int) -> str | None:
        blocked = self.request_guard.blocked_since(marker)
        if not blocked:
            return None
        return blocked[0][1]

    async def fetch(self, url: str) -> FetchResult:
        started = time.monotonic()
        async with self._semaphore:
            async with self._lifecycle:
                if self._closing:
                    return failed_result(
                        url, self.tier, self.cost_kind, "provider closing", started
                    )
                if self._crawler is None:
                    self._crawler = self._factory()
                    self._install_guard()
                crawler = self._crawler
                self._active_fetches += 1
            marker = self.request_guard.blocked_marker()
            try:
                config = self._run_config()
                try:
                    container = await crawler.arun(url=url, config=config)
                except Exception as exc:
                    network_error = (
                        "browser_navigation_failed"
                        if browser_network_error(
                            exc,
                            operation=FetchStage.NAVIGATION,
                            wrapped=True,
                        )
                        else None
                    )
                    policy_error = self._blocked_policy_error(marker)
                    if network_error or policy_error:
                        return failed_result(
                            url, self.tier, self.cost_kind, str(exc), started,
                            network_error=network_error,
                            policy_error=policy_error,
                        )
                    return failed_result(
                        url, self.tier, self.cost_kind, str(exc), started,
                        provider_error_kind=ProviderErrorKind.SERVICE,
                        provider_error=str(exc),
                    )
                results = getattr(container, "_results", container)
                result = results[0] if isinstance(results, list) else results
                error_message = getattr(result, "error_message", None)
                success = getattr(result, "success", True)
                if not success or error_message:
                    network_error = (
                        "browser_navigation_failed"
                        if error_message
                        and browser_network_error(
                            error_message,
                            operation=FetchStage.NAVIGATION,
                            wrapped=True,
                        )
                        else None
                    )
                    policy_error = self._blocked_policy_error(marker)
                    return failed_result(
                        url, self.tier, self.cost_kind,
                        error_message or "browser fetch failed", started,
                        target_status_code=getattr(result, "status_code", None),
                        network_error=network_error,
                        policy_error=policy_error,
                    )
                return FetchResult(
                    url=url,
                    tier=self.tier,
                    cost_kind=self.cost_kind,
                    target_status_code=getattr(result, "status_code", None),
                    html=getattr(result, "html", "") or "",
                    headers=dict(getattr(result, "response_headers", None) or {}),
                    redirected_url=getattr(result, "redirected_url", None),
                    elapsed_ms=int((time.monotonic() - started) * 1000),
                    error=error_message,
                )
            finally:
                async with self._lifecycle:
                    self._active_fetches -= 1
                    self._last_used = self._clock()
                    self._lifecycle.notify_all()

    async def reap_idle(self, now: float | None = None) -> None:
        now = self._clock() if now is None else now
        async with self._lifecycle:
            if self._closing or self._active_fetches > 0:
                return
            if self._crawler is None or now - self._last_used < self.idle_seconds:
                return
            crawler = self._crawler
            self._crawler = None
        if crawler is not None:
            try:
                await crawler.close()
            except Exception:
                pass

    async def close(self) -> None:
        async with self._lifecycle:
            if self._close_task is not None:
                task = self._close_task
            else:
                self._closing = True
                task = asyncio.create_task(self._close_resource())
                self._close_task = task
        await asyncio.shield(task)

    async def _close_resource(self) -> None:
        try:
            async with self._lifecycle:
                while self._active_fetches > 0:
                    await self._lifecycle.wait()
                crawler = self._crawler
                self._crawler = None
            if crawler is not None:
                try:
                    await crawler.close()
                except Exception:
                    pass
        finally:
            async with self._lifecycle:
                self._closing = False
                self._close_task = None
                self._lifecycle.notify_all()

    def active_fetch_count(self) -> int:
        return self._active_fetches

    def last_used(self) -> float:
        return self._last_used

    def is_active(self) -> bool:
        return self._crawler is not None

    def availability(self) -> ProviderAvailability:
        if self.tier == Tier.PROXY and not self.proxy_pool:
            return ProviderAvailability(
                enabled=True, ready=False, reason="no proxies configured"
            )
        return ProviderAvailability(enabled=True, ready=True)
