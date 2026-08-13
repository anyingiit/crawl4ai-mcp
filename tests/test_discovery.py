import pytest
from crawl4ai_mcp.cascade import CascadeEngine
from crawl4ai_mcp.discovery import crawl_site, map_urls
from crawl4ai_mcp.models import CostKind, FetchResult, ProviderAvailability, Tier
from crawl4ai_mcp.policy import PolicyStore


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

    def factory():
        seeder = FakeSeeder(entries)
        holder["seeder"] = seeder
        return seeder

    factory.holder = holder
    return factory


@pytest.mark.asyncio
async def test_map_urls_extracts_domain_and_config():
    factory = seeder_factory_for([])
    await map_urls("https://docs.example.com/guide", search=None, limit=100, seeder_factory=factory)
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
    await map_urls("https://docs.example.com/", search="installation", limit=10, seeder_factory=factory)
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
    urls = await map_urls("https://docs.example.com/", limit=100, seeder_factory=factory)
    assert urls == [
        "https://docs.example.com/a",
        "https://docs.example.com/b",
    ]


@pytest.mark.asyncio
async def test_map_urls_hard_limits_to_100():
    entries = [entry(f"https://docs.example.com/p{i}") for i in range(500)]
    factory = seeder_factory_for(entries)
    urls = await map_urls("https://docs.example.com/", limit=500, seeder_factory=factory)
    assert len(urls) == 100
    assert factory.holder["seeder"].calls[0][1].max_urls == 100


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
            url=url, tier=tier, cost_kind=CostKind.FREE, status_code=status,
            html=html, markdown="ok", elapsed_ms=1,
        )

    def stealth_handler(url, tier):
        path = urlsplit(url).path or "/"
        status, html = site[path]
        rendered_html = rendered.get(path, html)
        return FetchResult(
            url=url, tier=tier, cost_kind=CostKind.FREE, status_code=status,
            html=rendered_html, markdown="ok", elapsed_ms=1,
        )

    handlers = {Tier.HTTP: http_handler, Tier.STEALTH: stealth_handler}
    providers = {
        tier: ScriptedProvider(tier, handlers.get(tier, http_handler), calls)
        for tier in Tier
    }
    return CascadeEngine(providers, None, threshold=200)


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
    results = await crawl_site("http://localhost:9000/", engine=engine, max_pages=10, max_depth=2)
    scraped = [result.url for result in results]
    assert scraped == [
        "http://localhost:9000/",
        "http://localhost:9000/a",
        "http://localhost:9000/b",
        "http://localhost:9000/c",
    ]
    assert not any("off.example" in url for url in scraped)


@pytest.mark.asyncio
async def test_crawl_site_respects_max_pages_and_max_depth(policy_store):
    calls = []
    engine = make_site_engine(SITE, calls)
    engine.policy = policy_store
    results = await crawl_site("http://localhost:9000/", engine=engine, max_pages=2, max_depth=5)
    assert [r.url for r in results] == ["http://localhost:9000/", "http://localhost:9000/a"]
    calls = []
    engine2 = make_site_engine(SITE, calls)
    engine2.policy = policy_store
    results = await crawl_site("http://localhost:9000/", engine=engine2, max_pages=10, max_depth=1)
    assert [r.url for r in results] == ["http://localhost:9000/", "http://localhost:9000/a", "http://localhost:9000/b"]


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
    results = await crawl_site("http://localhost:9000/", engine=engine, max_pages=10, max_depth=2)
    assert results[0].tier_used == Tier.STEALTH
    first_page_calls = list(calls)
    assert first_page_calls[:2] == [Tier.HTTP, Tier.STEALTH]
    calls.clear()
    results = await crawl_site("http://localhost:9000/b", engine=engine, max_pages=2, max_depth=1)
    assert calls[0] == Tier.STEALTH
    assert results[0].tier_used == Tier.STEALTH
