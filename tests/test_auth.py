"""Tests for Settings and AuthLoader."""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from gemini_web_mcp.auth import (
    AuthConfigurationError,
    AuthLoader,
    clear_rotated_cache,
    get_rotated_cache_path,
    read_rotated_1psidts,
)
from gemini_web_mcp.config import Settings, mask_proxy


def test_settings_proxy_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GEMINI_WEB_MCP_PROXY", raising=False)
    monkeypatch.delenv("HTTPS_PROXY", raising=False)
    monkeypatch.delenv("https_proxy", raising=False)
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:7890")
    settings = Settings()
    assert settings.proxy == "http://127.0.0.1:7890"

    monkeypatch.setenv("GEMINI_WEB_MCP_PROXY", "socks5://user:secret@127.0.0.1:1080")
    settings_override = Settings()
    assert settings_override.proxy == "socks5://user:secret@127.0.0.1:1080"
    assert mask_proxy(settings_override.proxy) == "socks5://***:***@127.0.0.1:1080"
    assert mask_proxy("http://127.0.0.1:7890") == "http://127.0.0.1:7890"
    assert mask_proxy(None) is None


def test_auth_loader_env_priority(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GEMINI_COOKIE_PATH", str(tmp_path / "cache"))
    cookies_file = tmp_path / "cookies.json"
    cookies_file.write_text(
        json.dumps({"__Secure-1PSID": "file-psid", "__Secure-1PSIDTS": "file-ts"}),
        encoding="utf-8",
    )

    loader = AuthLoader(
        cookies_path=cookies_file,
        env={
            "GEMINI_SECURE_1PSID": "env-psid",
            "GEMINI_SECURE_1PSIDTS": "env-ts",
        },
        browser_loader=lambda: {},
    )
    creds = loader.load()
    assert creds.secure_1psid == "env-psid"
    assert creds.secure_1psidts == "env-ts"
    assert creds.source == "env"


def test_auth_loader_dict_and_array_cookies_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GEMINI_COOKIE_PATH", str(tmp_path / "cache"))
    cookies_file = tmp_path / "cookies.json"

    # Dict format
    cookies_file.write_text(
        json.dumps({"__Secure-1PSID": "dict-psid", "__Secure-1PSIDTS": "dict-ts"}),
        encoding="utf-8",
    )
    loader = AuthLoader(cookies_path=cookies_file, env={}, browser_loader=lambda: {})
    creds = loader.load()
    assert creds.secure_1psid == "dict-psid"
    assert creds.secure_1psidts == "dict-ts"
    assert "cookies_file" in creds.source

    # Browser extension array format
    cookies_file.write_text(
        json.dumps(
            [
                {"name": "SID", "value": "other"},
                {"name": "__Secure-1PSID", "value": "arr-psid"},
                {"name": "__Secure-1PSIDTS", "value": "arr-ts"},
            ]
        ),
        encoding="utf-8",
    )
    creds_arr = loader.load()
    assert creds_arr.secure_1psid == "arr-psid"
    assert creds_arr.secure_1psidts == "arr-ts"


def test_auth_loader_rotated_cache_coordination(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cache_dir = tmp_path / "cookie_cache"
    monkeypatch.setenv("GEMINI_COOKIE_PATH", str(cache_dir))

    cookies_file = tmp_path / "cookies.json"
    cookies_file.write_text(
        json.dumps({"__Secure-1PSID": "psid-1", "__Secure-1PSIDTS": "stale-static-ts"}),
        encoding="utf-8",
    )

    cache_path = get_rotated_cache_path("psid-1")
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(
        json.dumps(
            [
                {
                    "name": "__Secure-1PSID",
                    "value": "psid-1",
                    "expires": time.time() + 3600,
                },
                {
                    "name": "__Secure-1PSIDTS",
                    "value": "fresh-rotated-ts",
                    "expires": time.time() + 3600,
                },
            ]
        ),
        encoding="utf-8",
    )

    loader = AuthLoader(cookies_path=cookies_file, env={}, browser_loader=lambda: {})
    creds = loader.load(prefer_rotated_cache=True)
    assert creds.secure_1psidts == "fresh-rotated-ts"
    assert "rotated-cache" in creds.source

    # Clearing cache falls back cleanly to static config
    assert clear_rotated_cache("psid-1") is True
    assert read_rotated_1psidts("psid-1") is None
    creds_fallback = loader.load(prefer_rotated_cache=True)
    assert creds_fallback.secure_1psidts == "stale-static-ts"


def test_auth_loader_browser_fallback_and_errors(tmp_path: Path) -> None:
    missing_file = tmp_path / "nonexistent.json"

    loader_browser = AuthLoader(
        cookies_path=missing_file,
        env={},
        browser_loader=lambda: {
            "firefox": [
                {"name": "__Secure-1PSID", "value": "ff-psid"},
                {"name": "__Secure-1PSIDTS", "value": "ff-ts"},
            ]
        },
    )
    creds = loader_browser.load(prefer_rotated_cache=False)
    assert creds.secure_1psid == "ff-psid"
    assert creds.source == "browser:firefox"

    # Missing all cookies -> actionable error without secrets
    loader_empty = AuthLoader(cookies_path=missing_file, env={}, browser_loader=lambda: {})
    with pytest.raises(AuthConfigurationError, match="__Secure-1PSID"):
        loader_empty.load()

    # Keychain permission error -> actionable macOS guidance
    def raise_perm() -> dict[str, list[dict[str, object]]]:
        raise PermissionError("Keychain denied")

    loader_perm = AuthLoader(cookies_path=missing_file, env={}, browser_loader=raise_perm)
    with pytest.raises(AuthConfigurationError, match="Keychain"):
        loader_perm.load()

    # File exists without __Secure-1PSID -> actionable error
    bad_file = tmp_path / "bad.json"
    bad_file.write_text(json.dumps({"OTHER": "value"}), encoding="utf-8")
    loader_bad = AuthLoader(cookies_path=bad_file, env={}, browser_loader=lambda: {})
    with pytest.raises(AuthConfigurationError, match="__Secure-1PSID"):
        loader_bad.load()
