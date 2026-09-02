from __future__ import annotations

import os
import tomllib
import warnings
from pathlib import Path

from pydantic import BaseModel, Field, field_validator

from crawl4ai_mcp.models import Tier

SECRET_ENV_VARS = {
    "rayobyte_api_url": "RAYOBYTE_API_URL",
    "rayobyte_api_key": "RAYOBYTE_API_KEY",
    "firecrawl_api_key": "FIRECRAWL_API_KEY",
    "webshare_proxies": "WEBSHARE_PROXIES",
    "webshare_proxy_username": "WEBSHARE_PROXY_USERNAME",
    "webshare_proxy_password": "WEBSHARE_PROXY_PASSWORD",
    "oxylabs_proxies": "OXYLABS_PROXIES",
    "oxylabs_proxy_username": "OXYLABS_PROXY_USERNAME",
    "oxylabs_proxy_password": "OXYLABS_PROXY_PASSWORD",
}

DEFAULT_DATABASE_PATH = Path("~/.local/state/crawl4ai-mcp/policy.db")


class AppConfig(BaseModel):
    bind_host: str = "127.0.0.1"
    bind_port: int = 11236
    extra_allowed_hosts: list[str] = Field(default_factory=list)
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
    webshare_proxy_username: str | None = None
    webshare_proxy_password: str | None = None
    oxylabs_proxy_username: str | None = None
    oxylabs_proxy_password: str | None = None
    rayobyte_api_url: str | None = None
    rayobyte_api_key: str | None = None
    firecrawl_api_key: str | None = None

    def _redacted(self) -> str:
        fields = []
        for name in type(self).model_fields:
            value = getattr(self, name)
            if name in SECRET_ENV_VARS:
                value = "[REDACTED]" if value is not None else None
            fields.append(f"{name}={value!r}")
        return ", ".join(fields)

    def __repr__(self) -> str:
        return f"{type(self).__name__}({self._redacted()})"

    def __str__(self) -> str:
        return self._redacted()

    @field_validator("bind_host")
    @classmethod
    def loopback_only(cls, value: str) -> str:
        if value != "127.0.0.1":
            raise ValueError("service must bind to 127.0.0.1 only")
        return value

    @field_validator("extra_allowed_hosts")
    @classmethod
    def validate_extra_allowed_hosts(cls, value: list[str]) -> list[str]:
        stripped = []
        for entry in value:
            cleaned = entry.strip()
            if not cleaned:
                raise ValueError("extra_allowed_hosts entries must not be empty")
            if cleaned == "*":
                warnings.warn(
                    "extra_allowed_hosts contains '*', which disables host "
                    "origin protection",
                    UserWarning,
                    stacklevel=2,
                )
            stripped.append(cleaned)
        return stripped

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


def _validate_proxy_credentials(
    raw: dict, pool: str, username: str, password: str
) -> None:
    if not raw.get(pool):
        return
    has_username = bool(raw.get(username))
    has_password = bool(raw.get(password))
    if has_username != has_password:
        raise ValueError(
            f"{pool} requires both username and password; "
            "set either both or neither"
        )


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

    _validate_proxy_credentials(
        merged, "webshare_proxies", "webshare_proxy_username", "webshare_proxy_password"
    )
    _validate_proxy_credentials(
        merged, "oxylabs_proxies", "oxylabs_proxy_username", "oxylabs_proxy_password"
    )

    if "database_path" in merged:
        merged["database_path"] = Path(merged["database_path"]).expanduser()

    return AppConfig(**merged)
