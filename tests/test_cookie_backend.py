"""Tests for CookieBackend using mocked GeminiClient."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from gemini_webapi.constants import AccountStatus
from gemini_webapi.exceptions import AuthError
from gemini_webapi.exceptions import TimeoutError as GeminiTimeoutError

from gemini_web_mcp.auth import AuthLoader, get_rotated_cache_path
from gemini_web_mcp.backends import create_backend
from gemini_web_mcp.backends.base import AskRequest, BackendError
from gemini_web_mcp.backends.cookie_backend import CookieBackend
from gemini_web_mcp.config import Settings


@dataclass
class FakeAvailableModel:
    model_name: str
    model_id: str = "hex123"
    is_available: bool = True


@dataclass
class FakeOutput:
    text: str
    thoughts: str | None = None
    metadata: list[str | None] | None = None


class FakeChatSession:
    def __init__(self, client: FakeGeminiClient, **kwargs: Any) -> None:
        self.client = client
        self.metadata: list[str | None] = list(kwargs.get("metadata") or [])
        self.model = kwargs.get("model")
        self.calls: list[dict[str, Any]] = []

    async def send_message(self, prompt: str, **kwargs: Any) -> FakeOutput:
        self.calls.append({"prompt": prompt, **kwargs})
        self.client.sent_calls.append(
            {
                "prompt": prompt,
                "metadata": list(self.metadata),
                "model": self.model,
                **kwargs,
            }
        )
        new_meta = ["c_1", "r_1", "rc_1", "esid_1"]
        self.metadata = new_meta
        return FakeOutput(
            text=f"reply:{prompt}",
            thoughts="step-by-step reasoning",
            metadata=new_meta,
        )


class FakeGeminiClient:
    def __init__(
        self,
        secure_1psid: str,
        secure_1psidts: str | None = None,
        proxy: str | None = None,
    ) -> None:
        self.secure_1psid = secure_1psid
        self.secure_1psidts = secure_1psidts
        self.proxy = proxy
        self.init_calls = 0
        self.init_kwargs: dict[str, Any] = {}
        self.closed = False
        self._running = False
        self.account_status = AccountStatus.AVAILABLE
        self._cookie_source = "Base Cookies"
        self.timeout = 450.0
        self.watchdog_timeout = 120.0
        self.sent_calls: list[dict[str, Any]] = []
        self.models = [
            FakeAvailableModel("gemini-flash", "id_flash"),
            FakeAvailableModel("gemini-pro", "id_pro"),
        ]

    async def init(self, **kwargs: Any) -> None:
        await asyncio.sleep(0.02)
        self.init_calls += 1
        self.init_kwargs = dict(kwargs)
        if "timeout" in kwargs:
            self.timeout = float(kwargs["timeout"])
        if "watchdog_timeout" in kwargs:
            self.watchdog_timeout = float(kwargs["watchdog_timeout"])
        self._running = True

    def list_models(self) -> list[FakeAvailableModel]:
        return list(self.models)

    def resolve_model(self, name: str) -> FakeAvailableModel:
        low = name.strip().lower()
        for m in self.models:
            if low in (m.model_name, m.model_name.removeprefix("gemini-")):
                return m
        raise ValueError(f"Unknown model: {name}")

    def start_chat(self, **kwargs: Any) -> FakeChatSession:
        return FakeChatSession(self, **kwargs)

    async def close(self) -> None:
        self.closed = True
        self._running = False


async def test_cookie_backend_concurrent_init_once_and_ask(tmp_path: Path) -> None:
    created_clients: list[FakeGeminiClient] = []

    def factory(psid: str, psidts: str | None, proxy: str | None) -> FakeGeminiClient:
        c = FakeGeminiClient(psid, psidts, proxy)
        created_clients.append(c)
        return c

    settings = Settings(
        proxy="http://user:pass@127.0.0.1:7890",
        cookies_path=tmp_path / "cookies.json",
        sessions_db_path=tmp_path / "sessions.db",
    )
    loader = AuthLoader(
        cookies_path=settings.cookies_path,
        env={"GEMINI_SECURE_1PSID": "psid-abc", "GEMINI_SECURE_1PSIDTS": "ts-xyz"},
        browser_loader=lambda: {},
    )
    backend = CookieBackend(settings, auth_loader=loader, client_factory=factory)

    # Concurrent ask calls must initialize GeminiClient exactly once
    r1, r2 = await asyncio.gather(
        backend.ask(AskRequest(prompt="hello", model="flash", include_thoughts=True)),
        backend.ask(AskRequest(prompt="world", model="pro", include_thoughts=False)),
    )

    assert len(created_clients) == 1
    client = created_clients[0]
    assert client.init_calls == 1
    assert client.proxy == "http://user:pass@127.0.0.1:7890"

    assert r1.text == "reply:hello"
    assert r1.thoughts == "step-by-step reasoning"
    assert r1.resolved_model == "gemini-flash"
    assert r1.metadata == ["c_1", "r_1", "rc_1", "esid_1"]

    assert r2.text == "reply:world"
    assert r2.thoughts is None
    assert r2.resolved_model == "gemini-pro"

    # Model normalization aligns alias and resolved name
    assert backend.normalize_model("flash") == "gemini-flash"
    assert backend.normalize_model("gemini-flash") == "gemini-flash"

    # Health reports masked proxy and available models
    health = await backend.health()
    assert health.ok is True
    assert health.proxy_masked == "http://***:***@127.0.0.1:7890"
    assert health.available_models == ["gemini-flash", "gemini-pro"]

    await backend.close()
    assert client.closed is True


async def test_cookie_backend_thinking_mode_and_timeout(tmp_path: Path) -> None:
    created_clients: list[FakeGeminiClient] = []

    def factory(psid: str, psidts: str | None, proxy: str | None) -> FakeGeminiClient:
        c = FakeGeminiClient(psid, psidts, proxy)
        created_clients.append(c)
        return c

    settings = Settings(
        cookies_path=tmp_path / "cookies.json",
        sessions_db_path=tmp_path / "sessions.db",
        timeout_seconds=120,
        thinking_timeout_seconds=300,
    )
    loader = AuthLoader(
        cookies_path=settings.cookies_path,
        env={"GEMINI_SECURE_1PSID": "psid-1"},
        browser_loader=lambda: {},
    )
    backend = CookieBackend(settings, auth_loader=loader, client_factory=factory)

    res = await backend.ask(AskRequest(prompt="deep question", model="thinking"))
    assert res.resolved_model == "thinking"
    client = created_clients[0]
    assert client.timeout == 300.0
    assert client.watchdog_timeout == 300.0
    call = client.sent_calls[0]
    assert call["extended_thinking"] is True
    assert call["timeout"] == 300.0
    # Backing model header is resolved (gemini-pro) instead of sending None
    assert call["model"] is not None
    assert call["model"].model_name == "gemini-pro"

    # Explicit <model>-thinking alias resolves base model + extended_thinking
    res_flash_think = await backend.ask(AskRequest(prompt="fast deep", model="flash-thinking"))
    assert res_flash_think.resolved_model == "gemini-flash-thinking"
    assert backend.normalize_model("flash-thinking") == "gemini-flash-thinking"
    assert client.sent_calls[1]["model"].model_name == "gemini-flash"
    assert client.sent_calls[1]["extended_thinking"] is True


async def test_cookie_backend_auth_error_clears_cache_and_translates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GEMINI_COOKIE_PATH", str(tmp_path / "cache"))
    cache_path = get_rotated_cache_path("psid-1")
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(
        '[{"name": "__Secure-1PSIDTS", "value": "stale-cached"}]',
        encoding="utf-8",
    )

    attempts: list[str | None] = []

    class FailingInitClient(FakeGeminiClient):
        async def init(self, **kwargs: Any) -> None:
            attempts.append(self.secure_1psidts)
            raise AuthError("Invalid cookies")

    settings = Settings(
        cookies_path=tmp_path / "cookies.json",
        sessions_db_path=tmp_path / "sessions.db",
    )
    loader = AuthLoader(
        cookies_path=settings.cookies_path,
        env={"GEMINI_SECURE_1PSID": "psid-1", "GEMINI_SECURE_1PSIDTS": "env-ts"},
        browser_loader=lambda: {},
    )
    backend = CookieBackend(settings, auth_loader=loader, client_factory=FailingInitClient)

    with pytest.raises(BackendError, match="authentication failed"):
        await backend.ask(AskRequest(prompt="hi"))

    # Tried rotated cache first, cleared it on AuthError, then retried static env-ts
    assert attempts == ["stale-cached", "env-ts"]
    assert not cache_path.exists()

    # health() returns ok=False instead of crashing
    health = await backend.health()
    assert health.ok is False
    assert "authentication failed" in health.message


async def test_cookie_backend_timeout_translation(tmp_path: Path) -> None:
    class TimeoutChat(FakeChatSession):
        async def send_message(self, prompt: str, **kwargs: Any) -> FakeOutput:
            raise GeminiTimeoutError("Read timed out")

    class TimeoutClient(FakeGeminiClient):
        def start_chat(self, **kwargs: Any) -> TimeoutChat:
            return TimeoutChat(self, **kwargs)

    settings = Settings(
        cookies_path=tmp_path / "cookies.json",
        sessions_db_path=tmp_path / "sessions.db",
    )
    loader = AuthLoader(
        cookies_path=settings.cookies_path,
        env={"GEMINI_SECURE_1PSID": "psid-1"},
        browser_loader=lambda: {},
    )
    backend = CookieBackend(settings, auth_loader=loader, client_factory=TimeoutClient)

    with pytest.raises(BackendError, match="timed out"):
        await backend.ask(AskRequest(prompt="slow"))


async def test_cookie_backend_rejects_silent_guest_downgrade(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Upstream gemini-webapi can silently fall back to Guest (UNAUTHENTICATED) without raising."""
    monkeypatch.setenv("GEMINI_COOKIE_PATH", str(tmp_path / "cache"))
    cache_path = get_rotated_cache_path("psid-guest")
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(
        '[{"name": "__Secure-1PSIDTS", "value": "expired-cached-ts"}]',
        encoding="utf-8",
    )

    class GuestOnExpiredClient(FakeGeminiClient):
        async def init(self, **kwargs: Any) -> None:
            await super().init(**kwargs)
            if self.secure_1psidts == "expired-cached-ts":
                # Simulate upstream silent Guest fallback: no exception raised by init()
                self._cookie_source = "Guest"
                self.account_status = AccountStatus.UNAUTHENTICATED
                self.models = [
                    FakeAvailableModel("gemini-flash", "id_flash", is_available=True),
                    FakeAvailableModel("gemini-pro", "id_pro", is_available=False),
                ]
            elif self.secure_1psidts == "dead-static-ts":
                self._cookie_source = "Guest"
                self.account_status = AccountStatus.UNAUTHENTICATED

    settings = Settings(
        cookies_path=tmp_path / "cookies.json",
        sessions_db_path=tmp_path / "sessions.db",
    )

    # Case 1: Rotated cache downgrades to Guest -> clears cache and succeeds with fresh static ts
    loader_recover = AuthLoader(
        cookies_path=settings.cookies_path,
        env={"GEMINI_SECURE_1PSID": "psid-guest", "GEMINI_SECURE_1PSIDTS": "valid-static-ts"},
        browser_loader=lambda: {},
    )
    backend_ok = CookieBackend(
        settings, auth_loader=loader_recover, client_factory=GuestOnExpiredClient
    )
    health_ok = await backend_ok.health()
    assert health_ok.ok is True
    assert not cache_path.exists()

    # Case 2: Static credentials also downgrade to Guest -> health() reports ok=False (not True!)
    loader_dead = AuthLoader(
        cookies_path=settings.cookies_path,
        env={"GEMINI_SECURE_1PSID": "psid-guest", "GEMINI_SECURE_1PSIDTS": "dead-static-ts"},
        browser_loader=lambda: {},
    )
    backend_dead = CookieBackend(
        settings, auth_loader=loader_dead, client_factory=GuestOnExpiredClient
    )
    health_dead = await backend_dead.health()
    assert health_dead.ok is False
    assert "authentication failed" in health_dead.message


async def test_cookie_backend_runtime_auth_error_resets_and_hot_reloads_cookies(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When cookies expire mid-run, updating cookies.json works on retry without restart."""
    monkeypatch.setenv("GEMINI_COOKIE_PATH", str(tmp_path / "cache"))
    cookies_file = tmp_path / "cookies.json"
    cookies_file.write_text(
        json.dumps({"__Secure-1PSID": "psid-old", "__Secure-1PSIDTS": "ts-old"}),
        encoding="utf-8",
    )

    expire_now = False

    class ExpiringChat(FakeChatSession):
        async def send_message(self, prompt: str, **kwargs: Any) -> FakeOutput:
            if expire_now and self.client.secure_1psid == "psid-old":
                raise AuthError("Session expired at runtime")
            return await super().send_message(prompt, **kwargs)

    class ExpiringClient(FakeGeminiClient):
        def start_chat(self, **kwargs: Any) -> ExpiringChat:
            return ExpiringChat(self, **kwargs)

    settings = Settings(
        cookies_path=cookies_file,
        sessions_db_path=tmp_path / "sessions.db",
    )
    loader = AuthLoader(cookies_path=cookies_file, env={}, browser_loader=lambda: {})
    backend = CookieBackend(settings, auth_loader=loader, client_factory=ExpiringClient)

    # Turn 1 succeeds with initial cookies.json
    r1 = await backend.ask(AskRequest(prompt="turn1"))
    assert r1.text == "reply:turn1"

    # Cookies expire mid-run -> ask() raises actionable BackendError and resets backend state
    expire_now = True
    with pytest.raises(BackendError, match="authentication failed"):
        await backend.ask(AskRequest(prompt="turn2-fail"))

    # User updates cookies.json on disk without restarting MCP server -> next retry succeeds!
    cookies_file.write_text(
        json.dumps({"__Secure-1PSID": "psid-fresh", "__Secure-1PSIDTS": "ts-fresh"}),
        encoding="utf-8",
    )
    r2 = await backend.ask(AskRequest(prompt="turn2-recovered"))
    assert r2.text == "reply:turn2-recovered"


def test_create_backend_rejects_unknown_backend(tmp_path: Path) -> None:
    settings = Settings(
        backend="unknown-bridge",
        cookies_path=tmp_path / "cookies.json",
        sessions_db_path=tmp_path / "sessions.db",
    )
    with pytest.raises(ValueError, match="Unsupported backend"):
        create_backend(settings)
