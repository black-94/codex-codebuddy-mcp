from __future__ import annotations

import asyncio
import os
import shutil
import signal
import sys
import tempfile
from pathlib import Path

import pytest

from harness_acp_mcp.config import (
    AuthenticationSettings,
    DaemonSettings,
    IpcSettings,
    LoggingSettings,
    PersistenceSettings,
    ProcessSettings,
    RateLimitSettings,
    Settings,
    load_settings,
)
from harness_acp_mcp.daemon import HarnessDaemon, _acquire_singleton
from harness_acp_mcp.ipc import DaemonClient

FAKE_HARNESS = Path(__file__).with_name("fake_harness.py")


def isolated_settings(tmp_path: Path, *, auth_timeout: float = 2) -> Settings:
    return Settings(
        ipc=IpcSettings(
            socket_path=str(tmp_path / "daemon.sock"),
            lock_path=str(tmp_path / "daemon.lock"),
            daemon_start_timeout_seconds=5,
        ),
        daemon=DaemonSettings(idle_session_timeout_seconds=0),
        authentication=AuthenticationSettings(
            timeout_seconds=auth_timeout,
            ledger_path=str(tmp_path / "rate.sqlite3"),
            rate_limit=RateLimitSettings(enabled=False),
        ),
        persistence=PersistenceSettings(sessions_path=str(tmp_path / "sessions.sqlite3")),
        process=ProcessSettings(
            startup_timeout_seconds=5,
            turn_timeout_seconds=5,
            turn_cancel_timeout_seconds=0.2,
            terminate_grace_seconds=0.1,
            remote_cleanup_timeout_seconds=0.1,
        ),
        logging=LoggingSettings(path=str(tmp_path / "daemon.log")),
    )


def create_params(tmp_path: Path, **env: str) -> dict:
    return {
        "harness": "codex",
        "cwd": str(tmp_path),
        "model_id": "fake-model",
        "launch_mode": "local",
        "command": sys.executable,
        "args": [str(FAKE_HARNESS)],
        "env": env,
    }


@pytest.mark.asyncio
async def test_daemon_dispatches_generic_session_and_interaction(tmp_path: Path) -> None:
    daemon = HarnessDaemon(isolated_settings(tmp_path))
    created = await daemon.dispatch("create_session", create_params(tmp_path))
    session_id = created["session_id"]
    try:
        assert created["status"] == "ready"
        assert created["harness"] == "codex"
        assert created["launch_info"]["environment_names"] == []
        assert created["session_record_id"]
        record = await daemon.session_records.get(created["session_record_id"])
        assert record is not None
        assert record["harness_session_id"] == "fake-session"
        assert record["launch_info"]["cwd"] == str(tmp_path)
        assert record["launch_info"]["argument_count"] == 1
        assert "args" not in record["launch_info"]

        pending = await daemon.dispatch(
            "prompt", {"session_id": session_id, "prompt": "permission"}
        )
        assert pending["status"] == "interaction_required"
        completed = await daemon.dispatch(
            "respond_interaction",
            {
                "session_id": session_id,
                "request_id": pending["interaction"]["request_id"],
                "response": {"option_id": "allow"},
            },
        )
        assert completed["status"] == "completed"
        assert completed["text"].endswith(";answer:allow")
    finally:
        await daemon.registry.close_all()


@pytest.mark.asyncio
async def test_persisted_record_can_resume_harness_session(tmp_path: Path) -> None:
    settings = isolated_settings(tmp_path)
    first_daemon = HarnessDaemon(settings)
    first = await first_daemon.dispatch("create_session", create_params(tmp_path))
    record_id = first["session_record_id"]
    await first_daemon.registry.close_all()

    second_daemon = HarnessDaemon(settings)
    resumed = await second_daemon.dispatch(
        "create_session",
        {**create_params(tmp_path), "resume_record_id": record_id},
    )
    try:
        assert resumed["status"] == "ready"
        assert resumed["harness_session_id"] == "fake-session"
        assert resumed["session_record_id"] != record_id
    finally:
        await second_daemon.registry.close_all()


@pytest.mark.asyncio
async def test_same_target_authentication_is_serialized(tmp_path: Path) -> None:
    daemon = HarnessDaemon(isolated_settings(tmp_path))
    first = await daemon.dispatch(
        "create_session",
        create_params(tmp_path, FAKE_AUTHENTICATED="0", FAKE_AUTH_DELAY="0.3"),
    )
    second = await daemon.dispatch(
        "create_session", create_params(tmp_path, FAKE_AUTHENTICATED="0")
    )
    try:
        running = asyncio.create_task(
            daemon.dispatch(
                "authenticate", {"session_id": first["session_id"], "method_id": "browser"}
            )
        )
        await asyncio.sleep(0.05)
        blocked = await daemon.dispatch(
            "authenticate", {"session_id": second["session_id"], "method_id": "browser"}
        )
        assert blocked["status"] == "authentication_in_progress"
        assert (await running)["status"] == "ready"
    finally:
        await daemon.registry.close_all()


@pytest.mark.asyncio
async def test_authentication_timeout_removes_session(tmp_path: Path) -> None:
    daemon = HarnessDaemon(isolated_settings(tmp_path, auth_timeout=0.05))
    created = await daemon.dispatch(
        "create_session",
        create_params(tmp_path, FAKE_AUTHENTICATED="0", FAKE_AUTH_DELAY="1"),
    )
    result = await daemon.dispatch(
        "authenticate", {"session_id": created["session_id"], "method_id": "browser"}
    )

    assert result["status"] == "authentication_timed_out"
    with pytest.raises(KeyError, match="not found"):
        daemon.registry.get(created["session_id"])


@pytest.mark.asyncio
async def test_authentication_information_request_can_be_resumed(tmp_path: Path) -> None:
    daemon = HarnessDaemon(isolated_settings(tmp_path))
    created = await daemon.dispatch(
        "create_session",
        create_params(
            tmp_path,
            FAKE_AUTHENTICATED="0",
            FAKE_AUTH_INTERACTION="1",
        ),
    )
    try:
        pending = await daemon.dispatch(
            "authenticate", {"session_id": created["session_id"], "method_id": "browser"}
        )
        assert pending["status"] == "interaction_required"
        assert pending["interaction"]["kind"] == "information"
        completed = await daemon.dispatch(
            "respond_interaction",
            {
                "session_id": created["session_id"],
                "request_id": pending["interaction"]["request_id"],
                "response": {"value": "provided"},
            },
        )
        assert completed["status"] == "ready"
        assert completed["authenticated"] is True
    finally:
        await daemon.registry.close_all()


def test_singleton_lock_rejects_second_daemon(tmp_path: Path) -> None:
    settings = isolated_settings(tmp_path)
    first = _acquire_singleton(settings)
    try:
        with pytest.raises(RuntimeError, match="already running"):
            _acquire_singleton(settings)
    finally:
        first.close()


@pytest.mark.asyncio
async def test_concurrent_clients_autostart_only_one_daemon(tmp_path: Path, monkeypatch) -> None:
    runtime = Path(tempfile.mkdtemp(prefix="harness-acp-race-", dir="/private/tmp"))
    config = runtime / "settings.yaml"
    config.write_text(
        "schema_version: 1\n"
        "ipc:\n"
        f"  socket_path: '{runtime / 'daemon.sock'}'\n"
        f"  lock_path: '{runtime / 'daemon.lock'}'\n"
        "daemon:\n"
        "  idle_session_timeout_seconds: 0\n"
        "authentication:\n"
        f"  ledger_path: '{runtime / 'rate.sqlite3'}'\n"
        "persistence:\n"
        f"  sessions_path: '{runtime / 'sessions.sqlite3'}'\n"
        "logging:\n"
        f"  path: '{runtime / 'daemon.log'}'\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HARNESS_ACP_MCP_CONFIG", str(config))
    settings = load_settings(str(config))
    first = DaemonClient(settings)
    second = DaemonClient(settings)
    daemon_pid: int | None = None
    try:
        first_result, second_result = await asyncio.gather(
            first.ensure_daemon(), second.ensure_daemon()
        )
        assert first_result["pid"] == second_result["pid"]
        daemon_pid = int(first_result["pid"])
        assert int((runtime / "daemon.lock").read_text(encoding="utf-8")) == daemon_pid
    finally:
        if daemon_pid is not None:
            try:
                os.kill(daemon_pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            for _ in range(100):
                if not _pid_exists(daemon_pid):
                    break
                await asyncio.sleep(0.02)
        shutil.rmtree(runtime, ignore_errors=True)


@pytest.mark.asyncio
async def test_daemon_sigkill_closes_harness_and_background_child(tmp_path: Path) -> None:
    runtime = Path(tempfile.mkdtemp(prefix="harness-acp-test-", dir="/private/tmp"))
    config = runtime / "settings.yaml"
    config.write_text(
        "schema_version: 1\n"
        "ipc:\n"
        f"  socket_path: '{runtime / 'daemon.sock'}'\n"
        f"  lock_path: '{runtime / 'daemon.lock'}'\n"
        "daemon:\n"
        "  idle_session_timeout_seconds: 0\n"
        "authentication:\n"
        f"  ledger_path: '{runtime / 'rate.sqlite3'}'\n"
        "  rate_limit:\n"
        "    enabled: false\n"
        "process:\n"
        "  startup_timeout_seconds: 5\n"
        "  terminate_grace_seconds: 0.1\n"
        "  remote_cleanup_timeout_seconds: 0.1\n"
        "logging:\n"
        f"  path: '{runtime / 'daemon.log'}'\n",
        encoding="utf-8",
    )
    env = os.environ.copy()
    env["HARNESS_ACP_MCP_CONFIG"] = str(config)
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "harness_acp_mcp.daemon",
        env=env,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
        start_new_session=True,
    )
    settings = isolated_settings(runtime)
    client = DaemonClient(settings)
    pid_file = tmp_path / "pids.txt"
    try:
        for _ in range(100):
            try:
                await client.call("ping", {}, ensure=False, request_timeout=0.2)
                break
            except (OSError, TimeoutError, ConnectionError):
                await asyncio.sleep(0.02)
        await client.call(
            "create_session",
            create_params(tmp_path, FAKE_CHILD_PID_FILE=str(pid_file)),
            ensure=False,
            request_timeout=5,
        )
        for _ in range(100):
            if pid_file.exists():
                break
            await asyncio.sleep(0.01)
        harness_pid, child_pid = map(int, pid_file.read_text(encoding="utf-8").split())

        os.kill(process.pid, signal.SIGKILL)
        await asyncio.wait_for(process.wait(), 5)
        for _ in range(150):
            if not _pid_exists(harness_pid) and not _pid_exists(child_pid):
                break
            await asyncio.sleep(0.02)
        assert not _pid_exists(harness_pid)
        assert not _pid_exists(child_pid)
    finally:
        if process.returncode is None:
            process.kill()
            await asyncio.wait_for(process.wait(), 5)
        shutil.rmtree(runtime, ignore_errors=True)


def _pid_exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True
