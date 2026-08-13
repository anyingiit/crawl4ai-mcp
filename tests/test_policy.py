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
