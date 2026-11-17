"""Configuration management for gemini-web-mcp."""

from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_FALLBACK_PROXY_ENV_KEYS = (
    "HTTPS_PROXY",
    "https_proxy",
    "HTTP_PROXY",
    "http_proxy",
    "ALL_PROXY",
    "all_proxy",
)


def mask_proxy(proxy: str | None) -> str | None:
    """Mask embedded credentials in a proxy URL while keeping scheme, host, and port."""
    if not proxy:
        return None
    parts = urlsplit(proxy)
    if not parts.username and not parts.password:
        return proxy
    host = parts.hostname or ""
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    port_suffix = f":{parts.port}" if parts.port is not None else ""
    netloc = f"***:***@{host}{port_suffix}"
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))


class Settings(BaseSettings):
    """Runtime settings loaded from environment variables (`GEMINI_WEB_MCP_*`)."""

    model_config = SettingsConfigDict(
        env_prefix="GEMINI_WEB_MCP_",
        extra="ignore",
    )

    backend: str = "cookie"
    default_model: str = "unspecified"
    proxy: str | None = None
    enable_browser_cookies: bool = True
    cookies_path: Path = Field(
        default_factory=lambda: Path("~/.config/gemini-web-mcp/cookies.json").expanduser()
    )
    sessions_db_path: Path = Field(
        default_factory=lambda: Path("~/.config/gemini-web-mcp/sessions.db").expanduser()
    )
    timeout_seconds: int = Field(default=120, ge=1)
    thinking_timeout_seconds: int = Field(default=300, ge=1)
    max_files_per_request: int = Field(default=10, ge=1)
    max_file_size_mb: int = Field(default=20, ge=1)
    max_total_file_size_mb: int = Field(default=50, ge=1)
    log_level: str = "WARNING"

    @field_validator("backend", mode="before")
    @classmethod
    def _normalize_backend(cls, value: str) -> str:
        return value.strip().lower() or "cookie"

    @field_validator("cookies_path", "sessions_db_path", mode="before")
    @classmethod
    def _expand_path(cls, value: str | Path) -> Path:
        return Path(value).expanduser()

    @field_validator("proxy", mode="before")
    @classmethod
    def _normalize_proxy(cls, value: str | None) -> str | None:
        if value is None:
            return None
        stripped = value.strip()
        return stripped or None

    @field_validator("log_level", mode="before")
    @classmethod
    def _normalize_log_level(cls, value: str) -> str:
        return value.strip().upper() or "WARNING"

    @model_validator(mode="after")
    def _fallback_proxy_from_env(self) -> Settings:
        if self.proxy is not None:
            return self
        for key in _FALLBACK_PROXY_ENV_KEYS:
            candidate = os.environ.get(key, "").strip()
            if candidate:
                self.proxy = candidate
                break
        return self
