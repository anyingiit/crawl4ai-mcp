import asyncio
import pytest
from crawl4ai_mcp.models import CostKind, Tier
from crawl4ai_mcp.providers.http import HttpProvider


class _Handler:
    def __init__(self):
        self.responses = {
            "/": (200, {"content-type": "text/html"}, "<main>Hello world</main>"),
            "/missing": (404, {"content-type": "text/html"}, "not found"),
        }

    def route(self, raw_path: str):
        if raw_path.startswith("/redirect"):
            return (
                302,
                {"location": "/", "content-type": "text/html"},
                "<a href='/'>here</a>",
            )
        return self.responses.get(raw_path, (404, {"content-type": "text/html"}, "not found"))


async def _serve(handler: _Handler, started: asyncio.Future):
    server = await asyncio.start_server(
        lambda reader, writer: _handle(reader, writer, handler), "127.0.0.1", 0
    )
    port = server.sockets[0].getsockname()[1]
    started.set_result(port)
    async with server:
        await server.serve_forever()


async def _handle(reader, writer, handler: _Handler):
    try:
        request = (await reader.read(65536)).decode("latin-1")
        lines = request.split("\r\n")
        target = lines[0].split(" ")[1] if lines else "/"
        status, headers, body = handler.route(target)
        reason = {200: "OK", 302: "Found", 404: "Not Found"}[status]
        payload = body.encode("utf-8")
        head = f"HTTP/1.1 {status} {reason}\r\nContent-Length: {len(payload)}\r\n"
        for key, value in headers.items():
            head += f"{key}: {value}\r\n"
        head += "Connection: close\r\n\r\n"
        writer.write(head.encode() + payload)
    except Exception:
        pass
    finally:
        try:
            writer.close()
        except Exception:
            pass


@pytest.fixture
async def local_server():
    handler = _Handler()
    started = asyncio.Future()
    task = asyncio.create_task(_serve(handler, started))
    port = await started
    yield f"http://127.0.0.1:{port}"
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


@pytest.mark.asyncio
async def test_http_provider_returns_404_for_classifier(local_server):
    provider = HttpProvider(concurrency=8, timeout_seconds=10)
    result = await provider.fetch(f"{local_server}/missing")
    assert result.status_code == 404
    assert result.tier == Tier.HTTP
    assert result.cost_kind == CostKind.FREE
    await provider.close()


@pytest.mark.asyncio
async def test_http_provider_follows_redirects(local_server):
    provider = HttpProvider(concurrency=8, timeout_seconds=10)
    result = await provider.fetch(f"{local_server}/redirect")
    assert result.status_code == 200
    assert result.redirected_url == f"{local_server}/"
    await provider.close()


@pytest.mark.asyncio
async def test_http_provider_success_uses_free_tier(local_server):
    provider = HttpProvider(concurrency=8, timeout_seconds=10)
    result = await provider.fetch(f"{local_server}/")
    assert result.status_code == 200
    assert result.tier == Tier.HTTP
    assert result.cost_kind == CostKind.FREE
    assert "Hello world" in result.html
    await provider.close()


@pytest.mark.asyncio
async def test_http_provider_network_error_is_normalized(local_server):
    provider = HttpProvider(concurrency=8, timeout_seconds=10)
    result = await provider.fetch("http://127.0.0.1:1/unreachable")
    assert result.status_code is None
    assert result.error is not None
    assert result.tier == Tier.HTTP
    await provider.close()
