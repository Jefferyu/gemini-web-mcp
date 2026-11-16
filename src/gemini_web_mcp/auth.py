"""Multi-source cookie authentication loader and rotation cache coordinator."""

from __future__ import annotations

import json
import os
import tempfile
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_ENV_1PSID = "GEMINI_SECURE_1PSID"
_ENV_1PSIDTS = "GEMINI_SECURE_1PSIDTS"
_COOKIE_1PSID = "__Secure-1PSID"
_COOKIE_1PSIDTS = "__Secure-1PSIDTS"


class AuthConfigurationError(RuntimeError):
    """Raised when Gemini Web credentials cannot be resolved or are invalid."""


@dataclass(frozen=True, slots=True)
class AuthCredentials:
    """Resolved Gemini Web cookie credentials and provenance."""

    secure_1psid: str
    secure_1psidts: str | None
    source: str


def get_cookie_cache_dir() -> Path:
    """Return the upstream gemini-webapi cookie rotation cache directory."""
    override = os.getenv("GEMINI_COOKIE_PATH", "").strip()
    if override:
        return Path(override).expanduser()
    return Path(tempfile.gettempdir()) / "gemini_webapi"


def get_rotated_cache_path(secure_1psid: str) -> Path:
    """Return the upstream rotated cookie cache file path for a given __Secure-1PSID."""
    return get_cookie_cache_dir() / f".cached_cookies_{secure_1psid}.json"


def read_rotated_1psidts(secure_1psid: str, *, now: float | None = None) -> str | None:
    """Read a valid, non-expired rotated __Secure-1PSIDTS from upstream cache if present."""
    cache_path = get_rotated_cache_path(secure_1psid)
    if not cache_path.is_file():
        return None
    try:
        raw = cache_path.read_text(encoding="utf-8").strip()
        if not raw:
            return None
        data = json.loads(raw)
    except (OSError, json.JSONDecodeError):
        return None

    if not isinstance(data, list):
        return None

    current_ts = time.time() if now is None else now
    for item in data:
        if not isinstance(item, dict):
            continue
        if item.get("name") != _COOKIE_1PSIDTS:
            continue
        value = item.get("value")
        if not isinstance(value, str) or not value.strip():
            continue
        expires = item.get("expires")
        if isinstance(expires, (int, float)) and expires > 0 and expires < current_ts:
            continue
        return value.strip()
    return None


def clear_rotated_cache(secure_1psid: str) -> bool:
    """Remove the upstream rotated cookie cache file if it exists."""
    cache_path = get_rotated_cache_path(secure_1psid)
    if not cache_path.is_file():
        return False
    try:
        cache_path.unlink()
        return True
    except OSError:
        return False


def _default_browser_loader() -> dict[str, list[dict[str, Any]]]:
    """Load `.google.com` cookies from local browsers via upstream helper."""
    from gemini_webapi.utils.load_browser_cookies import HAS_BC3, load_browser_cookies

    if not HAS_BC3:
        return {}
    result: dict[str, list[dict[str, Any]]] = load_browser_cookies(
        domain_name="google.com", verbose=False
    )
    return result


def _parse_cookies_json(raw: Any, cookies_path: Path) -> tuple[str | None, str | None]:
    """Parse `{name: value}` dict or `[{name, value}, ...]` list cookie exports."""
    if isinstance(raw, dict):
        psid = raw.get(_COOKIE_1PSID)
        psidts = raw.get(_COOKIE_1PSIDTS)
        return (
            psid.strip() if isinstance(psid, str) and psid.strip() else None,
            psidts.strip() if isinstance(psidts, str) and psidts.strip() else None,
        )

    if isinstance(raw, list):
        cookie_map: dict[str, str] = {}
        for entry in raw:
            if not isinstance(entry, dict):
                continue
            name = entry.get("name")
            value = entry.get("value")
            if isinstance(name, str) and isinstance(value, str) and value.strip():
                cookie_map[name] = value.strip()
        return cookie_map.get(_COOKIE_1PSID), cookie_map.get(_COOKIE_1PSIDTS)

    raise AuthConfigurationError(
        f"Cookie file {cookies_path} must be a JSON object or an array of cookie objects."
    )


class AuthLoader:
    """Resolve Gemini Web cookies across env vars, JSON file, rotated cache, and browser."""

    def __init__(
        self,
        cookies_path: Path | None = None,
        *,
        env: Mapping[str, str] | None = None,
        browser_loader: Callable[[], dict[str, list[dict[str, Any]]]] | None = None,
    ) -> None:
        self.cookies_path = (
            cookies_path
            if cookies_path is not None
            else Path("~/.config/gemini-web-mcp/cookies.json").expanduser()
        )
        self._env = env if env is not None else os.environ
        self._browser_loader = (
            browser_loader if browser_loader is not None else _default_browser_loader
        )

    def load(self, *, prefer_rotated_cache: bool = True) -> AuthCredentials:
        """Load credentials in priority order: env -> cookies.json -> local browser."""
        creds = self._load_from_env()
        if creds is None:
            creds = self._load_from_file()
        if creds is None:
            creds = self._load_from_browser()
        if creds is None:
            raise AuthConfigurationError(self._missing_credentials_message())

        if prefer_rotated_cache:
            cached_1psidts = read_rotated_1psidts(creds.secure_1psid)
            if cached_1psidts:
                return AuthCredentials(
                    secure_1psid=creds.secure_1psid,
                    secure_1psidts=cached_1psidts,
                    source=f"{creds.source} (+rotated-cache)",
                )

        return creds

    def _load_from_env(self) -> AuthCredentials | None:
        psid = self._env.get(_ENV_1PSID, "").strip()
        if not psid:
            return None
        psidts = self._env.get(_ENV_1PSIDTS, "").strip() or None
        return AuthCredentials(secure_1psid=psid, secure_1psidts=psidts, source="env")

    def _load_from_file(self) -> AuthCredentials | None:
        if not self.cookies_path.exists():
            return None
        try:
            content = self.cookies_path.read_text(encoding="utf-8")
            parsed = json.loads(content)
        except OSError as exc:
            raise AuthConfigurationError(
                f"Failed to read cookie file at {self.cookies_path}: {exc}"
            ) from exc
        except json.JSONDecodeError as exc:
            raise AuthConfigurationError(
                f"Invalid JSON in cookie file {self.cookies_path}: {exc}. "
                'Expected {"__Secure-1PSID": "...", "__Secure-1PSIDTS": "..."} '
                "or a Cookie-Editor JSON array."
            ) from exc

        psid, psidts = _parse_cookies_json(parsed, self.cookies_path)
        if not psid:
            raise AuthConfigurationError(
                f"Cookie file {self.cookies_path} does not contain a non-empty "
                f'"{_COOKIE_1PSID}" entry. Export cookies from gemini.google.com and '
                f"save them to {self.cookies_path}."
            )
        return AuthCredentials(
            secure_1psid=psid,
            secure_1psidts=psidts,
            source=f"cookies_file:{self.cookies_path}",
        )

    def _load_from_browser(self) -> AuthCredentials | None:
        try:
            browser_cookies = self._browser_loader()
        except PermissionError as exc:
            raise AuthConfigurationError(
                "Permission denied reading browser cookies (macOS Keychain / Chrome 130+ "
                "App-Bound Encryption may block automated extraction). "
                f"Please export cookies manually to {self.cookies_path} or set "
                f"{_ENV_1PSID} / {_ENV_1PSIDTS}."
            ) from exc
        except Exception as exc:
            raise AuthConfigurationError(
                f"Browser cookie extraction failed ({type(exc).__name__}): {exc}. "
                f"Please export cookies manually to {self.cookies_path} or set "
                f"{_ENV_1PSID} / {_ENV_1PSIDTS}."
            ) from exc

        for browser_name, entries in browser_cookies.items():
            cookie_map = {
                str(item.get("name")): str(item.get("value")).strip()
                for item in entries
                if isinstance(item, dict)
                and item.get("name")
                and isinstance(item.get("value"), str)
                and item.get("value", "").strip()
            }
            psid = cookie_map.get(_COOKIE_1PSID)
            if psid:
                return AuthCredentials(
                    secure_1psid=psid,
                    secure_1psidts=cookie_map.get(_COOKIE_1PSIDTS),
                    source=f"browser:{browser_name}",
                )
        return None

    def _missing_credentials_message(self) -> str:
        return (
            "No Gemini Web cookies found. Configure credentials using one of:\n"
            f"1. Set {_ENV_1PSID} (and optionally {_ENV_1PSIDTS}) environment variables.\n"
            f"2. Save cookies to {self.cookies_path} as "
            '{"__Secure-1PSID": "...", "__Secure-1PSIDTS": "..."} '
            "or a browser Cookie-Editor JSON array.\n"
            "3. Install `gemini-web-mcp[browser-cookies]` and log into gemini.google.com "
            "(note: on macOS / Chrome 130+, Keychain or App-Bound Encryption may require "
            "manual export)."
        )
