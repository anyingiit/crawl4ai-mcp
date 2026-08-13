import asyncio

import pytest
from patchright._impl._errors import TimeoutError as PatchrightTimeoutError
from playwright._impl._errors import (
    Error as PlaywrightError,
    TargetClosedError,
    TimeoutError as PlaywrightTimeoutError,
)

from crawl4ai_mcp.providers.browser_errors import browser_network_error


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
    assert browser_network_error(RuntimeError(message)) is True


def test_crawl4ai_wrapped_navigation_error_is_classified():
    exc = RuntimeError(
        "Failed on navigating ACS-GOTO:\n"
        "net::ERR_CONNECTION_RESET at https://example.com/"
    )
    assert browser_network_error(exc) is True


@pytest.mark.parametrize(
    "message",
    [
        "Failed on navigating ACS-GOTO:\nTimeout 60000ms exceeded.",
        "Failed on navigating ACS-GOTO:\nNavigation timeout of 30000 ms exceeded",
    ],
)
def test_navigation_timeout_messages_are_classified(message):
    assert browser_network_error(RuntimeError(message)) is True


def test_playwright_and_patchright_timeout_types_are_classified():
    assert browser_network_error(PlaywrightTimeoutError("Timeout 60000ms exceeded.")) is True
    assert browser_network_error(PatchrightTimeoutError("Timeout 60000ms exceeded.")) is True


def test_playwright_error_with_marker_is_classified():
    exc = PlaywrightError("net::ERR_CONNECTION_REFUSED at https://example.com/")
    assert browser_network_error(exc) is True


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
    assert browser_network_error(RuntimeError(message)) is False


def test_target_closed_error_is_not_target_network():
    assert browser_network_error(TargetClosedError()) is False


def test_launch_failure_playwright_error_is_not_target_network():
    exc = PlaywrightError("Executable doesn't exist at /usr/lib/chromium/chrome")
    assert browser_network_error(exc) is False


def test_bare_asyncio_timeout_is_not_target_network():
    assert browser_network_error(asyncio.TimeoutError()) is False
