from __future__ import annotations

import os
import tomllib
from pathlib import Path

from pydantic import BaseModel, Field, field_validator

from crawl4ai_mcp.models import Tier

SECRET_ENV_VARS = {
    "rayobyte_api_url": "RAYOBYTE_API_URL",
    "rayobyte_api_key": "RAYOBYTE_API_KEY",
    "firecrawl_api_key": "FIRECRAWL_API_KEY",
    "webshare_proxies": "WEBSHARE_PROXIES",
    "oxylabs_proxies": "OXYLABS_PROXIES",
}

DEFAULT_DATABASE_PATH = Path("~/.local/state/crawl4ai-mcp/policy.db")


class AppConfig(BaseModel):
    bind_host: str = "127.0.0.1"
    bind_port: int = 11236
    database_path: Path = DEFAULT_DATABASE_PATH
    visible_text_threshold: int = 200
    http_concurrency: int = 8
    browser_concurrency: int = 2
    chromium_idle_seconds: int = 180
    camoufox_idle_seconds: int = 120
    policy_decay_days: int = 7
    cooldown_seconds: int = 600
    enabled_tiers: list[Tier] = Field(
        default_factory=lambda: [tier for tier in Tier]
    )
    webshare_proxies: list[str] = Field(default_factory=list)
    oxylabs_proxies: list[str] = Field(default_factory=list)
    rayobyte_api_url: str | None = None
    rayobyte_api_key: str | None = None
    firecrawl_api_key: str | None = None

    @field_validator("bind_host")
    @classmethod
    def loopback_only(cls, value: str) -> str:
        if value != "127.0.0.1":
            raise ValueError("service must bind to 127.0.0.1 only")
        return value

    @field_validator("enabled_tiers", mode="before")
    @classmethod
    def parse_tiers(cls, value: object) -> object:
        if isinstance(value, str):
            return [value]
        if isinstance(value, list):
            return [Tier(item) if isinstance(item, int) else Tier[item.upper()] for item in value]
        return value

    @property
    def database_path_expanded(self) -> Path:
        return self.database_path.expanduser()


def _parse_proxy_list(value: str | list[str]) -> list[str]:
    if isinstance(value, list):
        return value
    return [entry.strip() for entry in value.split(",") if entry.strip()]


def load_config(path: Path, env: dict[str, str] | None = None) -> AppConfig:
    env = dict(os.environ if env is None else env)
    raw: dict = {}
    if path.exists():
        with path.open("rb") as handle:
            raw = tomllib.load(handle)

    merged = dict(raw)
    for field_name, env_name in SECRET_ENV_VARS.items():
        if env.get(env_name):
            merged[field_name] = env[env_name]
    for proxy_field in ("webshare_proxies", "oxylabs_proxies"):
        env_name = SECRET_ENV_VARS[proxy_field]
        if env.get(env_name):
            merged[proxy_field] = _parse_proxy_list(env[env_name])

    if "database_path" in merged:
        merged["database_path"] = Path(merged["database_path"]).expanduser()

    return AppConfig(**merged)
