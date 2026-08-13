from __future__ import annotations

import time
from typing import Callable

from crawl4ai_mcp.detect import classify, next_tiers
from crawl4ai_mcp.models import Attempt, Decision, FetchResult, ScrapeResult, Tier
from crawl4ai_mcp.policy import PolicyStore
from crawl4ai_mcp.providers.base import FetchProvider
from crawl4ai_mcp.render import render_html


def _extend(queue: list[Tier], tiers: list[Tier]) -> None:
    for tier in tiers:
        if tier not in queue:
            queue.append(tier)


class CascadeEngine:
    def __init__(
        self,
        providers: dict[Tier, FetchProvider],
        policy: PolicyStore,
        threshold: int = 200,
        clock: Callable[[], float] | None = None,
        render: Callable[[str, str], object] | None = None,
    ):
        self.providers = providers
        self.policy = policy
        self.threshold = threshold
        self._clock = clock or time.time
        self._render = render or render_html
        self.calls: list[Tier] = []

    async def scrape(
        self, url: str, maximum: Tier = Tier.FIRECRAWL, force: Tier | None = None
    ) -> ScrapeResult:
        now = int(self._clock())
        if force is None:
            cooldown = await self.policy.get_active_cooldown(url, now)
            if cooldown is not None:
                return ScrapeResult.cooldown(
                    url, cooldown.cooldown_until, cooldown.last_error_kind
                )
        start = force if force is not None else await self.policy.get_start_tier(url, now)
        queue: list[Tier] = [start]
        attempted: set[Tier] = set()
        attempts: list[Attempt] = []
        network_retry_used = False
        while queue:
            tier = queue.pop(0)
            if tier > maximum or tier in attempted:
                continue
            provider = self.providers.get(tier)
            if provider is None or not provider.availability().ready:
                _extend(queue, next_tiers(tier, Decision.FAILED, maximum))
                continue
            fetched: FetchResult = await provider.fetch(url)
            self.calls.append(provider.tier)
            attempted.add(tier)
            decision = classify(fetched, self.threshold)
            attempts.append(
                Attempt(
                    tier=tier,
                    decision=decision,
                    cost_kind=fetched.cost_kind,
                    status_code=fetched.status_code,
                    elapsed_ms=fetched.elapsed_ms,
                    error=fetched.error,
                )
            )
            if decision in {Decision.SUCCESS, Decision.SHORT_STATIC}:
                markdown = fetched.markdown or await self._render(url, fetched.html)
                await self.policy.record_success(url, tier, now)
                return ScrapeResult.success_from(fetched, markdown, attempts)
            if decision == Decision.TERMINAL:
                return ScrapeResult.terminal_from(fetched, attempts)
            if decision == Decision.RETRYABLE_NETWORK and not network_retry_used:
                network_retry_used = True
                attempted.discard(tier)
                queue.insert(0, tier)
            else:
                _extend(queue, next_tiers(tier, decision, maximum))
        policy = await self.policy.record_failure(url, "all_failed", now)
        return ScrapeResult.failed(url, policy.cooldown_until, attempts)
