from __future__ import annotations

import asyncio
import hashlib
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

from .config import AuthenticationSettings


@dataclass(slots=True)
class RateLimitDecision:
    allowed: bool
    retry_after_seconds: float = 0.0
    remaining_attempts: int = 0
    window_resets_at: float | None = None


class AuthRateStore:
    def __init__(self, settings: AuthenticationSettings) -> None:
        self.settings = settings
        self.path = Path(settings.ledger_path)

    @staticmethod
    def target_key(harness: str, launch_mode: str, ssh_host: str | None) -> str:
        target = "local" if launch_mode == "local" else (ssh_host or "").strip().lower()
        material = f"{harness}\0{launch_mode}\0{target}"
        return hashlib.sha256(material.encode()).hexdigest()

    async def check_and_record(self, key: str) -> RateLimitDecision:
        if not self.settings.rate_limit.enabled:
            return RateLimitDecision(allowed=True)
        return await asyncio.to_thread(self._check_and_record_sync, key, time.time())

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=5, isolation_level=None)
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute(
            "CREATE TABLE IF NOT EXISTS auth_attempts ("
            "target_hash TEXT NOT NULL, attempted_at REAL NOT NULL)"
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS auth_attempts_key_time "
            "ON auth_attempts(target_hash, attempted_at)"
        )
        return connection

    def _check_and_record_sync(self, key: str, now: float) -> RateLimitDecision:
        rate = self.settings.rate_limit
        cutoff = now - rate.window_seconds
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("DELETE FROM auth_attempts WHERE attempted_at < ?", (cutoff,))
            rows = connection.execute(
                "SELECT attempted_at FROM auth_attempts WHERE target_hash = ? "
                "ORDER BY attempted_at",
                (key,),
            ).fetchall()
            attempts = [float(row[0]) for row in rows]
            retry_after = 0.0
            if attempts:
                retry_after = max(0.0, rate.min_interval_seconds - (now - attempts[-1]))
            if len(attempts) >= rate.max_attempts:
                reset_at = attempts[0] + rate.window_seconds
                retry_after = max(retry_after, reset_at - now)
                connection.execute("COMMIT")
                return RateLimitDecision(
                    allowed=False,
                    retry_after_seconds=retry_after,
                    remaining_attempts=0,
                    window_resets_at=reset_at,
                )
            if retry_after > 0:
                connection.execute("COMMIT")
                return RateLimitDecision(
                    allowed=False,
                    retry_after_seconds=retry_after,
                    remaining_attempts=max(0, rate.max_attempts - len(attempts)),
                    window_resets_at=(attempts[0] + rate.window_seconds) if attempts else None,
                )
            connection.execute(
                "INSERT INTO auth_attempts(target_hash, attempted_at) VALUES (?, ?)",
                (key, now),
            )
            connection.execute("COMMIT")
            return RateLimitDecision(
                allowed=True,
                remaining_attempts=max(0, rate.max_attempts - len(attempts) - 1),
                window_resets_at=(attempts[0] if attempts else now) + rate.window_seconds,
            )
        except BaseException:
            connection.execute("ROLLBACK")
            raise
        finally:
            connection.close()
