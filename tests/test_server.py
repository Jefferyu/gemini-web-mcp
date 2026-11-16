"""Tests for MCP server tools, session model guards, and stdio JSON-RPC purity."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any

import pytest
from mcp.types import TextContent

from gemini_web_mcp.backends.base import (
    AskRequest,
    AskResponse,
    GeminiBackend,
    HealthStatus,
)
from gemini_web_mcp.config import Settings
from gemini_web_mcp.server import create_server
from gemini_web_mcp.sessions import SessionStore


class MockGeminiBackend(GeminiBackend):
    def __init__(self) -> None:
        self.calls: list[AskRequest] = []
        self.closed = False

    async def init(self) -> None:
        return None

    def normalize_model(self, model: str | None) -> str:
        if model is None:
            return "unspecified"
        mapping = {
            "flash": "gemini-flash",
            "gemini-flash": "gemini-flash",
            "pro": "gemini-pro",
            "gemini-pro": "gemini-pro",
            "thinking": "thinking",
            "unspecified": "unspecified",
        }
        cleaned = model.strip().lower()
        return mapping.get(cleaned, cleaned or "unspecified")

    async def ask(self, req: AskRequest) -> AskResponse:
        self.calls.append(req)
        resolved = self.normalize_model(req.model)
        turn_idx = len(self.calls)
        return AskResponse(
            text=f"answer:{req.prompt}",
            thoughts="internal reasoning" if req.include_thoughts else None,
            metadata=[f"cid_{turn_idx}", f"rid_{turn_idx}", f"rcid_{turn_idx}", f"esid_{turn_idx}"],
            resolved_model=resolved,
        )

    async def health(self) -> HealthStatus:
        return HealthStatus(
            ok=True,
            backend="mock",
            auth_source="env",
            proxy_configured=True,
            proxy_masked="http://***:***@127.0.0.1:7890",
            available_models=["gemini-flash", "gemini-pro"],
            message="Mock backend ready.",
        )

    async def close(self) -> None:
        self.closed = True


def _extract_text(result: Any) -> str:
    content = result.content
    assert len(content) >= 1
    first = content[0]
    assert isinstance(first, TextContent)
    return first.text


async def test_gemini_ask_session_lifecycle_and_model_guard(tmp_path: Path) -> None:
    settings = Settings(
        cookies_path=tmp_path / "cookies.json",
        sessions_db_path=tmp_path / "sessions.db",
    )
    store = SessionStore(settings.sessions_db_path)
    backend = MockGeminiBackend()
    server = create_server(settings=settings, backend=backend, session_store=store)

    # Turn 1: create session with model="flash"
    r1 = _extract_text(
        await server.call_tool(
            "gemini_ask",
            {"prompt": "first question", "session": "review-1", "model": "flash"},
        )
    )
    assert "answer:first question" in r1
    assert "[session: review-1 | turn: 1 | model: gemini-flash]" in r1
    assert backend.calls[0].metadata is None

    # Turn 2: omit model -> inherits gemini-flash and passes 4-element metadata
    r2 = _extract_text(
        await server.call_tool(
            "gemini_ask",
            {"prompt": "follow up", "session": "review-1"},
        )
    )
    assert "[session: review-1 | turn: 2 | model: gemini-flash]" in r2
    assert backend.calls[1].metadata == ["cid_1", "rid_1", "rcid_1", "esid_1"]
    assert backend.calls[1].model == "gemini-flash"

    # Turn 2b: passing alias "flash" that normalizes to "gemini-flash" is allowed
    r2b = _extract_text(
        await server.call_tool(
            "gemini_ask",
            {"prompt": "alias follow up", "session": "review-1", "model": "flash"},
        )
    )
    assert "[session: review-1 | turn: 3 | model: gemini-flash]" in r2b

    # Turn 3: switching to "pro" without new_chat=True is blocked before backend call
    calls_before = len(backend.calls)
    r3 = _extract_text(
        await server.call_tool(
            "gemini_ask",
            {"prompt": "switch model", "session": "review-1", "model": "pro"},
        )
    )
    assert "1050" in r3
    assert "new_chat=True" in r3
    assert len(backend.calls) == calls_before

    # Turn 4: new_chat=True resets session and allows "pro" + include_thoughts
    r4 = _extract_text(
        await server.call_tool(
            "gemini_ask",
            {
                "prompt": "fresh start",
                "session": "review-1",
                "model": "pro",
                "new_chat": True,
                "include_thoughts": True,
            },
        )
    )
    assert "<summary>Thinking Process</summary>" in r4
    assert "internal reasoning" in r4
    assert "[session: review-1 | turn: 1 | model: gemini-pro]" in r4
    assert backend.calls[-1].metadata is None


async def test_gemini_ask_files_and_sessions_and_health(tmp_path: Path) -> None:
    settings = Settings(
        cookies_path=tmp_path / "cookies.json",
        sessions_db_path=tmp_path / "sessions.db",
    )
    store = SessionStore(settings.sessions_db_path)
    backend = MockGeminiBackend()
    server = create_server(settings=settings, backend=backend, session_store=store)

    valid_file = tmp_path / "snippet.py"
    valid_file.write_text("print('hi')\n", encoding="utf-8")
    secret_env = tmp_path / ".env"
    secret_env.write_text("TOKEN=secret\n", encoding="utf-8")

    # Blocked file never reaches backend
    err_res = _extract_text(
        await server.call_tool(
            "gemini_ask",
            {"prompt": "inspect env", "files": [str(secret_env)]},
        )
    )
    assert "File validation failed" in err_res
    assert len(backend.calls) == 0

    # Valid file reaches backend
    ok_res = _extract_text(
        await server.call_tool(
            "gemini_ask",
            {"prompt": "inspect code", "session": "files-test", "files": [str(valid_file)]},
        )
    )
    assert "answer:inspect code" in ok_res
    assert backend.calls[0].files == [valid_file.resolve()]

    # gemini_sessions list / get / delete / clear
    listed_raw = _extract_text(await server.call_tool("gemini_sessions", {"action": "list"}))
    listed = json.loads(listed_raw)
    assert len(listed) == 1
    assert listed[0]["label"] == "files-test"

    fetched = json.loads(
        _extract_text(
            await server.call_tool("gemini_sessions", {"action": "get", "session": "files-test"})
        )
    )
    assert fetched["metadata_length"] == 4

    deleted_msg = _extract_text(
        await server.call_tool("gemini_sessions", {"action": "delete", "session": "files-test"})
    )
    assert "Deleted session 'files-test'" in deleted_msg

    # Create another and clear
    await server.call_tool("gemini_ask", {"prompt": "temp", "session": "temp-1"})
    cleared_msg = _extract_text(await server.call_tool("gemini_sessions", {"action": "clear"}))
    assert "Cleared 1 session(s)." in cleared_msg

    # gemini_health
    health_data = json.loads(_extract_text(await server.call_tool("gemini_health", {})))
    assert health_data["ok"] is True
    assert health_data["proxy_masked"] == "http://***:***@127.0.0.1:7890"
    assert health_data["available_models"] == ["gemini-flash", "gemini-pro"]


@pytest.mark.asyncio
async def test_stdio_subprocess_jsonrpc_purity(tmp_path: Path) -> None:
    """Spawn the real MCP server over stdio and verify stdout only emits valid JSON-RPC."""
    env = {
        "PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin",
        "HOME": str(tmp_path),
        "GEMINI_WEB_MCP_SESSIONS_DB_PATH": str(tmp_path / "sessions.db"),
        "GEMINI_WEB_MCP_COOKIES_PATH": str(tmp_path / "cookies.json"),
        "GEMINI_WEB_MCP_LOG_LEVEL": "INFO",
    }
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "gemini_web_mcp.server",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
    )
    assert proc.stdin is not None
    assert proc.stdout is not None
    assert proc.stderr is not None

    try:
        init_req = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "pytest-stdio-client", "version": "1.0.0"},
            },
        }
        proc.stdin.write((json.dumps(init_req) + "\n").encode("utf-8"))
        await proc.stdin.drain()

        init_line = await asyncio.wait_for(proc.stdout.readline(), timeout=10.0)
        init_resp = json.loads(init_line.decode("utf-8"))
        assert init_resp["id"] == 1
        assert "result" in init_resp
        assert init_resp["result"]["serverInfo"]["name"] == "gemini-web-mcp"

        initialized_notif = {"jsonrpc": "2.0", "method": "notifications/initialized"}
        proc.stdin.write((json.dumps(initialized_notif) + "\n").encode("utf-8"))
        await proc.stdin.drain()

        list_req = {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}
        proc.stdin.write((json.dumps(list_req) + "\n").encode("utf-8"))
        await proc.stdin.drain()

        list_line = await asyncio.wait_for(proc.stdout.readline(), timeout=10.0)
        list_resp = json.loads(list_line.decode("utf-8"))
        assert list_resp["id"] == 2
        tools = list_resp["result"]["tools"]
        tool_names = {t["name"] for t in tools}
        assert tool_names == {"gemini_ask", "gemini_sessions", "gemini_health"}

        tools_by_name = {t["name"]: t for t in tools}
        assert tools_by_name["gemini_ask"]["annotations"]["openWorldHint"] is True
        assert tools_by_name["gemini_health"]["annotations"]["readOnlyHint"] is True

        proc.stdin.close()
        remaining_stdout, _stderr = await asyncio.wait_for(proc.communicate(), timeout=10.0)
        for line in remaining_stdout.decode("utf-8").splitlines():
            if line.strip():
                json.loads(line)
    finally:
        if proc.returncode is None:
            proc.kill()
            await proc.wait()
