from __future__ import annotations

from fastmcp import FastMCP
from starlette.responses import JSONResponse

from crawl4ai_mcp.models import (
    CrawlResponse,
    DiagnoseResponse,
    MapLimit,
    MapResponse,
    MaxDepth,
    MaxPages,
    ScrapeFormat,
    ScrapeResponse,
    TierName,
)


def create_server(service, lifespan=None) -> FastMCP:
    mcp = FastMCP("crawl4ai-mcp", lifespan=lifespan)

    @mcp.custom_route("/health", methods=["GET"])
    async def health(_request):
        return JSONResponse({"status": "ok"})

    @mcp.tool()
    async def scrape(
        url: str,
        format: ScrapeFormat = "markdown",
        max_tier: TierName = "firecrawl",
        force_tier: TierName | None = None,
    ) -> ScrapeResponse:
        """Fetch a page, escalating through tiers until it succeeds."""
        return ScrapeResponse.model_validate(
            await service.scrape(
                url, format=format, max_tier=max_tier, force_tier=force_tier
            )
        )

    @mcp.tool()
    async def crawl(
        url: str,
        max_pages: MaxPages = 10,
        max_depth: MaxDepth = 2,
        include_pattern: str | None = None,
    ) -> CrawlResponse:
        """Crawl a site breadth-first up to max_pages/max_depth, same-origin only."""
        return CrawlResponse.model_validate(
            await service.crawl(
                url,
                max_pages=max_pages,
                max_depth=max_depth,
                include_pattern=include_pattern,
            )
        )

    @mcp.tool()
    async def map(
        url: str, search: str | None = None, limit: MapLimit = 100
    ) -> MapResponse:
        """List URLs under a site from sitemaps and Common Crawl."""
        return MapResponse(urls=await service.map(url, search=search, limit=limit))

    @mcp.tool()
    async def diagnose(domain: str | None = None) -> DiagnoseResponse:
        """Report memory, provider availability, browser state, failures, and domain policy."""
        return DiagnoseResponse.model_validate(await service.diagnose(domain))

    return mcp
