from __future__ import annotations

import asyncio
import os
import shlex
import shutil
import signal
import sys
import tempfile
import time
from contextlib import suppress
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from harness_acp_mcp.acp import AcpError
from harness_acp_mcp.config import (
    AuthenticationSettings,
    BufferSettings,
    DaemonSettings,
    IpcSettings,
    LaunchSettings,
    LoggingSettings,
    ProcessSettings,
    RateLimitSettings,
    Settings,
    load_settings,
)
from harness_acp_mcp.daemon import HarnessDaemon, _acquire_singleton
from harness_acp_mcp.ipc import DaemonClient
from harness_acp_mcp.models import ContainerInspection, DockerMount, DockerPort
from harness_acp_mcp.output_log import HarnessOutputLog, daemon_directory

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
        process=ProcessSettings(
            startup_timeout_seconds=5,
            turn_timeout_seconds=5,
            turn_cancel_timeout_seconds=0.2,
            terminate_grace_seconds=0.1,
            remote_cleanup_timeout_seconds=0.1,
        ),
        logging=LoggingSettings(path=str(tmp_path / "daemon.log")),
        launch=LaunchSettings(codex_command=shlex.join([sys.executable, str(FAKE_HARNESS)])),
    )


@pytest.fixture(autouse=True)
def restore_fake_environment():
    original = {name: value for name, value in os.environ.items() if name.startswith("FAKE_")}
    yield
    for name in list(os.environ):
        if name.startswith("FAKE_"):
            del os.environ[name]
    os.environ.update(original)


def create_params(tmp_path: Path, **env: str) -> dict:
    os.environ.update(env)
    return {
        "harness": "codex",
        "cwd": str(tmp_path),
        "model_id": "fake-model",
        "target": "local",
    }


@pytest.mark.asyncio
async def test_daemon_dispatches_generic_session_and_interaction(tmp_path: Path) -> None:
    daemon = HarnessDaemon(isolated_settings(tmp_path))
    created = await daemon.dispatch("create_session", create_params(tmp_path))
    session_id = created["session_id"]
    try:
        assert created["status"] == "ready"
        assert created["harness"] == "codex"
        assert created["launch_info"]["runtime"] == "direct"
        assert "session_record_id" not in created
        assert created["harness_session_id"] == "fake-session"
        assert created["launch_info"]["cwd"] == str(tmp_path)
        assert created["launch_info"]["permission_mode"] == "auto"
        assert "args" not in created["launch_info"]

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
async def test_native_session_id_can_resume_harness_session(tmp_path: Path) -> None:
    settings = isolated_settings(tmp_path)
    first_daemon = HarnessDaemon(settings)
    first = await first_daemon.dispatch("create_session", create_params(tmp_path))
    await first_daemon.registry.close_all()

    second_daemon = HarnessDaemon(settings)
    resumed = await second_daemon.dispatch(
        "create_session",
        {**create_params(tmp_path), "resume_session_id": first["harness_session_id"]},
    )
    try:
        assert resumed["status"] == "ready"
        assert resumed["harness_session_id"] == "fake-session"
        assert "session_record_id" not in resumed
    finally:
        await second_daemon.registry.close_all()


@pytest.mark.asyncio
async def test_oversized_permission_line_returns_json_error_and_cancels_turn(
    tmp_path: Path,
) -> None:
    settings = replace(isolated_settings(tmp_path), buffers=BufferSettings(max_read_bytes=1024))
    daemon = HarnessDaemon(settings)
    cancel_log = tmp_path / "cancels.txt"
    created = await daemon.dispatch(
        "create_session", create_params(tmp_path, FAKE_CANCEL_LOG=str(cancel_log))
    )
    try:
        failed = await daemon.dispatch("prompt", {
            "session_id": created["session_id"], "prompt": "oversize_permission",
        })
        assert failed["status"] == "error"
        error = failed["error"]
        assert error["code"] == "acp_json_line_too_large"
        assert error["max_read_bytes"] == 1024
        assert error["discarded_line_only"] is True
        assert error["turn_cancelled"] == "best_effort"
        assert "only that line was discarded" in error["message"].lower()
        assert "best effort" in error["warning"]
        assert "request ID is unavailable" in error["warning"]
        cancels = ""
        for _ in range(100):
            cancels = cancel_log.read_text(encoding="utf-8") if cancel_log.exists() else ""
            if cancels:
                break
            await asyncio.sleep(0.02)
        assert cancels.splitlines() and set(cancels.splitlines()) == {"cancel"}

        completed = await daemon.dispatch("prompt", {
            "session_id": created["session_id"], "prompt": "hello",
        })
        assert completed["status"] == "completed"
        log = Path(created["output_log_path"])
        contents = await asyncio.to_thread(log.read_text)
        assert "acp_json_line_too_large" not in contents
        assert "exceeded max_read_bytes and was discarded" in contents
        assert "echo:hello" in contents
    finally:
        await daemon.registry.close_all()


@pytest.mark.asyncio
async def test_tracking_log_is_separate_from_externalized_mcp_result(tmp_path: Path) -> None:
    settings = replace(
        isolated_settings(tmp_path), buffers=BufferSettings(max_output_bytes=10)
    )
    daemon = HarnessDaemon(settings)
    created = await daemon.dispatch("create_session", create_params(tmp_path))
    result = await daemon.dispatch("prompt", {
        "session_id": created["session_id"], "prompt": "hello",
    })
    result_path = Path(result["output_path"])
    log_path = Path(created["output_log_path"])
    assert result_path != log_path
    assert result_path.suffix == ".json"
    assert log_path.suffix == ".jsonl"
    assert await asyncio.to_thread(result_path.exists)
    assert await asyncio.to_thread(log_path.exists)
    await daemon.registry.close_all()
    assert not await asyncio.to_thread(result_path.exists)
    assert await asyncio.to_thread(log_path.exists)


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


@pytest.mark.asyncio
async def test_respond_interaction_timeout_releases_turn_slot(tmp_path: Path) -> None:
    daemon = HarnessDaemon(isolated_settings(tmp_path))
    created = await daemon.dispatch(
        "create_session", create_params(tmp_path, FAKE_INTERACTION_HANG="1")
    )
    session = daemon.registry.get(created["session_id"])
    try:
        pending = await daemon.dispatch(
            "prompt", {"session_id": created["session_id"], "prompt": "permission"}
        )
        assert pending["status"] == "interaction_required"
        assert session.turn_slot_held is True

        with pytest.raises(TimeoutError):
            await daemon.dispatch(
                "respond_interaction",
                {
                    "session_id": created["session_id"],
                    "request_id": pending["interaction"]["request_id"],
                    "response": {"option_id": "allow"},
                    "timeout_seconds": 0.1,
                },
            )

        assert session.turn_slot_held is False
        assert session.lock.locked() is False
        recovered = await daemon.dispatch(
            "prompt", {"session_id": created["session_id"], "prompt": "hello"}
        )
        assert recovered["status"] == "completed"
    finally:
        await daemon.registry.close_all()


@pytest.mark.asyncio
async def test_respond_interaction_harness_exit_releases_turn_slot(tmp_path: Path) -> None:
    pid_file = tmp_path / "pids.txt"
    daemon = HarnessDaemon(isolated_settings(tmp_path))
    created = await daemon.dispatch(
        "create_session",
        create_params(
            tmp_path, FAKE_INTERACTION_HANG="1", FAKE_CHILD_PID_FILE=str(pid_file)
        ),
    )
    session = daemon.registry.get(created["session_id"])
    try:
        pending = await daemon.dispatch(
            "prompt", {"session_id": created["session_id"], "prompt": "permission"}
        )
        assert pending["status"] == "interaction_required"
        for _ in range(100):
            if pid_file.exists():
                break
            await asyncio.sleep(0.01)
        harness_pid = int(pid_file.read_text(encoding="utf-8").split()[0])

        responder = asyncio.create_task(
            daemon.dispatch(
                "respond_interaction",
                {
                    "session_id": created["session_id"],
                    "request_id": pending["interaction"]["request_id"],
                    "response": {"option_id": "allow"},
                    "timeout_seconds": 5,
                },
            )
        )
        await asyncio.sleep(0.2)
        os.kill(harness_pid, signal.SIGKILL)
        with pytest.raises(AcpError):
            await responder

        assert session.turn_slot_held is False
        assert session.lock.locked() is False
    finally:
        await daemon.registry.close_all()


@pytest.mark.asyncio
async def test_create_session_budget_closes_session_before_caller_timeout(
    tmp_path: Path, monkeypatch
) -> None:
    import harness_acp_mcp.daemon as daemon_module

    monkeypatch.setattr(daemon_module, "create_session_timeout_seconds", lambda _: 0.1)
    daemon = HarnessDaemon(isolated_settings(tmp_path))
    with pytest.raises(AcpError, match="budget"):
        await daemon.dispatch(
            "create_session", create_params(tmp_path, FAKE_INIT_DELAY="2")
        )
    assert daemon.registry._sessions == {}
    await daemon.registry.close_all()


@pytest.mark.asyncio
async def test_create_session_step_timeout_is_not_misreported_as_budget(tmp_path: Path) -> None:
    settings = replace(
        isolated_settings(tmp_path),
        process=ProcessSettings(
            startup_timeout_seconds=0.2,
            turn_timeout_seconds=5,
            turn_cancel_timeout_seconds=0.2,
            terminate_grace_seconds=0.1,
            remote_cleanup_timeout_seconds=0.1,
        ),
    )
    daemon = HarnessDaemon(settings)
    with pytest.raises(TimeoutError) as failure:
        await daemon.dispatch(
            "create_session", create_params(tmp_path, FAKE_INIT_DELAY="2")
        )
    assert "budget" not in str(failure.value)
    assert daemon.registry._sessions == {}
    await daemon.registry.close_all()


@pytest.mark.parametrize("value", ["", "   ", 123, {"id": "x"}])
async def test_resume_session_id_must_be_a_non_empty_string(
    tmp_path: Path, value: object
) -> None:
    daemon = HarnessDaemon(isolated_settings(tmp_path))
    with pytest.raises(ValueError, match="resume_session_id"):
        await daemon.dispatch(
            "create_session", {**create_params(tmp_path), "resume_session_id": value}
        )


@pytest.mark.asyncio
async def test_prompt_and_respond_reject_per_call_output_limits(tmp_path: Path) -> None:
    daemon = HarnessDaemon(isolated_settings(tmp_path))
    created = await daemon.dispatch("create_session", create_params(tmp_path))
    try:
        with pytest.raises(ValueError, match="max_output_bytes"):
            await daemon.dispatch(
                "prompt",
                {
                    "session_id": created["session_id"],
                    "prompt": "hello",
                    "max_output_bytes": 1024,
                },
            )
        with pytest.raises(ValueError, match="max_output_bytes"):
            await daemon.dispatch(
                "respond_interaction",
                {
                    "session_id": created["session_id"],
                    "request_id": "request",
                    "response": {},
                    "max_output_bytes": 1024,
                },
            )
    finally:
        await daemon.registry.close_all()


@pytest.mark.asyncio
async def test_output_logs_are_swept_only_after_retention(
    tmp_path: Path, monkeypatch
) -> None:
    def use_tmp_directory(_identity: str) -> Path:
        return tmp_path

    monkeypatch.setattr("harness_acp_mcp.daemon.daemon_directory", use_tmp_directory)
    monkeypatch.setattr("harness_acp_mcp.bridge.daemon_directory", use_tmp_directory)
    settings = replace(
        isolated_settings(tmp_path),
        daemon=DaemonSettings(idle_session_timeout_seconds=0, output_log_retention_seconds=100),
    )
    daemon = HarnessDaemon(settings)
    created = await daemon.dispatch("create_session", create_params(tmp_path))
    active = Path(created["output_log_path"])
    assert active.parent == tmp_path

    now = time.time()
    expired = tmp_path / f"{HarnessOutputLog.FILE_PREFIX}expired{HarnessOutputLog.FILE_SUFFIX}"
    expired.write_text("{}\n", encoding="utf-8")
    os.utime(expired, (now - 1000, now - 1000))
    recent = tmp_path / f"{HarnessOutputLog.FILE_PREFIX}recent{HarnessOutputLog.FILE_SUFFIX}"
    recent.write_text("{}\n", encoding="utf-8")
    # Even an aged log of a live session must survive sweeping.
    os.utime(active, (now - 10000, now - 10000))

    assert await daemon.registry.sweep_output_logs() == 1
    assert not await asyncio.to_thread(expired.exists)
    assert await asyncio.to_thread(recent.exists)
    assert await asyncio.to_thread(active.exists)

    await daemon.registry.close_all()
    # Preserved logs stay available for manual follow-up right after close.
    assert await asyncio.to_thread(active.exists)


@pytest.mark.asyncio
async def test_one_daemon_sweep_leaves_another_daemons_logs_alone(
    tmp_path: Path, monkeypatch
) -> None:
    base = tmp_path / "shared-temp"
    base.mkdir()
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(base))
    retention = DaemonSettings(
        idle_session_timeout_seconds=0, output_log_retention_seconds=100
    )
    first = HarnessDaemon(
        replace(
            isolated_settings(tmp_path),
            ipc=IpcSettings(
                socket_path=str(tmp_path / "first.sock"), lock_path=str(tmp_path / "first.lock")
            ),
            daemon=retention,
        )
    )
    second = HarnessDaemon(
        replace(
            isolated_settings(tmp_path),
            ipc=IpcSettings(
                socket_path=str(tmp_path / "second.sock"),
                lock_path=str(tmp_path / "second.lock"),
            ),
            daemon=retention,
        )
    )
    first_directory = daemon_directory(first.settings.ipc.lock_path)
    second_directory = daemon_directory(second.settings.ipc.lock_path)
    assert first_directory != second_directory

    now = time.time()
    stale: list[Path] = []
    for directory in (first_directory, second_directory):
        log = directory / f"{HarnessOutputLog.FILE_PREFIX}stale{HarnessOutputLog.FILE_SUFFIX}"
        log.write_text("{}\n", encoding="utf-8")
        os.utime(log, (now - 10000, now - 10000))
        stale.append(log)

    assert await first.registry.sweep_output_logs() == 1
    assert not await asyncio.to_thread(stale[0].exists)
    # The other daemon's aged log is outside the first daemon's namespace.
    assert await asyncio.to_thread(stale[1].exists)


@pytest.mark.asyncio
async def test_reaper_continues_after_a_failed_step(tmp_path: Path, monkeypatch) -> None:
    settings = replace(
        isolated_settings(tmp_path),
        daemon=DaemonSettings(idle_session_timeout_seconds=0, reap_interval_seconds=0.02),
    )
    daemon = HarnessDaemon(settings)
    reap_calls = 0
    sweep_calls = 0

    async def reap_idle() -> int:
        nonlocal reap_calls
        reap_calls += 1
        if reap_calls == 1:
            raise RuntimeError("boom")
        return 0

    async def sweep_output_logs() -> int:
        nonlocal sweep_calls
        sweep_calls += 1
        return 0

    monkeypatch.setattr(daemon.registry, "reap_idle", reap_idle)
    monkeypatch.setattr(daemon.registry, "sweep_output_logs", sweep_output_logs)
    task = asyncio.create_task(daemon._reap_loop())
    try:
        for _ in range(200):
            if reap_calls >= 3 and sweep_calls >= 2:
                break
            await asyncio.sleep(0.02)
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
    # A failed reap is logged and the loop keeps reaping and sweeping.
    assert reap_calls >= 3
    assert sweep_calls >= 2


def test_kept_docker_session_requires_container_id(tmp_path: Path) -> None:
    daemon = HarnessDaemon(isolated_settings(tmp_path))
    session = SimpleNamespace(
        session_id="session",
        config=SimpleNamespace(
            harness="codex",
            launch_mode="local",
            runtime="docker",
            container_policy="keep",
            permission_mode="auto",
            cwd=str(tmp_path),
            ssh_host=None,
            docker_image="fake:latest",
            docker_mounts=(),
            docker_ports=(),
            docker_host_network=False,
            reuse_container=False,
        ),
        client=SimpleNamespace(
            process_info={},
            session_id="harness",
            model_id="model",
            model_name="Model",
            output_log=SimpleNamespace(path=tmp_path / "log.jsonl"),
        ),
    )
    with pytest.raises(AcpError, match="container ID is unavailable"):
        daemon._created_result(session, "ready")

    session.client.process_info = {"docker_id": "a" * 64}
    created = daemon._created_result(session, "ready")
    assert created["docker_id"] == "a" * 64
    # A newly created container reports the creation-time configuration it applied.
    assert created["launch_info"]["reused_container"] is False
    assert created["launch_info"]["docker_image"] == "fake:latest"
    assert created["launch_info"]["mounts"] == []
    assert created["launch_info"]["ports"] == []
    assert created["launch_info"]["host_network"] is False


def test_reused_docker_session_reports_inspected_config(tmp_path: Path) -> None:
    daemon = HarnessDaemon(isolated_settings(tmp_path))
    inspection = ContainerInspection(
        image="example/image:1",
        mounts=(DockerMount(source="/host/data", target="/data", read_only=True),),
        ports=(DockerPort(host_port=18080, container_port=8080, protocol="tcp",
                          host_ip="127.0.0.1"),),
        host_network=True,
    )
    session = SimpleNamespace(
        session_id="session",
        config=SimpleNamespace(
            harness="codex",
            launch_mode="local",
            runtime="docker",
            container_policy="keep",
            permission_mode="auto",
            cwd="/container-only/work",
            ssh_host=None,
            docker_image=None,
            docker_mounts=(),
            docker_ports=(),
            docker_host_network=False,
            reuse_container=True,
            container_inspection=inspection,
        ),
        client=SimpleNamespace(
            process_info={"docker_id": "b" * 64},
            session_id="harness",
            model_id="model",
            model_name="Model",
            output_log=SimpleNamespace(path=tmp_path / "log.jsonl"),
        ),
    )
    created = daemon._created_result(session, "ready")
    assert created["docker_id"] == "b" * 64
    launch_info = created["launch_info"]
    # A reused container reports the configuration read back from ``docker inspect``,
    # never the absent request defaults such as host_network=False.
    assert launch_info["reused_container"] is True
    assert launch_info["docker_image"] == "example/image:1"
    assert launch_info["mounts"] == [
        {"source": "/host/data", "target": "/data", "read_only": True}
    ]
    assert launch_info["ports"] == [
        {"host_ip": "127.0.0.1", "host_port": 18080,
         "container_port": 8080, "protocol": "tcp"}
    ]
    assert launch_info["host_network"] is True


def test_reused_docker_session_requires_inspection_data(tmp_path: Path) -> None:
    daemon = HarnessDaemon(isolated_settings(tmp_path))
    session = SimpleNamespace(
        session_id="session",
        config=SimpleNamespace(
            harness="codex",
            launch_mode="local",
            runtime="docker",
            container_policy="keep",
            permission_mode="auto",
            cwd="/container-only/work",
            ssh_host=None,
            docker_image=None,
            docker_mounts=(),
            docker_ports=(),
            docker_host_network=False,
            reuse_container=True,
            container_inspection=None,
        ),
        client=SimpleNamespace(
            process_info={"docker_id": "b" * 64},
            session_id="harness",
            model_id="model",
            model_name="Model",
            output_log=SimpleNamespace(path=tmp_path / "log.jsonl"),
        ),
    )
    # Never fall back to defaults: a rejected/absent inspection is an error.
    with pytest.raises(AcpError, match="configuration is unavailable"):
        daemon._created_result(session, "ready")


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
    runtime = Path(tempfile.mkdtemp(prefix="harness-acp-race-", dir="/tmp"))
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
    runtime = Path(tempfile.mkdtemp(prefix="harness-acp-test-", dir="/tmp"))
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
        "launch:\n"
        f"  codex_command: '{shlex.join([sys.executable, str(FAKE_HARNESS)])}'\n"
        "logging:\n"
        f"  path: '{runtime / 'daemon.log'}'\n",
        encoding="utf-8",
    )
    env = os.environ.copy()
    env["HARNESS_ACP_MCP_CONFIG"] = str(config)
    pid_file = tmp_path / "pids.txt"
    env["FAKE_CHILD_PID_FILE"] = str(pid_file)
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
    try:
        for _ in range(100):
            try:
                await client.call("ping", {}, ensure=False, request_timeout=0.2)
                break
            except (OSError, TimeoutError, ConnectionError):
                await asyncio.sleep(0.02)
        await client.call(
            "create_session",
            create_params(tmp_path),
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
