from __future__ import annotations

import time
from typing import Protocol, runtime_checkable

from crawl4ai_mcp.models import (
    CostKind,
    FetchResult,
    ProviderAvailability,
    ProviderErrorKind,
    Tier,
)


def classify_provider_error(status_code: int, detail: str = "") -> ProviderErrorKind:
    if status_code in {401, 403}:
        return ProviderErrorKind.AUTH
    if status_code == 429:
        return ProviderErrorKind.RATE_LIMIT
    if status_code >= 500:
        return ProviderErrorKind.SERVICE
    if status_code == 402 or "credit" in detail.lower():
        return ProviderErrorKind.QUOTA
    return ProviderErrorKind.MALFORMED_RESPONSE


def failed_result(
    url: str,
    tier: Tier,
    cost_kind: CostKind,
    error: str,
    started_at: float,
    target_status_code: int | None = None,
    provider_status_code: int | None = None,
    provider_error_kind: ProviderErrorKind | None = None,
    provider_error: str | None = None,
    network_error: str | None = None,
    policy_error: str | None = None,
) -> FetchResult:
    return FetchResult(
        url=url,
        tier=tier,
        cost_kind=cost_kind,
        target_status_code=target_status_code,
        provider_status_code=provider_status_code,
        provider_error_kind=provider_error_kind,
        provider_error=provider_error,
        network_error=network_error,
        policy_error=policy_error,
        error=error,
        elapsed_ms=int((time.monotonic() - started_at) * 1000),
    )


@runtime_checkable
class FetchProvider(Protocol):
    tier: Tier
    cost_kind: CostKind

    async def fetch(self, url: str) -> FetchResult:
        raise NotImplementedError

    async def close(self) -> None:
        raise NotImplementedError

    def availability(self) -> ProviderAvailability:
        raise NotImplementedError
