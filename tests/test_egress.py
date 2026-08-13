import asyncio
import ipaddress

import pytest

from crawl4ai_mcp.egress import (
    Origin,
    PinnedEgressProxy,
    ResolvedTarget,
    UpstreamProxy,
    UrlPolicy,
    UrlPolicyError,
    UrlPolicyReason,
    build_upstream_connect,
    is_allowed_address,
    parse_public_url,
    same_origin,
)


@pytest.mark.parametrize("url", [
    "file:///etc/passwd", "raw:<h1>x</h1>", "ftp://example.com/a",
    "https://user@example.com/a", "https://user:pass@example.com/a",
    "https:///missing-host", "https://example.com:99999/a",
])
def test_parse_public_url_rejects_unsafe_syntax(url):
    with pytest.raises(UrlPolicyError):
        parse_public_url(url)


def test_parse_public_url_normalizes_host_port_and_fragment():
    parsed = parse_public_url("HTTPS://ExAmPlE.COM.:443/a?q=1#fragment")
    assert parsed.url == "https://example.com/a?q=1"
    assert parsed.origin == Origin("https", "example.com", 443)


def test_same_origin_includes_scheme_and_effective_port():
    assert same_origin("https://example.com/a", "https://example.com:443/b")
    assert not same_origin("http://example.com/a", "https://example.com/a")
    assert not same_origin("https://example.com/a", "https://example.com:444/a")


def test_parse_public_url_preserves_ipv6_non_default_port():
    parsed = parse_public_url("https://[2606:4700:4700::1111]:8443/a")
    assert parsed.url == "https://[2606:4700:4700::1111]:8443/a"
    assert parsed.origin == Origin("https", "2606:4700:4700::1111", 8443)


@pytest.mark.parametrize("url,expected", [
    ("https://[2606:4700:4700::1111]/a", "https://[2606:4700:4700::1111]/a"),
    ("https://[2606:4700:4700::1111]:443/a", "https://[2606:4700:4700::1111]/a"),
    ("http://[2606:4700:4700::1111]:80/a", "http://[2606:4700:4700::1111]/a"),
])
def test_parse_public_url_strips_default_ipv6_ports(url, expected):
    assert parse_public_url(url).url == expected


@pytest.mark.parametrize("url", [
    "https://exa mple.com/a",
    "https://a..b.com/a",
    "https://.example.com/a",
    "https://-bad.com/a",
    "https://bad-.com/a",
    "https://xn--.com/a",
    "https://" + "a" * 64 + ".com/a",
    "https://" + ".".join(["a" * 63] * 4) + "/a",
])
def test_parse_public_url_rejects_malformed_dns_hostnames(url):
    with pytest.raises(UrlPolicyError) as exc:
        parse_public_url(url)
    assert exc.value.reason == UrlPolicyReason.INVALID_HOST


@pytest.mark.parametrize("url", [
    "https://exa\tmple.com/a",
    "https://exa\nmple.com/a",
    "https://exa\rmple.com/a",
    "https://exa\u0001mple.com/a",
    "https://exa\u007fmple.com/a",
])
def test_parse_public_url_rejects_control_characters(url):
    with pytest.raises(UrlPolicyError) as exc:
        parse_public_url(url)
    assert exc.value.reason == UrlPolicyReason.INVALID_URL


def test_parse_public_url_accepts_valid_punycode_hostname():
    parsed = parse_public_url("https://BÜCHER.example/a")
    assert parsed.host == "xn--bcher-kva.example"
    assert parsed.url == "https://xn--bcher-kva.example/a"


@pytest.mark.parametrize("url", [
    "https://[fe80::1%25eth0]/a",
    "https://[fe80::1%eth0]/a",
    "https://[2606:4700:4700::1111%25eth0]/a",
    "http://[2606:4700:4700::1111%25en0]/a",
])
def test_parse_public_url_rejects_scoped_ipv6_literals(url):
    with pytest.raises(UrlPolicyError) as exc:
        parse_public_url(url)
    assert exc.value.reason == UrlPolicyReason.INVALID_HOST


@pytest.mark.parametrize("literal", [
    "127.0.0.1", "10.0.0.1", "169.254.169.254", "::1", "fe80::1",
    "::ffff:127.0.0.1", "::127.0.0.1", "2002:7f00:1::",
    "2001:0000:4136:e378:8000:63bf:3fff:fdd2", "64:ff9b::7f00:1",
    "64:ff9b:1::7f00:1", "2001:db8:0:1:0:5efe:127.0.0.1",
])
def test_is_allowed_address_rejects_non_global_and_transition_addresses(literal):
    assert is_allowed_address(ipaddress.ip_address(literal)) is False


@pytest.mark.parametrize("literal", ["8.8.8.8", "1.1.1.1", "2606:4700:4700::1111"])
def test_is_allowed_address_accepts_global_addresses(literal):
    assert is_allowed_address(ipaddress.ip_address(literal)) is True


@pytest.mark.asyncio
async def test_url_policy_rejects_mixed_global_and_private_dns_answers():
    async def resolver(_host, _port):
        return [ipaddress.ip_address("93.184.216.34"), ipaddress.ip_address("127.0.0.1")]
    with pytest.raises(UrlPolicyError) as exc:
        await UrlPolicy(resolver).resolve("https://example.com/")
    assert exc.value.reason == UrlPolicyReason.NON_GLOBAL_ADDRESS


@pytest.mark.asyncio
async def test_url_policy_returns_all_validated_global_answers():
    async def resolver(_host, _port):
        return [ipaddress.ip_address("93.184.216.34"), ipaddress.ip_address("2606:2800:220:1:248:1893:25c8:1946")]
    target = await UrlPolicy(resolver).resolve("https://example.com/")
    assert tuple(map(str, target.addresses)) == (
        "93.184.216.34", "2606:2800:220:1:248:1893:25c8:1946",
    )


@pytest.mark.asyncio
async def test_url_policy_rejects_scoped_ipv6_before_resolution():
    async def resolver(_host, _port):
        raise AssertionError("resolver must not run for a scoped IPv6 literal")
    with pytest.raises(UrlPolicyError) as exc:
        await UrlPolicy(resolver).resolve("https://[2606:4700:4700::1111%25eth0]/")
    assert exc.value.reason == UrlPolicyReason.INVALID_HOST


def public_policy(address: str = "93.184.216.34") -> UrlPolicy:
    async def resolver(_host: str, _port: int):
        return [ipaddress.ip_address(address)]
    return UrlPolicy(resolver)


def private_policy() -> UrlPolicy:
    async def resolver(_host: str, _port: int):
        return [ipaddress.ip_address("127.0.0.1")]
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


class FakeReader:
    async def read(self, n=-1):
        return b""

    async def readexactly(self, n):
        raise EOFError

    async def readuntil(self, separator=b""):
        raise EOFError


class FakeWriter:
    def __init__(self):
        self.data = b""

    def write(self, data):
        self.data += data

    async def drain(self):
        return None

    def close(self):
        return None

    async def wait_closed(self):
        return None

    def is_closing(self):
        return False


@pytest.mark.asyncio
async def test_pinning_proxy_connects_to_resolved_ip_not_hostname():
    connected = []
    async def connect(host, port, **_kwargs):
        connected.append((host, port)); return FakeReader(), FakeWriter()
    proxy = PinnedEgressProxy(public_policy("93.184.216.34"), connect=connect)
    await proxy._open_direct_tunnel("example.com", 443)
    assert connected == [("93.184.216.34", 443)]


@pytest.mark.asyncio
async def test_proxy_dial_tries_validated_addresses_in_order():
    calls = []
    async def connect(host, port, **_kwargs):
        calls.append(host)
        if host == "93.184.216.34":
            raise OSError("refused")
        return FakeReader(), FakeWriter()
    async def resolver(_host, _port):
        return [
            ipaddress.ip_address("93.184.216.34"),
            ipaddress.ip_address("93.184.216.35"),
        ]
    proxy = PinnedEgressProxy(UrlPolicy(resolver), connect=connect)
    await proxy._open_direct_tunnel("example.com", 443)
    assert calls == ["93.184.216.34", "93.184.216.35"]


@pytest.mark.asyncio
async def test_proxy_dial_fails_when_every_validated_address_fails():
    async def connect(host, port, **_kwargs):
        raise OSError("refused")
    proxy = PinnedEgressProxy(public_policy("93.184.216.34"), connect=connect)
    with pytest.raises(OSError):
        await proxy._open_direct_tunnel("example.com", 443)


def test_upstream_connect_uses_pinned_ip_and_proxy_auth():
    request = build_upstream_connect(
        "93.184.216.34", 443, UpstreamProxy("http://proxy.example:8080", "u", "p")
    )
    assert request.startswith(b"CONNECT 93.184.216.34:443 HTTP/1.1\r\n")
    assert b"Proxy-Authorization: Basic dTpw\r\n" in request


def test_upstream_connect_brackets_ipv6_literals():
    request = build_upstream_connect(
        "2606:4700:4700::1111", 8443, UpstreamProxy("http://proxy.example:8080")
    )
    assert request.startswith(b"CONNECT [2606:4700:4700::1111]:8443 HTTP/1.1\r\n")


def test_upstream_connect_omits_auth_without_credentials():
    request = build_upstream_connect(
        "93.184.216.34", 443, UpstreamProxy("http://proxy.example:8080")
    )
    assert b"Proxy-Authorization" not in request


async def _echo_server(started: asyncio.Future):
    async def handle(reader, writer):
        try:
            while True:
                data = await reader.read(65536)
                if not data:
                    break
                writer.write(data)
                await writer.drain()
        except Exception:
            pass
        finally:
            try:
                writer.close()
            except Exception:
                pass
    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    started.set_result(port)
    async with server:
        await server.serve_forever()


async def _connect_to(proxy_config):
    port = int(proxy_config.server.rsplit(":", 1)[1])
    return await asyncio.open_connection("127.0.0.1", port)


@pytest.mark.asyncio
async def test_proxy_connect_tunnels_bytes_to_validated_target():
    echo_started = asyncio.Future()
    echo_task = asyncio.create_task(_echo_server(echo_started))
    target_port = await echo_started
    proxy = PinnedEgressProxy(LocalPolicy())
    await proxy.start()
    try:
        reader, writer = await _connect_to(proxy.endpoint())
        writer.write(
            f"CONNECT 127.0.0.1:{target_port} HTTP/1.1\r\n"
            f"Host: 127.0.0.1:{target_port}\r\n\r\n".encode()
        )
        await writer.drain()
        assert (
            await reader.readuntil(b"\r\n\r\n")
            == b"HTTP/1.1 200 Connection established\r\n\r\n"
        )
        writer.write(b"ping")
        await writer.drain()
        assert await reader.readexactly(4) == b"ping"
        writer.close()
    finally:
        await proxy.close()
        echo_task.cancel()
        try:
            await echo_task
        except asyncio.CancelledError:
            pass


@pytest.mark.asyncio
async def test_proxy_rejects_private_connect_target_with_403():
    proxy = PinnedEgressProxy(private_policy())
    await proxy.start()
    try:
        reader, writer = await _connect_to(proxy.endpoint())
        writer.write(b"CONNECT 10.0.0.5:443 HTTP/1.1\r\nHost: 10.0.0.5:443\r\n\r\n")
        await writer.drain()
        response = await reader.readuntil(b"\r\n\r\n")
        assert response.startswith(b"HTTP/1.1 403")
        writer.close()
    finally:
        await proxy.close()


async def _http_echo_server(seen: dict, started: asyncio.Future):
    async def handle(reader, writer):
        try:
            seen["request"] = await reader.readuntil(b"\r\n\r\n")
            body = b"<main>echo</main>"
            head = (
                b"HTTP/1.1 200 OK\r\nContent-Length: 17\r\nConnection: close\r\n\r\n"
            )
            writer.write(head + body)
            await writer.drain()
        except Exception:
            pass
        finally:
            try:
                writer.close()
            except Exception:
                pass
    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    started.set_result(port)
    async with server:
        await server.serve_forever()


@pytest.mark.asyncio
async def test_proxy_rewrites_absolute_form_to_origin_form_preserving_host():
    seen = {}
    http_started = asyncio.Future()
    http_task = asyncio.create_task(_http_echo_server(seen, http_started))
    target_port = await http_started
    proxy = PinnedEgressProxy(LocalPolicy())
    await proxy.start()
    try:
        reader, writer = await _connect_to(proxy.endpoint())
        target = f"127.0.0.1:{target_port}"
        writer.write(
            f"GET http://{target}/path?q=1 HTTP/1.1\r\n"
            f"Host: {target}\r\n"
            f"X-Custom: keep\r\n"
            f"Proxy-Authorization: Basic Y2FsbGVy\r\n\r\n".encode()
        )
        await writer.drain()
        response = await reader.readuntil(b"\r\n\r\n")
        assert response.startswith(b"HTTP/1.1 200 OK")
        forwarded = seen["request"]
        assert forwarded.startswith(b"GET /path?q=1 HTTP/1.1\r\n")
        assert f"Host: {target}".encode() in forwarded
        assert b"X-Custom: keep" in forwarded
        assert b"Proxy-Authorization" not in forwarded
        writer.close()
    finally:
        await proxy.close()
        http_task.cancel()
        try:
            await http_task
        except asyncio.CancelledError:
            pass


@pytest.mark.asyncio
async def test_proxy_caps_request_headers_at_64_kib():
    proxy = PinnedEgressProxy(public_policy())
    await proxy.start()
    try:
        reader, writer = await _connect_to(proxy.endpoint())
        writer.write(b"GET http://example.com/ HTTP/1.1\r\nX-Filler: " + b"a" * 70000 + b"\r\n\r\n")
        await writer.drain()
        response = await reader.readuntil(b"\r\n\r\n")
        assert response.startswith(b"HTTP/1.1 431")
        writer.close()
    finally:
        await proxy.close()


@pytest.mark.asyncio
async def test_proxy_forwards_connect_with_pinned_ip_and_own_auth_only():
    received = {}

    async def handle(reader, writer):
        try:
            received["head"] = await reader.readuntil(b"\r\n\r\n")
            writer.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
            await writer.drain()
            while True:
                data = await reader.read(65536)
                if not data:
                    break
                writer.write(data)
                await writer.drain()
        except Exception:
            pass
        finally:
            try:
                writer.close()
            except Exception:
                pass

    upstream_started = asyncio.Future()
    upstream_server = await asyncio.start_server(handle, "127.0.0.1", 0)
    upstream_port = upstream_server.sockets[0].getsockname()[1]
    upstream_started.set_result(upstream_port)
    upstream_task = asyncio.create_task(upstream_server.serve_forever())

    upstream = UpstreamProxy(f"http://127.0.0.1:{upstream_port}", "u", "p")
    proxy = PinnedEgressProxy(public_policy("93.184.216.34"))
    await proxy.start()
    try:
        reader, writer = await _connect_to(proxy.endpoint(upstream))
        writer.write(
            b"CONNECT example.com:443 HTTP/1.1\r\n"
            b"Host: example.com:443\r\n"
            b"Proxy-Authorization: Basic Y2FsbGVy\r\n\r\n"
        )
        await writer.drain()
        assert (
            await reader.readuntil(b"\r\n\r\n")
            == b"HTTP/1.1 200 Connection established\r\n\r\n"
        )
        writer.write(b"tls-bytes")
        await writer.drain()
        assert await reader.readexactly(9) == b"tls-bytes"
        writer.close()
        forwarded = received["head"]
        assert forwarded.startswith(b"CONNECT 93.184.216.34:443 HTTP/1.1\r\n")
        assert b"Proxy-Authorization: Basic dTpw\r\n" in forwarded
        assert b"Y2FsbGVy" not in forwarded
    finally:
        await proxy.close()
        upstream_server.close()
        await upstream_server.wait_closed()
        upstream_task.cancel()
        try:
            await upstream_task
        except asyncio.CancelledError:
            pass


@pytest.mark.asyncio
async def test_proxy_rejects_malformed_connect_target_with_403():
    proxy = PinnedEgressProxy(public_policy())
    await proxy.start()
    try:
        reader, writer = await _connect_to(proxy.endpoint())
        writer.write(b"CONNECT example.com:notaport HTTP/1.1\r\n\r\n")
        await writer.drain()
        response = await reader.readuntil(b"\r\n\r\n")
        assert response.startswith(b"HTTP/1.1 403")
        writer.close()
    finally:
        await proxy.close()
