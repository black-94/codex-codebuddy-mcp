from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shlex
import signal
import uuid
from collections import deque
from contextlib import suppress
from typing import Any

from .models import PermissionRequest, SessionConfig, TurnBuffers

logger = logging.getLogger(__name__)


class AcpError(RuntimeError):
    """ACP transport or remote error."""


class AcpRpcError(AcpError):
    """Structured JSON-RPC error returned by the ACP peer."""

    def __init__(self, error: Any) -> None:
        self.error = error
        self.code = error.get("code") if isinstance(error, dict) else None
        super().__init__(f"ACP request failed: {error!r}")


_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_MANAGED_OR_INCOMPATIBLE_ARGS = {
    "--acp",
    "--acp-transport",
    "--serve",
    "--open",
    "--bg",
    "--background",
    "--print",
    "-p",
    "--input-format",
    "--output-format",
    "--tmux",
    "--tmux-classic",
    "--permission-mode",
}


async def _readline_discarding_overflow(
    reader: asyncio.StreamReader,
) -> tuple[bytes, bool]:
    """Read one framed line and fully drain it when it exceeds the stream limit."""
    overflowed = False
    while True:
        try:
            line = await reader.readuntil(b"\n")
        except asyncio.IncompleteReadError as exc:
            return (b"" if overflowed else exc.partial), overflowed
        except asyncio.LimitOverrunError as exc:
            overflowed = True
            # For a one-byte separator, consumed is always safe to discard.
            # Keep looping until the newline arrives so the next read starts at
            # a fresh ACP/stderr record even when the producer streams slowly.
            if exc.consumed:
                await reader.readexactly(exc.consumed)
            else:  # Defensive progress guard for unusual StreamReader variants.
                await reader.readexactly(1)
            continue
        return (b"" if overflowed else line), overflowed


def validate_config(config: SessionConfig) -> None:
    if not config.cwd:
        raise ValueError("cwd must not be empty")
    if config.launch_mode == "ssh" and not config.ssh_host:
        raise ValueError("ssh_host is required for SSH launch mode")
    if config.startup_timeout_seconds <= 0:
        raise ValueError("startup_timeout_seconds must be positive")
    if config.max_read <= 0:
        raise ValueError("max_read must be positive")
    if config.max_output <= 0:
        raise ValueError("max_output must be positive")
    if config.approval_mode not in {"elicitation", "compatible"}:
        raise ValueError(f"unsupported approval mode: {config.approval_mode!r}")
    if config.permission_mode not in {
        "acceptEdits",
        "bypassPermissions",
        "default",
        "plan",
        "dontAsk",
        "auto",
    }:
        raise ValueError(f"unsupported permission mode: {config.permission_mode!r}")
    for key in config.env:
        if not _ENV_NAME.fullmatch(key):
            raise ValueError(f"invalid environment variable name: {key!r}")

    for index, arg in enumerate(config.codebuddy_args):
        name = arg.split("=", 1)[0]
        if name in _MANAGED_OR_INCOMPATIBLE_ARGS:
            raise ValueError(f"CodeBuddy argument is managed or incompatible with ACP stdio: {arg}")
        if index and config.codebuddy_args[index - 1] in {"--acp-transport", "--output-format"}:
            raise ValueError(f"CodeBuddy argument is managed or incompatible with ACP stdio: {arg}")


def build_launch_argv(config: SessionConfig) -> tuple[list[str], str | None, dict[str, str]]:
    validate_config(config)
    codebuddy_argv = [
        config.codebuddy_command,
        *config.codebuddy_args,
        "--permission-mode",
        config.permission_mode,
        "--acp",
        "--acp-transport",
        "stdio",
    ]

    if config.launch_mode == "local":
        env = os.environ.copy()
        env.update(config.env)
        return codebuddy_argv, config.cwd, env

    remote_parts: list[str] = []
    if config.env:
        remote_parts.extend(["env", *(f"{key}={value}" for key, value in config.env.items())])
    remote_parts.extend(codebuddy_argv)
    remote_prefix = f"cd {shlex.quote(config.cwd)} && umask 077"
    remote_program = shlex.join(remote_parts)
    if config.remote_pid_file:
        pid_name = shlex.quote(config.remote_pid_file)
        remote_prefix += f' && remote_tmp="${{TMPDIR:-/tmp}}" && pid_file="$remote_tmp"/{pid_name}'
        leader_script = 'echo "$$" > "$1"; shift; exec "$@"'
        remote_command = (
            f"{remote_prefix} && "
            "if command -v setsid >/dev/null 2>&1; then "
            f"exec setsid sh -c {shlex.quote(leader_script)} codebuddy-session "
            f'"$pid_file" {remote_program}; '
            f'else echo "$$" > "$pid_file" && exec {remote_program}; fi'
        )
    else:
        remote_command = f"{remote_prefix} && exec {remote_program}"
    argv = [config.ssh_command, *config.ssh_args, "--", str(config.ssh_host), remote_command]
    return argv, None, os.environ.copy()


class AcpClient:
    def __init__(self, config: SessionConfig) -> None:
        self.config = config
        self.process: asyncio.subprocess.Process | None = None
        self.session_id: str | None = None
        self.model_id: str | None = None
        self.model_name: str | None = None
        self._model_names: dict[str, str] = {}
        self._next_id = 1
        self._pending: dict[int | str, asyncio.Future[dict[str, Any]]] = {}
        self._write_lock = asyncio.Lock()
        self._cancel_lock = asyncio.Lock()
        self._terminate_lock = asyncio.Lock()
        self._reader_task: asyncio.Task[None] | None = None
        self._stderr_task: asyncio.Task[None] | None = None
        self._permission_queue: asyncio.Queue[PermissionRequest] = asyncio.Queue()
        self._stderr_tail: deque[str] = deque(maxlen=80)
        self._turn_buffers = TurnBuffers(spool_max_size=config.max_output)
        self._turn_task: asyncio.Task[dict[str, Any]] | None = None
        self.pending_permission: PermissionRequest | None = None
        self._stdout_limit_failures = 0
        self._closed = False
        self._remote_pid_file = (
            f"codex-codebuddy-{uuid.uuid4().hex}.pid" if config.launch_mode == "ssh" else None
        )

    @property
    def running(self) -> bool:
        return self.process is not None and self.process.returncode is None

    @property
    def turn_active(self) -> bool:
        return self._turn_task is not None and not self._turn_task.done()

    @property
    def stderr_tail(self) -> str:
        return "\n".join(self._stderr_tail)

    @property
    def turn_text(self) -> str:
        return self._turn_buffers.text

    @property
    def tool_calls(self) -> list[dict[str, Any]]:
        return self._turn_buffers.tool_call_summaries()

    async def start(self) -> None:
        if self.running:
            return
        if self._closed:
            raise AcpError("ACP client is closed")

        launch_config = self.config
        if self._remote_pid_file:
            launch_config = SessionConfig(
                launch_mode=self.config.launch_mode,
                cwd=self.config.cwd,
                codebuddy_command=self.config.codebuddy_command,
                codebuddy_args=list(self.config.codebuddy_args),
                env=dict(self.config.env),
                ssh_host=self.config.ssh_host,
                ssh_command=self.config.ssh_command,
                ssh_args=list(self.config.ssh_args),
                auth_method_id=self.config.auth_method_id,
                resume_session_id=self.config.resume_session_id,
                permission_mode=self.config.permission_mode,
                startup_timeout_seconds=self.config.startup_timeout_seconds,
                max_read=self.config.max_read,
                max_output=self.config.max_output,
                approval_mode=self.config.approval_mode,
                remote_pid_file=self._remote_pid_file,
            )
        argv, cwd, env = build_launch_argv(launch_config)
        try:
            self.process = await asyncio.create_subprocess_exec(
                *argv,
                cwd=cwd,
                env=env,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                limit=self.config.max_read,
                start_new_session=os.name != "nt",
            )
        except (OSError, ValueError) as exc:
            raise AcpError(f"failed to launch CodeBuddy: {exc}") from exc

        self._reader_task = asyncio.create_task(self._read_stdout(), name="codebuddy-acp-stdout")
        self._stderr_task = asyncio.create_task(self._read_stderr(), name="codebuddy-acp-stderr")
        try:
            async with asyncio.timeout(self.config.startup_timeout_seconds):
                initialize_response = await self.request(
                    "initialize",
                    {
                        "protocolVersion": 1,
                        "clientCapabilities": {
                            "fs": {"readTextFile": False, "writeTextFile": False},
                            "terminal": False,
                        },
                        "clientInfo": {
                            "name": "codex-codebuddy-mcp",
                            "title": "Codex CodeBuddy MCP Bridge",
                            "version": "0.1.0",
                        },
                    },
                )
                self._update_model_info(initialize_response)
                await self._authenticate_if_required(initialize_response)
                if self.config.resume_session_id:
                    response = await self.request(
                        "session/load",
                        {
                            "sessionId": self.config.resume_session_id,
                            "cwd": self.config.cwd,
                            "mcpServers": [],
                        },
                    )
                else:
                    response = await self.request(
                        "session/new",
                        {"cwd": self.config.cwd, "mcpServers": []},
                    )
                self._update_model_info(response)
        except BaseException:
            await self.close()
            raise

        result = response.get("result", {})
        session_id = result.get("sessionId")
        if self.config.resume_session_id and not session_id:
            # CodeBuddy's successful session/load response returns the loaded
            # configuration but omits sessionId. The request already identifies
            # the session, so retain the caller-provided ID.
            session_id = self.config.resume_session_id
        if not isinstance(session_id, str) or not session_id:
            await self.close()
            raise AcpError(f"CodeBuddy did not return a sessionId: {response!r}")
        self.session_id = session_id

    def _update_model_info(self, response: dict[str, Any]) -> None:
        result = response.get("result")
        if not isinstance(result, dict):
            return
        models = result.get("models")
        if not isinstance(models, dict):
            models = result

        current_id = (
            models.get("currentModelId")
            or models.get("current_model_id")
            or models.get("modelId")
            or models.get("model")
        )
        if isinstance(current_id, dict):
            current_id = current_id.get("modelId") or current_id.get("id")
        if isinstance(current_id, str) and current_id:
            self.model_id = current_id
            self.model_name = self._model_names.get(current_id, current_id)

        available = models.get("availableModels")
        if not isinstance(available, list):
            return
        for item in available:
            if not isinstance(item, dict):
                continue
            item_id = item.get("modelId") or item.get("id")
            if not isinstance(item_id, str) or not item_id:
                continue
            name = item.get("name") or item.get("displayName")
            if isinstance(name, str) and name:
                self._model_names[item_id] = name
        if self.model_id:
            self.model_name = self._model_names.get(self.model_id, self.model_id)

    async def _authenticate_if_required(self, initialize_response: dict[str, Any]) -> None:
        result = initialize_response.get("result")
        if not isinstance(result, dict):
            return
        methods = result.get("authMethods")
        if not isinstance(methods, list) or not methods:
            return

        available = {
            item.get("id")
            for item in methods
            if isinstance(item, dict) and isinstance(item.get("id"), str)
        }
        try:
            user_info_response = await self.request("_codebuddy.ai/getUserInfo", {})
        except AcpRpcError as exc:
            if exc.code != -32601:
                raise
            user_info_response = {}
        user_info_result = user_info_response.get("result")
        if isinstance(user_info_result, dict) and user_info_result.get("userInfo"):
            return

        method_id = self.config.auth_method_id
        if method_id is None:
            choices = ", ".join(sorted(available))
            raise AcpError(
                "CodeBuddy authentication required. Log in with CodeBuddy first, or explicitly "
                f"set auth_method_id to one of: {choices}"
            )
        if method_id not in available:
            choices = ", ".join(sorted(available))
            raise AcpError(
                f"CodeBuddy auth method {method_id!r} is unavailable; choose from: {choices}"
            )
        await self.request("authenticate", {"methodId": method_id})

    async def request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        request_id = self._next_id
        self._next_id += 1
        loop = asyncio.get_running_loop()
        future: asyncio.Future[dict[str, Any]] = loop.create_future()
        self._pending[request_id] = future
        try:
            await self._send(
                {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}
            )
            return await future
        finally:
            self._pending.pop(request_id, None)

    async def notify(self, method: str, params: dict[str, Any]) -> None:
        await self._send({"jsonrpc": "2.0", "method": method, "params": params})

    async def begin_prompt(self, prompt: str) -> None:
        if not self.running or not self.session_id:
            raise AcpError("CodeBuddy ACP session is not running")
        if self.turn_active:
            raise AcpError("a CodeBuddy turn is already active")
        if self.pending_permission is not None:
            raise AcpError("a CodeBuddy permission request is awaiting a response")

        self._turn_buffers.close()
        self._turn_buffers = TurnBuffers(spool_max_size=self.config.max_output)
        self._drain_permission_queue()
        self._turn_task = asyncio.create_task(
            self.request(
                "session/prompt",
                {
                    "sessionId": self.session_id,
                    "prompt": [{"type": "text", "text": prompt}],
                },
            ),
            name=f"codebuddy-prompt-{self.session_id}",
        )

    async def set_model(self, model_id: str) -> None:
        """Switch the model selected for this ACP session.

        CodeBuddy applies this change at session scope and returns the effective
        model ID. The bridge never treats an unacknowledged request as a model
        change, so ACP errors leave the previous model metadata intact.
        """
        if not isinstance(model_id, str) or not model_id.strip():
            raise ValueError("model_id must not be empty")
        if not self.running or not self.session_id:
            raise AcpError("CodeBuddy ACP session is not running")
        if self.turn_active or self.pending_permission is not None:
            raise AcpError("cannot switch model while a CodeBuddy turn is active")

        previous_model_id = self.model_id
        response = await self.request(
            "session/set_model",
            {"sessionId": self.session_id, "modelId": model_id},
        )
        self._update_model_info(response)
        result = response.get("result")
        effective_model_id = result.get("modelId") if isinstance(result, dict) else None
        if isinstance(effective_model_id, str) and effective_model_id:
            self.model_id = effective_model_id
            self.model_name = self._model_names.get(effective_model_id, effective_model_id)
        elif self.model_id is None or self.model_id == previous_model_id:
            raise AcpError(f"CodeBuddy did not return the effective model: {response!r}")

    async def wait_for_turn_event(self, timeout_seconds: float) -> dict[str, Any]:
        turn_task = self._turn_task
        if turn_task is None:
            raise AcpError("no CodeBuddy turn is active")

        permission_task = asyncio.create_task(self._permission_queue.get())
        try:
            done, _ = await asyncio.wait(
                {turn_task, permission_task},
                timeout=timeout_seconds,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if not done:
                raise TimeoutError(f"CodeBuddy turn timed out after {timeout_seconds:g} seconds")
            if turn_task in done:
                permission_task.cancel()
                try:
                    response = await turn_task
                except asyncio.CancelledError:
                    if asyncio.current_task() is not None and asyncio.current_task().cancelling():
                        raise
                    response = {"result": {"stopReason": "cancelled"}}
                if self._turn_task is turn_task:
                    self._turn_task = None
                self.pending_permission = None
                self._stdout_limit_failures = 0
                result = response.get("result", {})
                stop_reason = result.get("stopReason")
                return {
                    "kind": "complete",
                    "result": {
                        "status": "cancelled" if stop_reason == "cancelled" else "completed",
                        "text": self._turn_buffers.text,
                        "stop_reason": stop_reason,
                        "tool_calls": self._turn_buffers.tool_call_summaries(),
                        "codebuddy_session_id": self.session_id,
                    },
                }

            if permission_task in done:
                permission = permission_task.result()
                self.pending_permission = permission
                return {"kind": "permission", "permission": permission}
        finally:
            if not permission_task.done():
                permission_task.cancel()

    async def resolve_permission(self, request_id: str, option_id: str) -> None:
        permission = self.pending_permission
        if permission is None:
            raise AcpError("no CodeBuddy permission request is pending")
        if permission.request_id != request_id:
            raise AcpError("permission request_id does not match the pending request")
        valid_options = {
            option.get("optionId")
            for option in permission.options
            if isinstance(option, dict) and isinstance(option.get("optionId"), str)
        }
        if option_id not in valid_options:
            raise AcpError(f"unknown permission option_id: {option_id}")

        self.pending_permission = None
        await self._send(
            {
                "jsonrpc": "2.0",
                "id": permission.rpc_id,
                "result": {"outcome": {"outcome": "selected", "optionId": option_id}},
            }
        )

    async def cancel_turn(self) -> None:
        async with self._cancel_lock:
            if self.session_id and self.running:
                await self.notify("session/cancel", {"sessionId": self.session_id})
            permission = self.pending_permission
            if permission is not None:
                reject = self.reject_option(permission)
                if reject:
                    await self.resolve_permission(permission.request_id, reject)
                else:
                    # session/cancel terminates the turn even when CodeBuddy did
                    # not offer a reject choice. Do not leave a stale local gate.
                    self.pending_permission = None
            turn_task = self._turn_task
            if turn_task is not None:
                done, _ = await asyncio.wait({turn_task}, timeout=5)
                if not done:
                    turn_task.cancel()
                    with suppress(Exception, asyncio.CancelledError):
                        await turn_task
                else:
                    with suppress(Exception, asyncio.CancelledError):
                        turn_task.result()
                if self._turn_task is turn_task:
                    self._turn_task = None
            self.pending_permission = None
            self._drain_permission_queue()

    @staticmethod
    def reject_option(permission: PermissionRequest) -> str | None:
        for option in permission.options:
            if option.get("kind") == "reject" and isinstance(option.get("optionId"), str):
                return option["optionId"]
        return None

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            if self.running and self.session_id:
                try:
                    await self.cancel_turn()
                except Exception:
                    logger.exception("failed to cancel CodeBuddy turn while closing ACP client")
        finally:
            await self._terminate_process()

            await self._cleanup_remote_process()

            for task in (self._reader_task, self._stderr_task):
                if task is not None and not task.done():
                    task.cancel()
            for future in self._pending.values():
                if not future.done():
                    future.set_exception(AcpError("CodeBuddy ACP client closed"))
            self._pending.clear()
            self._turn_buffers.close()

    async def _terminate_process(self) -> None:
        async with self._terminate_lock:
            process = self.process
            if process is None or process.returncode is not None:
                return
            try:
                if os.name != "nt":
                    os.killpg(process.pid, signal.SIGTERM)
                else:  # pragma: no cover - exercised on Windows
                    process.terminate()
            except (ProcessLookupError, PermissionError):
                with suppress(ProcessLookupError, PermissionError):
                    process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=5)
            except TimeoutError:
                try:
                    if os.name != "nt":
                        os.killpg(process.pid, signal.SIGKILL)
                    else:  # pragma: no cover
                        process.kill()
                except (ProcessLookupError, PermissionError):
                    with suppress(ProcessLookupError, PermissionError):
                        process.kill()
                await process.wait()

    async def _cleanup_remote_process(self) -> None:
        if self.config.launch_mode != "ssh" or not self._remote_pid_file:
            return
        if not self.config.ssh_host:
            return

        pid_name = shlex.quote(self._remote_pid_file)
        cleanup_command = (
            'remote_tmp="${TMPDIR:-/tmp}"; '
            f'pid_file="$remote_tmp"/{pid_name}; '
            'if [ -r "$pid_file" ]; then pid=$(cat "$pid_file"); '
            'case "$pid" in ""|*[!0-9]*) ;; *) '
            'kill -TERM -- "-$pid" 2>/dev/null || kill -TERM "$pid" 2>/dev/null; '
            "sleep 1; "
            'kill -KILL -- "-$pid" 2>/dev/null || kill -KILL "$pid" 2>/dev/null; '
            'esac; fi; rm -f "$pid_file"'
        )
        argv = [
            self.config.ssh_command,
            *self.config.ssh_args,
            "--",
            str(self.config.ssh_host),
            cleanup_command,
        ]
        try:
            cleanup_process = await asyncio.create_subprocess_exec(
                *argv,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
                start_new_session=os.name != "nt",
            )
            try:
                await asyncio.wait_for(cleanup_process.wait(), timeout=10)
            except TimeoutError:
                cleanup_process.kill()
                await cleanup_process.wait()
        except Exception:
            # Process cleanup is best effort when the SSH endpoint is unavailable.
            logger.warning("failed to clean up remote CodeBuddy process", exc_info=True)

    async def _send(self, message: dict[str, Any]) -> None:
        process = self.process
        if process is None or process.stdin is None or process.returncode is not None:
            raise AcpError(self._exit_message("CodeBuddy process is not running"))
        data = json.dumps(message, ensure_ascii=False, separators=(",", ":")) + "\n"
        async with self._write_lock:
            try:
                process.stdin.write(data.encode())
                await process.stdin.drain()
            except (BrokenPipeError, ConnectionResetError) as exc:
                raise AcpError(self._exit_message("CodeBuddy ACP stdin closed")) from exc

    async def _read_stdout(self) -> None:
        assert self.process is not None and self.process.stdout is not None
        try:
            while True:
                line, overflowed = await _readline_discarding_overflow(self.process.stdout)
                if overflowed:
                    self._stdout_limit_failures += 1
                    if self._stdout_limit_failures == 1:
                        failure = AcpError(
                            f"ACP stdout line exceeded max_read={self.config.max_read} bytes; "
                            "the current response was discarded and its turn was cancelled. "
                            "Create a new bridge session with a larger max_read, or retry this "
                            "session and ask CodeBuddy to compress the answer"
                        )
                    else:
                        failure = AcpError(
                            f"ACP stdout repeatedly exceeded max_read={self.config.max_read} "
                            "bytes; the CodeBuddy process was terminated. Create a new bridge "
                            "session with a larger max_read"
                        )
                    self._fail_pending(failure)
                    self.pending_permission = None
                    self._drain_permission_queue()
                    if self._stdout_limit_failures == 1:
                        logger.error("%s; keeping CodeBuddy available for one retry", failure)
                        if self.session_id and self.running:
                            try:
                                await self.notify("session/cancel", {"sessionId": self.session_id})
                            except Exception:
                                logger.warning(
                                    "failed to notify CodeBuddy after oversized stdout",
                                    exc_info=True,
                                )
                        continue
                    logger.error("%s", failure)
                    await self._terminate_process()
                    await self._cleanup_remote_process()
                    return
                if not line:
                    break
                try:
                    message = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise AcpError(f"invalid ACP JSON from CodeBuddy: {line[:200]!r}") from exc
                await self._dispatch(message)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error("ACP stdout reader failed; terminating CodeBuddy", exc_info=True)
            self._fail_pending(exc)
            await self._terminate_process()
            await self._cleanup_remote_process()
        else:
            self._fail_pending(AcpError(self._exit_message("CodeBuddy ACP stdout closed")))

    async def _read_stderr(self) -> None:
        assert self.process is not None and self.process.stderr is not None
        try:
            while True:
                line, overflowed = await _readline_discarding_overflow(self.process.stderr)
                if overflowed:
                    marker = (
                        f"[discarded stderr line exceeding max_read={self.config.max_read} bytes]"
                    )
                    self._stderr_tail.append(marker)
                    logger.warning(marker)
                    continue
                if not line:
                    break
                self._stderr_tail.append(line.decode(errors="replace").rstrip())
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error("ACP stderr reader failed; terminating CodeBuddy", exc_info=True)
            self._fail_pending(exc)
            await self._terminate_process()
            await self._cleanup_remote_process()

    async def _dispatch(self, message: Any) -> None:
        if not isinstance(message, dict):
            return
        if "id" in message and ("result" in message or "error" in message):
            future = self._pending.get(message["id"])
            if future is not None and not future.done():
                if "error" in message:
                    future.set_exception(AcpRpcError(message["error"]))
                else:
                    future.set_result(message)
            return

        method = message.get("method")
        if "id" in message and isinstance(method, str):
            if method == "session/request_permission":
                permission = self._parse_permission(message)
                await self._permission_queue.put(permission)
            else:
                await self._send(
                    {
                        "jsonrpc": "2.0",
                        "id": message["id"],
                        "error": {
                            "code": -32601,
                            "message": f"Unsupported ACP client method: {method}",
                        },
                    }
                )
            return

        if method == "session/update":
            self._record_update(message.get("params", {}).get("update"))

    def _parse_permission(self, message: dict[str, Any]) -> PermissionRequest:
        params = message.get("params") or {}
        tool_call = params.get("toolCall") or {}
        meta = params.get("_meta") or {}
        request_id = meta.get("codebuddy.ai/requestId") or str(message["id"])
        tool_meta = tool_call.get("_meta") or {}
        tool_name = tool_meta.get("codebuddy.ai/toolName") or tool_call.get("title") or "unknown"
        raw_input = tool_call.get("rawInput")
        return PermissionRequest(
            rpc_id=message["id"],
            request_id=str(request_id),
            session_id=str(params.get("sessionId") or self.session_id or ""),
            tool_name=str(tool_name),
            raw_input=raw_input if isinstance(raw_input, dict) else {},
            options=params.get("options") if isinstance(params.get("options"), list) else [],
            meta={**tool_meta, **meta},
        )

    def _record_update(self, update: Any) -> None:
        if not isinstance(update, dict):
            return
        kind = update.get("sessionUpdate")
        if kind == "agent_message_chunk":
            content = update.get("content")
            if isinstance(content, dict) and content.get("type") == "text":
                text = content.get("text")
                if isinstance(text, str):
                    self._turn_buffers.append_text(text)
            return

        if kind == "model_update":
            self._update_model_info({"result": {"models": update}})
            return

        if kind in {"tool_call", "tool_call_update"}:
            call_id = update.get("toolCallId")
            if not isinstance(call_id, str):
                return
            summary = self._turn_buffers.tool_calls.setdefault(call_id, {"tool_call_id": call_id})
            if isinstance(update.get("title"), str):
                summary["title"] = update["title"]
            if isinstance(update.get("status"), str):
                summary["status"] = update["status"]
            if isinstance(update.get("rawInput"), dict):
                summary["raw_input"] = update["rawInput"]

    def _fail_pending(self, exc: BaseException) -> None:
        for future in self._pending.values():
            if not future.done():
                future.set_exception(exc)

    def _drain_permission_queue(self) -> None:
        while not self._permission_queue.empty():
            try:
                self._permission_queue.get_nowait()
            except asyncio.QueueEmpty:  # pragma: no cover
                break

    def _exit_message(self, prefix: str) -> str:
        returncode = self.process.returncode if self.process else None
        detail = f"{prefix} (exit code: {returncode})"
        if self.stderr_tail:
            detail += f"\nstderr tail:\n{self.stderr_tail}"
        return detail
