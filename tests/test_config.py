from pathlib import Path

import pytest

from crawl4ai_mcp.config import AppConfig, load_config


def test_defaults_match_resource_contract(tmp_path: Path):
    path = tmp_path / "config.toml"
    path.write_text("", encoding="utf-8")
    config = load_config(path, env={})
    assert config.http_concurrency == 8
    assert config.browser_concurrency == 2
    assert config.visible_text_threshold == 200
    assert config.chromium_idle_seconds == 180
    assert config.camoufox_idle_seconds == 120
    assert config.bind_host == "127.0.0.1"
    assert config.bind_port == 11236


def test_proxy_credential_env_vars_load_into_config(tmp_path: Path):
    path = tmp_path / "config.toml"
    path.write_text("", encoding="utf-8")
    config = load_config(
        path,
        env={
            "WEBSHARE_PROXY_USERNAME": "ws-user",
            "WEBSHARE_PROXY_PASSWORD": "ws-pass",
            "OXYLABS_PROXY_USERNAME": "ox-user",
            "OXYLABS_PROXY_PASSWORD": "ox-pass",
            "WEBSHARE_PROXIES": "http://ws.proxy.example:8080",
            "OXYLABS_PROXIES": "http://dc.oxylabs.example:8000",
        },
    )
    assert config.webshare_proxy_username == "ws-user"
    assert config.webshare_proxy_password == "ws-pass"
    assert config.oxylabs_proxy_username == "ox-user"
    assert config.oxylabs_proxy_password == "ox-pass"


def test_repr_and_str_redact_secret_values(tmp_path: Path):
    path = tmp_path / "config.toml"
    path.write_text("", encoding="utf-8")
    config = load_config(
        path,
        env={
            "WEBSHARE_PROXY_USERNAME": "ws-user",
            "WEBSHARE_PROXY_PASSWORD": "ws-pass",
            "OXYLABS_PROXY_USERNAME": "ox-user",
            "OXYLABS_PROXY_PASSWORD": "ox-pass",
            "FIRECRAWL_API_KEY": "fc-secret",
            "RAYOBYTE_API_KEY": "rb-secret",
            "RAYOBYTE_API_URL": "https://api.example/",
            "WEBSHARE_PROXIES": "http://ws.proxy.example:8080",
        },
    )
    for rendered in (repr(config), str(config)):
        assert "ws-pass" not in rendered
        assert "ox-user" not in rendered
        assert "ox-pass" not in rendered
        assert "fc-secret" not in rendered
        assert "rb-secret" not in rendered
        assert "ws-user" not in rendered
        assert "[REDACTED]" in rendered


@pytest.mark.parametrize(
    "env",
    [
        {
            "WEBSHARE_PROXIES": "http://ws.proxy.example:8080",
            "WEBSHARE_PROXY_USERNAME": "only-user",
        },
        {
            "WEBSHARE_PROXIES": "http://ws.proxy.example:8080",
            "WEBSHARE_PROXY_PASSWORD": "only-pass",
        },
        {
            "OXYLABS_PROXIES": "http://dc.oxylabs.example:8000",
            "OXYLABS_PROXY_USERNAME": "only-user",
        },
        {
            "OXYLABS_PROXIES": "http://dc.oxylabs.example:8000",
            "OXYLABS_PROXY_PASSWORD": "only-pass",
        },
    ],
)
def test_partial_proxy_credential_pair_rejected_at_load(tmp_path: Path, env):
    path = tmp_path / "config.toml"
    path.write_text("", encoding="utf-8")
    with pytest.raises(ValueError, match="username and password"):
        load_config(path, env=env)


def test_appconfig_accepts_partial_pair_for_availability_reporting():
    config = AppConfig(
        webshare_proxies=["http://ws.proxy.example:8080"],
        webshare_proxy_username="only-user",
    )
    assert config.webshare_proxy_username == "only-user"
