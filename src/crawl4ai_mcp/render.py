from __future__ import annotations

from crawl4ai import AsyncWebCrawler
from crawl4ai.async_configs import CrawlerRunConfig
from crawl4ai.async_crawler_strategy import AsyncCrawlResponse, AsyncCrawlerStrategy
from crawl4ai.markdown_generation_strategy import DefaultMarkdownGenerator


class StaticHtmlStrategy(AsyncCrawlerStrategy):
    def __init__(self, html: str):
        self.html = html

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return None

    async def crawl(self, url: str, **kwargs) -> AsyncCrawlResponse:
        return AsyncCrawlResponse(
            html=self.html,
            response_headers={},
            status_code=200,
        )


async def render_html(url: str, html: str) -> str:
    crawler = AsyncWebCrawler(crawler_strategy=StaticHtmlStrategy(html))
    config = CrawlerRunConfig(markdown_generator=DefaultMarkdownGenerator())
    container = await crawler.arun(url=url, config=config)
    results = getattr(container, "_results", container)
    result = results[0] if isinstance(results, list) else results
    return result.markdown.fit_markdown or result.markdown.raw_markdown
