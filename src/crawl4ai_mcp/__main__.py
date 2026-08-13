from __future__ import annotations

import asyncio
from pathlib import Path

from dotenv import dotenv_values

from crawl4ai_mcp.config import load_config
from crawl4ai_mcp.server import create_server
from crawl4ai_mcp.service import CrawlService


def main() -> None:
    config = load_config(
        Path("config.toml"),
        env=dotenv_values(Path(".env")),
    )
    service = CrawlService(config)
    mcp = create_server(service)

    @mcp.lifespan
    async def lifespan(_app):
        await service.start()
        try:
            yield
        finally:
            await service.close()

    mcp.run(
        transport="http",
        host="127.0.0.1",
        port=11236,
        path="/mcp",
        host_origin_protection=True,
        allowed_hosts=["127.0.0.1:11236", "localhost:11236"],
    )


if __name__ == "__main__":
    main()
