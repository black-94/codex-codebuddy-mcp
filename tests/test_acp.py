from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

import pytest

from codex_codebuddy_mcp.acp import AcpClient, AcpError, build_launch_argv, validate_config
from codex_codebuddy_mcp.models import SessionConfig

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


def test_builds_safely_quoted_ssh_command() -> None:
    config = SessionConfig(
        launch_mode="ssh",
        cwd="/tmp/project with spaces",
        codebuddy_command="/opt/code buddy/bin/codebuddy",
        codebuddy_args=["--model", "fast-model"],
        env={"SAFE_VALUE": "value with spaces"},
        ssh_host="dev-box",
        ssh_args=["-T"],
    )
    argv, cwd, _ = build_launch_argv(config)
    assert argv[:4] == ["ssh", "-T", "--", "dev-box"]
    assert "cd '/tmp/project with spaces'" in argv[4]
    assert "'SAFE_VALUE=value with spaces'" in argv[4]
    assert "'/opt/code buddy/bin/codebuddy'" in argv[4]
    assert argv[4].endswith("--acp --acp-transport stdio")
    assert cwd is None


@pytest.mark.asyncio
async def test_lazy_start_and_prompt(tmp_path: Path) -> None:
    client = AcpClient(fake_config(tmp_path))
    assert client.process is None

    await client.start()
    try:
        assert client.running
        assert client.session_id and client.session_id.startswith("fake-session-")
        await client.begin_prompt("hello")
        event = await client.wait_for_turn_event(5)
        assert event["kind"] == "complete"
        assert event["result"]["text"] == "echo:hello"
        assert event["result"]["stop_reason"] == "end_turn"
    finally:
        await client.close()
    assert not client.running


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

        with pytest.raises(AcpError, match="unknown permission"):
            await client.resolve_permission("permission-1", "not-an-option")

        await client.resolve_permission("permission-1", "allow")
        completed = await client.wait_for_turn_event(5)
        assert completed["kind"] == "complete"
        assert completed["result"]["text"].endswith(";permission:allow")
    finally:
        await client.close()


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
