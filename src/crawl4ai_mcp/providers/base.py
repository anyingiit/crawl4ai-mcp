from __future__ import annotations

import time
from collections.abc import Iterable
from typing import Protocol, runtime_checkable

import httpx

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
    if status_code == 402 or "credit" in detail.lower():
        return ProviderErrorKind.QUOTA
    if status_code == 429:
        return ProviderErrorKind.RATE_LIMIT
    if status_code >= 500:
        return ProviderErrorKind.SERVICE
    return ProviderErrorKind.MALFORMED_RESPONSE


def safe_error_detail(
    value: object, secrets: Iterable[str] | None = None, limit: int = 200
) -> str:
    if not isinstance(value, str):
        return ""
    text = value.strip()
    if secrets:
        for secret in secrets:
            if secret:
                text = text.replace(secret, "[redacted]")
    return text[:limit]


def extract_error_detail(
    response: httpx.Response, secrets: Iterable[str] | None = None
) -> str:
    try:
        parsed = response.json()
    except Exception:
        try:
            return safe_error_detail(response.text, secrets)
        except Exception:
            return ""
    if isinstance(parsed, dict):
        for key in ("error", "detail", "message"):
            detail = safe_error_detail(parsed.get(key), secrets)
            if detail:
                return detail
        return ""
    try:
        return safe_error_detail(response.text, secrets)
    except Exception:
        return ""


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


def unexpected_failure(
    url: str, tier: Tier, cost_kind: CostKind, exc: Exception
) -> FetchResult:
    """Structured SERVICE failure for an unexpected provider exception."""
    detail = safe_error_detail(str(exc))
    return FetchResult(
        url=url,
        tier=tier,
        cost_kind=cost_kind,
        provider_error_kind=ProviderErrorKind.SERVICE,
        provider_error=detail,
        error=detail,
        elapsed_ms=0,
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
