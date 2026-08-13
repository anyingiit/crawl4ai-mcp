from enum import IntEnum, StrEnum
from pydantic import BaseModel, Field


class Tier(IntEnum):
    HTTP = 0
    STEALTH = 1
    UNDETECTED = 2
    CAMOUFOX = 3
    PROXY = 4
    RAYOBYTE = 5
    FIRECRAWL = 6


class CostKind(StrEnum):
    FREE = "free"
    PROXY_BANDWIDTH = "proxy_bandwidth"
    RAYOBYTE_CREDIT = "rayobyte_credit"
    FIRECRAWL_CREDIT = "firecrawl_credit"


class Decision(StrEnum):
    SUCCESS = "success"
    SHORT_STATIC = "short_static"
    NEEDS_JS = "needs_js"
    CLOUDFLARE = "cloudflare"
    RATE_LIMITED = "rate_limited"
    TERMINAL = "terminal"
    RETRYABLE_NETWORK = "retryable_network"
    FAILED = "failed"


class FetchResult(BaseModel):
    url: str
    tier: Tier
    cost_kind: CostKind
    status_code: int | None = None
    html: str = ""
    markdown: str | None = None
    headers: dict[str, str] = Field(default_factory=dict)
    redirected_url: str | None = None
    elapsed_ms: int
    error: str | None = None


class ProviderAvailability(BaseModel):
    enabled: bool
    ready: bool
    reason: str | None = None


class Attempt(BaseModel):
    tier: Tier
    decision: Decision
    cost_kind: CostKind
    status_code: int | None = None
    elapsed_ms: int
    error: str | None = None


class ScrapeResult(BaseModel):
    url: str
    status: str
    content: str = ""
    tier_used: Tier | None = None
    cost_kind: CostKind | None = None
    elapsed_ms: int
    attempts: list[Attempt] = Field(default_factory=list)
    cooldown_until: int | None = None
    error: str | None = None

    @classmethod
    def success_from(cls, fetched: FetchResult, markdown: str, attempts: list[Attempt]) -> "ScrapeResult":
        return cls(
            url=fetched.url,
            status="success",
            content=markdown,
            tier_used=fetched.tier,
            cost_kind=fetched.cost_kind,
            elapsed_ms=sum(attempt.elapsed_ms for attempt in attempts),
            attempts=attempts,
        )

    @classmethod
    def terminal_from(cls, fetched: FetchResult, attempts: list[Attempt]) -> "ScrapeResult":
        return cls(
            url=fetched.url,
            status="terminal",
            tier_used=fetched.tier,
            cost_kind=fetched.cost_kind,
            elapsed_ms=sum(attempt.elapsed_ms for attempt in attempts),
            attempts=attempts,
            error=fetched.error or f"HTTP {fetched.status_code}",
        )

    @classmethod
    def cooldown(cls, url: str, cooldown_until: int, error: str | None) -> "ScrapeResult":
        return cls(
            url=url,
            status="cooldown",
            elapsed_ms=0,
            cooldown_until=cooldown_until,
            error=error,
        )

    @classmethod
    def failed(cls, url: str, cooldown_until: int, attempts: list[Attempt]) -> "ScrapeResult":
        return cls(
            url=url,
            status="failed",
            elapsed_ms=sum(attempt.elapsed_ms for attempt in attempts),
            attempts=attempts,
            cooldown_until=cooldown_until,
            error="all tiers failed",
        )
