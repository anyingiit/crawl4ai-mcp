from __future__ import annotations

import time
from collections import deque
from fnmatch import fnmatch
from urllib.parse import urlsplit, urlunsplit, urljoin

import httpx
from bs4 import BeautifulSoup
from crawl4ai.async_url_seeder import COLLINFO_URL, AsyncUrlSeeder

from crawl4ai_mcp.egress import PinnedEgressProxy, UrlPolicy
from crawl4ai_mcp.models import ScrapeOutcome, Tier

MAX_MAP_URLS = 100
MAX_CRAWL_PAGES = 100
MAX_CRAWL_DEPTH = 5


def _hostname(url: str) -> str:
    return (urlsplit(url).hostname or "").lower().rstrip(".")


def _without_fragment(url: str) -> str:
    parts = urlsplit(url)
    return urlunsplit(parts._replace(fragment=""))


class PinnedUrlSeeder(AsyncUrlSeeder):
    """AsyncUrlSeeder that fetches the Common Crawl index via the injected
    pinned client instead of constructing an unconfigured httpx client."""

    async def _latest_index(self) -> str:
        if self.index_cache_path.exists() and (
            time.time() - self.index_cache_path.stat().st_mtime
        ) < self.ttl.total_seconds():
            self._log(
                "info",
                "Loading latest CC index from cache: {path}",
                params={"path": self.index_cache_path},
                tag="URL_SEED",
            )
            return self.index_cache_path.read_text().strip()

        self._log(
            "info",
            "Fetching latest Common Crawl index from {url}",
            params={"url": COLLINFO_URL},
            tag="URL_SEED",
        )
        try:
            j = await self.client.get(COLLINFO_URL, timeout=10)
            j.raise_for_status()
            idx = j.json()[0]["id"]
            self.index_cache_path.write_text(idx)
            self._log(
                "success",
                "Successfully fetched and cached CC index: {index_id}",
                params={"index_id": idx},
                tag="URL_SEED",
            )
            return idx
        except httpx.RequestError as exc:
            self._log(
                "error",
                "Network error fetching CC index info: {error}",
                params={"error": str(exc)},
                tag="URL_SEED",
            )
            raise
        except httpx.HTTPStatusError as exc:
            self._log(
                "error",
                "HTTP error fetching CC index info: {status_code}",
                params={"status_code": exc.response.status_code},
                tag="URL_SEED",
            )
            raise
        except Exception as exc:
            self._log(
                "error",
                "Unexpected error fetching CC index info: {error}",
                params={"error": str(exc)},
                tag="URL_SEED",
            )
            raise


async def map_urls(
    url: str,
    search: str | None = None,
    limit: int = 100,
    *,
    policy: UrlPolicy,
    proxy: PinnedEgressProxy,
    seeder_factory=None,
) -> list[str]:
    limit = min(limit, MAX_MAP_URLS)
    domain = _hostname(url)

    await policy.resolve(url)

    from crawl4ai.async_configs import SeedingConfig

    endpoint = proxy.endpoint()
    client = httpx.AsyncClient(
        proxy=endpoint.server,
        trust_env=False,
        follow_redirects=True,
        timeout=30,
    )
    try:
        seeder = (
            seeder_factory(client)
            if seeder_factory is not None
            else PinnedUrlSeeder(client=client)
        )
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
    finally:
        await client.aclose()

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
) -> list[ScrapeOutcome]:
    if not 1 <= max_pages <= MAX_CRAWL_PAGES:
        raise ValueError(f"max_pages must be between 1 and {MAX_CRAWL_PAGES}")
    if not 1 <= max_depth <= MAX_CRAWL_DEPTH:
        raise ValueError(f"max_depth must be between 1 and {MAX_CRAWL_DEPTH}")

    origin = _hostname(url)
    queue: deque[tuple[str, int]] = deque([(url, 0)])
    visited: set[str] = set()
    results: list[ScrapeOutcome] = []
    while queue and len(results) < max_pages:
        page_url, depth = queue.popleft()
        if page_url in visited:
            continue
        visited.add(page_url)
        result = await engine.scrape(page_url)
        results.append(result)
        if depth >= max_depth or result.response.status != "success":
            continue
        html = await _page_html(engine, page_url)
        for link in extract_links(html, page_url, origin, include_pattern):
            if link not in visited and link not in {item[0] for item in queue}:
                queue.append((link, depth + 1))
    return results
