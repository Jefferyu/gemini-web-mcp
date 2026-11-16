"""MCP stdio server exposing Web Gemini as a subagent."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, Literal, cast

from loguru import logger as _loguru_logger

_VALID_LOG_LEVELS = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
LogLevelLiteral = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]


def configure_logging(level: str | None = None) -> LogLevelLiteral:
    """Force loguru and stdlib logging onto sys.stderr before importing gemini_webapi."""
    resolved = (level or os.environ.get("GEMINI_WEB_MCP_LOG_LEVEL") or "WARNING").strip().upper()
    if resolved not in _VALID_LOG_LEVELS:
        resolved = "WARNING"

    _loguru_logger.remove()
    _loguru_logger.add(sys.stderr, level=resolved)

    logging.basicConfig(
        level=getattr(logging, resolved, logging.WARNING),
        stream=sys.stderr,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        force=True,
    )
    return cast(LogLevelLiteral, resolved)


# Isolate stdout for JSON-RPC before importing third-party libraries (e.g., gemini_webapi).
_INITIAL_LOG_LEVEL: LogLevelLiteral = configure_logging()

from mcp.server.mcpserver import Context, MCPServer  # noqa: E402
from mcp.types import ToolAnnotations  # noqa: E402

from gemini_web_mcp.backends.base import (  # noqa: E402
    AskRequest,
    BackendError,
    GeminiBackend,
)
from gemini_web_mcp.backends.cookie_backend import CookieBackend  # noqa: E402
from gemini_web_mcp.config import Settings  # noqa: E402
from gemini_web_mcp.files import FileValidationError, validate_upload_files  # noqa: E402
from gemini_web_mcp.sessions import SessionRecord, SessionStore, utc_now_iso  # noqa: E402


@dataclass(slots=True)
class AppState:
    """Shared server state initialized during MCP server setup."""

    settings: Settings
    backend: GeminiBackend
    session_store: SessionStore


async def _progress_heartbeat(ctx: Context[Any, Any] | None, interval: float = 10.0) -> None:
    """Emit periodic MCP progress notifications during long-running Gemini requests."""
    if ctx is None:
        return
    elapsed = 0
    while True:
        await asyncio.sleep(interval)
        elapsed += int(interval)
        with contextlib.suppress(Exception):
            await ctx.report_progress(
                progress=float(elapsed),
                total=None,
                message=f"Waiting for Gemini Web response ({elapsed}s)...",
            )


def _format_ask_output(
    *,
    text: str,
    thoughts: str | None,
    session_label: str,
    turn_count: int,
    resolved_model: str,
) -> str:
    body = text
    if thoughts:
        body = (
            "<details>\n"
            "<summary>Thinking Process</summary>\n\n"
            f"{thoughts}\n\n"
            "</details>\n\n"
            f"{text}"
        )
    footer = f"[session: {session_label} | turn: {turn_count} | model: {resolved_model}]"
    return f"{body}\n\n---\n{footer}"


def create_server(
    settings: Settings | None = None,
    backend: GeminiBackend | None = None,
    session_store: SessionStore | None = None,
) -> MCPServer[AppState]:
    """Create and configure the gemini-web-mcp MCPServer instance."""
    resolved_settings = settings or Settings()
    log_level = configure_logging(resolved_settings.log_level)
    resolved_store = session_store or SessionStore(resolved_settings.sessions_db_path)
    resolved_backend = backend or CookieBackend(resolved_settings)
    state = AppState(
        settings=resolved_settings,
        backend=resolved_backend,
        session_store=resolved_store,
    )

    @asynccontextmanager
    async def lifespan(_app: MCPServer[AppState]) -> AsyncIterator[AppState]:
        try:
            yield state
        finally:
            await state.backend.close()

    mcp: MCPServer[AppState] = MCPServer(
        name="gemini-web-mcp",
        instructions=(
            "MCP Server wrapping Web Gemini (gemini.google.com) for text reasoning, "
            "code/architecture second-opinion review, and multimodal file analysis."
        ),
        lifespan=lifespan,
        log_level=log_level,
    )

    @mcp.tool(
        name="gemini_ask",
        title="Ask Gemini Web",
        description=(
            "Send a prompt (and optional local files) to Web Gemini with persistent named "
            "multi-turn sessions. Use distinct `session` labels for independent tasks."
        ),
        annotations=ToolAnnotations(title="Ask Gemini Web", open_world_hint=True),
    )
    async def gemini_ask(
        prompt: str,
        session: str = "default",
        new_chat: bool = False,
        model: str | None = None,
        files: list[str] | None = None,
        include_thoughts: bool = False,
        ctx: Context[Any, Any] | None = None,
    ) -> str:
        """Ask Web Gemini a question or send files for multimodal analysis."""
        if not prompt or not prompt.strip():
            return "[Error] `prompt` must be a non-empty string."

        session_label = session.strip() or "default"

        try:
            validated_files = validate_upload_files(
                files or [],
                max_count=state.settings.max_files_per_request,
                max_size_mb=state.settings.max_file_size_mb,
                extra_blocked_paths=[
                    state.settings.cookies_path,
                    state.settings.sessions_db_path,
                ],
            )
        except FileValidationError as exc:
            return f"[Error] File validation failed: {exc}"

        async with state.session_store.lock(session_label):
            existing = None if new_chat else state.session_store.get(session_label)
            if existing is not None:
                if model is not None:
                    requested_norm = state.backend.normalize_model(model)
                    existing_norm = state.backend.normalize_model(existing.model)
                    if requested_norm != existing_norm:
                        return (
                            f"[Error] Session '{session_label}' is bound to model "
                            f"'{existing.model}'. Switching models mid-session can trigger "
                            "Gemini error 1050. Omit `model` to continue with "
                            f"'{existing.model}', pass `new_chat=True` to reset, or use a "
                            "new `session` label."
                        )
                effective_model = existing.model
                metadata_to_send = existing.metadata
            else:
                effective_model = (
                    model.strip()
                    if model is not None and model.strip()
                    else state.settings.default_model
                )
                metadata_to_send = None

            heartbeat_task = asyncio.create_task(_progress_heartbeat(ctx))
            try:
                response = await state.backend.ask(
                    AskRequest(
                        prompt=prompt,
                        metadata=metadata_to_send,
                        model=effective_model,
                        files=validated_files,
                        include_thoughts=include_thoughts,
                    )
                )
            except BackendError as exc:
                return f"[Error] {exc}"
            finally:
                heartbeat_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await heartbeat_task

            now_iso = utc_now_iso()
            turn_count = (existing.turn_count + 1) if existing is not None else 1
            created_at = existing.created_at if existing is not None else now_iso
            state.session_store.upsert(
                SessionRecord(
                    label=session_label,
                    metadata=response.metadata,
                    model=response.resolved_model,
                    turn_count=turn_count,
                    created_at=created_at,
                    updated_at=now_iso,
                )
            )

        return _format_ask_output(
            text=response.text,
            thoughts=response.thoughts if include_thoughts else None,
            session_label=session_label,
            turn_count=turn_count,
            resolved_model=response.resolved_model,
        )

    @mcp.tool(
        name="gemini_sessions",
        title="Manage Gemini Sessions",
        description="List, inspect, delete, or clear persisted named Gemini Web sessions.",
        annotations=ToolAnnotations(title="Manage Gemini Sessions"),
    )
    async def gemini_sessions(
        action: Literal["list", "get", "delete", "clear"] = "list",
        session: str | None = None,
    ) -> str:
        """Manage persisted multi-turn sessions in the local SQLite WAL store."""
        if action == "list":
            records = state.session_store.list_sessions()
            if not records:
                return "No persisted sessions found."
            payload = [
                {
                    "label": rec.label,
                    "model": rec.model,
                    "turn_count": rec.turn_count,
                    "updated_at": rec.updated_at,
                }
                for rec in records
            ]
            return json.dumps(payload, indent=2, ensure_ascii=False)

        if action == "get":
            if not session or not session.strip():
                return "[Error] `session` label is required for action='get'."
            rec = state.session_store.get(session.strip())
            if rec is None:
                return f"Session '{session.strip()}' not found."
            return json.dumps(
                {
                    "label": rec.label,
                    "model": rec.model,
                    "turn_count": rec.turn_count,
                    "metadata_length": len(rec.metadata),
                    "created_at": rec.created_at,
                    "updated_at": rec.updated_at,
                },
                indent=2,
                ensure_ascii=False,
            )

        if action == "delete":
            if not session or not session.strip():
                return "[Error] `session` label is required for action='delete'."
            deleted = state.session_store.delete(session.strip())
            if deleted:
                return f"Deleted session '{session.strip()}'."
            return f"Session '{session.strip()}' not found."

        if action == "clear":
            removed = state.session_store.clear()
            return f"Cleared {removed} session(s)."

        return f"[Error] Unsupported action: {action}"

    @mcp.tool(
        name="gemini_health",
        title="Gemini Web Health Check",
        description=(
            "Check Gemini Web authentication status, active backend, masked proxy "
            "configuration, and dynamically discovered models."
        ),
        annotations=ToolAnnotations(title="Gemini Web Health Check", read_only_hint=True),
    )
    async def gemini_health() -> str:
        """Return diagnostic health status of the Gemini Web backend."""
        status = await state.backend.health()
        payload = status.model_dump()
        payload["sessions_db"] = str(state.settings.sessions_db_path)
        return json.dumps(payload, indent=2, ensure_ascii=False)

    return mcp


def main() -> None:
    """CLI entrypoint for `gemini-web-mcp` over stdio."""
    configure_logging()
    server = create_server()
    server.run(transport="stdio")


if __name__ == "__main__":
    main()
