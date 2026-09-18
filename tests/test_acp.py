from __future__ import annotations

import asyncio
import os
import shutil
import sys
from pathlib import Path

import pytest

from codex_codebuddy_mcp.acp import (
    AcpClient,
    AcpError,
    _readline_discarding_overflow,
    build_launch_argv,
    validate_config,
)
from codex_codebuddy_mcp.models import PermissionRequest, SessionConfig

FAKE_CODEBUDDY = Path(__file__).with_name("fake_codebuddy.py")


def fake_config(cwd: Path) -> SessionConfig:
    return SessionConfig(
        launch_mode="local",
        cwd=str(cwd),
        codebuddy_command=sys.executable,
        codebuddy_args=[str(FAKE_CODEBUDDY)],
        startup_timeout_seconds=5,
    )


def test_rejects_arguments_that_break_acp_stdio(tmp_path: Path) -> None:
    config = fake_config(tmp_path)
    config.codebuddy_args.append("--serve")
    with pytest.raises(ValueError, match="incompatible"):
        validate_config(config)


def test_rejects_whitespace_only_working_directory(tmp_path: Path) -> None:
    config = fake_config(tmp_path)
    config.cwd = "   "

    with pytest.raises(ValueError, match="cwd must not be empty"):
        validate_config(config)


def test_uses_auto_permission_mode_by_default(tmp_path: Path) -> None:
    argv, _, _ = build_launch_argv(fake_config(tmp_path))
    mode_index = argv.index("--permission-mode")
    assert argv[mode_index + 1] == "auto"


def test_rejects_overriding_managed_permission_mode(tmp_path: Path) -> None:
    config = fake_config(tmp_path)
    config.codebuddy_args.extend(["--permission-mode", "default"])
    with pytest.raises(ValueError, match="incompatible"):
        validate_config(config)


def test_rejects_nonpositive_max_read(tmp_path: Path) -> None:
    config = fake_config(tmp_path)
    config.max_read = 0
    with pytest.raises(ValueError, match="max_read must be positive"):
        validate_config(config)


def test_builds_safely_quoted_ssh_command() -> None:
    config = SessionConfig(
        launch_mode="ssh",
        cwd="/tmp/project with spaces",
        codebuddy_command="/opt/code buddy/bin/codebuddy",
        codebuddy_args=["--model", "fast-model"],
        env={"SAFE_VALUE": "value with spaces"},
        ssh_host="dev-box",
        ssh_args=["-T"],
        remote_pid_file="codex-codebuddy-test.pid",
    )
    argv, cwd, _ = build_launch_argv(config)
    assert argv[:4] == ["ssh", "-T", "--", "dev-box"]
    assert "cd '/tmp/project with spaces'" in argv[4]
    assert "'SAFE_VALUE=value with spaces'" in argv[4]
    assert "'/opt/code buddy/bin/codebuddy'" in argv[4]
    assert "--permission-mode auto" in argv[4]
    assert "${TMPDIR:-/tmp}" in argv[4]
    assert "set -m" in argv[4]
    assert "codebuddy_pid=$!" in argv[4]
    assert 'wait "$codebuddy_pid"' in argv[4]
    assert "setsid" not in argv[4]
    assert "codex-codebuddy-test.pid" in argv[4]
    assert "--acp --acp-transport stdio" in argv[4]
    assert cwd is None


@pytest.mark.skipif(os.name == "nt", reason="remote launch requires a POSIX shell")
@pytest.mark.asyncio
async def test_remote_monitor_shell_keeps_stdio_open_until_child_exits(tmp_path: Path) -> None:
    shell = shutil.which("sh")
    if shell is None:
        pytest.skip("sh is not installed")

    config = SessionConfig(
        launch_mode="ssh",
        cwd=str(tmp_path),
        codebuddy_command=sys.executable,
        codebuddy_args=[
            "-c",
            "import sys; line = sys.stdin.readline(); print('reply:' + line, end='', flush=True)",
        ],
        ssh_host="unused",
        remote_pid_file="set-m-test.pid",
    )
    argv, _, _ = build_launch_argv(config)
    env = os.environ.copy()
    env["TMPDIR"] = str(tmp_path)
    process = await asyncio.create_subprocess_exec(
        shell,
        "-c",
        argv[-1],
        env=env,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    stdout, _ = await asyncio.wait_for(process.communicate(b"ping\n"), timeout=5)

    assert process.returncode == 0
    assert stdout == b"reply:ping\n"
    assert (tmp_path / "set-m-test.pid").read_text(encoding="utf-8").strip().isdigit()


@pytest.mark.asyncio
async def test_overflow_reader_drains_through_newline() -> None:
    reader = asyncio.StreamReader(limit=8)
    reader.feed_data(b"x" * 20 + b"\nnext\n")
    reader.feed_eof()

    discarded, overflowed = await _readline_discarding_overflow(reader)
    next_line, next_overflowed = await _readline_discarding_overflow(reader)

    assert discarded == b""
    assert overflowed is True
    assert next_line == b"next\n"
    assert next_overflowed is False


@pytest.mark.asyncio
async def test_start_and_prompt(tmp_path: Path) -> None:
    client = AcpClient(fake_config(tmp_path))
    assert client.process is None

    await client.start()
    try:
        assert client.running
        assert client.session_id and client.session_id.startswith("fake-session-")
        assert client.model_id == "fake-model-id"
        assert client.model_name == "Fake Model"
        await client.set_model("fake-fast-id")
        assert client.model_id == "fake-fast-id"
        assert client.model_name == "Fake Fast Model"
        with pytest.raises(AcpError, match="Unknown model"):
            await client.set_model("unknown-model")
        assert client.model_id == "fake-fast-id"
        await client.begin_prompt("hello")
        event = await client.wait_for_turn_event(5)
        assert event["kind"] == "complete"
        assert event["result"]["text"] == "echo:hello"
        assert event["result"]["stop_reason"] == "end_turn"
    finally:
        await client.close()
    assert not client.running


@pytest.mark.asyncio
async def test_resume_uses_requested_id_when_load_omits_it(tmp_path: Path) -> None:
    first = AcpClient(fake_config(tmp_path))
    await first.start()
    session_id = first.session_id
    await first.close()

    resumed_config = fake_config(tmp_path)
    resumed_config.resume_session_id = session_id
    resumed_config.env["FAKE_LOAD_OMIT_SESSION_ID"] = "1"
    resumed = AcpClient(resumed_config)
    await resumed.start()
    try:
        assert resumed.session_id == session_id
    finally:
        await resumed.close()


@pytest.mark.asyncio
async def test_authenticates_when_agent_advertises_methods(tmp_path: Path) -> None:
    client = AcpClient(fake_config(tmp_path))
    client.config.auth_method_id = "external"
    client.config.env["FAKE_AUTHENTICATED"] = "0"
    await client.start()
    try:
        assert client.session_id
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_authentication_tolerates_missing_private_user_info_method(tmp_path: Path) -> None:
    client = AcpClient(fake_config(tmp_path))
    client.config.auth_method_id = "external"
    client.config.env["FAKE_USER_INFO_UNSUPPORTED"] = "1"
    await client.start()
    try:
        assert client.session_id
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_permission_round_trip(tmp_path: Path) -> None:
    client = AcpClient(fake_config(tmp_path))
    await client.start()
    try:
        await client.begin_prompt("needs permission")
        event = await client.wait_for_turn_event(5)
        assert event["kind"] == "permission"
        permission = event["permission"]
        assert permission.request_id == "permission-1"
        assert permission.tool_name == "Bash"
        assert permission.raw_input == {"command": "touch sample.txt"}

        with pytest.raises(AcpError, match="turn is active"):
            await client.set_model("fake-fast-id")

        with pytest.raises(AcpError, match="unknown permission"):
            await client.resolve_permission("permission-1", "not-an-option")

        await client.resolve_permission("permission-1", "allow")
        completed = await client.wait_for_turn_event(5)
        assert completed["kind"] == "complete"
        assert completed["result"]["text"].endswith(";permission:allow")
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_cancel_clears_permission_without_reject_option(tmp_path: Path) -> None:
    client = AcpClient(fake_config(tmp_path))
    client.pending_permission = PermissionRequest(
        rpc_id=1,
        request_id="permission-1",
        session_id="session-1",
        tool_name="Bash",
        raw_input={},
        options=[{"kind": "allow", "optionId": "allow"}],
        meta={},
    )

    await client.cancel_turn()

    assert client.pending_permission is None


@pytest.mark.asyncio
async def test_waiter_returns_cancelled_during_concurrent_cancel(tmp_path: Path) -> None:
    client = AcpClient(fake_config(tmp_path))
    await client.start()
    try:
        await client.begin_prompt("needs permission")
        permission = await client.wait_for_turn_event(5)
        assert permission["kind"] == "permission"

        waiter = asyncio.create_task(client.wait_for_turn_event(5))
        await asyncio.sleep(0)
        await client.cancel_turn()
        result = await waiter

        assert result["kind"] == "complete"
        assert result["result"]["status"] == "cancelled"
        assert client.pending_permission is None
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_first_oversized_stdout_allows_retry_and_second_terminates(tmp_path: Path) -> None:
    config = fake_config(tmp_path)
    config.max_read = 1024
    client = AcpClient(config)
    await client.start()
    try:
        await client.begin_prompt("large-output")
        with pytest.raises(AcpError, match="response was discarded") as first_error:
            await client.wait_for_turn_event(5)
        assert "larger max_read" in str(first_error.value)
        assert "compress the answer" in str(first_error.value)
        await client.cancel_turn()
        assert client.running

        await client.begin_prompt("short answer")
        recovered = await client.wait_for_turn_event(5)
        assert recovered["result"]["text"] == "echo:short answer"

        await client.begin_prompt("large-output")
        with pytest.raises(AcpError, match="response was discarded"):
            await client.wait_for_turn_event(5)
        await client.cancel_turn()
        assert client.running

        await client.begin_prompt("large-output")
        with pytest.raises(AcpError, match="repeatedly exceeded"):
            await client.wait_for_turn_event(5)
        for _ in range(50):
            if not client.running:
                break
            await asyncio.sleep(0.01)
        assert not client.running
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_oversized_stderr_line_is_discarded_without_stopping_session(tmp_path: Path) -> None:
    config = fake_config(tmp_path)
    config.max_read = 1024
    client = AcpClient(config)
    await client.start()
    try:
        await client.begin_prompt("large-stderr")
        completed = await client.wait_for_turn_event(5)
        assert completed["result"]["text"] == "echo:large-stderr"
        for _ in range(50):
            if "discarded stderr line" in client.stderr_tail:
                break
            await asyncio.sleep(0.01)
        assert "discarded stderr line" in client.stderr_tail
        assert client.running
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_remote_cleanup_validates_pid_before_kill(tmp_path: Path, monkeypatch) -> None:
    config = SessionConfig(launch_mode="ssh", cwd=str(tmp_path), ssh_host="dev-box")
    client = AcpClient(config)
    captured: list[str] = []

    class CleanupProcess:
        async def wait(self) -> int:
            return 0

        def kill(self) -> None:
            pass

    async def fake_exec(*argv, **kwargs):
        captured.extend(argv)
        return CleanupProcess()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    await client._cleanup_remote_process()

    cleanup_command = captured[-1]
    assert 'case "$pid" in ""|*[!0-9]*' in cleanup_command
    assert "${TMPDIR:-/tmp}" in cleanup_command


@pytest.mark.asyncio
async def test_two_real_processes_are_isolated(tmp_path: Path) -> None:
    first = AcpClient(fake_config(tmp_path))
    second = AcpClient(fake_config(tmp_path))
    await first.start()
    await second.start()
    try:
        assert first.process is not None and second.process is not None
        assert first.process.pid != second.process.pid
        assert first.session_id != second.session_id

        await first.begin_prompt("first")
        await second.begin_prompt("second")
        first_result = await first.wait_for_turn_event(5)
        second_result = await second.wait_for_turn_event(5)
        assert first_result["result"]["text"] == "echo:first"
        assert second_result["result"]["text"] == "echo:second"
    finally:
        await first.close()
        await second.close()


@pytest.mark.real_codebuddy
@pytest.mark.asyncio
async def test_installed_codebuddy_acp_handshake(tmp_path: Path) -> None:
    """Start actual CodeBuddy and verify ACP initialization/auth state handling."""
    executable = shutil.which("codebuddy")
    if executable is None:
        pytest.skip("codebuddy is not installed")

    client = AcpClient(
        SessionConfig(
            launch_mode="local",
            cwd=str(tmp_path),
            codebuddy_command=executable,
            startup_timeout_seconds=120,
        )
    )
    try:
        await client.start()
    except AcpError as exc:
        assert "authentication required" in str(exc).lower()
        assert client.process is not None
    else:
        assert client.running
        assert client.session_id
    finally:
        await client.close()


@pytest.mark.real_codebuddy
@pytest.mark.model
@pytest.mark.asyncio
async def test_installed_codebuddy_model_prompt(tmp_path: Path) -> None:
    """Opt-in test that sends a real prompt and may consume account quota."""
    if os.environ.get("RUN_CODEBUDDY_MODEL_TEST") != "1":
        pytest.skip("set RUN_CODEBUDDY_MODEL_TEST=1 to run the real model prompt")
    executable = shutil.which("codebuddy")
    if executable is None:
        pytest.skip("codebuddy is not installed")

    client = AcpClient(
        SessionConfig(
            launch_mode="local",
            cwd=str(tmp_path),
            codebuddy_command=executable,
            codebuddy_args=["--tools", ""],
            startup_timeout_seconds=120,
        )
    )
    await client.start()
    try:
        await client.begin_prompt("Reply with exactly: OK")
        result = await client.wait_for_turn_event(300)
        assert result["kind"] == "complete"
        assert "OK" in result["result"]["text"]
    finally:
        await client.close()
