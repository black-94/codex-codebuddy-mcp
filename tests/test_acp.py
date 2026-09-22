from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from pathlib import Path

import pytest

from harness_acp_mcp.acp import AcpClient, AcpError
from harness_acp_mcp.adapters import _normalize_auth_status, get_adapter
from harness_acp_mcp.models import SessionConfig
from harness_acp_mcp.supervisor import _cleanup_remote, _remote_command

FAKE_HARNESS = Path(__file__).with_name("fake_harness.py")


def fake_config(tmp_path: Path, harness: str = "codex") -> SessionConfig:
    return SessionConfig(
        harness=harness,
        launch_mode="local",
        cwd=str(tmp_path),
        model_id="fake-model",
        command=sys.executable,
        args=[str(FAKE_HARNESS)],
        startup_timeout_seconds=5,
        terminate_grace_seconds=0.1,
        remote_cleanup_timeout_seconds=0.1,
    )


@pytest.mark.asyncio
async def test_generic_transport_auth_session_model_and_prompt(tmp_path: Path) -> None:
    client = AcpClient(fake_config(tmp_path))
    await client.start_transport()
    try:
        info = await client.get_auth_info()
        assert info.authenticated is True
        assert info.user == {"email": "user@example.invalid"}
        await client.open_session()
        assert client.session_id == "fake-session"
        assert client.model_id == "fake-model"
        await client.set_model("fake-fast")
        assert client.model_id == "fake-fast"

        await client.begin_prompt("hello")
        event = await client.wait_for_turn_event(5)
        assert event["kind"] == "complete"
        assert event["result"]["text"] == "echo:hello"
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_permission_and_information_interactions_are_independent(tmp_path: Path) -> None:
    client = AcpClient(fake_config(tmp_path))
    await client.start_transport()
    await client.open_session()
    try:
        await client.begin_prompt("permission")
        event = await client.wait_for_turn_event(5)
        permission = event["interaction"]
        assert permission.kind == "permission"
        await client.respond_interaction(permission.request_id, {"option_id": "allow"})
        completed = await client.wait_for_turn_event(5)
        assert completed["result"]["text"].endswith(";answer:allow")

        await client.begin_prompt("information")
        event = await client.wait_for_turn_event(5)
        information = event["interaction"]
        assert information.kind == "information"
        assert information.schema["required"] == ["value"]
        await client.respond_interaction(information.request_id, {"value": "chosen"})
        completed = await client.wait_for_turn_event(5)
        assert completed["result"]["text"].endswith(";answer:chosen")

        await client.begin_prompt("elicitation")
        event = await client.wait_for_turn_event(5)
        elicitation = event["interaction"]
        assert elicitation.response_style == "elicitation"
        await client.respond_interaction(elicitation.request_id, {"value": "accepted"})
        completed = await client.wait_for_turn_event(5)
        assert completed["result"]["text"].endswith(";answer:accepted")
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_authentication_can_complete_after_initialization(tmp_path: Path) -> None:
    config = fake_config(tmp_path)
    config.env["FAKE_AUTHENTICATED"] = "0"
    client = AcpClient(config)
    await client.start_transport()
    try:
        assert (await client.get_auth_info()).authenticated is False
        assert (await client.authenticate("browser")).authenticated is True
        await client.open_session()
        assert client.session_id == "fake-session"
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_closing_supervisor_kills_harness_process_group(tmp_path: Path) -> None:
    pid_file = tmp_path / "pids.txt"
    config = fake_config(tmp_path)
    config.env["FAKE_CHILD_PID_FILE"] = str(pid_file)
    client = AcpClient(config)
    await client.start_transport()
    await client.open_session()
    for _ in range(100):
        if pid_file.exists():
            break
        await asyncio.sleep(0.01)
    harness_pid, child_pid = map(int, pid_file.read_text(encoding="utf-8").split())
    assert client.process_info["transport_pgid"] == harness_pid

    await client.close()

    for _ in range(100):
        if not _pid_exists(harness_pid) and not _pid_exists(child_pid):
            break
        await asyncio.sleep(0.02)
    assert not _pid_exists(harness_pid)
    assert not _pid_exists(child_pid)


@pytest.mark.asyncio
async def test_harness_exit_kills_its_remaining_background_processes(tmp_path: Path) -> None:
    pid_file = tmp_path / "pids.txt"
    config = fake_config(tmp_path)
    config.env.update(
        {"FAKE_CHILD_PID_FILE": str(pid_file), "FAKE_EXIT_AFTER_CHILD": "1"}
    )
    client = AcpClient(config)
    with pytest.raises(AcpError, match="stdout closed"):
        await client.start_transport()
    for _ in range(100):
        if pid_file.exists():
            break
        await asyncio.sleep(0.01)
    _harness_pid, child_pid = map(int, pid_file.read_text(encoding="utf-8").split())
    for _ in range(100):
        if not _pid_exists(child_pid):
            break
        await asyncio.sleep(0.02)
    assert not _pid_exists(child_pid)


def test_adapters_reject_detaching_and_managed_arguments(tmp_path: Path) -> None:
    config = fake_config(tmp_path, "codebuddy")
    config.args.append("--background")
    with pytest.raises(ValueError, match="detach"):
        get_adapter("codebuddy").build_argv(config)


@pytest.mark.parametrize(
    ("status", "authenticated"),
    [
        ({"type": "gateway", "name": "provider"}, True),
        ({"type": "chat-gpt", "email": "user@example.invalid"}, True),
        ({"type": "unauthenticated"}, False),
    ],
)
def test_authentication_status_type_is_normalized(status: dict, authenticated: bool) -> None:
    info = _normalize_auth_status({"result": status}, [])
    assert info.authenticated is authenticated


def _remote_spec(tmp_path: Path) -> dict:
    return {
        "cwd": str(tmp_path),
        "argv": [sys.executable, str(FAKE_HARNESS)],
        "harness_env": {"FAKE_NO_AUTH": "1"},
        "remote_pid_file": "harness-acp-test.pid",
        "terminate_grace_seconds": 0.01,
        "remote_cleanup_timeout_seconds": 1,
        "ssh_command": "ssh",
        "ssh_args": [],
        "ssh_host": "placeholder.invalid",
    }


def test_remote_wrapper_tracks_actual_process_group_and_has_valid_shell(tmp_path: Path) -> None:
    command = _remote_command(_remote_spec(tmp_path))

    assert "set -m" in command
    assert "setsid" not in command
    assert 'harness_pid=$!' in command
    assert 'ps -o pgid= -p "$harness_pid"' in command
    assert 'kill -TERM -"$harness_pgid"' in command
    assert 'wait "$harness_pid"' in command
    subprocess.run(["/bin/sh", "-n", "-c", command], check=True)


@pytest.mark.asyncio
async def test_remote_wrapper_keeps_stdio_open_until_harness_exits(tmp_path: Path) -> None:
    command = _remote_command(_remote_spec(tmp_path))
    env = os.environ.copy()
    env["TMPDIR"] = str(tmp_path)
    process = await asyncio.create_subprocess_exec(
        "/bin/sh",
        "-c",
        command,
        env=env,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
    )
    assert process.stdin is not None
    assert process.stdout is not None
    process.stdin.write(b'{"jsonrpc":"2.0","id":1,"method":"initialize"}\n')
    await process.stdin.drain()
    response = await asyncio.wait_for(process.stdout.readline(), 5)
    assert b'"id":1' in response

    pid_path = tmp_path / "harness-acp-test.pid"
    for _ in range(100):
        if pid_path.exists():
            break
        await asyncio.sleep(0.01)
    process_id, process_group_id = pid_path.read_text(encoding="utf-8").split()
    assert process_id.isdigit()
    assert process_group_id.isdigit()

    process.stdin.close()
    await process.stdin.wait_closed()
    await asyncio.wait_for(process.wait(), 5)
    assert not pid_path.exists()


def test_secondary_remote_cleanup_reads_and_kills_process_group(
    monkeypatch, tmp_path: Path
) -> None:
    captured: dict[str, object] = {}

    def fake_run(argv, **kwargs):
        captured["argv"] = argv
        captured["kwargs"] = kwargs

    monkeypatch.setattr(subprocess, "run", fake_run)
    _cleanup_remote(_remote_spec(tmp_path))

    command = captured["argv"][-1]
    assert 'read harness_pid harness_pgid' in command
    assert 'kill -TERM -"$harness_pgid"' in command
    assert 'kill -KILL -"$harness_pgid"' in command

    config = fake_config(tmp_path, "codebuddy")
    config.args.append("--model")
    with pytest.raises(ValueError, match="managed"):
        get_adapter("codebuddy").build_argv(config)


def _pid_exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True
