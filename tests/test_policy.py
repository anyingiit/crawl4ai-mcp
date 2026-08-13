import asyncio

import pytest
from crawl4ai_mcp.egress import UrlPolicyError
from crawl4ai_mcp.models import Tier
from crawl4ai_mcp.policy import PolicyStore, normalize_domain


def test_normalize_domain_rejects_non_public_urls():
    with pytest.raises(UrlPolicyError):
        normalize_domain("file:///etc/passwd")


@pytest.mark.asyncio
async def test_invalid_url_cannot_create_empty_domain_row(tmp_path):
    store = await PolicyStore.open(tmp_path / "policy.db")
    with pytest.raises(UrlPolicyError):
        await store.record_success("file:///etc/passwd", Tier.UNDETECTED, now=1_000)
    assert await store.list_policies() == []
    await store.close()


@pytest.mark.parametrize("url", [
    "https://exa mple.com/a",
    "https://-bad.com/a",
    "https://a..b.com/a",
])
@pytest.mark.asyncio
async def test_policy_store_rejects_malformed_url_before_write(tmp_path, url):
    store = await PolicyStore.open(tmp_path / "policy.db")
    with pytest.raises(UrlPolicyError):
        await store.record_success(url, Tier.UNDETECTED, now=1_000)
    with pytest.raises(UrlPolicyError):
        await store.record_failure(url, "all_failed", now=1_000)
    assert await store.list_policies() == []
    await store.close()


@pytest.mark.parametrize("url", [
    "https://exa mple.com/a",
    "https://-bad.com/a",
    "https://a..b.com/a",
])
@pytest.mark.asyncio
async def test_policy_store_rejects_malformed_url_before_read(tmp_path, url):
    store = await PolicyStore.open(tmp_path / "policy.db")
    with pytest.raises(UrlPolicyError):
        await store.get_start_tier(url, now=1_000)
    with pytest.raises(UrlPolicyError):
        await store.get_active_cooldown(url, now=1_000)
    await store.close()


@pytest.mark.asyncio
async def test_success_is_reused_for_same_domain(tmp_path):
    store = await PolicyStore.open(tmp_path / "policy.db")
    await store.record_success("https://docs.example.com/a", Tier.UNDETECTED, now=1_000)
    assert await store.get_start_tier("https://docs.example.com/b", now=1_001) == Tier.UNDETECTED
    await store.close()


@pytest.mark.asyncio
async def test_policy_decays_one_tier_after_seven_days(tmp_path):
    store = await PolicyStore.open(tmp_path / "policy.db", decay_days=7)
    await store.record_success("https://example.com/a", Tier.RAYOBYTE, now=1_000)
    eight_days = 1_000 + 8 * 86_400
    assert await store.get_start_tier("https://example.com/b", now=eight_days) == Tier.PROXY
    await store.close()


@pytest.mark.asyncio
async def test_failure_backoff_sequence_is_capped_at_24_hours(tmp_path):
    store = await PolicyStore.open(tmp_path / "policy.db")
    expected = [600, 3_600, 21_600, 86_400, 86_400]
    for index, seconds in enumerate(expected, start=1):
        policy = await store.record_failure("https://bad.example/x", "all_failed", now=1_000)
        assert policy.cooldown_until == 1_000 + seconds
    await store.close()


@pytest.mark.asyncio
async def test_active_cooldown_returns_cached_failure(tmp_path):
    store = await PolicyStore.open(tmp_path / "policy.db")
    await store.record_failure("https://bad.example/x", "all_failed", now=1_000)
    policy = await store.get_active_cooldown("https://bad.example/y", now=1_100)
    assert policy is not None
    assert policy.last_error_kind == "all_failed"
    await store.close()


@pytest.mark.asyncio
async def test_concurrent_failures_never_lose_increments(tmp_path):
    store_a = await PolicyStore.open(tmp_path / "policy.db")
    store_b = await PolicyStore.open(tmp_path / "policy.db")

    async def fail_four(store):
        for _ in range(4):
            await store.record_failure(
                "https://race.example/x", "all_failed", now=1_000
            )

    await asyncio.gather(fail_four(store_a), fail_four(store_b))
    policy = await store_a.get_active_cooldown("https://race.example/x", now=1_000)
    assert policy is not None
    assert policy.fail_count == 8
    assert policy.cooldown_until == 1_000 + 86_400
    await store_a.close()
    await store_b.close()


@pytest.mark.asyncio
async def test_concurrent_success_and_failure_never_leave_cooldown_with_zero_failures(tmp_path):
    store_a = await PolicyStore.open(tmp_path / "policy.db")
    store_b = await PolicyStore.open(tmp_path / "policy.db")

    async def succeed_ten(store):
        for _ in range(10):
            await store.record_success(
                "https://mix.example/x", Tier.STEALTH, now=1_000
            )

    async def fail_ten(store):
        for _ in range(10):
            await store.record_failure(
                "https://mix.example/x", "all_failed", now=1_000
            )

    await asyncio.gather(succeed_ten(store_a), fail_ten(store_b))
    row = (await store_a.list_policies())[0]
    if row.fail_count == 0:
        assert row.cooldown_until is None
    else:
        backoff = (600, 3_600, 21_600, 86_400)[min(row.fail_count, 4) - 1]
        assert row.cooldown_until == 1_000 + backoff
    await store_a.close()
    await store_b.close()


@pytest.mark.asyncio
async def test_concurrent_successes_and_failures_serialize_without_mixed_rows(tmp_path):
    store = await PolicyStore.open(tmp_path / "policy.db")
    await asyncio.gather(
        *(
            store.record_success("https://same.example/x", Tier.UNDETECTED, now=1_000 + index)
            if index % 2 == 0
            else store.record_failure("https://same.example/x", "target_network", now=1_000 + index)
            for index in range(12)
        )
    )
    rows = await store.list_policies()
    assert len(rows) == 1
    row = rows[0]
    if row.last_error_kind == "target_network":
        assert row.fail_count == 1
        assert row.cooldown_until is not None
        assert row.cooldown_until >= 1_000 + 600
    else:
        assert row.fail_count == 0
        assert row.cooldown_until is None
        assert row.best_tier == Tier.UNDETECTED
    await store.close()


@pytest.mark.asyncio
async def test_list_policies_accepts_bare_hostname(tmp_path):
    store = await PolicyStore.open(tmp_path / "policy.db")
    await store.record_success("https://example.com/a", Tier.STEALTH, now=1_000)
    rows = await store.list_policies("example.com")
    assert [row.domain for row in rows] == ["example.com"]
    rows = await store.list_policies("ExAmPlE.COM.")
    assert [row.domain for row in rows] == ["example.com"]
    await store.close()


@pytest.mark.asyncio
async def test_list_policies_rejects_private_literal_hostname(tmp_path):
    store = await PolicyStore.open(tmp_path / "policy.db")
    with pytest.raises(UrlPolicyError):
        await store.list_policies("127.0.0.1")
    with pytest.raises(UrlPolicyError):
        await store.list_policies("user@example.com")
    await store.close()
