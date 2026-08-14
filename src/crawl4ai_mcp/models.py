from dataclasses import dataclass
from enum import IntEnum, StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, Field

TierName = Literal[
    "http",
    "stealth",
    "undetected",
    "camoufox",
    "proxy",
    "rayobyte",
    "firecrawl",
]

ScrapeFormat = Literal["markdown", "html"]

MapLimit = Annotated[int, Field(ge=1, le=100)]
MaxPages = Annotated[int, Field(ge=1, le=100)]
MaxDepth = Annotated[int, Field(ge=1, le=5)]


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


class ProviderErrorKind(StrEnum):
    AUTH = "auth"
    QUOTA = "quota"
    RATE_LIMIT = "rate_limit"
    TRANSPORT = "transport"
    SERVICE = "service"
    MALFORMED_RESPONSE = "malformed_response"


class Decision(StrEnum):
    SUCCESS = "success"
    SHORT_STATIC = "short_static"
    NEEDS_JS = "needs_js"
    CLOUDFLARE = "cloudflare"
    RATE_LIMITED = "rate_limited"
    TERMINAL = "terminal"
    TARGET_NETWORK = "target_network"
    PROVIDER_FAILURE = "provider_failure"
    POLICY_REJECTED = "policy_rejected"
    FAILED = "failed"


class FetchResult(BaseModel):
    url: str
    tier: Tier
    cost_kind: CostKind
    target_status_code: int | None = None
    provider_status_code: int | None = None
    provider_error_kind: ProviderErrorKind | None = None
    provider_error: str | None = None
    network_error: str | None = None
    policy_error: str | None = None
    html: str = ""
    markdown: str | None = None
    headers: dict[str, str] = Field(default_factory=dict)
    redirected_url: str | None = None
    elapsed_ms: int
    error: str | None = None


class AttemptResponse(BaseModel):
    tier: TierName
    decision: Decision
    cost_kind: CostKind
    target_status_code: int | None = None
    provider_status_code: int | None = None
    provider_error_kind: ProviderErrorKind | None = None
    provider_error: str | None = None
    elapsed_ms: int
    error: str | None = None


class ScrapeResponse(BaseModel):
    url: str
    status: str
    content: str = ""
    tier_used: TierName | None = None
    cost_kind: CostKind | None = None
    elapsed_ms: int
    attempts: list[AttemptResponse] = Field(default_factory=list)
    cooldown_until: int | None = None
    error: str | None = None


@dataclass(frozen=True, slots=True)
class ScrapeOutcome:
    response: ScrapeResponse
    raw_html: str | None
    effective_url: str


class CrawlPage(BaseModel):
    url: str
    response: ScrapeResponse


class CrawlStats(BaseModel):
    attempted_pages: int
    successful_pages: int
    failed_pages: int
    max_depth_reached: int
    elapsed_ms: int


class CrawlResponse(BaseModel):
    pages: list[CrawlPage] = Field(default_factory=list)
    stats: CrawlStats


class ProviderAvailability(BaseModel):
    enabled: bool
    ready: bool
    reason: str | None = None


class MapResponse(BaseModel):
    urls: list[str]


class BrowserState(BaseModel):
    active: bool | None = None
    active_fetches: int | None = None
    last_used: float | None = None


class RecentFailure(BaseModel):
    url: str
    time: int
    error: str | None = None
    attempts: list[TierName] = Field(default_factory=list)


class DiagnoseDomainPolicy(BaseModel):
    domain: str
    best_tier: TierName | None = None
    last_success_at: int | None = None
    fail_count: int = 0
    cooldown_until: int | None = None
    last_error_kind: str | None = None
    updated_at: int


class DiagnoseResponse(BaseModel):
    rss_bytes: int
    providers: dict[str, ProviderAvailability]
    browsers: dict[str, BrowserState]
    recent_failures: list[RecentFailure] = Field(default_factory=list)
    domain_policies: list[DiagnoseDomainPolicy] = Field(default_factory=list)
