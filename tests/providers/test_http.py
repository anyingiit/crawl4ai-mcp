import asyncio
import ipaddress

import pytest
from curl_cffi import CurlOpt

from crawl4ai_mcp.egress import (
    ResolvedTarget,
    UrlPolicy,
    parse_public_url,
)
from crawl4ai_mcp.models import CostKind, Tier
from crawl4ai_mcp.providers.http import HttpProvider


def public_policy(address: str = "93.184.216.34") -> UrlPolicy:
    async def resolver(_host: str, _port: int):
        return [ipaddress.ip_address(address)]
    return UrlPolicy(resolver)


def two_host_public_policy() -> UrlPolicy:
    async def resolver(host: str, _port: int):
        address = "93.184.216.34" if host == "example.com" else "93.184.216.35"
        return [ipaddress.ip_address(address)]
    return UrlPolicy(resolver)


class LocalPolicy(UrlPolicy):
    """Test-only policy that permits dialing loopback fixtures."""

    async def resolve(self, url):
        validated = parse_public_url(url)
        return ResolvedTarget(
            url=validated,
            host=validated.host,
            port=validated.port,
            addresses=(ipaddress.ip_address("127.0.0.1"),),
        )


def local_policy() -> UrlPolicy:
    return LocalPolicy()


class FakeResponse:
    def __init__(self, status_code, headers=None, url=None, text=""):
        self.status_code = status_code
        self.headers = headers or {}
        self.url = url
        self.text = text


class FakeSession:
    def __init__(self, factory, kwargs):
        self.factory = factory
        self.kwargs = kwargs
        self.closed = False
        self.requested = []

    async def get(self, url, headers=None):
        self.requested.append(url)
        response = self.factory.responses.pop(0)
        response.url = response.url or url
        return response

    async def close(self):
        self.closed = True


class FakeSessionFactory:
    def __init__(self, responses):
        self.responses = list(responses)
        self.sessions = []

    def __call__(self, **kwargs):
        session = FakeSession(self, kwargs)
        self.sessions.append(session)
        return session


@pytest.mark.asyncio
async def test_http_provider_pins_validated_address_with_curl_resolve():
    factory = FakeSessionFactory([FakeResponse(status_code=200)])
    provider = HttpProvider(public_policy("93.184.216.34"), session_factory=factory)
    result = await provider.fetch("https://example.com/")
    assert result.target_status_code == 200
    assert factory.sessions[0].kwargs["trust_env"] is False
    assert factory.sessions[0].kwargs["allow_redirects"] is False
    assert factory.sessions[0].kwargs["curl_options"][CurlOpt.RESOLVE] == [
        "example.com:443:93.184.216.34"
    ]


@pytest.mark.asyncio
async def test_http_provider_validates_and_repins_every_redirect_hop():
    factory = FakeSessionFactory([
        FakeResponse(302, headers={"location": "https://www.example.net/final"}),
        FakeResponse(200),
    ])
    provider = HttpProvider(two_host_public_policy(), session_factory=factory)
    result = await provider.fetch("https://example.com/start")
    assert len(factory.sessions) == 2
    assert factory.sessions[1].kwargs["curl_options"][CurlOpt.RESOLVE] == [
        "www.example.net:443:93.184.216.35"
    ]
    assert result.redirected_url == "https://www.example.net/final"


@pytest.mark.asyncio
async def test_http_provider_never_dials_private_target():
    async def resolver(_host, _port):
        return [ipaddress.ip_address("127.0.0.1")]
    factory = FakeSessionFactory([FakeResponse(status_code=200)])
    provider = HttpProvider(UrlPolicy(resolver), session_factory=factory)
    result = await provider.fetch("https://internal.example/")
    assert len(factory.sessions) == 0
    assert result.policy_error == "non_global_address"
    assert result.target_status_code is None
    assert result.redirected_url is None


@pytest.mark.asyncio
async def test_http_provider_rejects_private_redirect_before_second_dial():
    async def resolver(host, _port):
        if host == "example.com":
            return [ipaddress.ip_address("93.184.216.34")]
        return [ipaddress.ip_address("127.0.0.1")]
    factory = FakeSessionFactory([
        FakeResponse(302, headers={"location": "https://private.example/secret"}),
        FakeResponse(200),
    ])
    provider = HttpProvider(UrlPolicy(resolver), session_factory=factory)
    result = await provider.fetch("https://example.com/start")
    assert len(factory.sessions) == 1
    assert result.policy_error == "non_global_address"
    assert result.target_status_code is None


@pytest.mark.asyncio
async def test_http_provider_stops_with_too_many_redirects_after_max_hops():
    factory = FakeSessionFactory([
        FakeResponse(302, headers={"location": "https://example.com/loop"})
    ] * 20)
    provider = HttpProvider(
        public_policy(), max_redirects=10, session_factory=factory
    )
    result = await provider.fetch("https://example.com/start")
    assert result.network_error == "too_many_redirects"
    assert result.target_status_code is None
    assert len(factory.sessions) == 10


@pytest.mark.asyncio
async def test_trailing_dot_authority_fetches_normalized_url_with_pin():
    factory = FakeSessionFactory([FakeResponse(status_code=200)])
    provider = HttpProvider(public_policy("93.184.216.34"), session_factory=factory)
    result = await provider.fetch("https://example.com.:443/start?q=1#frag")
    assert factory.sessions[0].kwargs["curl_options"][CurlOpt.RESOLVE] == [
        "example.com:443:93.184.216.34"
    ]
    assert factory.sessions[0].requested == ["https://example.com/start?q=1"]
    assert result.target_status_code == 200
    assert result.redirected_url is None


@pytest.mark.asyncio
async def test_trailing_dot_redirect_hops_fetch_normalized_urls():
    factory = FakeSessionFactory([
        FakeResponse(302, headers={"location": "https://www.example.net./final"}),
        FakeResponse(200),
    ])
    provider = HttpProvider(two_host_public_policy(), session_factory=factory)
    result = await provider.fetch("https://example.com./start")
    assert factory.sessions[0].requested == ["https://example.com/start"]
    assert factory.sessions[1].kwargs["curl_options"][CurlOpt.RESOLVE] == [
        "www.example.net:443:93.184.216.35"
    ]
    assert factory.sessions[1].requested == ["https://www.example.net/final"]
    assert result.redirected_url == "https://www.example.net/final"


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
    provider = HttpProvider(local_policy(), concurrency=8, timeout_seconds=10)
    result = await provider.fetch(f"{local_server}/missing")
    assert result.target_status_code == 404
    assert result.tier == Tier.HTTP
    assert result.cost_kind == CostKind.FREE
    await provider.close()


@pytest.mark.asyncio
async def test_http_provider_follows_redirects(local_server):
    provider = HttpProvider(local_policy(), concurrency=8, timeout_seconds=10)
    result = await provider.fetch(f"{local_server}/redirect")
    assert result.target_status_code == 200
    assert result.redirected_url == f"{local_server}/"
    await provider.close()


@pytest.mark.asyncio
async def test_http_provider_success_uses_free_tier(local_server):
    provider = HttpProvider(local_policy(), concurrency=8, timeout_seconds=10)
    result = await provider.fetch(f"{local_server}/")
    assert result.target_status_code == 200
    assert result.tier == Tier.HTTP
    assert result.cost_kind == CostKind.FREE
    assert "Hello world" in result.html
    await provider.close()


@pytest.mark.asyncio
async def test_http_provider_network_error_is_normalized(local_server):
    provider = HttpProvider(local_policy(), concurrency=8, timeout_seconds=10)
    result = await provider.fetch("http://127.0.0.1:1/unreachable")
    assert result.target_status_code is None
    assert result.network_error is not None
    assert result.error is not None
    assert result.tier == Tier.HTTP
    await provider.close()
