from __future__ import annotations

import asyncio
import json
import logging
import os
import tempfile
from contextlib import asynccontextmanager
from typing import Any, Literal

from mcp.server.fastmcp import Context, FastMCP

from .acp import AcpError
from .bridge import BridgeSession, SessionRegistry, permission_result
from .config import load_defaults
from .models import ApprovalMode, PermissionMode, PermissionRequest, SessionConfig

logger = logging.getLogger(__name__)

DEFAULTS = load_defaults()
registry = SessionRegistry(max_concurrency=DEFAULTS.max_concurrency)


@asynccontextmanager
async def lifespan(_: FastMCP):
    try:
        yield registry
    finally:
        await registry.close_all()


mcp = FastMCP("codex-codebuddy-mcp", lifespan=lifespan, json_response=True)


def _owner_key(ctx: Context) -> object:
    # Holding the actual session object keeps identity stable and prevents a
    # recycled CPython id from inheriting another transport's bridge sessions.
    return ctx.client_id or ctx.session


def _supports_elicitation(ctx: Context) -> bool:
    params = getattr(ctx.session, "client_params", None)
    return bool(params and getattr(params.capabilities, "elicitation", None) is not None)


async def _resolve_working_directory(ctx: Context, cwd: str) -> str:
    working_directory = cwd.strip()
    if working_directory:
        return working_directory

    if not _supports_elicitation(ctx):
        raise AcpError(
            "Working directory is required; the MCP client does not support elicitation, "
            "so call create_codebuddy_session again with a non-empty cwd"
        )

    schema = {
        "type": "object",
        "properties": {
            "cwd": {
                "type": "string",
                "title": "Working directory",
                "description": "Local or remote working directory for the CodeBuddy session",
                "minLength": 1,
            }
        },
        "required": ["cwd"],
    }
    message = "A working directory is required to create the CodeBuddy session. Please enter it."
    try:
        result = await ctx.session.elicit_form(message, schema, ctx.request_id)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        raise AcpError(f"Working directory elicitation failed: {exc}") from exc

    if result.action == "accept" and result.content:
        value = result.content.get("cwd")
        if isinstance(value, str) and value.strip():
            return value.strip()
        raise AcpError("Working directory must not be empty")
    if result.action in {"decline", "cancel"}:
        raise AcpError("Working directory is required but was not provided")
    raise AcpError(f"Working directory elicitation returned unsupported action: {result.action!r}")


async def _elicit_permission(ctx: Context, permission: PermissionRequest) -> str | None:
    if not _supports_elicitation(ctx):
        raise AcpError(
            "MCP client does not support elicitation; create the session with "
            "approval_mode='compatible' to use the two-step permission flow"
        )

    option_ids = [
        item.get("optionId")
        for item in permission.options
        if isinstance(item, dict) and isinstance(item.get("optionId"), str)
    ]
    if not option_ids:
        raise AcpError("CodeBuddy permission request contains no selectable options")

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
    except Exception as exc:
        raise AcpError(f"MCP elicitation failed: {exc}") from exc
    if result.action == "accept" and result.content:
        option_id = result.content.get("option_id")
        if option_id in option_ids:
            return option_id
        raise AcpError("MCP elicitation returned an invalid permission option")
    if result.action not in {"decline", "cancel"}:
        raise AcpError(f"MCP elicitation returned unsupported action: {result.action!r}")
    return "__reject__"


async def _wait_with_permissions(
    ctx: Context,
    bridge_session_id: str,
    timeout_seconds: float,
    max_output: int,
) -> dict[str, Any]:
    session = registry.get(bridge_session_id, _owner_key(ctx))
    while True:
        event = await session.client.wait_for_turn_event(timeout_seconds)
        if event["kind"] == "complete":
            await registry.release_turn(session)
            result = {"bridge_session_id": bridge_session_id, **event["result"]}
            return await _externalize_result(result, max_output, session)

        permission: PermissionRequest = event["permission"]
        if session.config.approval_mode == "compatible":
            result = await _externalize_result(
                permission_result(session, permission),
                max_output,
                session,
            )
            registry.arm_permission_timeout(session, timeout_seconds)
            return result
        option_id = await _elicit_permission(ctx, permission)
        if option_id == "__reject__":
            option_id = session.client.reject_option(permission)
            if option_id is None:
                await session.client.cancel_turn()
                await registry.release_turn(session)
                return await _externalize_result(
                    {
                        "status": "cancelled",
                        "bridge_session_id": bridge_session_id,
                        "codebuddy_session_id": session.client.session_id,
                        "text": session.client.turn_text,
                        "tool_calls": session.client.tool_calls,
                    },
                    max_output,
                    session,
                )
        await session.client.resolve_permission(permission.request_id, option_id)


async def _externalize_result(
    result: dict[str, Any],
    max_output: int,
    session: BridgeSession | None = None,
) -> dict[str, Any]:
    serialized = json.dumps(result, ensure_ascii=False, separators=(",", ":"))
    output_bytes = len(serialized.encode("utf-8"))
    if output_bytes <= max_output:
        return result

    def write_result() -> str:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            prefix="codex-codebuddy-",
            suffix=".json",
            delete=False,
        ) as output_file:
            output_file.write(serialized)
            output_file.write("\n")
            output_path = output_file.name
        # NamedTemporaryFile uses 0600 on supported platforms; make the
        # contract explicit for platforms or custom tempfile implementations
        # that apply a different umask.
        os.chmod(output_path, 0o600)
        return output_path

    output_path = await asyncio.to_thread(write_result)
    logger.info("externalized oversized result to %s (%d bytes)", output_path, output_bytes)
    if session is not None:
        session.output_paths.add(output_path)
    compact = {
        "status": result.get("status", "completed"),
        "bridge_session_id": result.get("bridge_session_id"),
        "codebuddy_session_id": result.get("codebuddy_session_id"),
        "stop_reason": result.get("stop_reason"),
        "output_path": output_path,
        "output_format": "json",
        "output_bytes": output_bytes,
        "max_output": max_output,
        "text_available_in_file": True,
        "tool_calls_count": len(result.get("tool_calls") or []),
    }
    if "permission" in result:
        compact["permission"] = result["permission"]
    return compact


@mcp.tool()
async def create_codebuddy_session(
    cwd: str,
    model_id: str,
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
    permission_mode: PermissionMode = "auto",
    approval_mode: ApprovalMode | None = None,
    startup_timeout_seconds: float = DEFAULTS.startup_timeout_seconds,
    max_read: int | None = None,
    max_output: int | None = None,
) -> dict[str, Any]:
    """Create and start an isolated CodeBuddy ACP session.

    Use launch_mode="ssh" to run CodeBuddy remotely over an existing OpenSSH
    configuration. The process is started and the ACP session is established
    before this call returns.
    ``approval_mode``, ``max_read`` and ``max_output`` override the YAML
    defaults for this session. The timeout and process-buffer settings are
    loaded from the YAML defaults when the MCP server starts.
    """
    cwd = await _resolve_working_directory(ctx, cwd)
    if not isinstance(model_id, str) or not model_id.strip():
        raise ValueError("model_id must not be empty")
    config = SessionConfig(
        launch_mode=launch_mode,
        cwd=cwd,
        model_id=model_id.strip(),
        codebuddy_command=codebuddy_command,
        codebuddy_args=list(codebuddy_args or []),
        env=dict(env or {}),
        ssh_host=ssh_host,
        ssh_command=ssh_command,
        ssh_args=list(ssh_args or []),
        auth_method_id=auth_method_id,
        resume_session_id=resume_session_id,
        permission_mode=permission_mode,
        approval_mode=DEFAULTS.approval_mode if approval_mode is None else approval_mode,
        startup_timeout_seconds=startup_timeout_seconds,
        turn_cancel_timeout_seconds=DEFAULTS.turn_cancel_timeout_seconds,
        local_process_terminate_timeout_seconds=DEFAULTS.local_process_terminate_timeout_seconds,
        remote_ssh_cleanup_timeout_seconds=DEFAULTS.remote_ssh_cleanup_timeout_seconds,
        stdout_overflow_retry_tolerance=DEFAULTS.stdout_overflow_retry_tolerance,
        stderr_tail_buffer_size=DEFAULTS.stderr_tail_buffer_size,
        max_read=DEFAULTS.max_read if max_read is None else max_read,
        max_output=DEFAULTS.max_output if max_output is None else max_output,
    )
    from .acp import validate_config

    validate_config(config)
    session = await registry.create(_owner_key(ctx), config)
    try:
        await session.client.start()
    except Exception:
        await registry.close(session.bridge_session_id, _owner_key(ctx))
        raise
    return {
        "status": "created",
        "bridge_session_id": session.bridge_session_id,
        "launch_mode": launch_mode,
        "codebuddy_session_id": session.client.session_id,
        "model_id": session.client.model_id,
        "model_name": session.client.model_name,
        "permission_mode": config.permission_mode,
        "approval_mode": config.approval_mode,
        "startup_timeout_seconds": config.startup_timeout_seconds,
        "turn_cancel_timeout_seconds": config.turn_cancel_timeout_seconds,
        "local_process_terminate_timeout_seconds": config.local_process_terminate_timeout_seconds,
        "remote_ssh_cleanup_timeout_seconds": config.remote_ssh_cleanup_timeout_seconds,
        "stdout_overflow_retry_tolerance": config.stdout_overflow_retry_tolerance,
        "stderr_tail_buffer_size": config.stderr_tail_buffer_size,
        "max_read": config.max_read,
        "max_output": config.max_output,
    }


@mcp.tool()
async def switch_codebuddy_model(
    bridge_session_id: str,
    model_id: str,
    ctx: Context,
) -> dict[str, Any]:
    """Switch the model for an existing CodeBuddy ACP session.

    Model changes are session-scoped and can only happen between turns. The
    returned model fields come from CodeBuddy's successful ACP response.
    """
    if not isinstance(model_id, str) or not model_id.strip():
        raise ValueError("model_id must not be empty")
    session = registry.get(bridge_session_id, _owner_key(ctx))
    if session.lock.locked():
        raise AcpError("this bridge session already has an active MCP operation")

    async with session.lock:
        if session.client.turn_active or session.client.pending_permission is not None:
            raise AcpError("cannot switch model while a CodeBuddy turn is active")
        if not session.client.running:
            raise AcpError("CodeBuddy process is no longer running; create a new CodeBuddy session")
        await session.client.set_model(model_id)

    return {
        "status": "model_switched",
        "bridge_session_id": bridge_session_id,
        "codebuddy_session_id": session.client.session_id,
        "model_id": session.client.model_id,
        "model_name": session.client.model_name,
    }


@mcp.tool()
async def prompt_codebuddy(
    bridge_session_id: str,
    prompt: str,
    ctx: Context,
    timeout_seconds: float = DEFAULTS.timeout_seconds,
    max_output: int | None = None,
) -> dict[str, Any]:
    """Send a text prompt to the bound CodeBuddy session.

    The CodeBuddy process and ACP session are created by create_codebuddy_session.
    The result either completes the turn or returns a permission request for
    respond_codebuddy_permission.
    """
    if timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be positive")
    session = registry.get(bridge_session_id, _owner_key(ctx))
    effective_max_output = session.config.max_output if max_output is None else max_output
    if effective_max_output <= 0:
        raise ValueError("max_output must be positive")
    if session.lock.locked():
        raise AcpError("this bridge session already has an active MCP operation")

    async with session.lock:
        try:
            await registry.acquire_turn(session, timeout_seconds)
            if not session.client.running:
                raise AcpError(
                    "CodeBuddy process is no longer running; create a new CodeBuddy session"
                )
            await session.client.begin_prompt(prompt)
            return await _wait_with_permissions(
                ctx, bridge_session_id, timeout_seconds, effective_max_output
            )
        except asyncio.CancelledError:
            try:
                await session.client.cancel_turn()
            finally:
                await registry.release_turn(session)
            raise
        except TimeoutError:
            try:
                await session.client.cancel_turn()
            finally:
                await registry.release_turn(session)
            raise
        except Exception:
            try:
                await session.client.cancel_turn()
            finally:
                await registry.release_turn(session)
            raise


@mcp.tool()
async def respond_codebuddy_permission(
    bridge_session_id: str,
    request_id: str,
    option_id: str,
    ctx: Context,
    timeout_seconds: float = DEFAULTS.timeout_seconds,
    max_output: int | None = None,
) -> dict[str, Any]:
    """Resolve a pending CodeBuddy permission request and continue the turn."""
    if timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be positive")
    session = registry.get(bridge_session_id, _owner_key(ctx))
    effective_max_output = session.config.max_output if max_output is None else max_output
    if effective_max_output <= 0:
        raise ValueError("max_output must be positive")
    if session.lock.locked():
        raise AcpError("this bridge session already has an active MCP operation")

    async with session.lock:
        try:
            await registry.acquire_turn(session, timeout_seconds)
            registry.disarm_permission_timeout(session)
            try:
                await session.client.resolve_permission(request_id, option_id)
            except Exception:
                # Keep the pending request retryable, but retain the expiry so
                # malformed client responses cannot leak the global turn slot.
                registry.arm_permission_timeout(session, timeout_seconds)
                raise
            try:
                return await _wait_with_permissions(
                    ctx, bridge_session_id, timeout_seconds, effective_max_output
                )
            except Exception:
                try:
                    await session.client.cancel_turn()
                finally:
                    await registry.release_turn(session)
                raise
        except asyncio.CancelledError:
            try:
                await session.client.cancel_turn()
            finally:
                await registry.release_turn(session)
            raise
        except TimeoutError:
            try:
                await session.client.cancel_turn()
            finally:
                await registry.release_turn(session)
            raise


@mcp.tool()
async def cancel_codebuddy_turn(bridge_session_id: str, ctx: Context) -> dict[str, Any]:
    """Cancel the active turn while keeping the CodeBuddy session available."""
    session = registry.get(bridge_session_id, _owner_key(ctx))
    registry.disarm_permission_timeout(session)
    try:
        await session.client.cancel_turn()
    finally:
        await registry.release_turn(session)
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
    level_name = os.environ.get("CODEX_CODEBUDDY_MCP_LOG_LEVEL", "INFO").upper()
    logging.basicConfig(
        level=getattr(logging, level_name, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
