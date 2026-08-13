import ipaddress

import pytest

from crawl4ai_mcp.egress import (
    Origin,
    UrlPolicy,
    UrlPolicyError,
    UrlPolicyReason,
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
