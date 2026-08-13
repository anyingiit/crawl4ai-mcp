from __future__ import annotations

from collections import deque
from fnmatch import fnmatch
from urllib.parse import urlsplit, urlunsplit, urljoin

from bs4 import BeautifulSoup

from crawl4ai_mcp.models import ScrapeResult, Tier

MAX_MAP_URLS = 100
MAX_CRAWL_PAGES = 100
MAX_CRAWL_DEPTH = 5


def _hostname(url: str) -> str:
    return (urlsplit(url).hostname or "").lower().rstrip(".")


def _without_fragment(url: str) -> str:
    parts = urlsplit(url)
    return urlunsplit(parts._replace(fragment=""))


async def map_urls(
    url: str,
    search: str | None = None,
    limit: int = 100,
    seeder_factory=None,
) -> list[str]:
    limit = min(limit, MAX_MAP_URLS)
    domain = _hostname(url)

    from crawl4ai.async_configs import SeedingConfig
    from crawl4ai.async_url_seeder import AsyncUrlSeeder

    seeder = seeder_factory() if seeder_factory is not None else AsyncUrlSeeder()
    try:
        config = SeedingConfig(
            source="sitemap+cc",
            max_urls=limit,
            concurrency=20,
            hits_per_sec=5,
            query=search,
            extract_head=bool(search),
        )
        entries = await seeder.urls(domain, config)
    finally:
        await seeder.close()

    seen: set[str] = set()
    urls: list[str] = []
    for entry in entries:
        candidate = entry.get("url") if isinstance(entry, dict) else entry
        if not candidate:
            continue
        candidate = _without_fragment(candidate)
        if _hostname(candidate) != domain:
            continue
        if candidate in seen:
            continue
        seen.add(candidate)
        urls.append(candidate)
        if len(urls) >= MAX_MAP_URLS:
            break
    return urls


def extract_links(
    html: str, base_url: str, origin: str, include_pattern: str | None = None
) -> list[str]:
    links: list[str] = []
    soup = BeautifulSoup(html, "lxml")
    for anchor in soup.find_all("a", href=True):
        href = anchor["href"].strip()
        if not href or href.startswith(("javascript:", "mailto:", "tel:", "#")):
            continue
        absolute = _without_fragment(urljoin(base_url, href))
        if _hostname(absolute) != origin:
            continue
        if include_pattern and not fnmatch(absolute, include_pattern):
            continue
        links.append(absolute)
    return links


async def _page_html(engine, page_url: str) -> str:
    provider = engine.providers.get(Tier.HTTP)
    if provider is None:
        return ""
    try:
        fetched = await provider.fetch(page_url)
        return fetched.html
    except Exception:
        return ""


async def crawl_site(
    url: str,
    max_pages: int = 10,
    max_depth: int = 2,
    include_pattern: str | None = None,
    engine=None,
) -> list[ScrapeResult]:
    if not 1 <= max_pages <= MAX_CRAWL_PAGES:
        raise ValueError(f"max_pages must be between 1 and {MAX_CRAWL_PAGES}")
    if not 1 <= max_depth <= MAX_CRAWL_DEPTH:
        raise ValueError(f"max_depth must be between 1 and {MAX_CRAWL_DEPTH}")

    origin = _hostname(url)
    queue: deque[tuple[str, int]] = deque([(url, 0)])
    visited: set[str] = set()
    results: list[ScrapeResult] = []
    while queue and len(results) < max_pages:
        page_url, depth = queue.popleft()
        if page_url in visited:
            continue
        visited.add(page_url)
        result = await engine.scrape(page_url)
        results.append(result)
        if depth >= max_depth or result.status != "success":
            continue
        html = await _page_html(engine, page_url)
        for link in extract_links(html, page_url, origin, include_pattern):
            if link not in visited and link not in {item[0] for item in queue}:
                queue.append((link, depth + 1))
    return results
