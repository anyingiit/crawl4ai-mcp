from __future__ import annotations

import asyncio
import time
from collections import deque
from urllib.parse import unquote, urlsplit

import psutil
from crawl4ai.async_configs import ProxyConfig

from crawl4ai_mcp.cascade import CascadeEngine, CascadeInputError
from crawl4ai_mcp.config import AppConfig
from crawl4ai_mcp.discovery import crawl_site, map_urls
from crawl4ai_mcp.egress import (
    BrowserRequestGuard,
    PinnedEgressProxy,
    UpstreamProxy,
    UrlPolicy,
)
from crawl4ai_mcp.models import (
    DiagnoseDomainPolicy,
    MaxDepth,
    MaxPages,
    ScrapeFormat,
    Tier,
    TierName,
)
from crawl4ai_mcp.policy import PolicyStore
from crawl4ai_mcp.providers.base import FetchProvider
from crawl4ai_mcp.providers.browser import BrowserProvider
from crawl4ai_mcp.providers.camoufox import CamoufoxProvider
from crawl4ai_mcp.providers.firecrawl import FirecrawlProvider
from crawl4ai_mcp.providers.http import HttpProvider
from crawl4ai_mcp.providers.rayobyte import RayobyteProvider


def parse_tier(value: str | None) -> Tier | None:
    if value is None or value == "":
        return None
    try:
        return Tier[value.upper()]
    except KeyError:
        raise ValueError(
            f"unknown tier {value!r}; expected one of "
            f"{', '.join(tier.name.lower() for tier in Tier)}"
        ) from None


def parse_proxy_url(url: str) -> ProxyConfig:
    parts = urlsplit(url)
    scheme = parts.scheme or "http"
    host = parts.hostname or ""
    port = parts.port or (443 if scheme == "https" else 80)
    return ProxyConfig(
        server=f"{scheme}://{host}:{port}",
        username=unquote(parts.username) if parts.username else None,
        password=unquote(parts.password) if parts.password else None,
    )


def parse_upstream_proxy(url: str) -> UpstreamProxy:
    parts = urlsplit(url)
    scheme = parts.scheme or "http"
    host = parts.hostname or ""
    port = parts.port or (443 if scheme == "https" else 80)
    return UpstreamProxy(
        server=f"{scheme}://{host}:{port}",
        username=unquote(parts.username) if parts.username else None,
        password=unquote(parts.password) if parts.password else None,
    )


class CrawlService:
    def __init__(
        self,
        config: AppConfig,
        providers: dict[Tier, FetchProvider] | None = None,
        engine: CascadeEngine | None = None,
        reaper_interval: float = 30.0,
    ):
        self.config = config
        self.policy: PolicyStore | None = None
        self.providers: dict[Tier, FetchProvider] = providers or {}
        self.engine: CascadeEngine | None = engine
        self._reaper_interval = reaper_interval
        self._reaper_task: asyncio.Task | None = None
        self._recent_failures: deque[dict] = deque(maxlen=50)
        self._close_events: list[str] = []
        self._url_policy: UrlPolicy | None = None
        self._egress_proxy: PinnedEgressProxy | None = None
        self._request_guard: BrowserRequestGuard | None = None

    def _build_providers(
        self, enabled: set[Tier], semaphore: asyncio.Semaphore
    ) -> dict[Tier, FetchProvider]:
        providers: dict[Tier, FetchProvider] = {}
        if Tier.HTTP in enabled:
            providers[Tier.HTTP] = HttpProvider(
                policy=self._url_policy,
                concurrency=self.config.http_concurrency,
                timeout_seconds=10,
            )
        if Tier.STEALTH in enabled:
            providers[Tier.STEALTH] = BrowserProvider(
                Tier.STEALTH, self.config.chromium_idle_seconds, semaphore,
                egress_proxy=self._egress_proxy,
                request_guard=self._request_guard,
            )
        if Tier.UNDETECTED in enabled:
            providers[Tier.UNDETECTED] = BrowserProvider(
                Tier.UNDETECTED, self.config.chromium_idle_seconds, semaphore,
                egress_proxy=self._egress_proxy,
                request_guard=self._request_guard,
            )
        if Tier.CAMOUFOX in enabled:
            providers[Tier.CAMOUFOX] = CamoufoxProvider(
                enabled=True,
                idle_seconds=self.config.camoufox_idle_seconds,
                semaphore=semaphore,
                egress_proxy=self._egress_proxy,
                request_guard=self._request_guard,
            )
        if Tier.PROXY in enabled:
            proxies = list(self.config.webshare_proxies) + list(
                self.config.oxylabs_proxies
            )
            providers[Tier.PROXY] = BrowserProvider(
                Tier.PROXY,
                self.config.chromium_idle_seconds,
                semaphore,
                egress_proxy=self._egress_proxy,
                request_guard=self._request_guard,
                proxy_pool=[parse_upstream_proxy(proxy) for proxy in proxies],
            )
        if Tier.RAYOBYTE in enabled:
            providers[Tier.RAYOBYTE] = RayobyteProvider(
                api_url=self.config.rayobyte_api_url,
                api_key=self.config.rayobyte_api_key,
            )
        if Tier.FIRECRAWL in enabled:
            providers[Tier.FIRECRAWL] = FirecrawlProvider(
                api_key=self.config.firecrawl_api_key
            )
        return providers

    async def start(self) -> None:
        self.policy = await PolicyStore.open(
            self.config.database_path_expanded,
            decay_days=self.config.policy_decay_days,
        )
        self._url_policy = UrlPolicy()
        self._egress_proxy = PinnedEgressProxy(self._url_policy)
        await self._egress_proxy.start()
        self._request_guard = BrowserRequestGuard(self._url_policy)
        if not self.providers:
            semaphore = asyncio.Semaphore(self.config.browser_concurrency)
            enabled = set(self.config.enabled_tiers)
            self.providers = self._build_providers(enabled, semaphore)
        if self.engine is None:
            self.engine = CascadeEngine(
                self.providers,
                self.policy,
                threshold=self.config.visible_text_threshold,
            )
        self._reaper_task = asyncio.create_task(self._reaper_loop())

    async def _reaper_loop(self) -> None:
        while True:
            await asyncio.sleep(self._reaper_interval)
            for provider in self.providers.values():
                reap = getattr(provider, "reap_idle", None)
                if reap is not None:
                    try:
                        await reap()
                    except Exception:
                        pass

    async def close(self) -> None:
        if self._reaper_task is not None:
            self._reaper_task.cancel()
            try:
                await self._reaper_task
            except (asyncio.CancelledError, Exception):
                pass
            self._reaper_task = None
        self._close_events.append("_reaper_cancelled")
        await asyncio.gather(
            *(provider.close() for provider in self.providers.values()),
            return_exceptions=True,
        )
        self._close_events.append("_providers_closed")
        if self._egress_proxy is not None:
            await self._egress_proxy.close()
            self._egress_proxy = None
        self._close_events.append("_egress_closed")
        if self.policy is not None:
            await self.policy.close()
            self.policy = None
        self._close_events.append("_policy_closed")

    async def scrape(
        self,
        url: str,
        format: ScrapeFormat = "markdown",
        max_tier: TierName = "firecrawl",
        force_tier: TierName | None = None,
    ) -> dict:
        maximum = parse_tier(max_tier) if max_tier else Tier.FIRECRAWL
        force = parse_tier(force_tier)
        if force is not None and force > maximum:
            raise CascadeInputError(
                f"force tier {force.name} exceeds maximum tier {maximum.name}"
            )
        if self._url_policy is not None:
            await self._url_policy.resolve(url)
        outcome = await self.engine.scrape(url, maximum=maximum, force=force)
        response = outcome.response
        if format == "html":
            if response.status == "success":
                if outcome.raw_html is None:
                    response = response.model_copy(
                        update={
                            "status": "failed",
                            "content": "",
                            "error": "successful provider did not return html",
                        }
                    )
                else:
                    response = response.model_copy(update={"content": outcome.raw_html})
        payload = response.model_dump(mode="json")
        if response.status == "failed":
            self._recent_failures.appendleft(
                {
                    "url": url,
                    "time": int(time.time()),
                    "error": response.error,
                    "attempts": [attempt.tier for attempt in response.attempts],
                }
            )
        return payload

    async def crawl(
        self,
        url: str,
        max_pages: MaxPages = 10,
        max_depth: MaxDepth = 2,
        include_pattern: str | None = None,
    ) -> dict:
        response = await crawl_site(
            url,
            max_pages=max_pages,
            max_depth=max_depth,
            include_pattern=include_pattern,
            engine=self.engine,
            policy=self._url_policy,
        )
        return response.model_dump(mode="json")

    async def map(
        self, url: str, search: str | None = None, limit: int = 100
    ) -> list[str]:
        return await map_urls(
            url,
            search=search,
            limit=limit,
            policy=self._url_policy,
            proxy=self._egress_proxy,
        )

    async def diagnose(self, domain: str | None = None) -> dict:
        provider_status = {}
        browser_state = {}
        for tier, provider in sorted(self.providers.items()):
            availability = provider.availability()
            provider_status[tier.name] = availability.model_dump(mode="json")
            active = getattr(provider, "is_active", None)
            active_fetches = getattr(provider, "active_fetch_count", None)
            last_used = getattr(provider, "last_used", None)
            browser_state[tier.name] = {
                "active": bool(active()) if callable(active) else None,
                "active_fetches": (
                    int(active_fetches()) if callable(active_fetches) else None
                ),
                "last_used": last_used() if callable(last_used) else None,
            }
        policies = await self.policy.list_policies(domain)
        return {
            "rss_bytes": psutil.Process().memory_info().rss,
            "providers": provider_status,
            "browsers": browser_state,
            "recent_failures": list(self._recent_failures),
            "domain_policies": [
                DiagnoseDomainPolicy(
                    domain=policy.domain,
                    best_tier=(
                        policy.best_tier.name.lower()
                        if policy.best_tier is not None
                        else None
                    ),
                    last_success_at=policy.last_success_at,
                    fail_count=policy.fail_count,
                    cooldown_until=policy.cooldown_until,
                    last_error_kind=policy.last_error_kind,
                    updated_at=policy.updated_at,
                ).model_dump(mode="json")
                for policy in policies
            ],
        }
