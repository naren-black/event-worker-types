"""SQLite-backed idempotency store.

Survives worker restarts so a redelivered message for a file that was
already fully copied to both destinations is recognised and skipped rather
than re-uploaded.
"""

from __future__ import annotations

import os
import sqlite3
import threading

_SCHEMA = """
CREATE TABLE IF NOT EXISTS processed_events (
    idempotency_key TEXT PRIMARY KEY,
    correlation_id TEXT NOT NULL,
    processed_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);
"""


class IdempotencyStore:
    def __init__(self, db_path: str) -> None:
        if db_path != ":memory:":
            directory = os.path.dirname(db_path)
            if directory:
                os.makedirs(directory, exist_ok=True)

        self._lock = threading.Lock()
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        with self._conn:
            self._conn.execute(_SCHEMA)

    def is_done(self, idempotency_key: str) -> bool:
        with self._lock:
            cursor = self._conn.execute(
                "SELECT 1 FROM processed_events WHERE idempotency_key = ?",
                (idempotency_key,),
            )
            return cursor.fetchone() is not None

    def mark_done(self, idempotency_key: str, correlation_id: str) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT OR IGNORE INTO processed_events (idempotency_key, correlation_id) VALUES (?, ?)",
                (idempotency_key, correlation_id),
            )

    def close(self) -> None:
        with self._lock:
            self._conn.close()
