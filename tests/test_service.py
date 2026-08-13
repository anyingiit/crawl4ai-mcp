import asyncio
import ipaddress

import pytest

from crawl4ai_mcp.config import AppConfig
from crawl4ai_mcp.egress import UrlPolicy, UrlPolicyError, UrlPolicyReason
from crawl4ai_mcp.models import CostKind, FetchResult, ProviderAvailability, Tier
from crawl4ai_mcp.service import CrawlService


def public_policy(address: str = "93.184.216.34") -> UrlPolicy:
    async def resolver(_host: str, _port: int):
        return [ipaddress.ip_address(address)]

    return UrlPolicy(resolver)


class StubProvider:
    def __init__(self, tier, semaphore=None, close_log=None, reap_log=None):
        self.tier = tier
        self.semaphore = semaphore
        self.close_log = close_log
        self.reap_log = reap_log
        self.closed = False
        self.last_used = None
        self.reason = None

    async def fetch(self, url):
        return FetchResult(
            url=url, tier=self.tier, cost_kind=CostKind.FREE,
            status_code=200, html="<main>" + "x" * 300 + "</main>",
            markdown="# Ok", elapsed_ms=1,
        )

    async def close(self):
        self.closed = True
        if self.close_log is not None:
            self.close_log.append(self.tier.name)

    async def reap_idle(self):
        if self.reap_log is not None:
            self.reap_log.append(self.tier.name)

    def availability(self):
        return ProviderAvailability(enabled=True, ready=True, reason=self.reason)

    def is_active(self):
        return False


@pytest.fixture
def config(tmp_path):
    return AppConfig(database_path=tmp_path / "policy.db")


async def make_service(config, providers=None, reaper_interval=30.0):
    service = CrawlService(config, providers=providers, reaper_interval=reaper_interval)
    await service.start()
    return service


@pytest.mark.asyncio
async def test_browser_providers_share_one_semaphore(config):
    service = await make_service(config)
    stealth_sem = service.providers[Tier.STEALTH]._semaphore
    undetected_sem = service.providers[Tier.UNDETECTED]._semaphore
    proxy_sem = service.providers[Tier.PROXY]._semaphore
    assert stealth_sem is undetected_sem
    assert stealth_sem is proxy_sem
    await service.close()


@pytest.mark.asyncio
async def test_providers_are_lazy_after_start(config):
    service = await make_service(config)
    assert service.providers[Tier.STEALTH].is_active() is False
    assert service.providers[Tier.CAMOUFOX].is_active() is False
    await service.close()


@pytest.mark.asyncio
async def test_reaper_runs_periodically(config):
    reaped = []
    providers = {
        Tier.STEALTH: StubProvider(Tier.STEALTH, reap_log=reaped),
    }
    service = await make_service(config, providers=providers, reaper_interval=0.05)
    await asyncio.sleep(0.15)
    assert reaped
    await service.close()


@pytest.mark.asyncio
async def test_close_order_reaper_then_providers_then_egress_then_policy(config):
    close_log = []
    providers = {
        Tier.STEALTH: StubProvider(Tier.STEALTH, close_log=close_log),
    }
    service = await make_service(config, providers=providers, reaper_interval=0.05)
    await service.close()
    assert service._close_events == [
        "_reaper_cancelled", "_providers_closed", "_egress_closed", "_policy_closed",
    ]
    assert close_log == ["STEALTH"]
    assert service.providers[Tier.STEALTH].closed is True
    assert service.policy is None


@pytest.mark.asyncio
async def test_service_shares_one_url_policy_egress_proxy_and_guard(config):
    service = await make_service(config)
    try:
        assert service.providers[Tier.STEALTH].egress_proxy is service._egress_proxy
        assert service.providers[Tier.UNDETECTED].egress_proxy is service._egress_proxy
        assert service.providers[Tier.PROXY].egress_proxy is service._egress_proxy
        assert service.providers[Tier.CAMOUFOX].egress_proxy is service._egress_proxy
        assert service.providers[Tier.HTTP]._policy is service._url_policy
        guard = service._request_guard
        assert service.providers[Tier.STEALTH].request_guard is guard
        assert service.providers[Tier.UNDETECTED].request_guard is guard
        assert service.providers[Tier.PROXY].request_guard is guard
        assert service.providers[Tier.CAMOUFOX].request_guard is guard
        assert service._url_policy is not None
        assert service._egress_proxy is not None
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_scrape_success_returns_structured_result(config):
    providers = {Tier.HTTP: StubProvider(Tier.HTTP)}
    service = await make_service(config, providers=providers)
    service._url_policy = public_policy()
    try:
        payload = await service.scrape("https://example.com")
        assert payload["status"] == "success"
        assert payload["tier_used"] == "http"
        assert payload["cost_kind"] == "free"
        assert isinstance(payload["elapsed_ms"], int)
        assert payload["content"] == "# Ok"
        assert len(payload["attempts"]) == 1
        assert payload["attempts"][0]["decision"] == "success"
        assert list(service._recent_failures) == []
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_service_rejects_private_url_before_policy_lookup(config):
    class BoomEngine:
        def __init__(self):
            self.calls = []

        async def scrape(self, url, maximum=Tier.FIRECRAWL, force=None):
            self.calls.append(url)
            raise AssertionError("engine must not run for rejected url")

    engine = BoomEngine()
    service = CrawlService(
        config,
        providers={Tier.HTTP: StubProvider(Tier.HTTP)},
        engine=engine,
    )
    await service.start()
    try:
        with pytest.raises(UrlPolicyError) as exc:
            await service.scrape("http://127.0.0.1/")
        assert exc.value.reason == UrlPolicyReason.NON_GLOBAL_ADDRESS
        assert engine.calls == []
        assert await service.policy.list_policies() == []
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_diagnose_reports_expected_sections(config):
    providers = {Tier.HTTP: StubProvider(Tier.HTTP)}
    service = await make_service(config, providers=providers)
    try:
        report = await service.diagnose()
        assert report["rss_bytes"] > 0
        assert report["providers"]["HTTP"]["ready"] is True
        assert report["browsers"]["HTTP"]["active"] is False
        assert report["recent_failures"] == []
        assert report["domain_policies"] == []
    finally:
        await service.close()
