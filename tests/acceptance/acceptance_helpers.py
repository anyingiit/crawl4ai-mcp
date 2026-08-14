"""Shared decision helpers for the configured-provider acceptance tests.

Used by the live tier tests (tests/acceptance/test_live_tiers.py) and
exercised directly, without network, by the unpaid tests in
tests/acceptance/test_acceptance_providers.py.
"""

import re


def configured_provider_skip_reason(provider, tier, enabled_tiers) -> str | None:
    """Return a skip reason, or None when the provider must be asserted.

    Skips are allowed only for intentionally disabled or unconfigured
    optional providers. A provider that is enabled and configured but not
    ready is NOT a skip: the acceptance test must fail on it.
    """
    if tier not in enabled_tiers or provider is None:
        return f"{tier.name.lower()} disabled in config enabled_tiers"
    availability = provider.availability()
    if not availability.enabled:
        return f"{tier.name.lower()} disabled: {availability.reason or 'disabled'}"
    if not availability.ready:
        reason = availability.reason or ""
        if "not configured" in reason or "no proxies" in reason:
            return f"{tier.name.lower()} unconfigured: {reason}"
        return None
    return None


def assert_configured_provider_success(
    result,
    tier,
    expected_cost_kind: str,
    marker: str,
    *,
    marker_is_regex: bool = False,
) -> None:
    """Contract a configured provider must satisfy: successful fetch, exact
    tier, exact cost kind, and the target marker."""
    assert result["status"] == "success", result.get("error")
    assert result["tier_used"] == tier.name.lower()
    assert result["cost_kind"] == expected_cost_kind
    if marker:
        if marker_is_regex:
            assert re.search(marker, result["content"]), (
                f"marker {marker!r} not found"
            )
        else:
            assert marker in result["content"], f"marker {marker!r} not found"
