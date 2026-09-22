from __future__ import annotations

import asyncio
import json
import logging
import os
from contextlib import asynccontextmanager
from typing import Any, Literal

from mcp.server.fastmcp import Context, FastMCP

from .config import load_settings
from .ipc import DaemonClient

logger = logging.getLogger(__name__)
SETTINGS = load_settings()
daemon = DaemonClient(SETTINGS)


@asynccontextmanager
async def lifespan(_: FastMCP):
    await daemon.ensure_daemon()
    yield daemon


mcp = FastMCP("harness-acp-mcp", lifespan=lifespan, json_response=True)


def _supports_elicitation(ctx: Context) -> bool:
    params = getattr(ctx.session, "client_params", None)
    return bool(params and getattr(params.capabilities, "elicitation", None) is not None)


async def _resolve_cwd(ctx: Context, cwd: str) -> str:
    if cwd.strip():
        return cwd.strip()
    if not _supports_elicitation(ctx):
        raise ValueError("cwd is required; retry with a non-empty value")
    result = await ctx.session.elicit_form(
        "A working directory is required for the harness session.",
        {
            "type": "object",
            "properties": {
                "cwd": {"type": "string", "title": "Working directory", "minLength": 1}
            },
            "required": ["cwd"],
        },
        ctx.request_id,
    )
    if result.action == "accept" and result.content:
        value = result.content.get("cwd")
        if isinstance(value, str) and value.strip():
            return value.strip()
    raise ValueError("working directory was not provided")


async def _elicit_interaction(
    ctx: Context, session_id: str, interaction: dict[str, Any], timeout_seconds: float
) -> dict[str, Any]:
    kind = interaction.get("kind")
    message = str(interaction.get("message") or interaction.get("title") or "Input required")
    if kind == "permission":
        options = interaction.get("options") or []
        option_ids = [
            item.get("optionId")
            for item in options
            if isinstance(item, dict) and isinstance(item.get("optionId"), str)
        ]
        if not option_ids:
            await daemon.call("cancel_turn", {"session_id": session_id})
            return {"status": "cancelled", "session_id": session_id}
        labels = [
            f"{item.get('optionId')}: {item.get('name', item.get('kind', 'option'))}"
            for item in options
            if isinstance(item, dict)
        ]
        raw_input = interaction.get("raw_input")
        if raw_input:
            message += "\nInput: " + json.dumps(raw_input, ensure_ascii=False)
        if labels:
            message += "\nOptions: " + "; ".join(labels)
        schema = {
            "type": "object",
            "properties": {
                "option_id": {
                    "type": "string",
                    "title": "Permission decision",
                    "enum": option_ids,
                }
            },
            "required": ["option_id"],
        }
    else:
        schema = interaction.get("schema")
        if not isinstance(schema, dict) or schema.get("type") != "object":
            return {
                "status": "interaction_required",
                "session_id": session_id,
                "interaction": interaction,
                "reason": "information schema cannot be represented by MCP elicitation",
            }

    try:
        result = await ctx.session.elicit_form(message, schema, ctx.request_id)
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.warning("MCP elicitation failed; cancelling harness turn", exc_info=True)
        await daemon.call("cancel_turn", {"session_id": session_id})
        return {"status": "cancelled", "session_id": session_id}
    if result.action != "accept" or not result.content:
        if kind == "permission":
            reject = next(
                (
                    item.get("optionId")
                    for item in interaction.get("options") or []
                    if isinstance(item, dict)
                    and item.get("kind") == "reject"
                    and isinstance(item.get("optionId"), str)
                ),
                None,
            )
            if reject:
                response: dict[str, Any] = {"option_id": reject}
            else:
                await daemon.call("cancel_turn", {"session_id": session_id})
                return {"status": "cancelled", "session_id": session_id}
        else:
            await daemon.call("cancel_turn", {"session_id": session_id})
            return {"status": "cancelled", "session_id": session_id}
    elif kind == "permission":
        response = {"option_id": result.content.get("option_id")}
    else:
        response = dict(result.content)
    return await daemon.call(
        "respond_interaction",
        {
            "session_id": session_id,
            "request_id": interaction["request_id"],
            "response": response,
            "timeout_seconds": timeout_seconds,
        },
        request_timeout=timeout_seconds + 30,
    )


async def _prefer_elicitation(
    ctx: Context, result: dict[str, Any], timeout_seconds: float
) -> dict[str, Any]:
    while result.get("status") == "interaction_required" and _supports_elicitation(ctx):
        interaction = result.get("interaction")
        session_id = result.get("session_id")
        if not isinstance(interaction, dict) or not isinstance(session_id, str):
            break
        result = await _elicit_interaction(ctx, session_id, interaction, timeout_seconds)
    return result


@mcp.tool()
async def create_session(
    harness: Literal["codebuddy", "agy", "codex"],
    cwd: str,
    model_id: str,
    ctx: Context,
    launch_mode: Literal["local", "ssh"] = "local",
    command: str | None = None,
    args: list[str] | None = None,
    env: dict[str, str] | None = None,
    ssh_host: str | None = None,
    ssh_command: str = "ssh",
    ssh_args: list[str] | None = None,
    resume_session_id: str | None = None,
    resume_record_id: str | None = None,
    harness_options: dict[str, Any] | None = None,
    startup_timeout_seconds: float | None = None,
    auth_timeout_seconds: float | None = None,
    max_read_bytes: int | None = None,
    max_output_bytes: int | None = None,
) -> dict[str, Any]:
    """Launch one local or SSH ACP harness and initialize its session."""
    if not isinstance(model_id, str) or not model_id.strip():
        raise ValueError("model_id must not be empty")
    params: dict[str, Any] = {
        "harness": harness,
        "cwd": await _resolve_cwd(ctx, cwd),
        "model_id": model_id.strip(),
        "launch_mode": launch_mode,
        "command": command,
        "args": list(args or []),
        "env": dict(env or {}),
        "ssh_host": ssh_host,
        "ssh_command": ssh_command,
        "ssh_args": list(ssh_args or []),
        "resume_session_id": resume_session_id,
        "resume_record_id": resume_record_id,
        "harness_options": dict(harness_options or {}),
    }
    for key, value in (
        ("startup_timeout_seconds", startup_timeout_seconds),
        ("auth_timeout_seconds", auth_timeout_seconds),
        ("max_read_bytes", max_read_bytes),
        ("max_output_bytes", max_output_bytes),
    ):
        if value is not None:
            params[key] = value
    timeout = (startup_timeout_seconds or SETTINGS.process.startup_timeout_seconds) + 30
    return await daemon.call("create_session", params, request_timeout=timeout)


@mcp.tool()
async def authenticate(session_id: str, method_id: str, ctx: Context) -> dict[str, Any]:
    """Authenticate an initialized harness connection and finish session creation."""
    result = await daemon.call(
        "authenticate",
        {"session_id": session_id, "method_id": method_id},
        request_timeout=SETTINGS.authentication.timeout_seconds + 30,
    )
    return await _prefer_elicitation(ctx, result, SETTINGS.authentication.timeout_seconds)


@mcp.tool()
async def get_user_info(session_id: str) -> dict[str, Any]:
    """Return a reliable login boolean and optional harness account details."""
    return await daemon.call("get_user_info", {"session_id": session_id})


@mcp.tool()
async def set_model(session_id: str, model_id: str) -> dict[str, Any]:
    """Change the model for a ready harness session between turns."""
    return await daemon.call("set_model", {"session_id": session_id, "model_id": model_id})


@mcp.tool()
async def prompt(
    session_id: str,
    prompt: str,
    ctx: Context,
    timeout_seconds: float = SETTINGS.process.turn_timeout_seconds,
    max_output_bytes: int | None = None,
) -> dict[str, Any]:
    """Run a harness turn, preferring MCP elicitation for all interactions."""
    params: dict[str, Any] = {
        "session_id": session_id,
        "prompt": prompt,
        "timeout_seconds": timeout_seconds,
    }
    if max_output_bytes is not None:
        params["max_output_bytes"] = max_output_bytes
    result = await daemon.call(
        "prompt", params, request_timeout=timeout_seconds + 30
    )
    return await _prefer_elicitation(ctx, result, timeout_seconds)


@mcp.tool()
async def respond_interaction(
    session_id: str,
    request_id: str,
    response: dict[str, Any],
    ctx: Context,
    timeout_seconds: float = SETTINGS.process.turn_timeout_seconds,
    max_output_bytes: int | None = None,
) -> dict[str, Any]:
    """Answer a pending permission or information request and continue the turn."""
    params: dict[str, Any] = {
        "session_id": session_id,
        "request_id": request_id,
        "response": response,
        "timeout_seconds": timeout_seconds,
    }
    if max_output_bytes is not None:
        params["max_output_bytes"] = max_output_bytes
    result = await daemon.call(
        "respond_interaction", params, request_timeout=timeout_seconds + 30
    )
    return await _prefer_elicitation(ctx, result, timeout_seconds)


@mcp.tool()
async def cancel_turn(session_id: str) -> dict[str, Any]:
    """Cancel the current turn while keeping the harness session available."""
    return await daemon.call("cancel_turn", {"session_id": session_id})


@mcp.tool()
async def close_session(session_id: str) -> dict[str, Any]:
    """Close the session supervisor and its complete harness process group."""
    return await daemon.call("close_session", {"session_id": session_id})


def main() -> None:
    level_name = os.environ.get("HARNESS_ACP_MCP_LOG_LEVEL", "INFO").upper()
    logging.basicConfig(
        level=getattr(logging, level_name, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
