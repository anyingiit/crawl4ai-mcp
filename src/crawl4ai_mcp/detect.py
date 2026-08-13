from __future__ import annotations

from bs4 import BeautifulSoup

from crawl4ai_mcp.models import Decision, FetchResult, Tier

TERMINAL_STATUSES = {401, 404, 410}
CLOUDFLARE_MARKERS = (
    "just a moment", "attention required", "cf-challenge",
    "turnstile", "__cf_chl", "cf-mitigated",
)


def classify(result: FetchResult, visible_text_threshold: int = 200) -> Decision:
    if result.status_code in TERMINAL_STATUSES:
        return Decision.TERMINAL
    haystack = f"{result.html}\n{result.headers}".lower()
    if any(marker in haystack for marker in CLOUDFLARE_MARKERS):
        return Decision.CLOUDFLARE
    if result.status_code in {429, 503} and "retry-after" in {
        key.lower() for key in result.headers
    }:
        return Decision.RATE_LIMITED
    if result.error and result.status_code is None:
        return Decision.RETRYABLE_NETWORK
    text = BeautifulSoup(result.html, "lxml").get_text(" ", strip=True)
    if result.status_code == 200 and len(text) >= visible_text_threshold:
        return Decision.SUCCESS
    if result.status_code == 200 and len(text) < visible_text_threshold:
        return Decision.NEEDS_JS if "<script" in result.html.lower() else Decision.SHORT_STATIC
    return Decision.FAILED


def next_tiers(current: Tier, decision: Decision, maximum: Tier) -> list[Tier]:
    if decision in {Decision.SUCCESS, Decision.SHORT_STATIC, Decision.TERMINAL}:
        return []
    skip_proxy = decision == Decision.CLOUDFLARE
    if decision == Decision.RATE_LIMITED:
        start = max(current + 1, Tier.PROXY)
    else:
        start = current + 1
    return [
        tier for tier in Tier
        if start <= tier <= maximum
        and not (skip_proxy and tier == Tier.PROXY)
    ]
