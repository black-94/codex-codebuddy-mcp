from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from mcp.types import CallToolResult

import harness_acp_mcp.server as server
from harness_acp_mcp.config import (
    CREATE_SESSION_IPC_GRACE_SECONDS,
    create_session_timeout_seconds,
)
from harness_acp_mcp.ipc import _omit_empty_fields


@pytest.mark.asyncio
async def test_create_schema_requires_generic_harness_cwd_and_model() -> None:
    tools = await server.mcp.list_tools()
    create = next(item for item in tools if item.name == "create_session")

    assert {"harness", "cwd", "model_id"} <= set(create.inputSchema["required"])
    harness_schema = create.inputSchema["properties"]["harness"]
    assert set(harness_schema["enum"]) == {"codebuddy", "agy", "codex"}
    properties = create.inputSchema["properties"]
    assert {"target", "remote_host", "runtime", "permission_mode", "container_policy",
            "docker_id", "docker_image", "mounts", "ports", "host_network",
            "resume_session_id"} <= properties.keys()
    assert not {"command", "args", "ssh_host", "ssh_args", "max_read_bytes",
                "max_output_bytes", "container_record_id", "resume_record_id"} & properties.keys()
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
async def test_output_limits_are_config_only_and_not_tool_parameters() -> None:
    tools = {item.name: item for item in await server.mcp.list_tools()}
    for name in ("create_session", "prompt", "respond_interaction"):
        properties = tools[name].inputSchema["properties"]
        assert "max_read_bytes" not in properties
        assert "max_output_bytes" not in properties


def test_mcp_results_omit_empty_fields_but_keep_false_and_zero() -> None:
    assert _omit_empty_fields({
        "none": None, "empty": "", "list": [], "mapping": {},
        "false": False, "zero": 0, "nested": {"unused": None, "value": "ok"},
    }) == {"false": False, "zero": 0, "nested": {"value": "ok"}}


@pytest.mark.parametrize("reused", [False, True])
def test_mcp_results_omit_empty_docker_launch_info_lists(reused: bool) -> None:
    # A newly created and a reused container both follow the generic empty-field rule:
    # empty mounts/ports are dropped, while a meaningful ``false`` and a non-empty image
    # survive.
    filtered = _omit_empty_fields({
        "status": "ready",
        "launch_info": {
            "target": "local", "runtime": "docker", "container_policy": "remove",
            "permission_mode": "auto", "cwd": "/work", "remote_host": None,
            "reused_container": reused, "docker_image": "example/image:1",
            "mounts": [], "ports": [], "host_network": False,
        },
    })
    result = filtered["launch_info"]
    assert "mounts" not in result
    assert "ports" not in result
    assert result["docker_image"] == "example/image:1"
    assert result["host_network"] is False
    # Every other empty field keeps the normal omission semantics too.
    assert "remote_host" not in result


def test_mcp_results_omit_empty_launch_info_fields_for_direct_runtime() -> None:
    filtered = _omit_empty_fields({
        "status": "ready",
        "launch_info": {"runtime": "direct", "mounts": [], "ports": []},
    })
    launch_info = filtered["launch_info"]
    assert "mounts" not in launch_info
    assert "ports" not in launch_info


def test_status_error_is_exposed_as_mcp_error_with_structured_payload() -> None:
    result = {
        "status": "error",
        "error": {"code": "acp_json_line_too_large", "max_read_bytes": 1024},
    }
    wrapped = server._tool_result(result)
    assert isinstance(wrapped, CallToolResult)
    assert wrapped.isError is True
    assert wrapped.structuredContent == result
    assert json.loads(wrapped.content[0].text) == result

    success = {"status": "ready"}
    assert server._tool_result(success) is success


@pytest.mark.asyncio
async def test_create_session_ipc_timeout_covers_full_daemon_budget(monkeypatch) -> None:
    captured: dict[str, object] = {}

    async def fake_call(method, params, **kwargs):
        captured["method"] = method
        captured["timeout"] = kwargs.get("request_timeout")
        return {"status": "ready"}

    monkeypatch.setattr(server.daemon, "call", fake_call)
    ctx = SimpleNamespace(session=SimpleNamespace(client_params=None))
    result = await server.create_session(
        harness="codex",
        cwd="/tmp",
        model_id="model",
        ctx=ctx,
        startup_timeout_seconds=2.0,
    )
    assert captured["method"] == "create_session"
    assert captured["timeout"] == (
        create_session_timeout_seconds(2.0) + CREATE_SESSION_IPC_GRACE_SECONDS
    )
    assert captured["timeout"] > create_session_timeout_seconds(2.0)
    assert result == {"status": "ready"}


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
