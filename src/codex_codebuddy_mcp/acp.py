from __future__ import annotations

import asyncio
import json
import os
import re
import shlex
import signal
from collections import deque
from contextlib import suppress
from pathlib import Path
from typing import Any

from .models import PermissionRequest, SessionConfig, TurnBuffers


class AcpError(RuntimeError):
    """ACP transport or remote error."""


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
}


def validate_config(config: SessionConfig) -> None:
    if not config.cwd:
        raise ValueError("cwd must not be empty")
    if config.launch_mode == "ssh" and not config.ssh_host:
        raise ValueError("ssh_host is required for SSH launch mode")
    if config.startup_timeout_seconds <= 0:
        raise ValueError("startup_timeout_seconds must be positive")
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
    remote_command = f"cd {shlex.quote(config.cwd)} && exec {shlex.join(remote_parts)}"
    argv = [config.ssh_command, *config.ssh_args, "--", str(config.ssh_host), remote_command]
    return argv, None, os.environ.copy()


class AcpClient:
    def __init__(self, config: SessionConfig) -> None:
        self.config = config
        self.process: asyncio.subprocess.Process | None = None
        self.session_id: str | None = None
        self._next_id = 1
        self._pending: dict[int | str, asyncio.Future[dict[str, Any]]] = {}
        self._write_lock = asyncio.Lock()
        self._reader_task: asyncio.Task[None] | None = None
        self._stderr_task: asyncio.Task[None] | None = None
        self._permission_queue: asyncio.Queue[PermissionRequest] = asyncio.Queue()
        self._stderr_tail: deque[str] = deque(maxlen=80)
        self._turn_buffers = TurnBuffers()
        self._turn_task: asyncio.Task[dict[str, Any]] | None = None
        self.pending_permission: PermissionRequest | None = None
        self._closed = False

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

        argv, cwd, env = build_launch_argv(self.config)
        try:
            self.process = await asyncio.create_subprocess_exec(
                *argv,
                cwd=cwd,
                env=env,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
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
        except BaseException:
            await self.close()
            raise

        result = response.get("result", {})
        session_id = result.get("sessionId")
        if not isinstance(session_id, str) or not session_id:
            await self.close()
            raise AcpError(f"CodeBuddy did not return a sessionId: {response!r}")
        self.session_id = session_id

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
        user_info_response = await self.request("_codebuddy.ai/getUserInfo", {})
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

        self._turn_buffers = TurnBuffers()
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

    async def wait_for_turn_event(self, timeout_seconds: float) -> dict[str, Any]:
        if self._turn_task is None:
            raise AcpError("no CodeBuddy turn is active")

        permission_task = asyncio.create_task(self._permission_queue.get())
        try:
            done, _ = await asyncio.wait(
                {self._turn_task, permission_task},
                timeout=timeout_seconds,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if not done:
                raise TimeoutError(f"CodeBuddy turn timed out after {timeout_seconds:g} seconds")
            if permission_task in done:
                permission = permission_task.result()
                self.pending_permission = permission
                return {"kind": "permission", "permission": permission}

            permission_task.cancel()
            response = await self._turn_task
            self._turn_task = None
            result = response.get("result", {})
            return {
                "kind": "complete",
                "result": {
                    "status": "completed",
                    "text": self._turn_buffers.text,
                    "stop_reason": result.get("stopReason"),
                    "tool_calls": self._turn_buffers.tool_call_summaries(),
                    "codebuddy_session_id": self.session_id,
                },
            }
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
        if self.session_id and self.running:
            await self.notify("session/cancel", {"sessionId": self.session_id})
        if self.pending_permission is not None:
            reject = self.reject_option(self.pending_permission)
            if reject:
                await self.resolve_permission(self.pending_permission.request_id, reject)
        if self._turn_task is not None:
            done, _ = await asyncio.wait({self._turn_task}, timeout=5)
            if not done:
                self._turn_task.cancel()
            else:
                with suppress(Exception, asyncio.CancelledError):
                    self._turn_task.result()
            self._turn_task = None

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
                    pass
        finally:
            process = self.process
            if process and process.returncode is None:
                try:
                    if os.name != "nt":
                        os.killpg(process.pid, signal.SIGTERM)
                    else:  # pragma: no cover - exercised on Windows
                        process.terminate()
                except ProcessLookupError:
                    pass
                try:
                    await asyncio.wait_for(process.wait(), timeout=5)
                except TimeoutError:
                    try:
                        if os.name != "nt":
                            os.killpg(process.pid, signal.SIGKILL)
                        else:  # pragma: no cover
                            process.kill()
                    except ProcessLookupError:
                        pass
                    await process.wait()

            for task in (self._reader_task, self._stderr_task):
                if task is not None and not task.done():
                    task.cancel()
            for future in self._pending.values():
                if not future.done():
                    future.set_exception(AcpError("CodeBuddy ACP client closed"))
            self._pending.clear()

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
            while line := await self.process.stdout.readline():
                try:
                    message = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise AcpError(f"invalid ACP JSON from CodeBuddy: {line[:200]!r}") from exc
                await self._dispatch(message)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._fail_pending(exc)
        else:
            self._fail_pending(AcpError(self._exit_message("CodeBuddy ACP stdout closed")))

    async def _read_stderr(self) -> None:
        assert self.process is not None and self.process.stderr is not None
        try:
            while line := await self.process.stderr.readline():
                self._stderr_tail.append(line.decode(errors="replace").rstrip())
        except asyncio.CancelledError:
            raise

    async def _dispatch(self, message: Any) -> None:
        if not isinstance(message, dict):
            return
        if "id" in message and ("result" in message or "error" in message):
            future = self._pending.get(message["id"])
            if future is not None and not future.done():
                if "error" in message:
                    future.set_exception(AcpError(f"ACP request failed: {message['error']!r}"))
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
                    self._turn_buffers.text_parts.append(text)
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


def is_executable_file(path: str) -> bool:
    candidate = Path(path)
    return candidate.is_file() and os.access(candidate, os.X_OK)
