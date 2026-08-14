import asyncio

import pytest
from patchright._impl._errors import TimeoutError as PatchrightTimeoutError
from playwright._impl._errors import (
    Error as PlaywrightError,
    TargetClosedError,
    TimeoutError as PlaywrightTimeoutError,
)

from crawl4ai_mcp.providers.browser_errors import FetchStage, browser_network_error

NAV = FetchStage.NAVIGATION


@pytest.mark.parametrize(
    "message",
    [
        "net::ERR_NAME_NOT_RESOLVED at https://nonexistent.example/",
        "net::ERR_NAME_RESOLUTION_FAILED at https://nonexistent.example/",
        "net::ERR_DNS_SERVER_FAILED at https://example.com/",
        "net::ERR_CONNECTION_REFUSED at https://example.com:443",
        "net::ERR_CONNECTION_RESET at https://example.com/",
        "net::ERR_CONNECTION_CLOSED at https://example.com/",
        "net::ERR_CONNECTION_TIMED_OUT at https://example.com/",
        "net::ERR_CONNECTION_FAILED at https://example.com/",
        "net::ERR_TIMED_OUT at https://example.com/",
        "net::ERR_SSL_PROTOCOL_ERROR at https://example.com/",
        "net::ERR_TLS_CERT_ALTNAME_INVALID at https://example.com/",
        "net::ERR_CERT_AUTHORITY_INVALID at https://example.com/",
        "net::ERR_HOST_UNREACHABLE at https://example.com/",
        "net::ERR_ADDRESS_UNREACHABLE at https://example.com/",
        "net::ERR_INTERNET_DISCONNECTED at https://example.com/",
    ],
)
def test_target_network_markers_are_classified(message):
    assert browser_network_error(RuntimeError(message), operation=NAV) is True


def test_crawl4ai_wrapped_navigation_marker_is_classified():
    exc = RuntimeError(
        "Failed on navigating ACS-GOTO:\n"
        "net::ERR_CONNECTION_RESET at https://example.com/"
    )
    assert browser_network_error(exc, operation=NAV, wrapped=True) is True
    assert browser_network_error(exc, operation=NAV) is True


def test_crawl4ai_wrapped_navigation_timeout_is_classified():
    exc = RuntimeError("Failed on navigating ACS-GOTO:\nTimeout 60000ms exceeded.")
    assert browser_network_error(exc, operation=NAV, wrapped=True) is True
    assert browser_network_error(exc, operation=NAV) is True


def test_playwright_and_patchright_timeout_types_are_classified():
    exc = PlaywrightTimeoutError("Timeout 60000ms exceeded.")
    assert browser_network_error(exc, operation=NAV) is True
    exc = PatchrightTimeoutError("Timeout 60000ms exceeded.")
    assert browser_network_error(exc, operation=NAV) is True


def test_playwright_error_with_marker_is_classified():
    exc = PlaywrightError("net::ERR_CONNECTION_REFUSED at https://example.com/")
    assert browser_network_error(exc, operation=NAV) is True


def test_wrapped_mode_requires_navigation_wrapper():
    assert (
        browser_network_error(
            PlaywrightTimeoutError("Timeout 60000ms exceeded."),
            operation=NAV, wrapped=True,
        )
        is False
    )
    assert (
        browser_network_error(
            RuntimeError("Timeout 60000ms exceeded."), operation=NAV, wrapped=True
        )
        is False
    )
    assert (
        browser_network_error(
            RuntimeError("Navigation timeout of 30000 ms exceeded"),
            operation=NAV, wrapped=True,
        )
        is False
    )
    assert (
        browser_network_error(
            RuntimeError("render stage failed"), operation=NAV, wrapped=True
        )
        is False
    )


def test_bare_timeout_message_without_navigation_context_is_not_classified():
    assert (
        browser_network_error(RuntimeError("Timeout 60000ms exceeded."), operation=NAV)
        is False
    )
    assert (
        browser_network_error(
            RuntimeError("Navigation timeout of 30000 ms exceeded"), operation=NAV
        )
        is False
    )


@pytest.mark.parametrize(
    "stage",
    [
        FetchStage.CONTEXT_CREATION,
        FetchStage.GUARD_INSTALL,
        FetchStage.PAGE_CREATION,
        FetchStage.CONTENT,
        FetchStage.LAUNCH,
    ],
)
@pytest.mark.parametrize(
    "exc",
    [
        RuntimeError("Timeout 60000ms exceeded."),
        PlaywrightTimeoutError("Timeout 60000ms exceeded."),
        RuntimeError("net::ERR_CONNECTION_REFUSED at https://example.com/"),
    ],
)
def test_non_navigation_stages_are_never_target_network(stage, exc):
    assert browser_network_error(exc, operation=stage) is False


@pytest.mark.parametrize(
    "message",
    [
        "net::ERR_ABORTED at https://example.com/",
        "net::ERR_BLOCKED_BY_CLIENT at https://example.com/",
        "net::ERR_UNKNOWN_URL_SCHEME at chrome://settings",
        "Executable doesn't exist at /usr/lib/chromium/chrome",
        "Target page, context or browser has been closed",
        "context creation failed",
        "route registration failed",
        "Failed to navigate: browser process crashed",
    ],
)
def test_provider_internal_failures_are_not_target_network(message):
    assert browser_network_error(RuntimeError(message), operation=NAV) is False


def test_target_closed_error_is_not_target_network():
    assert browser_network_error(TargetClosedError(), operation=NAV) is False


def test_launch_failure_playwright_error_is_not_target_network():
    exc = PlaywrightError("Executable doesn't exist at /usr/lib/chromium/chrome")
    assert browser_network_error(exc, operation=NAV) is False


def test_bare_asyncio_timeout_is_not_target_network():
    assert browser_network_error(asyncio.TimeoutError(), operation=NAV) is False
