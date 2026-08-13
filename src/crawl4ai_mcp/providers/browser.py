from __future__ import annotations

import time
from typing import Callable

from crawl4ai import AsyncWebCrawler
from crawl4ai.async_configs import BrowserConfig, ProxyConfig
from crawl4ai.async_crawler_strategy import AsyncPlaywrightCrawlerStrategy
from crawl4ai.browser_adapter import UndetectedAdapter

from crawl4ai_mcp.models import CostKind, FetchResult, ProviderAvailability, Tier
from crawl4ai_mcp.providers.base import failed_result


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


def default_factory(tier: Tier, proxy: ProxyConfig | None = None) -> AsyncWebCrawler:
    if tier == Tier.STEALTH:
        config = _stealth_config()
        return AsyncWebCrawler(
            crawler_strategy=AsyncPlaywrightCrawlerStrategy(browser_config=config)
        )
    config = _undetected_config()
    if proxy is not None:
        config.proxy_config = proxy
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
        proxy_pool=(),
        factory: Callable | None = None,
        clock: Callable | None = None,
    ):
        assert tier in {Tier.STEALTH, Tier.UNDETECTED, Tier.PROXY}
        self.tier = tier
        self.idle_seconds = idle_seconds
        self._semaphore = semaphore
        self.proxy_pool = list(proxy_pool)
        self._proxy_index = 0
        self._factory = factory or (lambda proxy: default_factory(self.tier, proxy))
        self._clock = clock or time.monotonic
        self.cost_kind = (
            CostKind.PROXY_BANDWIDTH if tier == Tier.PROXY else CostKind.FREE
        )
        self._crawler = None
        self._last_used = 0.0

    def _next_proxy(self) -> ProxyConfig | None:
        if self.tier != Tier.PROXY or not self.proxy_pool:
            return None
        proxy = self.proxy_pool[self._proxy_index % len(self.proxy_pool)]
        self._proxy_index += 1
        return proxy

    async def fetch(self, url: str) -> FetchResult:
        started = time.monotonic()
        async with self._semaphore:
            if self._crawler is None:
                proxy = self._next_proxy()
                self._crawler = self._factory(proxy)
            try:
                container = await self._crawler.arun(url=url, config=None)
                results = getattr(container, "_results", container)
                result = results[0] if isinstance(results, list) else results
                self._last_used = self._clock()
                return FetchResult(
                    url=url,
                    tier=self.tier,
                    cost_kind=self.cost_kind,
                    status_code=getattr(result, "status_code", None),
                    html=getattr(result, "html", "") or "",
                    headers=dict(getattr(result, "response_headers", None) or {}),
                    redirected_url=getattr(result, "redirected_url", None),
                    elapsed_ms=int((time.monotonic() - started) * 1000),
                    error=getattr(result, "error_message", None),
                )
            except Exception as exc:
                return failed_result(
                    url, self.tier, self.cost_kind, str(exc), started
                )

    async def _close_crawler(self) -> None:
        if self._crawler is not None:
            try:
                await self._crawler.close()
            except Exception:
                pass
            self._crawler = None

    async def reap_idle(self, now: float | None = None) -> None:
        now = self._clock() if now is None else now
        if self._crawler is not None and now - self._last_used >= self.idle_seconds:
            await self._close_crawler()

    async def close(self) -> None:
        await self._close_crawler()

    def availability(self) -> ProviderAvailability:
        if self.tier == Tier.PROXY and not self.proxy_pool:
            return ProviderAvailability(
                enabled=True, ready=False, reason="no proxies configured"
            )
        return ProviderAvailability(enabled=True, ready=True)
