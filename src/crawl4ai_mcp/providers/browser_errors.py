from __future__ import annotations

import re

_NET_ERROR_MARKERS = (
    "net::ERR_NAME_NOT_RESOLVED",
    "net::ERR_NAME_RESOLUTION_FAILED",
    "net::ERR_DNS_",
    "net::ERR_CONNECTION_REFUSED",
    "net::ERR_CONNECTION_RESET",
    "net::ERR_CONNECTION_CLOSED",
    "net::ERR_CONNECTION_TIMED_OUT",
    "net::ERR_CONNECTION_FAILED",
    "net::ERR_TIMED_OUT",
    "net::ERR_SSL_",
    "net::ERR_TLS_",
    "net::ERR_CERT_",
    "net::ERR_HOST_UNREACHABLE",
    "net::ERR_ADDRESS_UNREACHABLE",
    "net::ERR_ADDRESS_INVALID",
    "net::ERR_INTERNET_DISCONNECTED",
)

_TIMEOUT_PATTERN = re.compile(r"timeout(?: of)? \d+ ?ms exceeded", re.IGNORECASE)

_timeout_types: tuple[type, ...] | None = None


def _playwright_timeout_types() -> tuple[type, ...]:
    global _timeout_types
    if _timeout_types is None:
        types = []
        for module_name in ("playwright._impl._errors", "patchright._impl._errors"):
            try:
                errors = __import__(module_name, fromlist=["TimeoutError"])
                types.append(errors.TimeoutError)
            except ImportError:
                pass
        _timeout_types = tuple(types)
    return _timeout_types


def browser_network_error(exc: BaseException) -> bool:
    """True when a browser fetch failure is a target-network failure.

    Matches stable Playwright/Patchright markers: navigation timeout,
    DNS resolution, connection refused/reset/closed, TLS connection
    failures, and unreachable hosts. Launch failures, context or route
    installation failures, browser/protocol crashes unrelated to the
    target connection, and malformed results return False so they fall
    through as ordinary provider failures.
    """
    if isinstance(exc, _playwright_timeout_types()):
        return True
    message = str(exc)
    if _TIMEOUT_PATTERN.search(message):
        return True
    return any(marker in message for marker in _NET_ERROR_MARKERS)
