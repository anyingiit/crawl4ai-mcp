from __future__ import annotations

import time
from typing import Protocol, runtime_checkable

from crawl4ai_mcp.models import (
    CostKind,
    FetchResult,
    ProviderAvailability,
    Tier,
)


def failed_result(
    url: str,
    tier: Tier,
    cost_kind: CostKind,
    error: str,
    started_at: float,
    status_code: int | None = None,
    network_error: str | None = None,
    policy_error: str | None = None,
) -> FetchResult:
    return FetchResult(
        url=url,
        tier=tier,
        cost_kind=cost_kind,
        target_status_code=status_code,
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
