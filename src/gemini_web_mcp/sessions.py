"""SQLite WAL-backed, process-safe and coroutine-safe session persistence."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel, Field


def utc_now_iso() -> str:
    """Return current UTC timestamp in ISO-8601 format."""
    return datetime.now(tz=UTC).isoformat()


class SessionRecord(BaseModel):
    """Persisted multi-turn Gemini Web session state."""

    label: str
    metadata: list[str | None] = Field(default_factory=list)
    model: str = "unspecified"
    turn_count: int = 0
    created_at: str = Field(default_factory=utc_now_iso)
    updated_at: str = Field(default_factory=utc_now_iso)


class SessionStore:
    """Thread/process-safe SQLite WAL session store with per-label asyncio locks."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = Path(db_path).expanduser()
        self._locks: dict[str, asyncio.Lock] = {}
        self._ensure_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=5.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout = 5000;")
        return conn

    def _ensure_schema(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self._connect() as conn:
            conn.execute("PRAGMA journal_mode = WAL;")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS sessions (
                    label TEXT PRIMARY KEY,
                    metadata_json TEXT NOT NULL,
                    model TEXT NOT NULL,
                    turn_count INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            conn.commit()
        with suppress(OSError):
            self.db_path.chmod(0o600)

    @asynccontextmanager
    async def lock(self, label: str) -> AsyncIterator[None]:
        """Serialize requests sharing the same session label within this process."""
        label_lock = self._locks.setdefault(label, asyncio.Lock())
        async with label_lock:
            yield

    @staticmethod
    def _row_to_record(row: sqlite3.Row) -> SessionRecord:
        metadata_raw = json.loads(row["metadata_json"])
        if not isinstance(metadata_raw, list):
            metadata_raw = []
        return SessionRecord(
            label=str(row["label"]),
            metadata=[None if item is None else str(item) for item in metadata_raw],
            model=str(row["model"]),
            turn_count=int(row["turn_count"]),
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
        )

    def get(self, label: str) -> SessionRecord | None:
        """Fetch a session record by label, or None if not found."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT label, metadata_json, model, turn_count, created_at, updated_at "
                "FROM sessions WHERE label = ?",
                (label,),
            ).fetchone()
        return self._row_to_record(row) if row is not None else None

    def upsert(self, record: SessionRecord) -> SessionRecord:
        """Insert or update a session record while preserving complete metadata."""
        metadata_json = json.dumps(record.metadata)
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO sessions (
                    label, metadata_json, model, turn_count, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(label) DO UPDATE SET
                    metadata_json = excluded.metadata_json,
                    model = excluded.model,
                    turn_count = excluded.turn_count,
                    created_at = excluded.created_at,
                    updated_at = excluded.updated_at
                """,
                (
                    record.label,
                    metadata_json,
                    record.model,
                    record.turn_count,
                    record.created_at,
                    record.updated_at,
                ),
            )
            conn.commit()
        return record

    def list_sessions(self) -> list[SessionRecord]:
        """List all persisted sessions ordered by most recently updated."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT label, metadata_json, model, turn_count, created_at, updated_at "
                "FROM sessions ORDER BY updated_at DESC, label ASC"
            ).fetchall()
        return [self._row_to_record(row) for row in rows]

    def delete(self, label: str) -> bool:
        """Delete a session by label. Returns True if a row was removed."""
        with self._connect() as conn:
            cursor = conn.execute("DELETE FROM sessions WHERE label = ?", (label,))
            conn.commit()
            return cursor.rowcount > 0

    def clear(self) -> int:
        """Delete all sessions and return the number of deleted rows."""
        with self._connect() as conn:
            cursor = conn.execute("DELETE FROM sessions")
            conn.commit()
            return cursor.rowcount
