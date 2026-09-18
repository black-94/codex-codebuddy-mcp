from __future__ import annotations

import asyncio
import json
import os
import shutil
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from codex_codebuddy_mcp.acp import AcpClient, AcpError, build_launch_argv
from codex_codebuddy_mcp.models import PermissionRequest
from codex_codebuddy_mcp.server import (
    _elicit_permission,
    _externalize_result,
    _resolve_working_directory,
    close_codebuddy_session,
    create_codebuddy_session,
    mcp,
    prompt_codebuddy,
    registry,
    respond_codebuddy_permission,
)


@pytest.mark.asyncio
async def test_externalized_permission_keeps_resume_metadata() -> None:
    holder = SimpleNamespace(output_paths=set())
    permission = {
        "request_id": "permission-1",
        "options": [{"optionId": "allow", "kind": "allow"}],
    }
    compact = await _externalize_result(
        {
            "status": "permission_required",
            "bridge_session_id": "bridge-1",
            "codebuddy_session_id": "session-1",
            "text": "x" * 1024,
            "permission": permission,
        },
        64,
        holder,
    )
    output_path = compact["output_path"]
    try:
        assert compact["permission"] == permission
        assert output_path in holder.output_paths

        def read_result() -> dict:
            with open(output_path, encoding="utf-8") as output_file:
                return json.load(output_file)

        assert (await asyncio.to_thread(read_result))["permission"] == permission
    finally:
        await asyncio.to_thread(os.unlink, output_path)


@pytest.mark.asyncio
async def test_elicitation_mode_does_not_downgrade_without_client_capability() -> None:
    ctx = SimpleNamespace(
        request_id="mcp-request-1",
        session=SimpleNamespace(client_params=None),
    )
    permission = PermissionRequest(
        rpc_id=1,
        request_id="permission-1",
        session_id="session-1",
        tool_name="Bash",
        raw_input={"command": "echo hi"},
        options=[{"kind": "allow", "optionId": "allow"}],
        meta={},
    )

    with pytest.raises(AcpError, match="does not support elicitation"):
        await _elicit_permission(ctx, permission)


@pytest.mark.asyncio
async def test_create_session_schema_requires_working_directory() -> None:
    tools = await mcp.list_tools()
    create_tool = next(tool for tool in tools if tool.name == "create_codebuddy_session")

    assert "cwd" in create_tool.inputSchema["required"]


@pytest.mark.asyncio
async def test_empty_working_directory_is_elicited() -> None:
    session = SimpleNamespace(
        client_params=SimpleNamespace(
            capabilities=SimpleNamespace(elicitation=object()),
        ),
        elicit_form=AsyncMock(
            return_value=SimpleNamespace(
                action="accept",
                content={"cwd": "  /tmp/user-project  "},
            )
        ),
    )
    ctx = SimpleNamespace(request_id="mcp-request-1", session=session)

    resolved = await _resolve_working_directory(ctx, "   ")

    assert resolved == "/tmp/user-project"
    session.elicit_form.assert_awaited_once()
    message, schema, request_id = session.elicit_form.await_args.args
    assert "working directory" in message.lower()
    assert schema["required"] == ["cwd"]
    assert request_id == "mcp-request-1"


@pytest.mark.asyncio
async def test_empty_working_directory_without_elicitation_is_actionable() -> None:
    ctx = SimpleNamespace(
        request_id="mcp-request-1",
        session=SimpleNamespace(client_params=None),
    )

    with pytest.raises(AcpError, match="call create_codebuddy_session again"):
        await _resolve_working_directory(ctx, "")


@pytest.mark.asyncio
async def test_remote_session_starts_during_creation(monkeypatch, tmp_path) -> None:
    async def fake_start(self: AcpClient) -> None:
        self.session_id = "remote-codebuddy-session"

    async def fake_close(self: AcpClient) -> None:
        self._closed = True

    monkeypatch.setattr(AcpClient, "start", fake_start)
    monkeypatch.setattr(AcpClient, "close", fake_close)
    ctx = SimpleNamespace(client_id="remote-owner", session=SimpleNamespace())

    result = await create_codebuddy_session(
        cwd=str(tmp_path),
        ctx=ctx,
        launch_mode="ssh",
        ssh_host="test-host",
    )
    try:
        assert result["codebuddy_session_id"] == "remote-codebuddy-session"
    finally:
        await registry.close(result["bridge_session_id"], "remote-owner")


@pytest.mark.real_codebuddy
@pytest.mark.asyncio
async def test_real_create_session_uses_default_auto_permission_mode(tmp_path) -> None:
    """Create a real session with the API default auto mode, never plan."""
    executable = shutil.which("codebuddy")
    if executable is None:
        pytest.skip("codebuddy is not installed")

    ctx = SimpleNamespace(client_id="real-auto-owner", session=SimpleNamespace())
    created = await create_codebuddy_session(
        cwd=str(tmp_path),
        ctx=ctx,
        codebuddy_command=executable,
        startup_timeout_seconds=120,
    )
    bridge_session_id = created["bridge_session_id"]
    try:
        assert created["permission_mode"] == "auto"
        session = registry.get(bridge_session_id, ctx.client_id)
        argv, _, _ = build_launch_argv(session.config)
        mode_index = argv.index("--permission-mode")
        assert argv[mode_index + 1] == "auto"
        assert argv[mode_index + 1] != "plan"
        assert session.client.running
        assert session.client.session_id == created["codebuddy_session_id"]
    finally:
        await close_codebuddy_session(bridge_session_id=bridge_session_id, ctx=ctx)


@pytest.mark.real_codebuddy
@pytest.mark.model
@pytest.mark.asyncio
@pytest.mark.parametrize("approval_mode", ["compatible", "elicitation"])
async def test_real_codebuddy_permission_request_is_forwarded(tmp_path, approval_mode: str) -> None:
    """Forward a real CodeBuddy Bash permission request and resume the same turn."""
    if os.environ.get("RUN_CODEBUDDY_PERMISSION_TEST") != "1":
        pytest.skip("set RUN_CODEBUDDY_PERMISSION_TEST=1 to run the real permission test")
    executable = shutil.which("codebuddy")
    if executable is None:
        pytest.skip("codebuddy is not installed")

    elicitation_requests: list[tuple[str, dict, str]] = []

    async def approve_permission(message: str, schema: dict, request_id: str):
        elicitation_requests.append((message, schema, request_id))
        option_ids = schema["properties"]["option_id"]["enum"]
        option_id = next(
            (item for item in option_ids if "allow" in item.lower()),
            option_ids[0],
        )
        return SimpleNamespace(action="accept", content={"option_id": option_id})

    if approval_mode == "elicitation":
        mcp_session = SimpleNamespace(
            client_params=SimpleNamespace(
                capabilities=SimpleNamespace(elicitation=object()),
            ),
            elicit_form=AsyncMock(side_effect=approve_permission),
        )
    else:
        mcp_session = SimpleNamespace()
    ctx = SimpleNamespace(
        client_id=f"real-permission-owner-{approval_mode}",
        request_id=f"real-permission-request-{approval_mode}",
        session=mcp_session,
    )
    created = await create_codebuddy_session(
        cwd=str(tmp_path),
        ctx=ctx,
        codebuddy_command=executable,
        codebuddy_args=["--tools", "Bash"],
        permission_mode="default",
        approval_mode=approval_mode,
        startup_timeout_seconds=120,
    )
    bridge_session_id = created["bridge_session_id"]
    try:
        pending = await prompt_codebuddy(
            bridge_session_id=bridge_session_id,
            prompt=(
                "Use the Bash tool exactly once to execute "
                "`printf CODEBUDDY_PERMISSION_FORWARD_OK`. Do not answer before using Bash."
            ),
            ctx=ctx,
            timeout_seconds=300,
        )
        if approval_mode == "compatible":
            assert pending["status"] == "permission_required"
            permission = pending["permission"]
            assert permission["tool_name"].lower() == "bash"
            assert "CODEBUDDY_PERMISSION_FORWARD_OK" in json.dumps(
                permission["raw_input"], ensure_ascii=False
            )

            allow_option = next(
                option
                for option in permission["options"]
                if str(option.get("kind", "")).startswith("allow")
                or "allow" in str(option.get("name", "")).lower()
                or "allow" in str(option.get("optionId", "")).lower()
            )
            completed = await respond_codebuddy_permission(
                bridge_session_id=bridge_session_id,
                request_id=permission["request_id"],
                option_id=allow_option["optionId"],
                ctx=ctx,
                timeout_seconds=300,
            )
        else:
            completed = pending
            assert len(elicitation_requests) == 1
            message, schema, request_id = elicitation_requests[0]
            assert "Bash" in message
            assert "CODEBUDDY_PERMISSION_FORWARD_OK" in message
            assert schema["required"] == ["option_id"]
            assert request_id == ctx.request_id

        assert completed["status"] == "completed"
        assert completed["codebuddy_session_id"] == created["codebuddy_session_id"]
        assert completed["tool_calls"]
        assert "CODEBUDDY_PERMISSION_FORWARD_OK" in json.dumps(
            completed["tool_calls"], ensure_ascii=False
        )
        assert any(call.get("status") == "completed" for call in completed["tool_calls"])
    finally:
        await close_codebuddy_session(bridge_session_id=bridge_session_id, ctx=ctx)
