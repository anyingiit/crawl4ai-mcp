from __future__ import annotations

import asyncio
import base64
import ipaddress
import socket
import ssl
import weakref
from dataclasses import dataclass
from enum import StrEnum
from typing import Awaitable, Callable, Sequence
from urllib.parse import urlsplit, urlunsplit

from crawl4ai.async_configs import ProxyConfig

IPv4Address = ipaddress.IPv4Address
IPv6Address = ipaddress.IPv6Address

_IPV4_COMPATIBLE = ipaddress.IPv6Network("::/96")
_SIX_TO_FOUR = ipaddress.IPv6Network("2002::/16")
_TEREDO = ipaddress.IPv6Network("2001::/32")
_NAT64_WELL_KNOWN = ipaddress.IPv6Network("64:ff9b::/96")
_NAT64_LOCAL_USE = ipaddress.IPv6Network("64:ff9b:1::/48")
_ISATAP_IDENTIFIERS = (b"\x00\x00\x5e\xfe", b"\x02\x00\x5e\xfe")

_HOSTNAME_MAX_LENGTH = 253
_LABEL_MAX_LENGTH = 63


class UrlPolicyReason(StrEnum):
    INVALID_URL = "invalid_url"
    UNSUPPORTED_SCHEME = "unsupported_scheme"
    CREDENTIALS = "credentials"
    INVALID_HOST = "invalid_host"
    MISSING_HOST = "missing_host"
    INVALID_PORT = "invalid_port"
    NON_GLOBAL_ADDRESS = "non_global_address"
    DNS_FAILED = "dns_failed"
    RESOLUTION_FAILED = "resolution_failed"


class UrlPolicyError(Exception):
    def __init__(self, reason: UrlPolicyReason, url: str, detail: str = ""):
        if detail:
            message = f"{reason.value}: {url} ({detail})"
        else:
            message = f"{reason.value}: {url}"
        super().__init__(message)
        self.reason = reason
        self.url = url
        self.detail = detail


@dataclass(frozen=True)
class Origin:
    scheme: str
    host: str
    port: int


@dataclass(frozen=True)
class ValidatedUrl:
    url: str
    origin: Origin
    host: str
    port: int


@dataclass(frozen=True)
class ResolvedTarget:
    url: ValidatedUrl
    host: str
    port: int
    addresses: tuple[IPv4Address | IPv6Address, ...]


def parse_public_url(url: str) -> ValidatedUrl:
    if any(ord(char) < 0x20 or ord(char) == 0x7F for char in url):
        raise UrlPolicyError(UrlPolicyReason.INVALID_URL, url, "control characters")
    try:
        parts = urlsplit(url)
    except ValueError as exc:
        raise UrlPolicyError(UrlPolicyReason.INVALID_URL, url, str(exc)) from exc
    scheme = parts.scheme.lower()
    if scheme not in ("http", "https"):
        raise UrlPolicyError(UrlPolicyReason.UNSUPPORTED_SCHEME, url, scheme)
    if parts.username is not None or parts.password is not None:
        raise UrlPolicyError(UrlPolicyReason.CREDENTIALS, url, "userinfo is not allowed")
    try:
        hostname = parts.hostname
        port = parts.port
    except ValueError as exc:
        raise UrlPolicyError(UrlPolicyReason.INVALID_PORT, url, str(exc)) from exc
    if not hostname:
        raise UrlPolicyError(UrlPolicyReason.MISSING_HOST, url, "")
    if port is not None and not 1 <= port <= 65535:
        raise UrlPolicyError(UrlPolicyReason.INVALID_PORT, url, str(port))
    if hostname.startswith("[") and hostname.endswith("]"):
        hostname = hostname[1:-1]
    if ":" in hostname:
        if "%" in hostname:
            raise UrlPolicyError(
                UrlPolicyReason.INVALID_HOST, url, "ipv6 zone id is not allowed"
            )
        try:
            address = ipaddress.IPv6Address(hostname)
        except ValueError as exc:
            raise UrlPolicyError(UrlPolicyReason.INVALID_HOST, url, str(exc)) from exc
        if address.scope_id:
            raise UrlPolicyError(
                UrlPolicyReason.INVALID_HOST, url, "ipv6 zone id is not allowed"
            )
        host = address.compressed
    else:
        hostname = hostname.rstrip(".")
        if not hostname:
            raise UrlPolicyError(UrlPolicyReason.MISSING_HOST, url, "")
        try:
            host = hostname.encode("idna").decode("ascii").lower()
        except UnicodeError as exc:
            raise UrlPolicyError(UrlPolicyReason.INVALID_HOST, url, str(exc)) from exc
        _validate_dns_host(host, url)
    if port is None:
        port = 443 if scheme == "https" else 80
    authority = f"[{host}]" if ":" in host else host
    if port != (443 if scheme == "https" else 80):
        authority = f"{authority}:{port}"
    normalized = urlunsplit((scheme, authority, parts.path, parts.query, ""))
    return ValidatedUrl(
        url=normalized,
        origin=Origin(scheme, host, port),
        host=host,
        port=port,
    )


def _validate_dns_host(host: str, url: str) -> None:
    if len(host) > _HOSTNAME_MAX_LENGTH:
        raise UrlPolicyError(UrlPolicyReason.INVALID_HOST, url, "hostname too long")
    for label in host.split("."):
        if not label:
            raise UrlPolicyError(UrlPolicyReason.INVALID_HOST, url, "empty label")
        if len(label) > _LABEL_MAX_LENGTH:
            raise UrlPolicyError(UrlPolicyReason.INVALID_HOST, url, "label too long")
        if label.startswith("-") or label.endswith("-"):
            raise UrlPolicyError(
                UrlPolicyReason.INVALID_HOST, url, "label hyphen at edge"
            )
        if not all(
            "a" <= char <= "z" or "0" <= char <= "9" or char == "-" for char in label
        ):
            raise UrlPolicyError(
                UrlPolicyReason.INVALID_HOST, url, "non-LDH character"
            )


def normalized_origin(url: str) -> Origin:
    return parse_public_url(url).origin


def same_origin(left: str, right: str) -> bool:
    try:
        return normalized_origin(left) == normalized_origin(right)
    except UrlPolicyError:
        return False


def is_allowed_address(address: IPv4Address | IPv6Address) -> bool:
    if isinstance(address, IPv6Address):
        return _is_allowed_ipv6(address)
    return bool(address.is_global)


def _is_allowed_ipv6(address: IPv6Address) -> bool:
    if not address.is_global:
        return False
    mapped = address.ipv4_mapped
    if mapped is not None:
        return is_allowed_address(mapped)
    if address in _IPV4_COMPATIBLE:
        return is_allowed_address(IPv4Address(address.packed[12:16]))
    if address in _SIX_TO_FOUR:
        return is_allowed_address(IPv4Address(address.packed[2:6]))
    if address in _TEREDO:
        server = IPv4Address(address.packed[4:8])
        client = IPv4Address(bytes(byte ^ 0xFF for byte in address.packed[12:16]))
        return is_allowed_address(server) and is_allowed_address(client)
    if address in _NAT64_WELL_KNOWN or address in _NAT64_LOCAL_USE:
        return is_allowed_address(IPv4Address(address.packed[12:16]))
    if address.packed[8:12] in _ISATAP_IDENTIFIERS:
        return is_allowed_address(IPv4Address(address.packed[12:16]))
    return True


async def _default_resolver(host: str, port: int) -> list[IPv4Address | IPv6Address]:
    loop = asyncio.get_running_loop()
    infos = await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    addresses: list[IPv4Address | IPv6Address] = []
    for family, _, _, _, sockaddr in infos:
        if family == socket.AF_INET:
            addresses.append(IPv4Address(sockaddr[0]))
        elif family == socket.AF_INET6:
            addresses.append(IPv6Address(sockaddr[0].split("%")[0]))
    return addresses


class UrlPolicy:
    def __init__(
        self,
        resolver: Callable[[str, int], Awaitable[Sequence[IPv4Address | IPv6Address]]]
        | None = None,
    ):
        self._resolver = resolver or _default_resolver

    async def resolve(self, url: str) -> ResolvedTarget:
        validated = parse_public_url(url)
        try:
            literal = ipaddress.ip_address(validated.host)
        except ValueError:
            literal = None
        if literal is not None:
            answers = [literal]
        else:
            try:
                answers = list(await self._resolver(validated.host, validated.port))
            except UrlPolicyError:
                raise
            except Exception as exc:
                raise UrlPolicyError(
                    UrlPolicyReason.RESOLUTION_FAILED, url, str(exc)
                ) from exc
        unique = list(dict.fromkeys(answers))
        if not unique:
            raise UrlPolicyError(UrlPolicyReason.DNS_FAILED, url, "no addresses resolved")
        for address in unique:
            if not is_allowed_address(address):
                raise UrlPolicyError(
                    UrlPolicyReason.NON_GLOBAL_ADDRESS, url, str(address)
                )
        return ResolvedTarget(
            url=validated,
            host=validated.host,
            port=validated.port,
            addresses=tuple(unique),
        )


class BrowserRequestGuard:
    """Intercepts every browser subresource and continues only public URLs.

    Syntax and credential violations fail fast inside the browser; the
    pinning proxy remains the enforcement point for DNS and dial safety.
    Allowed requests fall back to previously registered handlers so
    crawl4ai's own route logic still runs.

    Install is idempotent per context and synchronized: concurrent
    installs await the same in-flight registration instead of racing
    past a pending `context.route()` call, and the shared registration
    is shielded so cancelling one waiter never cancels it for everyone.
    """

    def __init__(self, policy: UrlPolicy):
        self._policy = policy
        self._installed = weakref.WeakSet()
        self._inflight: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()

    async def install(self, context) -> None:
        pending = self._inflight.get(context)
        if pending is not None:
            await asyncio.shield(pending)
            return
        if context in self._installed:
            return
        registration = asyncio.get_running_loop().create_task(
            self._register(context)
        )
        self._inflight[context] = registration
        registration.add_done_callback(
            lambda _task, ctx=context: self._inflight.pop(ctx, None)
        )
        await asyncio.shield(registration)

    async def _register(self, context) -> None:
        try:
            await context.route("**/*", self.handle)
        except BaseException:
            raise
        self._installed.add(context)

    async def handle(self, route, request) -> None:
        try:
            await self._policy.resolve(request.url)
        except UrlPolicyError:
            await route.abort()
        else:
            await route.fallback()


_MAX_HEADER_BYTES = 64 * 1024


@dataclass(frozen=True)
class UpstreamProxy:
    server: str
    username: str | None = None
    password: str | None = None


def _basic_auth(upstream: UpstreamProxy) -> str | None:
    if upstream.username is None and upstream.password is None:
        return None
    token = base64.b64encode(
        f"{upstream.username or ''}:{upstream.password or ''}".encode()
    ).decode()
    return f"Basic {token}"


def _authority(address: str, port: int) -> str:
    host = f"[{address}]" if ":" in address else address
    return f"{host}:{port}"


def build_upstream_connect(
    address: str, port: int, upstream: UpstreamProxy
) -> bytes:
    authority = _authority(address, port)
    lines = [f"CONNECT {authority} HTTP/1.1", f"Host: {authority}"]
    auth = _basic_auth(upstream)
    if auth is not None:
        lines.append(f"Proxy-Authorization: {auth}")
    return ("\r\n".join(lines) + "\r\n\r\n").encode("latin-1")


def _parse_upstream_server(upstream: UpstreamProxy) -> tuple[str, int, bool]:
    server = upstream.server
    if "://" not in server:
        server = f"http://{server}"
    parts = urlsplit(server)
    scheme = parts.scheme.lower() or "http"
    host = parts.hostname or ""
    if not host:
        label = server.split("@")[-1] if "@" in server else server
        raise UrlPolicyError(UrlPolicyReason.INVALID_HOST, label, "missing proxy host")
    port = parts.port or (443 if scheme == "https" else 80)
    return host, port, scheme == "https"


def _connect_probe_url(host: str, port: int) -> str:
    return f"https://[{host}]:{port}" if ":" in host else f"https://{host}:{port}"


class PinnedEgressProxy:
    def __init__(
        self,
        policy: UrlPolicy,
        connect: Callable[[str, int], Awaitable[tuple]] | None = None,
    ):
        self._policy = policy
        self._connect = connect
        self._direct_server: asyncio.AbstractServer | None = None
        self._direct_endpoint: ProxyConfig | None = None
        self._upstream_servers: dict[UpstreamProxy, asyncio.AbstractServer] = {}
        self._upstream_endpoints: dict[UpstreamProxy, ProxyConfig] = {}
        self._upstream_bind_tasks: set[asyncio.Task] = set()
        self._connections: set[asyncio.StreamWriter] = set()
        self._connection_tasks: set[asyncio.Task] = set()

    async def start(self) -> None:
        if self._direct_server is not None:
            return
        self._direct_server = await asyncio.start_server(
            self._client_connected, "127.0.0.1", 0
        )

    def _client_connected(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> asyncio.Task:
        return self._track(self._serve(reader, writer))

    def _track(self, coro: Awaitable[None]) -> asyncio.Task:
        task = asyncio.ensure_future(coro)
        self._connection_tasks.add(task)
        task.add_done_callback(self._connection_tasks.discard)
        return task

    def endpoint(self, upstream: UpstreamProxy | None = None) -> ProxyConfig:
        if upstream is None:
            if self._direct_server is None:
                raise RuntimeError("PinnedEgressProxy.start() must be called first")
            if self._direct_endpoint is None:
                port = self._direct_server.sockets[0].getsockname()[1]
                self._direct_endpoint = ProxyConfig(
                    server=f"http://127.0.0.1:{port}"
                )
            return self._direct_endpoint
        if upstream not in self._upstream_endpoints:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind(("127.0.0.1", 0))
            sock.listen(128)
            sock.setblocking(False)
            port = sock.getsockname()[1]
            task = asyncio.get_running_loop().create_task(
                self._start_upstream_server(sock, upstream)
            )
            self._upstream_bind_tasks.add(task)
            self._upstream_endpoints[upstream] = ProxyConfig(
                server=f"http://127.0.0.1:{port}"
            )
        return self._upstream_endpoints[upstream]

    async def _start_upstream_server(
        self, sock: socket.socket, upstream: UpstreamProxy
    ) -> None:
        try:
            server = await asyncio.start_server(
                lambda reader, writer: self._track(
                    self._serve(reader, writer, upstream)
                ),
                sock=sock,
            )
        except BaseException:
            sock.close()
            raise
        self._upstream_servers[upstream] = server

    async def close(self) -> None:
        for task in list(self._upstream_bind_tasks):
            task.cancel()
        if self._upstream_bind_tasks:
            await asyncio.gather(*self._upstream_bind_tasks, return_exceptions=True)
        self._upstream_bind_tasks.clear()
        servers = [self._direct_server, *self._upstream_servers.values()]
        for server in servers:
            if server is not None:
                server.close()
        for task in list(self._connection_tasks):
            task.cancel()
        if self._connection_tasks:
            await asyncio.gather(*self._connection_tasks, return_exceptions=True)
        self._connection_tasks.clear()
        for server in servers:
            if server is not None:
                try:
                    await server.wait_closed()
                except Exception:
                    pass
        self._direct_server = None
        self._upstream_servers.clear()
        self._direct_endpoint = None
        self._upstream_endpoints.clear()
        for writer in list(self._connections):
            try:
                writer.close()
            except Exception:
                pass
        self._connections.clear()

    async def _open_direct_tunnel(self, host: str, port: int) -> tuple:
        target = await self._policy.resolve(_connect_probe_url(host, port))
        last_error: Exception | None = None
        for address in target.addresses:
            try:
                reader, writer = await self._dial(str(address), port)
                return reader, writer, b""
            except Exception as exc:
                last_error = exc
        raise OSError(f"all validated addresses failed: {last_error}") from last_error

    async def _dial(self, host: str, port: int) -> tuple:
        if self._connect is not None:
            return await self._connect(host, port)
        return await asyncio.open_connection(host, port)

    async def _open_upstream_tunnel(
        self, host: str, port: int, upstream: UpstreamProxy
    ) -> tuple:
        target = await self._policy.resolve(_connect_probe_url(host, port))
        up_host, up_port, use_tls = _parse_upstream_server(upstream)
        last_error: Exception | None = None
        for address in target.addresses:
            remote_writer: asyncio.StreamWriter | None = None
            try:
                remote_reader, remote_writer = await self._dial_upstream(
                    up_host, up_port, use_tls
                )
                request = build_upstream_connect(str(address), port, upstream)
                remote_writer.write(request)
                await remote_writer.drain()
                head, early = await self._read_head(remote_reader)
                if head is None:
                    raise OSError("upstream closed during CONNECT handshake")
                status = int(head.split(b"\r\n", 1)[0].split(b" ", 2)[1])
                if status == 200:
                    result = (remote_reader, remote_writer, early)
                    remote_writer = None
                    return result
                if status == 407:
                    raise OSError("upstream proxy requires authentication")
                raise OSError(f"upstream proxy rejected CONNECT with status {status}")
            except UrlPolicyError:
                raise
            except Exception as exc:
                last_error = exc
            finally:
                if remote_writer is not None:
                    await self._close_remote(remote_writer)
        raise OSError(f"all validated addresses failed via upstream: {last_error}") from last_error

    async def _dial_upstream(
        self, host: str, port: int, use_tls: bool
    ) -> tuple:
        target = await self._policy.resolve(_connect_probe_url(host, port))
        ssl_context = ssl.create_default_context() if use_tls else None
        last_error: Exception | None = None
        for address in target.addresses:
            try:
                if self._connect is not None:
                    return await self._connect(
                        str(address),
                        port,
                        ssl=ssl_context,
                        server_hostname=host if use_tls else None,
                    )
                return await asyncio.open_connection(
                    str(address),
                    port,
                    ssl=ssl_context,
                    server_hostname=host if use_tls else None,
                )
            except Exception as exc:
                last_error = exc
        raise OSError(
            f"all validated upstream proxy addresses failed: {last_error}"
        ) from last_error

    @staticmethod
    async def _close_remote(writer: asyncio.StreamWriter | None) -> None:
        if writer is None:
            return
        try:
            writer.close()
        except Exception:
            pass
        try:
            await writer.wait_closed()
        except Exception:
            pass

    async def _serve(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        upstream: UpstreamProxy | None = None,
    ) -> None:
        self._connections.add(writer)
        try:
            head, remainder = await self._read_head(reader, writer)
            if head is None:
                return
            request_line, _, header_block = head.partition(b"\r\n")
            parts = request_line.split(b" ")
            if len(parts) != 3:
                await self._reject(writer, 400, "bad request line")
                return
            method = parts[0].decode("latin-1")
            target = parts[1].decode("latin-1")
            if method.upper() == "CONNECT":
                await self._handle_connect(
                    target, header_block, reader, writer, upstream, remainder
                )
            else:
                await self._handle_absolute_form(
                    method, target, header_block, reader, writer, upstream, remainder
                )
        finally:
            try:
                writer.close()
            except Exception:
                pass
            try:
                await writer.wait_closed()
            except Exception:
                pass
            self._connections.discard(writer)

    async def _read_head(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter | None = None,
    ) -> tuple[bytes | None, bytes]:
        head = b""
        while True:
            terminator = head.find(b"\r\n\r\n")
            if terminator >= 0:
                end = terminator + 4
                if end > _MAX_HEADER_BYTES:
                    if writer is not None:
                        await self._reject(writer, 431, "request header fields too large")
                    return None, b""
                return head[:end], head[end:]
            if len(head) >= _MAX_HEADER_BYTES:
                if writer is not None:
                    await self._reject(writer, 431, "request header fields too large")
                return None, b""
            remaining = _MAX_HEADER_BYTES - len(head)
            chunk = await reader.read(min(4096, remaining))
            if not chunk:
                return None, b""
            head += chunk

    async def _reject(self, writer: asyncio.StreamWriter, status: int, reason: str) -> None:
        body = f"{status} {reason}".encode()
        head = (
            f"HTTP/1.1 {status} {reason}\r\n"
            f"Content-Length: {len(body)}\r\n"
            f"Connection: close\r\n\r\n"
        ).encode()
        try:
            writer.write(head + body)
            await writer.drain()
        except Exception:
            pass

    async def _handle_connect(
        self,
        target: str,
        header_block: bytes,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        upstream: UpstreamProxy | None,
        remainder: bytes,
    ) -> None:
        try:
            try:
                parts = urlsplit(f"//{target}")
                host = parts.hostname
                port = parts.port or 443
            except ValueError as exc:
                raise UrlPolicyError(
                    UrlPolicyReason.INVALID_URL, target, str(exc)
                ) from exc
            if not host:
                raise UrlPolicyError(
                    UrlPolicyReason.MISSING_HOST, target, "CONNECT target has no host"
                )
            if host.startswith("[") and host.endswith("]"):
                host = host[1:-1]
            if upstream is None:
                remote_reader, remote_writer, early = await self._open_direct_tunnel(
                    host, port
                )
            else:
                remote_reader, remote_writer, early = await self._open_upstream_tunnel(
                    host, port, upstream
                )
        except UrlPolicyError as exc:
            await self._reject(writer, 403, str(exc))
            return
        except Exception as exc:
            await self._reject(writer, 502, f"tunnel failed: {exc}")
            return
        try:
            writer.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
            if early:
                writer.write(early)
            await writer.drain()
            if remainder:
                remote_writer.write(remainder)
                await remote_writer.drain()
        except asyncio.CancelledError:
            await self._close_remote(remote_writer)
            raise
        except Exception:
            await self._close_remote(remote_writer)
            return
        await self._relay(reader, writer, remote_reader, remote_writer)

    async def _handle_absolute_form(
        self,
        method: str,
        target: str,
        header_block: bytes,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        upstream: UpstreamProxy | None,
        remainder: bytes,
    ) -> None:
        try:
            resolved = await self._policy.resolve(target)
        except UrlPolicyError as exc:
            await self._reject(writer, 403, str(exc))
            return
        parts = urlsplit(resolved.url.url)
        origin_form = parts.path or "/"
        if parts.query:
            origin_form += f"?{parts.query}"
        headers = self._parse_header_lines(header_block)
        if not any(name.lower() == "host" for name, _value in headers):
            headers.insert(0, ("Host", parts.netloc))
        outgoing = self._build_request(method, origin_form, headers)
        remote_writer: asyncio.StreamWriter | None = None
        try:
            if upstream is None:
                remote_reader, remote_writer, early = await self._open_direct_tunnel(
                    resolved.host, resolved.port
                )
            else:
                remote_reader, remote_writer, early = await self._open_upstream_tunnel(
                    resolved.host, resolved.port, upstream
                )
            remote_writer.write(outgoing)
            if remainder:
                remote_writer.write(remainder)
            await remote_writer.drain()
            if early:
                writer.write(early)
                await writer.drain()
        except asyncio.CancelledError:
            await self._close_remote(remote_writer)
            raise
        except UrlPolicyError as exc:
            await self._close_remote(remote_writer)
            await self._reject(writer, 403, str(exc))
            return
        except Exception as exc:
            await self._close_remote(remote_writer)
            await self._reject(writer, 502, f"tunnel failed: {exc}")
            return
        await self._relay(reader, writer, remote_reader, remote_writer)

    @staticmethod
    def _parse_header_lines(header_block: bytes) -> list[tuple[str, str]]:
        headers: list[tuple[str, str]] = []
        for line in header_block.split(b"\r\n"):
            if not line:
                continue
            name, _, value = line.partition(b":")
            headers.append(
                (name.decode("latin-1").strip(), value.decode("latin-1").strip())
            )
        return headers

    @classmethod
    def _build_request(
        cls,
        method: str,
        origin_form: str,
        headers: list[tuple[str, str]],
    ) -> bytes:
        lines = [f"{method} {origin_form} HTTP/1.1"]
        for name, value in headers:
            if name.lower() == "proxy-authorization":
                continue
            lines.append(f"{name}: {value}")
        return ("\r\n".join(lines) + "\r\n\r\n").encode("latin-1")

    @staticmethod
    async def _relay(
        reader_a: asyncio.StreamReader,
        writer_a: asyncio.StreamWriter,
        reader_b: asyncio.StreamReader,
        writer_b: asyncio.StreamWriter,
    ) -> None:
        async def _copy(src: asyncio.StreamReader, dst: asyncio.StreamWriter) -> None:
            while True:
                chunk = await src.read(65536)
                if not chunk:
                    return
                dst.write(chunk)
                await dst.drain()

        task_a = asyncio.create_task(_copy(reader_a, writer_b))
        task_b = asyncio.create_task(_copy(reader_b, writer_a))
        try:
            await asyncio.wait({task_a, task_b}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            task_a.cancel()
            task_b.cancel()
            await asyncio.gather(task_a, task_b, return_exceptions=True)
            for writer in (writer_a, writer_b):
                try:
                    writer.close()
                except Exception:
                    pass
                try:
                    await writer.wait_closed()
                except Exception:
                    pass
