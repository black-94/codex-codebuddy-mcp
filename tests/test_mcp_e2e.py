from __future__ import annotations

import asyncio
import json
import os
import shlex
import shutil
import signal
import sys
import tempfile
import time
from contextlib import suppress
from datetime import timedelta
from pathlib import Path

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp import types as mcp_types
from mcp.client.stdio import stdio_client

FAKE_HARNESS = Path(__file__).with_name("fake_harness.py")


def docker_shim(runtime: Path) -> Path:
    """A fake ``docker`` CLI: enough of run/start/inspect/exec/stop/rm for an E2E."""
    shim = runtime / "docker-shim"
    shim.write_text(
        f"#!{sys.executable}\n"
        "import hashlib, json, os, sys\n"
        "args = sys.argv[1:]\n"
        "if not args:\n"
        "    sys.exit(0)\n"
        "if args[0] == 'run':\n"
        "    print(hashlib.sha256(args[args.index('--name') + 1].encode()).hexdigest())\n"
        "elif args[0] == 'inspect':\n"
        "    payload = json.loads(os.environ.get('FAKE_DOCKER_INSPECT_JSON') or '{}')\n"
        "    payload['id'] = args[-1]\n"
        "    payload.setdefault('image', 'fake:latest')\n"
        "    print(json.dumps(payload))\n"
        "elif args[0] == 'exec' and '--workdir' in args:\n"
        "    image_index = args.index('--workdir') + 2\n"
        "    os.execv(args[image_index + 1], args[image_index + 1:])\n",
        encoding="utf-8",
    )
    shim.chmod(0o700)
    return shim


def terminate_daemon(lock_path: Path, daemon_pid: int | None) -> None:
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
            time.sleep(0.02)


@pytest.mark.asyncio
async def test_mcp_reports_oversized_line_as_structured_error(tmp_path: Path) -> None:
    runtime = Path(tempfile.mkdtemp(prefix="harness-acp-e2e-oversize-", dir="/tmp"))
    config = runtime / "settings.yaml"
    lock_path = runtime / "daemon.lock"
    config.write_text(
        "schema_version: 1\n"
        "ipc:\n"
        f"  socket_path: '{runtime / 'daemon.sock'}'\n"
        f"  lock_path: '{lock_path}'\n"
        "daemon:\n"
        "  idle_session_timeout_seconds: 0\n"
        "authentication:\n"
        f"  ledger_path: '{runtime / 'rate.sqlite3'}'\n"
        "  rate_limit:\n"
        "    enabled: false\n"
        "process:\n"
        "  startup_timeout_seconds: 5\n"
        "  turn_timeout_seconds: 5\n"
        "  terminate_grace_seconds: 0.1\n"
        "  remote_cleanup_timeout_seconds: 0.1\n"
        "buffers:\n"
        "  max_read_bytes: 64KiB\n"
        "launch:\n"
        f"  codex_command: '{shlex.join([sys.executable, str(FAKE_HARNESS)])}'\n"
        "logging:\n"
        f"  path: '{runtime / 'daemon.log'}'\n",
        encoding="utf-8",
    )
    env = os.environ.copy()
    env["HARNESS_ACP_MCP_CONFIG"] = str(config)
    env["FAKE_OVERSIZE_BYTES"] = "200000"
    parameters = StdioServerParameters(
        command=sys.executable,
        args=["-m", "harness_acp_mcp.server"],
        env=env,
        cwd=str(tmp_path),
    )
    daemon_pid: int | None = None
    try:
        async with stdio_client(parameters) as (read_stream, write_stream):
            async with ClientSession(
                read_stream,
                write_stream,
                read_timeout_seconds=timedelta(seconds=10),
            ) as client:
                await client.initialize()
                created = await client.call_tool(
                    "create_session",
                    {"harness": "codex", "cwd": str(tmp_path), "model_id": "fake-model"},
                )
                assert created.isError is False
                payload = created.structuredContent
                assert payload is not None
                daemon_pid = int(lock_path.read_text(encoding="utf-8"))

                failed = await client.call_tool(
                    "prompt",
                    {"session_id": payload["session_id"], "prompt": "oversize_permission"},
                )
                assert failed.isError is True
                error = failed.structuredContent
                assert error is not None
                assert error["status"] == "error"
                assert error["error"]["code"] == "acp_json_line_too_large"
                assert error["error"]["max_read_bytes"] == 64 * 1024

                recovered = await client.call_tool(
                    "prompt",
                    {"session_id": payload["session_id"], "prompt": "hello"},
                )
                assert recovered.isError is False
                assert recovered.structuredContent["text"] == "echo:hello"

                closed = await client.call_tool(
                    "close_session", {"session_id": payload["session_id"]}
                )
                assert closed.isError is False
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


@pytest.mark.asyncio
async def test_stdio_client_autostarts_single_daemon_and_reaches_harness(tmp_path: Path) -> None:
    runtime = Path(tempfile.mkdtemp(prefix="harness-acp-e2e-", dir="/tmp"))
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
        "process:\n"
        "  startup_timeout_seconds: 5\n"
        "  turn_timeout_seconds: 5\n"
        "  terminate_grace_seconds: 0.1\n"
        "  remote_cleanup_timeout_seconds: 0.1\n"
        "launch:\n"
        f"  codex_command: '{shlex.join([sys.executable, str(FAKE_HARNESS)])}'\n"
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
                    },
                )
                assert created.isError is False
                payload = created.structuredContent
                assert payload is not None
                assert payload["status"] == "ready"
                assert "docker_id" not in payload
                assert "remote_host" not in payload["launch_info"]
                assert "session_record_id" not in payload
                assert payload["output_log_path"].endswith(".jsonl")
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
        terminate_daemon(lock_path, daemon_pid)
        shutil.rmtree(runtime, ignore_errors=True)


@pytest.mark.asyncio
async def test_mcp_docker_launch_info_omits_empty_lists_but_keeps_meaningful_values(
    tmp_path: Path,
) -> None:
    """The final MCP result drops empty Docker fields but keeps meaningful ones.

    This exercises the whole stack an MCP client uses (server -> DaemonClient, whose
    generic empty-field omission drops empty ``mounts``/``ports``). A newly created
    container with no bind mounts and no published ports omits those keys while keeping
    ``docker_image`` and the meaningful boolean ``host_network: false``; a reused
    container still reports the real non-empty configuration read back from
    ``docker inspect``.
    """
    runtime = Path(tempfile.mkdtemp(prefix="harness-acp-e2e-docker-", dir="/tmp"))
    config = runtime / "settings.yaml"
    lock_path = runtime / "daemon.lock"
    shim = docker_shim(runtime)
    config.write_text(
        "schema_version: 1\n"
        "ipc:\n"
        f"  socket_path: '{runtime / 'daemon.sock'}'\n"
        f"  lock_path: '{lock_path}'\n"
        "daemon:\n"
        "  idle_session_timeout_seconds: 0\n"
        "authentication:\n"
        f"  ledger_path: '{runtime / 'rate.sqlite3'}'\n"
        "  rate_limit:\n"
        "    enabled: false\n"
        "process:\n"
        "  startup_timeout_seconds: 5\n"
        "  turn_timeout_seconds: 5\n"
        "  terminate_grace_seconds: 0.1\n"
        "  remote_cleanup_timeout_seconds: 0.1\n"
        "launch:\n"
        f"  codex_command: '{shlex.join([sys.executable, str(FAKE_HARNESS)])}'\n"
        f"  docker_command: '{shim}'\n"
        "logging:\n"
        f"  path: '{runtime / 'daemon.log'}'\n",
        encoding="utf-8",
    )
    env = os.environ.copy()
    env["HARNESS_ACP_MCP_CONFIG"] = str(config)
    # A retained container with a real non-empty configuration, used only by the reuse
    # path's ``docker inspect``: a bind mount and a published port on the default network.
    env["FAKE_DOCKER_INSPECT_JSON"] = json.dumps({
        "image": "example/reused:1",
        "network_mode": "bridge",
        "port_bindings": {"8080/tcp": [{"HostIp": "127.0.0.1", "HostPort": "18080"}]},
        "mounts": [
            {"Type": "bind", "Source": "/host/data", "Destination": "/data", "RW": False},
        ],
    })
    parameters = StdioServerParameters(
        command=sys.executable,
        args=["-m", "harness_acp_mcp.server"],
        env=env,
        cwd=str(tmp_path),
    )
    daemon_pid: int | None = None
    try:
        async with stdio_client(parameters) as (read_stream, write_stream):
            async with ClientSession(
                read_stream,
                write_stream,
                read_timeout_seconds=timedelta(seconds=15),
            ) as client:
                await client.initialize()
                created = await client.call_tool(
                    "create_session",
                    {
                        "harness": "codex", "cwd": "/container-only/work",
                        "model_id": "fake-model", "runtime": "docker",
                        "docker_image": "fake:latest", "container_policy": "keep",
                    },
                )
                assert created.isError is False
                payload = created.structuredContent
                assert payload is not None
                launched = payload["launch_info"]
                assert launched["reused_container"] is False
                assert launched["docker_image"] == "fake:latest"
                # Empty mounts/ports follow the generic empty-field omission.
                assert "mounts" not in launched
                assert "ports" not in launched
                # A meaningful ``false`` boolean is kept.
                assert launched["host_network"] is False
                docker_id = payload["docker_id"]
                daemon_pid = int(lock_path.read_text(encoding="utf-8"))
                await client.call_tool("close_session", {"session_id": payload["session_id"]})

                reused = await client.call_tool(
                    "create_session",
                    {
                        "harness": "codex", "cwd": "/container-only/work",
                        "model_id": "fake-model", "runtime": "docker",
                        "docker_id": docker_id,
                    },
                )
                assert reused.isError is False
                reused_payload = reused.structuredContent
                assert reused_payload is not None
                reused_info = reused_payload["launch_info"]
                assert reused_info["reused_container"] is True
                # The reused container's real non-empty configuration survives omission.
                assert reused_info["docker_image"] == "example/reused:1"
                assert reused_info["mounts"] == [
                    {"source": "/host/data", "target": "/data", "read_only": True}
                ]
                assert reused_info["ports"] == [
                    {"host_ip": "127.0.0.1", "host_port": 18080,
                     "container_port": 8080, "protocol": "tcp"}
                ]
                assert reused_info["host_network"] is False
                await client.call_tool(
                    "close_session", {"session_id": reused_payload["session_id"]}
                )
    finally:
        terminate_daemon(lock_path, daemon_pid)
        shutil.rmtree(runtime, ignore_errors=True)
