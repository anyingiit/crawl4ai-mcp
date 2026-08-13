from pathlib import Path
from crawl4ai_mcp.config import load_config


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
