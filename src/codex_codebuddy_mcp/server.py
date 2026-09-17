from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from typing import Any, Literal

from mcp.server.fastmcp import Context, FastMCP

from .acp import AcpError
from .bridge import SessionRegistry, permission_result
from .models import PermissionRequest, SessionConfig

registry = SessionRegistry()


@asynccontextmanager
async def lifespan(_: FastMCP):
    try:
        yield registry
    finally:
        await registry.close_all()


mcp = FastMCP("codex-codebuddy-mcp", lifespan=lifespan, json_response=True)


def _owner_key(ctx: Context) -> str:
    return ctx.client_id or f"mcp-session:{id(ctx.session)}"


def _supports_elicitation(ctx: Context) -> bool:
    params = getattr(ctx.session, "client_params", None)
    return bool(params and getattr(params.capabilities, "elicitation", None) is not None)


async def _elicit_permission(ctx: Context, permission: PermissionRequest) -> str | None:
    if not _supports_elicitation(ctx):
        return None

    option_ids = [
        item.get("optionId")
        for item in permission.options
        if isinstance(item, dict) and isinstance(item.get("optionId"), str)
    ]
    if not option_ids:
        return None

    labels = [
        f"{item.get('optionId')}: {item.get('name', item.get('kind', 'option'))}"
        for item in permission.options
        if isinstance(item, dict)
    ]
    schema = {
        "type": "object",
        "properties": {
            "option_id": {
                "type": "string",
                "title": "Permission decision",
                "description": "Choose one of the CodeBuddy permission options",
                "enum": option_ids,
            }
        },
        "required": ["option_id"],
    }
    message = (
        f"CodeBuddy requests permission for {permission.tool_name}.\n"
        f"Input: {json.dumps(permission.raw_input, ensure_ascii=False)}\n"
        f"Options: {'; '.join(labels)}"
    )
    try:
        result = await ctx.session.elicit_form(message, schema, ctx.request_id)
    except asyncio.CancelledError:
        raise
    except Exception:
        return None
    if result.action == "accept" and result.content:
        option_id = result.content.get("option_id")
        return option_id if option_id in option_ids else None
    return "__reject__"


async def _wait_with_permissions(
    ctx: Context,
    bridge_session_id: str,
    timeout_seconds: float,
) -> dict[str, Any]:
    session = registry.get(bridge_session_id, _owner_key(ctx))
    while True:
        event = await session.client.wait_for_turn_event(timeout_seconds)
        if event["kind"] == "complete":
            return {"bridge_session_id": bridge_session_id, **event["result"]}

        permission: PermissionRequest = event["permission"]
        option_id = await _elicit_permission(ctx, permission)
        if option_id is None:
            return permission_result(session, permission)
        if option_id == "__reject__":
            option_id = session.client.reject_option(permission)
            if option_id is None:
                await session.client.cancel_turn()
                return {
                    "status": "cancelled",
                    "bridge_session_id": bridge_session_id,
                    "codebuddy_session_id": session.client.session_id,
                    "text": session.client.turn_text,
                    "tool_calls": session.client.tool_calls,
                }
        await session.client.resolve_permission(permission.request_id, option_id)


@mcp.tool()
async def create_codebuddy_session(
    cwd: str,
    ctx: Context,
    launch_mode: Literal["local", "ssh"] = "local",
    codebuddy_command: str = "codebuddy",
    codebuddy_args: list[str] | None = None,
    env: dict[str, str] | None = None,
    ssh_host: str | None = None,
    ssh_command: str = "ssh",
    ssh_args: list[str] | None = None,
    auth_method_id: str | None = None,
    resume_session_id: str | None = None,
    startup_timeout_seconds: float = 60.0,
) -> dict[str, Any]:
    """Configure an isolated CodeBuddy ACP session without starting its process.

    Use launch_mode="ssh" to run CodeBuddy remotely over an existing OpenSSH
    configuration. The CodeBuddy process starts lazily on the first prompt.
    """
    config = SessionConfig(
        launch_mode=launch_mode,
        cwd=cwd,
        codebuddy_command=codebuddy_command,
        codebuddy_args=list(codebuddy_args or []),
        env=dict(env or {}),
        ssh_host=ssh_host,
        ssh_command=ssh_command,
        ssh_args=list(ssh_args or []),
        auth_method_id=auth_method_id,
        resume_session_id=resume_session_id,
        startup_timeout_seconds=startup_timeout_seconds,
    )
    from .acp import validate_config

    validate_config(config)
    session = await registry.create(_owner_key(ctx), config)
    return {
        "status": "configured",
        "bridge_session_id": session.bridge_session_id,
        "launch_mode": launch_mode,
        "lazy_start": True,
    }


@mcp.tool()
async def prompt_codebuddy(
    bridge_session_id: str,
    prompt: str,
    ctx: Context,
    timeout_seconds: float = 900.0,
) -> dict[str, Any]:
    """Send a text prompt to the bound CodeBuddy session.

    This lazily starts CodeBuddy. The result either completes the turn or returns
    a permission request for respond_codebuddy_permission.
    """
    if timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be positive")
    session = registry.get(bridge_session_id, _owner_key(ctx))
    if session.lock.locked():
        raise AcpError("this bridge session already has an active MCP operation")

    async with session.lock:
        try:
            await session.client.start()
            await session.client.begin_prompt(prompt)
            return await _wait_with_permissions(ctx, bridge_session_id, timeout_seconds)
        except asyncio.CancelledError:
            await session.client.cancel_turn()
            raise
        except TimeoutError:
            await session.client.cancel_turn()
            raise


@mcp.tool()
async def respond_codebuddy_permission(
    bridge_session_id: str,
    request_id: str,
    option_id: str,
    ctx: Context,
    timeout_seconds: float = 900.0,
) -> dict[str, Any]:
    """Resolve a pending CodeBuddy permission request and continue the turn."""
    if timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be positive")
    session = registry.get(bridge_session_id, _owner_key(ctx))
    if session.lock.locked():
        raise AcpError("this bridge session already has an active MCP operation")

    async with session.lock:
        await session.client.resolve_permission(request_id, option_id)
        return await _wait_with_permissions(ctx, bridge_session_id, timeout_seconds)


@mcp.tool()
async def cancel_codebuddy_turn(bridge_session_id: str, ctx: Context) -> dict[str, Any]:
    """Cancel the active turn while keeping the CodeBuddy session available."""
    session = registry.get(bridge_session_id, _owner_key(ctx))
    await session.client.cancel_turn()
    return {
        "status": "cancelled",
        "bridge_session_id": bridge_session_id,
        "codebuddy_session_id": session.client.session_id,
    }


@mcp.tool()
async def close_codebuddy_session(bridge_session_id: str, ctx: Context) -> dict[str, Any]:
    """Close and remove a CodeBuddy bridge session."""
    await registry.close(bridge_session_id, _owner_key(ctx))
    return {"status": "closed", "bridge_session_id": bridge_session_id}


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
