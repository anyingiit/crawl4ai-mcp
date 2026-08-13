from __future__ import annotations

import asyncio
import ipaddress
import socket
from dataclasses import dataclass
from enum import StrEnum
from typing import Awaitable, Callable, Sequence
from urllib.parse import urlsplit, urlunsplit

IPv4Address = ipaddress.IPv4Address
IPv6Address = ipaddress.IPv6Address

_IPV4_COMPATIBLE = ipaddress.IPv6Network("::/96")
_SIX_TO_FOUR = ipaddress.IPv6Network("2002::/16")
_TEREDO = ipaddress.IPv6Network("2001::/32")
_NAT64_WELL_KNOWN = ipaddress.IPv6Network("64:ff9b::/96")
_NAT64_LOCAL_USE = ipaddress.IPv6Network("64:ff9b:1::/48")
_ISATAP_IDENTIFIERS = (b"\x00\x00\x5e\xfe", b"\x02\x00\x5e\xfe")


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
        try:
            host = ipaddress.IPv6Address(hostname).compressed
        except ValueError as exc:
            raise UrlPolicyError(UrlPolicyReason.INVALID_HOST, url, str(exc)) from exc
    else:
        hostname = hostname.rstrip(".")
        if not hostname:
            raise UrlPolicyError(UrlPolicyReason.MISSING_HOST, url, "")
        try:
            host = hostname.encode("idna").decode("ascii").lower()
        except UnicodeError as exc:
            raise UrlPolicyError(UrlPolicyReason.INVALID_HOST, url, str(exc)) from exc
    if port is None:
        port = 443 if scheme == "https" else 80
    if port != (443 if scheme == "https" else 80):
        authority = f"[{host}]" if ":" in host else f"{host}:{port}"
    else:
        authority = f"[{host}]" if ":" in host else host
    normalized = urlunsplit((scheme, authority, parts.path, parts.query, ""))
    return ValidatedUrl(
        url=normalized,
        origin=Origin(scheme, host, port),
        host=host,
        port=port,
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
