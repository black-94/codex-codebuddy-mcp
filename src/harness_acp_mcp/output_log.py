from __future__ import annotations

import asyncio
import hashlib
import json
import os
import tempfile
import threading
import uuid
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

DEFAULT_MAX_BYTES = 100 * 1024 * 1024

_COPY_CHUNK_BYTES = 64 * 1024
DIRECTORY_PREFIX = "harness-acp-mcp-output-"


def daemon_directory(identity: str) -> Path:
    """Return the private output-log directory for one daemon instance.

    The directory is namespaced by a stable daemon identity, so a sweep run by one
    daemon can never delete another daemon's logs even though both live under the
    system temporary directory. The identity survives restarts, so retained logs of a
    restarted daemon are still swept once their retention elapses.
    """
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:16]
    owner = f"-{os.getuid()}" if hasattr(os, "getuid") else ""
    directory = Path(tempfile.gettempdir()) / f"{DIRECTORY_PREFIX}{digest}{owner}"
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    return directory


class HarnessOutputLog:
    """A private, bounded JSON-lines log of one harness transport.

    The file always stays at or below ``max_bytes``. When appending would exceed the
    cap, the log is compacted down to roughly half of ``max_bytes`` and the new record
    is appended afterwards. Compaction only removes the oldest *complete* records and
    copies the surviving tail in fixed-size chunks, so both the per-append cost and the
    peak memory stay bounded instead of rewriting the whole file on every append.
    """

    FILE_PREFIX = "harness-acp-output-"
    FILE_SUFFIX = ".jsonl"
    FILE_GLOB = f"{FILE_PREFIX}*{FILE_SUFFIX}"

    def __init__(
        self, *, directory: Path | None = None, max_bytes: int = DEFAULT_MAX_BYTES
    ) -> None:
        self.max_bytes = max_bytes
        base = directory or self.default_directory()
        self.path = base / f"{self.FILE_PREFIX}{uuid.uuid4().hex}{self.FILE_SUFFIX}"
        descriptor = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(descriptor)
        self._lock = threading.Lock()

    @staticmethod
    def default_directory() -> Path:
        return Path(tempfile.gettempdir())

    async def append(self, stream: str, value: Any) -> None:
        entry = {
            "timestamp": datetime.now(UTC).isoformat(),
            "stream": stream,
            "value": value,
        }
        await asyncio.to_thread(self._append_sync, entry)

    def _append_sync(self, entry: dict[str, Any]) -> None:
        line = self._encode(entry)
        if len(line) > self.max_bytes:
            line = self._encode({
                "timestamp": entry["timestamp"],
                "stream": entry["stream"],
                "error": "Output entry exceeded the tracking file limit and was omitted.",
                "bytes": len(line),
            })
        if len(line) > self.max_bytes:
            return
        with self._lock:
            size = self.path.stat().st_size
            if size + len(line) <= self.max_bytes:
                with self.path.open("ab") as output:
                    output.write(line)
                return
            self._compact_and_append(line, size)

    def _compact_and_append(self, line: bytes, size: int) -> None:
        """Rewrite the retained tail plus ``line`` through a private temporary file.

        ``keep_target`` is half of the cap so a subsequent burst of appends is cheap;
        the retained region starts on the first newline at or after the target offset,
        which guarantees that only complete JSONL records survive.
        """
        keep_target = min(self.max_bytes // 2, self.max_bytes - len(line))
        start = max(0, size - keep_target)
        descriptor, temp_name = tempfile.mkstemp(
            prefix=".harness-acp-output-", dir=str(self.path.parent)
        )
        try:
            with os.fdopen(descriptor, "wb") as target:
                begin = self._record_start(start)
                if begin < size:
                    with self.path.open("rb") as source:
                        source.seek(begin)
                        while True:
                            chunk = source.read(_COPY_CHUNK_BYTES)
                            if not chunk:
                                break
                            target.write(chunk)
                target.write(line)
            os.chmod(temp_name, 0o600)
            os.replace(temp_name, self.path)
        except BaseException:
            with suppress(OSError):
                os.unlink(temp_name)
            raise

    def _record_start(self, start: int) -> int:
        """Return the offset just past the first newline at or after ``start``."""
        if start <= 0:
            return 0
        position = start
        with self.path.open("rb") as source:
            source.seek(position)
            while True:
                chunk = source.read(_COPY_CHUNK_BYTES)
                if not chunk:
                    return self.path.stat().st_size
                boundary = chunk.find(b"\n")
                if boundary >= 0:
                    return position + boundary + 1
                position += len(chunk)

    @staticmethod
    def _encode(entry: dict[str, Any]) -> bytes:
        return (json.dumps(entry, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")
