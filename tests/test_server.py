from __future__ import annotations

import asyncio
import json
import os
from types import SimpleNamespace

import pytest

from codex_codebuddy_mcp.acp import AcpClient, AcpError
from codex_codebuddy_mcp.models import PermissionRequest
from codex_codebuddy_mcp.server import (
    _elicit_permission,
    _externalize_result,
    create_codebuddy_session,
    registry,
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
