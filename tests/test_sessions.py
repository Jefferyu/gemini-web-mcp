"""Tests for SQLite WAL SessionStore."""

from __future__ import annotations

import asyncio
from pathlib import Path

from gemini_web_mcp.sessions import SessionRecord, SessionStore


def test_session_crud_and_four_element_metadata(tmp_path: Path) -> None:
    db_path = tmp_path / "nested" / "sessions.db"
    store = SessionStore(db_path)

    metadata_4: list[str | None] = ["c_123", "r_456", "rc_789", "esid_extra_0"]
    rec = SessionRecord(
        label="arch-review",
        metadata=metadata_4,
        model="gemini-pro",
        turn_count=1,
    )
    store.upsert(rec)

    loaded = store.get("arch-review")
    assert loaded is not None
    assert loaded.metadata == metadata_4
    assert len(loaded.metadata) == 4
    assert loaded.model == "gemini-pro"
    assert loaded.turn_count == 1

    # Update with None element in metadata
    updated = loaded.model_copy(
        update={
            "metadata": ["c_123", "r_457", None, "esid_extra_1"],
            "turn_count": 2,
        }
    )
    store.upsert(updated)
    reloaded = store.get("arch-review")
    assert reloaded is not None
    assert reloaded.metadata == ["c_123", "r_457", None, "esid_extra_1"]
    assert reloaded.turn_count == 2

    # List, delete, clear
    store.upsert(SessionRecord(label="bug-hunt", metadata=["c_2", "r_2", "rc_2"], turn_count=1))
    listed = store.list_sessions()
    assert {item.label for item in listed} == {"arch-review", "bug-hunt"}

    assert store.delete("arch-review") is True
    assert store.delete("arch-review") is False
    assert store.get("arch-review") is None

    assert store.clear() == 1
    assert store.list_sessions() == []


async def test_session_same_label_lock_serializes(tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "sessions.db")
    events: list[str] = []

    async def worker(name: str, label: str, delay: float) -> None:
        async with store.lock(label):
            events.append(f"start:{name}")
            await asyncio.sleep(delay)
            events.append(f"end:{name}")

    # Same label serializes: w1 completes before w2 starts
    await asyncio.gather(
        worker("w1", "shared", 0.05),
        worker("w2", "shared", 0.01),
    )
    assert events == ["start:w1", "end:w1", "start:w2", "end:w2"]

    # Different labels run concurrently
    diff_events: list[str] = []

    async def diff_worker(name: str, label: str, delay: float) -> None:
        async with store.lock(label):
            diff_events.append(f"start:{name}")
            await asyncio.sleep(delay)
            diff_events.append(f"end:{name}")

    await asyncio.gather(
        diff_worker("a", "label-a", 0.05),
        diff_worker("b", "label-b", 0.01),
    )
    assert diff_events.index("start:b") < diff_events.index("end:a")


def test_two_independent_stores_concurrent_writes(tmp_path: Path) -> None:
    db_path = tmp_path / "sessions.db"
    store_a = SessionStore(db_path)
    store_b = SessionStore(db_path)

    for idx in range(25):
        store_a.upsert(
            SessionRecord(
                label=f"win-a-{idx}",
                metadata=[f"cid_a_{idx}", f"rid_a_{idx}", f"rcid_a_{idx}", f"esid_a_{idx}"],
                model="gemini-flash",
                turn_count=idx + 1,
            )
        )
        store_b.upsert(
            SessionRecord(
                label=f"win-b-{idx}",
                metadata=[f"cid_b_{idx}", f"rid_b_{idx}", f"rcid_b_{idx}", f"esid_b_{idx}"],
                model="gemini-pro",
                turn_count=idx + 1,
            )
        )

    all_records = store_a.list_sessions()
    assert len(all_records) == 50
    labels = {r.label for r in all_records}
    assert "win-a-0" in labels and "win-b-24" in labels
