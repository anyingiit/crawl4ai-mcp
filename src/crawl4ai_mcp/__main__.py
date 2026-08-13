from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path

from dotenv import dotenv_values
from fastmcp import FastMCP

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

    @asynccontextmanager
    async def lifespan(_app: FastMCP):
        await service.start()
        try:
            yield
        finally:
            await service.close()

    mcp = create_server(service, lifespan=lifespan)
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
