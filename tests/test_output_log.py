from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from harness_acp_mcp.output_log import HarnessOutputLog


@pytest.mark.asyncio
async def test_output_log_is_private_json_lines_and_discards_oldest_entries(tmp_path: Path) -> None:
    log = HarnessOutputLog(directory=tmp_path, max_bytes=512)
    for index in range(12):
        await log.append("stdout", {"index": index, "text": "输出" * 8})
    contents = log.path.read_bytes()
    entries = [json.loads(line) for line in contents.splitlines()]
    assert len(contents) <= 512
    assert entries[-1]["value"]["index"] == 11
    assert entries[0]["value"]["index"] > 0
    assert all(item["stream"] == "stdout" for item in entries)
    assert "输出".encode() in contents
    assert os.stat(log.path).st_mode & 0o777 == 0o600


@pytest.mark.asyncio
async def test_oversized_tracking_entry_gets_valid_json_summary(tmp_path: Path) -> None:
    log = HarnessOutputLog(directory=tmp_path, max_bytes=512)
    await log.append("stdout", {"text": "x" * 1000})
    entry = json.loads(log.path.read_text())
    assert entry["error"].startswith("Output entry exceeded")
    assert log.path.stat().st_size <= 512


@pytest.mark.asyncio
async def test_compaction_does_not_rewrite_the_file_on_every_append(
    tmp_path: Path, monkeypatch
) -> None:
    max_bytes = 16 * 1024
    log = HarnessOutputLog(directory=tmp_path, max_bytes=max_bytes)
    rewrites: list[int] = []
    original = HarnessOutputLog._compact_and_append

    def counting(self: HarnessOutputLog, line: bytes, size: int) -> None:
        rewrites.append(size)
        original(self, line, size)

    monkeypatch.setattr(HarnessOutputLog, "_compact_and_append", counting)

    appends = 500
    for index in range(appends):
        await log.append("stdout", {"index": index, "text": "x" * 128})

    # A full rewrite must happen only every ~max_bytes/2 of appended data, not per record.
    assert 0 < len(rewrites) < appends // 10
    assert log.path.stat().st_size <= max_bytes
    entries = [json.loads(line) for line in log.path.read_bytes().splitlines()]
    assert entries[-1]["value"]["index"] == appends - 1
    assert entries[0]["value"]["index"] > 0


@pytest.mark.asyncio
async def test_large_compaction_copies_multiple_chunks_of_complete_records(
    tmp_path: Path, monkeypatch
) -> None:
    max_bytes = 4 * 1024 * 1024
    log = HarnessOutputLog(directory=tmp_path, max_bytes=max_bytes)
    rewrites = 0
    original = HarnessOutputLog._compact_and_append

    def counting(self: HarnessOutputLog, line: bytes, size: int) -> None:
        nonlocal rewrites
        rewrites += 1
        original(self, line, size)

    monkeypatch.setattr(HarnessOutputLog, "_compact_and_append", counting)

    appends = 1200
    payload = "z" * 8192
    for index in range(appends):
        await log.append("stdout", {"index": index, "text": payload})

    assert rewrites >= 2
    assert log.path.stat().st_size <= max_bytes
    entries = [json.loads(line) for line in log.path.read_bytes().splitlines()]
    first = entries[0]["value"]["index"]
    assert first > 0
    # Every retained record is a complete, contiguous JSON line across 64KiB copy chunks.
    assert [entry["value"]["index"] for entry in entries] == list(range(first, appends))
    assert len(log.path.read_bytes()) > 64 * 1024
