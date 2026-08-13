from __future__ import annotations

from fastmcp import FastMCP
from starlette.responses import JSONResponse


def create_server(service, lifespan=None) -> FastMCP:
    mcp = FastMCP("crawl4ai-mcp", lifespan=lifespan)

    @mcp.custom_route("/health", methods=["GET"])
    async def health(_request):
        return JSONResponse({"status": "ok"})

    @mcp.tool()
    async def scrape(
        url: str,
        format: str = "markdown",
        max_tier: str = "firecrawl",
        force_tier: str | None = None,
    ) -> dict:
        """Fetch a page, escalating through tiers until it succeeds."""
        return await service.scrape(url, max_tier=max_tier, force_tier=force_tier)

    @mcp.tool()
    async def crawl(
        url: str,
        max_pages: int = 10,
        max_depth: int = 2,
        include_pattern: str | None = None,
    ) -> list[dict]:
        """Crawl a site breadth-first up to max_pages/max_depth, same-origin only."""
        return await service.crawl(
            url,
            max_pages=max_pages,
            max_depth=max_depth,
            include_pattern=include_pattern,
        )

    @mcp.tool()
    async def map(
        url: str, search: str | None = None, limit: int = 100
    ) -> list[str]:
        """List URLs under a site from sitemaps and Common Crawl."""
        return await service.map(url, search=search, limit=limit)

    @mcp.tool()
    async def diagnose(domain: str | None = None) -> dict:
        """Report memory, provider availability, browser state, failures, and domain policy."""
        return await service.diagnose(domain)

    return mcp
