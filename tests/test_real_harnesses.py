from __future__ import annotations

import os
import shutil

import pytest

from harness_acp_mcp.acp import AcpClient
from harness_acp_mcp.models import SessionConfig


async def _run_real_handshake(
    tmp_path, harness: str, executable: str, model_id: str
) -> None:
    client = AcpClient(
        SessionConfig(
            harness=harness,
            launch_mode="local",
            cwd=str(tmp_path),
            model_id=model_id,
            command=executable,
            startup_timeout_seconds=120,
            terminate_grace_seconds=2,
            remote_cleanup_timeout_seconds=2,
        )
    )
    try:
        initialized = await client.start_transport()
        assert initialized["result"]["protocolVersion"] == 1
        info = await client.get_auth_info()
        assert isinstance(info.authenticated, bool)
        if info.authenticated:
            await client.open_session()
            assert client.session_id
            assert client.model_id == model_id
    finally:
        await client.close()


async def _run_real_prompt(tmp_path, harness: str, executable: str, model_id: str) -> None:
    client = AcpClient(
        SessionConfig(
            harness=harness,
            launch_mode="local",
            cwd=str(tmp_path),
            model_id=model_id,
            command=executable,
            startup_timeout_seconds=120,
            terminate_grace_seconds=2,
            remote_cleanup_timeout_seconds=2,
        )
    )
    try:
        await client.start_transport()
        info = await client.get_auth_info()
        if not info.authenticated:
            pytest.skip(f"{harness} is not authenticated")
        await client.open_session()
        await client.begin_prompt("Reply with exactly HARNESS_ACP_OK. Do not use tools.")
        event = await client.wait_for_turn_event(300)
        assert event["kind"] == "complete"
        assert "HARNESS_ACP_OK" in event["result"]["text"]
    finally:
        await client.close()


@pytest.mark.real_codebuddy
@pytest.mark.asyncio
async def test_installed_codebuddy_acp_handshake(tmp_path) -> None:
    executable = shutil.which("codebuddy")
    model_id = os.environ.get("HARNESS_ACP_REAL_CODEBUDDY_MODEL")
    if executable is None or not model_id:
        pytest.skip("installed CodeBuddy and HARNESS_ACP_REAL_CODEBUDDY_MODEL are required")
    await _run_real_handshake(tmp_path, "codebuddy", executable, model_id)


@pytest.mark.real_codex
@pytest.mark.asyncio
async def test_installed_codex_acp_handshake(tmp_path) -> None:
    executable = shutil.which("codex-acp")
    model_id = os.environ.get("HARNESS_ACP_REAL_CODEX_MODEL")
    if executable is None or not model_id:
        pytest.skip("installed codex-acp and HARNESS_ACP_REAL_CODEX_MODEL are required")
    await _run_real_handshake(tmp_path, "codex", executable, model_id)


@pytest.mark.real_codebuddy
@pytest.mark.model
@pytest.mark.asyncio
async def test_installed_codebuddy_model_prompt(tmp_path) -> None:
    if os.environ.get("RUN_HARNESS_ACP_REAL_MODEL_TEST") != "1":
        pytest.skip("set RUN_HARNESS_ACP_REAL_MODEL_TEST=1")
    executable = shutil.which("codebuddy")
    model_id = os.environ.get("HARNESS_ACP_REAL_CODEBUDDY_MODEL")
    if executable is None or not model_id:
        pytest.skip("installed CodeBuddy and HARNESS_ACP_REAL_CODEBUDDY_MODEL are required")
    await _run_real_prompt(tmp_path, "codebuddy", executable, model_id)


@pytest.mark.real_codex
@pytest.mark.model
@pytest.mark.asyncio
async def test_installed_codex_acp_model_prompt(tmp_path) -> None:
    if os.environ.get("RUN_HARNESS_ACP_REAL_MODEL_TEST") != "1":
        pytest.skip("set RUN_HARNESS_ACP_REAL_MODEL_TEST=1")
    executable = shutil.which("codex-acp")
    model_id = os.environ.get("HARNESS_ACP_REAL_CODEX_MODEL")
    if executable is None or not model_id:
        pytest.skip("installed codex-acp and HARNESS_ACP_REAL_CODEX_MODEL are required")
    await _run_real_prompt(tmp_path, "codex", executable, model_id)


@pytest.mark.real_codebuddy
@pytest.mark.model
@pytest.mark.asyncio
async def test_installed_codebuddy_permission_round_trip(tmp_path) -> None:
    if os.environ.get("RUN_HARNESS_ACP_REAL_PERMISSION_TEST") != "1":
        pytest.skip("set RUN_HARNESS_ACP_REAL_PERMISSION_TEST=1")
    executable = shutil.which("codebuddy")
    model_id = os.environ.get("HARNESS_ACP_REAL_CODEBUDDY_MODEL")
    if executable is None or not model_id:
        pytest.skip("installed CodeBuddy and HARNESS_ACP_REAL_CODEBUDDY_MODEL are required")
    client = AcpClient(
        SessionConfig(
            harness="codebuddy",
            launch_mode="local",
            cwd=str(tmp_path),
            model_id=model_id,
            command=executable,
            args=["--tools", "Bash"],
            harness_options={"permission_mode": "default"},
            startup_timeout_seconds=120,
            turn_cancel_timeout_seconds=5,
            terminate_grace_seconds=2,
            remote_cleanup_timeout_seconds=2,
        )
    )
    try:
        await client.start_transport()
        if not (await client.get_auth_info()).authenticated:
            pytest.skip("CodeBuddy is not authenticated")
        await client.open_session()
        await client.begin_prompt(
            "Use Bash exactly once to run `printf HARNESS_ACP_PERMISSION_OK`. "
            "Do not answer before using Bash."
        )
        pending = await client.wait_for_turn_event(300)
        assert pending["kind"] == "interaction"
        interaction = pending["interaction"]
        assert interaction.kind == "permission"
        allow = next(
            item["optionId"]
            for item in interaction.options
            if str(item.get("kind", "")).startswith("allow")
            or "allow" in str(item.get("name", "")).lower()
            or "allow" in str(item.get("optionId", "")).lower()
        )
        await client.respond_interaction(interaction.request_id, {"option_id": allow})
        completed = await client.wait_for_turn_event(300)
        assert completed["kind"] == "complete"
        assert completed["result"]["tool_calls"]
    finally:
        await client.close()
