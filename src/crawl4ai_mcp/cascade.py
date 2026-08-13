from __future__ import annotations

import time
from typing import Callable

from crawl4ai_mcp.detect import classify, next_tiers
from crawl4ai_mcp.models import (
    AttemptResponse,
    Decision,
    FetchResult,
    ScrapeOutcome,
    ScrapeResponse,
    Tier,
)
from crawl4ai_mcp.policy import PolicyStore
from crawl4ai_mcp.providers.base import FetchProvider
from crawl4ai_mcp.render import render_html


class CascadeInputError(ValueError):
    """Rejects invalid cascade inputs before any cooldown, policy, or provider work."""


def _extend(queue: list[Tier], tiers: list[Tier]) -> None:
    for tier in tiers:
        if tier not in queue:
            queue.append(tier)


def _attempt(fetched: FetchResult, decision: Decision) -> AttemptResponse:
    return AttemptResponse(
        tier=fetched.tier.name.lower(),
        decision=decision,
        cost_kind=fetched.cost_kind,
        status_code=fetched.target_status_code,
        elapsed_ms=fetched.elapsed_ms,
        error=fetched.error,
    )


def _elapsed(attempts: list[AttemptResponse]) -> int:
    return sum(attempt.elapsed_ms for attempt in attempts)


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
    ) -> ScrapeOutcome:
        if force is not None and force > maximum:
            raise CascadeInputError(
                f"force tier {force.name} exceeds maximum tier {maximum.name}"
            )
        now = int(self._clock())
        if force is None:
            cooldown = await self.policy.get_active_cooldown(url, now)
            if cooldown is not None:
                response = ScrapeResponse(
                    url=url,
                    status="cooldown",
                    elapsed_ms=0,
                    cooldown_until=cooldown.cooldown_until,
                    error=cooldown.last_error_kind,
                )
                return ScrapeOutcome(
                    response=response, raw_html=None, effective_url=url
                )
        if force is not None:
            start = force
        else:
            start = min(await self.policy.get_start_tier(url, now), maximum)
        queue: list[Tier] = [start]
        attempted: set[Tier] = set()
        attempt_counts: dict[Tier, int] = {}
        cloudflare_seen = False
        attempts: list[AttemptResponse] = []
        while queue:
            tier = queue.pop(0)
            if tier > maximum or tier in attempted:
                continue
            provider = self.providers.get(tier)
            if provider is None or not provider.availability().ready:
                _extend(queue, next_tiers(tier, Decision.PROVIDER_FAILURE, maximum))
                continue
            fetched: FetchResult = await provider.fetch(url)
            self.calls.append(provider.tier)
            attempted.add(tier)
            count = attempt_counts.get(tier, 0) + 1
            attempt_counts[tier] = count
            decision = classify(fetched, self.threshold)
            if decision == Decision.CLOUDFLARE:
                cloudflare_seen = True
            attempts.append(_attempt(fetched, decision))
            if decision in {Decision.SUCCESS, Decision.SHORT_STATIC}:
                markdown = fetched.markdown or await self._render(url, fetched.html)
                await self.policy.record_success(url, tier, now)
                response = ScrapeResponse(
                    url=fetched.url,
                    status="success",
                    content=markdown,
                    tier_used=fetched.tier.name.lower(),
                    cost_kind=fetched.cost_kind,
                    elapsed_ms=_elapsed(attempts),
                    attempts=attempts,
                )
                return ScrapeOutcome(
                    response=response, raw_html=None, effective_url=fetched.url
                )
            if decision == Decision.TERMINAL:
                response = ScrapeResponse(
                    url=fetched.url,
                    status="terminal",
                    tier_used=fetched.tier.name.lower(),
                    cost_kind=fetched.cost_kind,
                    elapsed_ms=_elapsed(attempts),
                    attempts=attempts,
                    error=fetched.error or f"HTTP {fetched.target_status_code}",
                )
                return ScrapeOutcome(
                    response=response, raw_html=None, effective_url=fetched.url
                )
            if decision == Decision.POLICY_REJECTED:
                response = ScrapeResponse(
                    url=url,
                    status="failed",
                    elapsed_ms=_elapsed(attempts),
                    attempts=attempts,
                    error=fetched.policy_error or "policy rejected",
                )
                return ScrapeOutcome(
                    response=response, raw_html=None, effective_url=url
                )
            if decision == Decision.TARGET_NETWORK and count == 1:
                attempted.discard(tier)
                queue.insert(0, tier)
                continue
            if decision == Decision.TARGET_NETWORK:
                policy = await self.policy.record_failure(url, "target_network", now)
                response = ScrapeResponse(
                    url=url,
                    status="failed",
                    elapsed_ms=_elapsed(attempts),
                    attempts=attempts,
                    cooldown_until=policy.cooldown_until,
                    error="target network failure",
                )
                return ScrapeOutcome(
                    response=response, raw_html=None, effective_url=url
                )
            candidates = next_tiers(tier, decision, maximum)
            if cloudflare_seen:
                candidates = [candidate for candidate in candidates if candidate != Tier.PROXY]
            _extend(queue, candidates)
        policy = await self.policy.record_failure(url, "all_failed", now)
        response = ScrapeResponse(
            url=url,
            status="failed",
            elapsed_ms=_elapsed(attempts),
            attempts=attempts,
            cooldown_until=policy.cooldown_until,
            error="all tiers failed",
        )
        return ScrapeOutcome(response=response, raw_html=None, effective_url=url)
