from __future__ import annotations

import asyncio
import os
import shutil
import signal
import sys
import tempfile
from contextlib import suppress
from datetime import timedelta
from pathlib import Path

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp import types as mcp_types
from mcp.client.stdio import stdio_client

FAKE_HARNESS = Path(__file__).with_name("fake_harness.py")


@pytest.mark.asyncio
async def test_stdio_client_autostarts_single_daemon_and_reaches_harness(tmp_path: Path) -> None:
    runtime = Path(tempfile.mkdtemp(prefix="harness-acp-e2e-", dir="/private/tmp"))
    config = runtime / "settings.yaml"
    socket_path = runtime / "daemon.sock"
    lock_path = runtime / "daemon.lock"
    config.write_text(
        "schema_version: 1\n"
        "ipc:\n"
        f"  socket_path: '{socket_path}'\n"
        f"  lock_path: '{lock_path}'\n"
        "daemon:\n"
        "  idle_session_timeout_seconds: 0\n"
        "authentication:\n"
        f"  ledger_path: '{runtime / 'rate.sqlite3'}'\n"
        "  rate_limit:\n"
        "    enabled: false\n"
        "persistence:\n"
        f"  sessions_path: '{runtime / 'sessions.sqlite3'}'\n"
        "process:\n"
        "  startup_timeout_seconds: 5\n"
        "  turn_timeout_seconds: 5\n"
        "  terminate_grace_seconds: 0.1\n"
        "  remote_cleanup_timeout_seconds: 0.1\n"
        "logging:\n"
        f"  path: '{runtime / 'daemon.log'}'\n",
        encoding="utf-8",
    )
    env = os.environ.copy()
    env["HARNESS_ACP_MCP_CONFIG"] = str(config)
    parameters = StdioServerParameters(
        command=sys.executable,
        args=["-m", "harness_acp_mcp.server"],
        env=env,
        cwd=str(tmp_path),
    )
    daemon_pid: int | None = None
    elicitation_messages: list[str] = []

    async def accept_permission(_context, params):
        elicitation_messages.append(params.message)
        return mcp_types.ElicitResult(action="accept", content={"option_id": "allow"})

    try:
        async with stdio_client(parameters) as (read_stream, write_stream):
            async with ClientSession(
                read_stream,
                write_stream,
                read_timeout_seconds=timedelta(seconds=10),
                elicitation_callback=accept_permission,
            ) as client:
                await client.initialize()
                created = await client.call_tool(
                    "create_session",
                    {
                        "harness": "codex",
                        "cwd": str(tmp_path),
                        "model_id": "fake-model",
                        "command": sys.executable,
                        "args": [str(FAKE_HARNESS)],
                    },
                )
                assert created.isError is False
                payload = created.structuredContent
                assert payload is not None
                assert payload["status"] == "ready"
                daemon_pid = int(lock_path.read_text(encoding="utf-8"))

                prompted = await client.call_tool(
                    "prompt",
                    {"session_id": payload["session_id"], "prompt": "hello"},
                )
                assert prompted.isError is False
                assert prompted.structuredContent["text"] == "echo:hello"

                permission = await client.call_tool(
                    "prompt",
                    {"session_id": payload["session_id"], "prompt": "permission"},
                    read_timeout_seconds=timedelta(seconds=10),
                )
                assert permission.isError is False
                assert permission.structuredContent["status"] == "completed"
                assert permission.structuredContent["text"].endswith(";answer:allow")
                assert len(elicitation_messages) == 1

                closed = await client.call_tool(
                    "close_session", {"session_id": payload["session_id"]}
                )
                assert closed.isError is False
        assert daemon_pid is not None
    finally:
        if daemon_pid is None and lock_path.exists():
            daemon_pid = int(lock_path.read_text(encoding="utf-8"))
        if daemon_pid is not None:
            with suppress(ProcessLookupError):
                os.kill(daemon_pid, signal.SIGTERM)
            for _ in range(100):
                try:
                    os.kill(daemon_pid, 0)
                except ProcessLookupError:
                    break
                await asyncio.sleep(0.02)
        shutil.rmtree(runtime, ignore_errors=True)
