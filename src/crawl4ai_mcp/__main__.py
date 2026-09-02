from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from dotenv import dotenv_values
from fastmcp import FastMCP

from crawl4ai_mcp.config import AppConfig, load_config
from crawl4ai_mcp.server import create_server
from crawl4ai_mcp.service import CrawlService


def run_server(config: AppConfig, service: CrawlService | None = None) -> None:
    owned = service is None
    if owned:
        service = CrawlService(config)

    @asynccontextmanager
    async def lifespan(_app: FastMCP):
        if owned:
            await service.start()
        try:
            yield
        finally:
            if owned:
                await service.close()

    mcp = create_server(service, lifespan=lifespan)
    mcp.run(
        transport="http",
        host=config.bind_host,
        port=config.bind_port,
        path="/mcp",
        host_origin_protection=True,
        allowed_hosts=[
            f"{config.bind_host}:{config.bind_port}",
            f"localhost:{config.bind_port}",
            *config.extra_allowed_hosts,
        ],
    )


def main() -> None:
    config = load_config(
        Path("config.toml"),
        env=dotenv_values(Path(".env")),
    )
    run_server(config)


if __name__ == "__main__":
    main()
