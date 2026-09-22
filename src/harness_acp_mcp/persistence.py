from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import time
from pathlib import Path
from typing import Any


class SessionRecordStore:
    def __init__(self, path: str) -> None:
        self.path = Path(path)

    async def upsert(self, record_id: str, record: dict[str, Any]) -> None:
        payload = json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        await asyncio.to_thread(self._upsert_sync, record_id, payload, time.time())

    async def get(self, record_id: str) -> dict[str, Any] | None:
        return await asyncio.to_thread(self._get_sync, record_id)

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        os.chmod(self.path.parent, 0o700)
        connection = sqlite3.connect(self.path, timeout=5)
        connection.execute("PRAGMA journal_mode=DELETE")
        connection.execute("PRAGMA synchronous=FULL")
        connection.execute(
            "CREATE TABLE IF NOT EXISTS session_records ("
            "record_id TEXT PRIMARY KEY, payload TEXT NOT NULL, updated_at REAL NOT NULL)"
        )
        os.chmod(self.path, 0o600)
        return connection

    def _upsert_sync(self, record_id: str, payload: str, updated_at: float) -> None:
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO session_records(record_id, payload, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(record_id) DO UPDATE SET "
                "payload=excluded.payload, updated_at=excluded.updated_at",
                (record_id, payload, updated_at),
            )

    def _get_sync(self, record_id: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT payload FROM session_records WHERE record_id = ?", (record_id,)
            ).fetchone()
        if row is None:
            return None
        value = json.loads(row[0])
        return value if isinstance(value, dict) else None
