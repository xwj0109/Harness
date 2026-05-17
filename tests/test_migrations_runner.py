from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path

import pytest

from harness.memory.sqlite_store import SCHEMA_MIGRATIONS, SQLiteStore


def _migration_checksum(filename: str) -> str:
    migration_path = Path(__file__).parents[1] / "src" / "harness" / "memory" / "migrations" / filename
    return hashlib.sha256(migration_path.read_text(encoding="utf-8").encode("utf-8")).hexdigest()


def test_schema_migrations_apply_in_declared_order_and_are_idempotent(tmp_path) -> None:
    store = SQLiteStore(tmp_path)
    store.initialize()
    first = store.list_schema_migrations()

    store.initialize()
    second = store.list_schema_migrations()

    assert [row["id"] for row in first] == [migration[0] for migration in SCHEMA_MIGRATIONS]
    assert first == second
    assert first[0]["checksum"] == _migration_checksum(SCHEMA_MIGRATIONS[0][1])
    assert first[0]["metadata_json"]

    session = store.create_session(title="Migration smoke")
    assert store.get_session(session.id).title == "Migration smoke"


def test_schema_migration_checksum_mismatch_fails_closed(tmp_path) -> None:
    store = SQLiteStore(tmp_path)
    store.initialize()
    migration_id = SCHEMA_MIGRATIONS[0][0]

    with sqlite3.connect(store.db_path) as conn:
        conn.execute("UPDATE schema_migrations SET checksum = ? WHERE id = ?", ("bad-checksum", migration_id))

    with pytest.raises(RuntimeError, match=f"Schema migration checksum mismatch for {migration_id}"):
        SQLiteStore(tmp_path).initialize()


def test_unknown_future_schema_migration_fails_closed(tmp_path) -> None:
    store = SQLiteStore(tmp_path)
    store.initialize()

    with sqlite3.connect(store.db_path) as conn:
        conn.execute(
            """
            INSERT INTO schema_migrations (id, checksum, applied_at, metadata_json)
            VALUES (?, ?, ?, ?)
            """,
            ("29990101_001_future", "future-checksum", "2999-01-01T00:00:00+00:00", "{}"),
        )

    with pytest.raises(RuntimeError, match="Unknown future schema migration"):
        SQLiteStore(tmp_path).initialize()

