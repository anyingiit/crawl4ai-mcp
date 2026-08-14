import pytest
from crawl4ai_mcp.render import render_html


@pytest.mark.asyncio
async def test_render_html_uses_crawl4ai_markdown_pipeline():
    markdown = await render_html(
        "https://example.com",
        "<html><nav>Noise</nav><main><h1>Title</h1><p>Body text</p></main></html>",
    )
    assert "# Title" in markdown
    assert "Body text" in markdown
