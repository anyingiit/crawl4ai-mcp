import ipaddress

import httpx
import pytest
from crawl4ai.async_configs import ProxyConfig
from crawl4ai.async_url_seeder import COLLINFO_URL
from crawl4ai_mcp.cascade import CascadeEngine
from crawl4ai_mcp.discovery import PinnedUrlSeeder, crawl_site, extract_links, map_urls
from crawl4ai_mcp.egress import Origin, UrlPolicy, UrlPolicyError
from crawl4ai_mcp.models import (
    CostKind,
    FetchResult,
    ProviderAvailability,
    ScrapeOutcome,
    ScrapeResponse,
    Tier,
)
from crawl4ai_mcp.policy import PolicyStore


def public_policy(address: str = "93.184.216.34") -> UrlPolicy:
    async def resolver(_host: str, _port: int):
        return [ipaddress.ip_address(address)]
    return UrlPolicy(resolver)


def private_policy() -> UrlPolicy:
    async def resolver(_host: str, _port: int):
        return [ipaddress.ip_address("127.0.0.1")]
    return UrlPolicy(resolver)


class FakePinnedProxy:
    def __init__(self):
        self.endpoint_calls = []
        self._upstream_ports = {}

    def endpoint(self, upstream=None):
        self.endpoint_calls.append(upstream)
        if upstream is None:
            port = 41000
        else:
            port = self._upstream_ports.setdefault(
                upstream, 41001 + len(self._upstream_ports)
            )
        return ProxyConfig(server=f"http://127.0.0.1:{port}")


class FakeSeeder:
    def __init__(self, entries):
        self.entries = entries
        self.calls = []
        self.closed = False

    async def urls(self, domain, config):
        self.calls.append((domain, config))
        return self.entries

    async def close(self):
        self.closed = True


def entry(url):
    return {"url": url, "status": 200, "head_data": {}}


def seeder_factory_for(entries):
    holder = {}

    def factory(client):
        seeder = FakeSeeder(entries)
        holder["seeder"] = seeder
        return seeder

    factory.holder = holder
    return factory


class SeederFactory:
    def __init__(self, entries=()):
        self.entries = list(entries)
        self.clients = []
        self.calls = []

    def __call__(self, client):
        pool = next(iter(client._mounts.values()))._pool
        url = pool._proxy_url
        client.proxy_url = f"{url.scheme.decode()}://{url.host.decode()}:{url.port}"
        self.clients.append(client)
        seeder = FakeSeeder(self.entries)
        self.calls.append(seeder)
        return seeder


@pytest.mark.asyncio
async def test_map_urls_validates_root_before_constructing_seeder():
    factory = SeederFactory()
    with pytest.raises(UrlPolicyError):
        await map_urls("http://127.0.0.1/", policy=private_policy(), proxy=FakePinnedProxy(), seeder_factory=factory)
    assert factory.calls == []


@pytest.mark.asyncio
async def test_map_urls_routes_seeder_client_through_pinning_proxy():
    factory = SeederFactory()
    await map_urls("https://example.com/", policy=public_policy(), proxy=FakePinnedProxy(), seeder_factory=factory)
    assert factory.clients[0].proxy_url == "http://127.0.0.1:41000"


class FakeIndexResponse:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


class FakeIndexClient:
    def __init__(self, payload=None):
        self.payload = payload or [{"id": "CC-MAIN-2026-30"}]
        self.calls = []

    async def get(self, url, timeout=30):
        self.calls.append((url, timeout))
        return FakeIndexResponse(self.payload)


def forbid_unconfigured_client(*args, **kwargs):
    raise AssertionError("unconfigured httpx.AsyncClient constructed")


@pytest.mark.asyncio
async def test_pinned_seeder_latest_index_uses_injected_client(tmp_path):
    client = FakeIndexClient()
    seeder = PinnedUrlSeeder(
        client=client,
        base_directory=tmp_path / "base",
        cache_root=tmp_path / "cache",
    )
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(httpx, "AsyncClient", forbid_unconfigured_client)
        index = await seeder._latest_index()
    assert index == "CC-MAIN-2026-30"
    assert client.calls == [(COLLINFO_URL, 10)]
    assert seeder.index_cache_path.read_text() == "CC-MAIN-2026-30"
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(httpx, "AsyncClient", forbid_unconfigured_client)
        cached = await seeder._latest_index()
    assert cached == "CC-MAIN-2026-30"
    assert client.calls == [(COLLINFO_URL, 10)]


class ExplodingSeeder:
    def __init__(self, urls_error=None, close_error=None):
        self.urls_error = urls_error
        self.close_error = close_error
        self.closed = False

    async def urls(self, domain, config):
        if self.urls_error is not None:
            raise self.urls_error
        return []

    async def close(self):
        self.closed = True
        if self.close_error is not None:
            raise self.close_error


class ExplodingSeederFactory:
    def __init__(self, urls_error=None, close_error=None):
        self.clients = []
        self.calls = []
        self.urls_error = urls_error
        self.close_error = close_error

    def __call__(self, client):
        self.clients.append(client)
        seeder = ExplodingSeeder(self.urls_error, self.close_error)
        self.calls.append(seeder)
        return seeder


class ExplodingClientFactory:
    def __init__(self, error=RuntimeError("seeder factory failed")):
        self.clients = []
        self.calls = []
        self.error = error

    def __call__(self, client):
        self.clients.append(client)
        raise self.error


@pytest.mark.asyncio
async def test_map_urls_closes_client_when_seeder_factory_raises():
    factory = ExplodingClientFactory()
    with pytest.raises(RuntimeError):
        await map_urls("https://example.com/", policy=public_policy(), proxy=FakePinnedProxy(), seeder_factory=factory)
    assert factory.clients[0].is_closed is True
    assert factory.calls == []


@pytest.mark.asyncio
async def test_map_urls_closes_seeder_and_client_when_urls_raises():
    factory = ExplodingSeederFactory(urls_error=RuntimeError("boom"))
    with pytest.raises(RuntimeError):
        await map_urls("https://example.com/", policy=public_policy(), proxy=FakePinnedProxy(), seeder_factory=factory)
    assert factory.calls[0].closed is True
    assert factory.clients[0].is_closed is True


@pytest.mark.asyncio
async def test_map_urls_closes_client_when_seeder_close_raises():
    factory = ExplodingSeederFactory(close_error=RuntimeError("close boom"))
    with pytest.raises(RuntimeError):
        await map_urls("https://example.com/", policy=public_policy(), proxy=FakePinnedProxy(), seeder_factory=factory)
    assert factory.calls[0].closed is True
    assert factory.clients[0].is_closed is True


@pytest.mark.asyncio
async def test_map_urls_extracts_domain_and_config():
    factory = seeder_factory_for([])
    await map_urls("https://docs.example.com/guide", search=None, limit=100, policy=public_policy(), proxy=FakePinnedProxy(), seeder_factory=factory)
    seeder = factory.holder["seeder"]
    domain, config = seeder.calls[0]
    assert domain == "docs.example.com"
    assert config.source == "sitemap+cc"
    assert config.max_urls == 100
    assert config.concurrency == 20
    assert config.hits_per_sec == 5
    assert config.query is None
    assert config.extract_head is False
    assert seeder.closed is True


@pytest.mark.asyncio
async def test_map_urls_passes_search_query():
    factory = seeder_factory_for([])
    await map_urls("https://docs.example.com/", search="installation", limit=10, policy=public_policy(), proxy=FakePinnedProxy(), seeder_factory=factory)
    config = factory.holder["seeder"].calls[0][1]
    assert config.query == "installation"
    assert config.extract_head is True
    assert config.max_urls == 10


@pytest.mark.asyncio
async def test_map_urls_dedups_and_filters_origin():
    factory = seeder_factory_for([
        entry("https://docs.example.com/a"),
        entry("https://docs.example.com/a"),
        entry("https://docs.example.com/b"),
        entry("https://docs.example.com/b"),
        entry("https://cdn.other.net/x"),
    ])
    urls = await map_urls("https://docs.example.com/", limit=100, policy=public_policy(), proxy=FakePinnedProxy(), seeder_factory=factory)
    assert urls == [
        "https://docs.example.com/a",
        "https://docs.example.com/b",
    ]


@pytest.mark.asyncio
async def test_map_urls_output_caps_at_100_entries():
    entries = [entry(f"https://docs.example.com/p{i}") for i in range(500)]
    factory = seeder_factory_for(entries)
    urls = await map_urls("https://docs.example.com/", limit=100, policy=public_policy(), proxy=FakePinnedProxy(), seeder_factory=factory)
    assert len(urls) == 100
    assert factory.holder["seeder"].calls[0][1].max_urls == 100


@pytest.mark.parametrize("limit", [0, -1, 101])
@pytest.mark.asyncio
async def test_map_urls_rejects_limit_outside_one_to_one_hundred(limit):
    with pytest.raises(ValueError, match="limit must be between 1 and 100"):
        await map_urls(
            "https://example.com/", limit=limit, policy=public_policy(), proxy=FakePinnedProxy()
        )


@pytest.mark.asyncio
async def test_map_urls_rejects_bad_limit_before_any_client_or_seeder():
    factory = SeederFactory()
    with pytest.raises(ValueError):
        await map_urls(
            "https://example.com/", limit=0, policy=public_policy(), proxy=FakePinnedProxy(), seeder_factory=factory
        )
    assert factory.calls == []
    assert factory.clients == []


class ScriptedProvider:
    def __init__(self, tier, handlers, calls):
        self.tier = tier
        self.handlers = handlers
        self.calls = calls

    async def fetch(self, url):
        self.calls.append(self.tier)
        return self.handlers(url, self.tier)

    async def close(self):
        pass

    def availability(self):
        return ProviderAvailability(enabled=True, ready=True)


def make_site_engine(site, calls, rendered=None):
    from urllib.parse import urlsplit

    rendered = rendered or {}

    def http_handler(url, tier):
        status, html = site[urlsplit(url).path or "/"]
        return FetchResult(
            url=url, tier=tier, cost_kind=CostKind.FREE, target_status_code=status,
            html=html, markdown="ok", elapsed_ms=1,
        )

    def stealth_handler(url, tier):
        path = urlsplit(url).path or "/"
        status, html = site[path]
        rendered_html = rendered.get(path, html)
        return FetchResult(
            url=url, tier=tier, cost_kind=CostKind.FREE, target_status_code=status,
            html=rendered_html, markdown="ok", elapsed_ms=1,
        )

    handlers = {Tier.HTTP: http_handler, Tier.STEALTH: stealth_handler}
    providers = {
        tier: ScriptedProvider(tier, handlers.get(tier, http_handler), calls)
        for tier in Tier
    }
    return CascadeEngine(providers, None, threshold=200)


class ScriptedEngine:
    def __init__(self, outcomes):
        self.outcomes = dict(outcomes)
        self.calls = []

    async def scrape(self, url, maximum=Tier.FIRECRAWL, force=None):
        self.calls.append(url)
        return self.outcomes.get(url) or successful_outcome(url, raw_html=None)


def successful_outcome(url, raw_html="", effective_url=None, markdown="# ok"):
    response = ScrapeResponse(
        url=url,
        status="success",
        content=markdown,
        tier_used="http",
        cost_kind=CostKind.FREE,
        elapsed_ms=1,
    )
    return ScrapeOutcome(
        response=response,
        raw_html=raw_html,
        effective_url=effective_url or url,
    )


def rendered_root_and_child_outcomes():
    return {
        "https://example.com/": successful_outcome(
            "https://example.com/", raw_html="<a href='/rendered'>x</a>"
        ),
        "https://example.com/rendered": successful_outcome(
            "https://example.com/rendered", raw_html="<main>rendered</main>"
        ),
    }


def cross_origin_redirect_outcome():
    return {
        "https://example.com/": successful_outcome(
            "https://example.com/",
            raw_html="<a href='/landed'>x</a>",
            effective_url="https://evil.example/landed",
        ),
    }


@pytest.mark.asyncio
async def test_crawl_extracts_links_from_successful_rendered_response_without_refetch():
    engine = ScriptedEngine(rendered_root_and_child_outcomes())
    crawl = await crawl_site("https://example.com/", engine=engine, policy=public_policy(), max_depth=1)
    assert [page.url for page in crawl.pages] == [
        "https://example.com/", "https://example.com/rendered",
    ]
    assert engine.calls == ["https://example.com/", "https://example.com/rendered"]


def test_extract_links_rejects_scheme_port_and_non_http_changes():
    html = '<a href="https://example.com:443/ok">ok</a><a href="http://example.com/no">no</a><a href="https://example.com:444/no">no</a><a href="file:///etc/passwd">no</a>'
    assert extract_links(html, "https://example.com/root", Origin("https", "example.com", 443)) == [
        "https://example.com/ok"
    ]


@pytest.mark.asyncio
async def test_crawl_does_not_follow_links_after_cross_origin_redirect():
    engine = ScriptedEngine(cross_origin_redirect_outcome())
    crawl = await crawl_site("https://example.com/", engine=engine, policy=public_policy())
    assert len(crawl.pages) == 1


@pytest.mark.asyncio
async def test_crawl_stops_discovery_after_cross_origin_redirect_through_cascade(policy_store):
    calls = []

    def redirect_handler(url, tier):
        return FetchResult(
            url=url, tier=tier, cost_kind=CostKind.FREE, target_status_code=200,
            html='<main><a href="/child">child</a></main>',
            markdown="ok", elapsed_ms=1,
            redirected_url="https://evil.example/landed",
        )

    providers = {tier: ScriptedProvider(tier, redirect_handler, calls) for tier in Tier}
    engine = CascadeEngine(providers, None, threshold=200)
    engine.policy = policy_store
    crawl = await crawl_site("http://localhost:9000/", engine=engine, max_pages=10, max_depth=2)
    assert [page.url for page in crawl.pages] == ["http://localhost:9000/"]
    assert calls == [Tier.HTTP]
    assert crawl.stats.attempted_pages == 1


@pytest.mark.asyncio
async def test_crawl_canonicalizes_root_before_queue_and_fetch():
    engine = ScriptedEngine({
        "https://example.com/path": successful_outcome(
            "https://example.com/path",
            raw_html='<main><a href="path">self</a></main>',
        ),
    })
    crawl = await crawl_site(
        "https://EXAMPLE.com.:443/path#frag", engine=engine,
        policy=public_policy(), max_pages=10, max_depth=2,
    )
    assert [page.url for page in crawl.pages] == ["https://example.com/path"]
    assert engine.calls == ["https://example.com/path"]
    assert crawl.stats.attempted_pages == 1
    assert crawl.stats.successful_pages == 1


def duplicate_links_outcome():
    dups = "".join('<a href="/dup">dup</a>' for _ in range(50))
    return {
        "https://example.com/": successful_outcome(
            "https://example.com/", raw_html=f"<main>{dups}</main>"
        ),
        "https://example.com/dup": successful_outcome(
            "https://example.com/dup", raw_html="<main>dup</main>"
        ),
    }


@pytest.mark.asyncio
async def test_crawl_bounds_queue_when_page_has_duplicate_links():
    engine = ScriptedEngine(duplicate_links_outcome())
    crawl = await crawl_site("https://example.com/", engine=engine, policy=public_policy(), max_pages=10, max_depth=2)
    assert [page.url for page in crawl.pages] == [
        "https://example.com/", "https://example.com/dup",
    ]
    assert engine.calls == ["https://example.com/", "https://example.com/dup"]
    assert crawl.stats.attempted_pages == 2
    assert crawl.stats.successful_pages == 2


@pytest.fixture
async def policy_store(tmp_path):
    store = await PolicyStore.open(tmp_path / "policy.db")
    yield store
    await store.close()


SITE = {
    "/": (200, '<main><a href="/a">A</a><a href="/b">B</a><a href="https://off.example/x">off</a></main>'),
    "/a": (200, '<main><a href="/b">B</a><a href="/c">C</a></main>'),
    "/b": (200, '<main><a href="/c">C</a></main>'),
    "/c": (200, '<main><a href="/c">C</a></main>'),
}


@pytest.mark.asyncio
async def test_crawl_site_bfs_dedup_origin_and_cycle(policy_store):
    calls = []
    engine = make_site_engine(SITE, calls)
    engine.policy = policy_store
    crawl = await crawl_site("http://localhost:9000/", engine=engine, max_pages=10, max_depth=2)
    scraped = [page.url for page in crawl.pages]
    assert scraped == [
        "http://localhost:9000/",
        "http://localhost:9000/a",
        "http://localhost:9000/b",
        "http://localhost:9000/c",
    ]
    assert not any("off.example" in url for url in scraped)
    assert crawl.stats.attempted_pages == 4
    assert crawl.stats.successful_pages == 4
    assert crawl.stats.failed_pages == 0
    assert crawl.stats.max_depth_reached == 2


@pytest.mark.asyncio
async def test_crawl_site_respects_max_pages_and_max_depth(policy_store):
    calls = []
    engine = make_site_engine(SITE, calls)
    engine.policy = policy_store
    crawl = await crawl_site("http://localhost:9000/", engine=engine, max_pages=2, max_depth=5)
    assert [page.url for page in crawl.pages] == ["http://localhost:9000/", "http://localhost:9000/a"]
    assert crawl.stats.max_depth_reached == 1
    calls = []
    engine2 = make_site_engine(SITE, calls)
    engine2.policy = policy_store
    crawl = await crawl_site("http://localhost:9000/", engine=engine2, max_pages=10, max_depth=1)
    assert [page.url for page in crawl.pages] == ["http://localhost:9000/", "http://localhost:9000/a", "http://localhost:9000/b"]


@pytest.mark.asyncio
async def test_crawl_site_rejects_excessive_bounds():
    with pytest.raises(ValueError):
        await crawl_site("http://localhost:9000/", engine=None, max_pages=101)
    with pytest.raises(ValueError):
        await crawl_site("http://localhost:9000/", engine=None, max_depth=6)


@pytest.mark.asyncio
async def test_crawl_site_first_success_sets_tier_for_rest(policy_store):
    hard = dict(SITE)
    hard["/"] = (200, '<div id="app"></div><script src="app.js"></script>')
    calls = []
    engine = make_site_engine(
        hard, calls,
        rendered={"/": "<main>Long rendered root content with links</main>"},
    )
    engine.policy = policy_store
    crawl = await crawl_site("http://localhost:9000/", engine=engine, max_pages=10, max_depth=2)
    assert crawl.pages[0].response.tier_used == "stealth"
    first_page_calls = list(calls)
    assert first_page_calls[:2] == [Tier.HTTP, Tier.STEALTH]
    assert len(calls) == 2
    calls.clear()
    crawl = await crawl_site("http://localhost:9000/b", engine=engine, max_pages=2, max_depth=1)
    assert calls[0] == Tier.STEALTH
    assert crawl.pages[0].response.tier_used == "stealth"


def redirect_alias_outcomes(paid=False):
    root = successful_outcome(
        "https://example.com/start",
        raw_html=(
            '<main><a href="/landed">landed</a>'
            '<a href="/landed">landed again</a></main>'
        ),
        effective_url="https://example.com/landed",
    )
    if paid:
        root.response.tier_used = "rayobyte"
        root.response.cost_kind = CostKind.RAYOBYTE_CREDIT
    return {"https://example.com/start": root}


@pytest.mark.asyncio
async def test_crawl_reports_same_origin_redirect_alias_as_page_url_without_refetch():
    engine = ScriptedEngine(redirect_alias_outcomes())
    crawl = await crawl_site(
        "https://example.com/start", engine=engine, policy=public_policy(),
        max_pages=10, max_depth=2,
    )
    assert [page.url for page in crawl.pages] == ["https://example.com/landed"]
    assert engine.calls == ["https://example.com/start"]
    assert crawl.stats.attempted_pages == 1
    assert crawl.stats.successful_pages == 1


@pytest.mark.asyncio
async def test_paid_tier_redirect_alias_is_never_refetched():
    engine = ScriptedEngine(redirect_alias_outcomes(paid=True))
    crawl = await crawl_site(
        "https://example.com/start", engine=engine, policy=public_policy(),
        max_pages=10, max_depth=2,
    )
    assert [page.url for page in crawl.pages] == ["https://example.com/landed"]
    assert engine.calls == ["https://example.com/start"]
    assert crawl.pages[0].response.tier_used == "rayobyte"
    assert crawl.pages[0].response.cost_kind == CostKind.RAYOBYTE_CREDIT


@pytest.mark.asyncio
async def test_crawl_reports_requested_alias_when_redirect_escapes_origin():
    engine = ScriptedEngine(cross_origin_redirect_outcome())
    crawl = await crawl_site(
        "https://example.com/", engine=engine, policy=public_policy()
    )
    assert [page.url for page in crawl.pages] == ["https://example.com/"]
    assert engine.calls == ["https://example.com/"]


@pytest.mark.asyncio
async def test_map_urls_rejects_scheme_port_and_credential_candidates():
    factory = seeder_factory_for([
        entry("https://docs.example.com/ok"),
        entry("http://docs.example.com/no-scheme"),
        entry("https://docs.example.com:444/no-port"),
        entry("https://user:pass@docs.example.com/no-creds"),
        entry("file:///etc/passwd"),
        entry("https://cdn.other.net/x"),
        entry("not a url"),
    ])
    urls = await map_urls(
        "https://docs.example.com/", limit=100,
        policy=public_policy(), proxy=FakePinnedProxy(), seeder_factory=factory,
    )
    assert urls == ["https://docs.example.com/ok"]


@pytest.mark.asyncio
async def test_map_urls_normalizes_candidate_hosts_and_fragments():
    factory = seeder_factory_for([
        entry("HTTPS://ExAmPlE.COM.:443/a#frag"),
        entry("https://docs.example.com/b#frag"),
    ])
    urls = await map_urls(
        "https://docs.example.com/", limit=100,
        policy=public_policy(), proxy=FakePinnedProxy(), seeder_factory=factory,
    )
    assert urls == ["https://docs.example.com/b"]


@pytest.mark.asyncio
async def test_map_urls_dedupes_normalized_candidates():
    factory = seeder_factory_for([
        entry("https://docs.example.com/a#one"),
        entry("https://docs.example.com/a#two"),
        entry("https://docs.example.com/a"),
    ])
    urls = await map_urls(
        "https://docs.example.com/", limit=100,
        policy=public_policy(), proxy=FakePinnedProxy(), seeder_factory=factory,
    )
    assert urls == ["https://docs.example.com/a"]
