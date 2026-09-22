from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import harness_acp_mcp.server as server


@pytest.mark.asyncio
async def test_create_schema_requires_generic_harness_cwd_and_model() -> None:
    tools = await server.mcp.list_tools()
    create = next(item for item in tools if item.name == "create_session")

    assert {"harness", "cwd", "model_id"} <= set(create.inputSchema["required"])
    harness_schema = create.inputSchema["properties"]["harness"]
    assert set(harness_schema["enum"]) == {"codebuddy", "agy", "codex"}
    assert {item.name for item in tools} == {
        "create_session",
        "authenticate",
        "get_user_info",
        "set_model",
        "prompt",
        "respond_interaction",
        "cancel_turn",
        "close_session",
    }


@pytest.mark.asyncio
async def test_permission_prefers_mcp_elicitation(monkeypatch) -> None:
    elicitation = AsyncMock(
        return_value=SimpleNamespace(action="accept", content={"option_id": "allow"})
    )
    ctx = SimpleNamespace(
        request_id="request-id",
        session=SimpleNamespace(
            client_params=SimpleNamespace(
                capabilities=SimpleNamespace(elicitation=object())
            ),
            elicit_form=elicitation,
        ),
    )
    call = AsyncMock(
        return_value={"status": "completed", "session_id": "session-id", "text": "done"}
    )
    monkeypatch.setattr(server.daemon, "call", call)
    pending = {
        "status": "interaction_required",
        "session_id": "session-id",
        "interaction": {
            "request_id": "interaction-id",
            "kind": "permission",
            "message": "Allow command?",
            "raw_input": {"command": "printf ok"},
            "options": [
                {"kind": "allow", "name": "Allow", "optionId": "allow"},
                {"kind": "reject", "name": "Deny", "optionId": "deny"},
            ],
        },
    }

    result = await server._prefer_elicitation(ctx, pending, 30)

    assert result["status"] == "completed"
    elicitation.assert_awaited_once()
    sent = call.await_args.args[1]
    assert sent["response"] == {"option_id": "allow"}


@pytest.mark.asyncio
async def test_information_uses_original_form_schema(monkeypatch) -> None:
    schema = {
        "type": "object",
        "properties": {"value": {"type": "string"}},
        "required": ["value"],
    }
    elicitation = AsyncMock(
        return_value=SimpleNamespace(action="accept", content={"value": "chosen"})
    )
    ctx = SimpleNamespace(
        request_id="request-id",
        session=SimpleNamespace(
            client_params=SimpleNamespace(
                capabilities=SimpleNamespace(elicitation=object())
            ),
            elicit_form=elicitation,
        ),
    )
    monkeypatch.setattr(
        server.daemon,
        "call",
        AsyncMock(return_value={"status": "completed", "session_id": "session-id"}),
    )
    pending = {
        "status": "interaction_required",
        "session_id": "session-id",
        "interaction": {
            "request_id": "interaction-id",
            "kind": "information",
            "message": "Choose a value",
            "schema": schema,
        },
    }

    await server._prefer_elicitation(ctx, pending, 30)

    assert elicitation.await_args.args[1] == schema
    assert server.daemon.call.await_args.args[1]["response"] == {"value": "chosen"}


@pytest.mark.asyncio
async def test_without_elicitation_interaction_remains_two_step() -> None:
    ctx = SimpleNamespace(session=SimpleNamespace(client_params=None))
    pending = {
        "status": "interaction_required",
        "session_id": "session-id",
        "interaction": {"kind": "permission", "request_id": "interaction-id"},
    }

    assert await server._prefer_elicitation(ctx, pending, 30) is pending
