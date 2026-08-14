from __future__ import annotations

import re
from enum import StrEnum

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

_NAVIGATION_WRAPPER = "Failed on navigating ACS-GOTO"

_TIMEOUT_PATTERN = re.compile(r"timeout(?: of)? \d+ ?ms exceeded", re.IGNORECASE)

_timeout_types: tuple[type, ...] | None = None


class FetchStage(StrEnum):
    NAVIGATION = "navigation"
    CONTEXT_CREATION = "context_creation"
    GUARD_INSTALL = "guard_install"
    PAGE_CREATION = "page_creation"
    CONTENT = "content"
    LAUNCH = "launch"


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


def _has_net_marker(message: str) -> bool:
    return any(marker in message for marker in _NET_ERROR_MARKERS)


def browser_network_error(
    exc: BaseException,
    *,
    operation: FetchStage,
    wrapped: bool = False,
) -> bool:
    """Classify a browser fetch failure as target-network.

    Only the navigation stage may ever classify. Non-navigation stages
    (context creation, guard install, page creation, content, launch)
    always return False so provider-internal timeouts remain
    fallback-eligible.

    Direct ``page.goto`` evidence (``wrapped=False``) accepts
    Playwright/Patchright timeout types and concrete ``net::`` markers.

    With ``wrapped=True`` (crawl4ai ``arun``, which wraps many stages),
    only the stable ``Failed on navigating ACS-GOTO`` wrapper counts:
    concrete net markers or a navigation timeout inside that wrapper.
    A bare Playwright/Patchright TimeoutError or a generic
    ``Timeout Nms exceeded`` message without that wrapper stays False.
    """
    if operation is not FetchStage.NAVIGATION:
        return False
    message = str(exc)
    if _NAVIGATION_WRAPPER in message:
        return _has_net_marker(message) or _TIMEOUT_PATTERN.search(message) is not None
    if wrapped:
        return False
    if isinstance(exc, _playwright_timeout_types()):
        return True
    return _has_net_marker(message)
