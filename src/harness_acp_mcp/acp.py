from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import sys
import tempfile
import uuid
from collections import deque
from contextlib import suppress
from pathlib import Path
from typing import Any

from .adapters import AuthStatusUnsupported, get_adapter
from .models import AuthInfo, InteractionRequest, SessionConfig, TurnBuffers
from .supervisor import SPEC_ENV

logger = logging.getLogger(__name__)


class AcpError(RuntimeError):
    """ACP transport, lifecycle, or peer error."""


class AcpRpcError(AcpError):
    def __init__(self, error: Any) -> None:
        self.error = error
        self.code = error.get("code") if isinstance(error, dict) else None
        super().__init__(f"ACP request failed: {error!r}")


async def _readline_discarding_overflow(
    reader: asyncio.StreamReader,
) -> tuple[bytes, bool]:
    overflowed = False
    while True:
        try:
            line = await reader.readuntil(b"\n")
        except asyncio.IncompleteReadError as exc:
            return (b"" if overflowed else exc.partial), overflowed
        except asyncio.LimitOverrunError as exc:
            overflowed = True
            await reader.readexactly(exc.consumed or 1)
            continue
        return (b"" if overflowed else line), overflowed


class AcpClient:
    def __init__(self, config: SessionConfig) -> None:
        self.config = config
        self.adapter = get_adapter(config.harness)
        self.adapter.validate(config)
        self.process: asyncio.subprocess.Process | None = None
        self.initialize_response: dict[str, Any] = {}
        self.session_id: str | None = None
        self.model_id: str | None = None
        self.model_name: str | None = None
        self.auth_info: AuthInfo | None = None
        self._model_names: dict[str, str] = {}
        self._last_auth_status: dict[str, Any] | None = None
        self._next_id = 1
        self._pending: dict[int | str, asyncio.Future[dict[str, Any]]] = {}
        self._write_lock = asyncio.Lock()
        self._cancel_lock = asyncio.Lock()
        self._terminate_lock = asyncio.Lock()
        self._reader_task: asyncio.Task[None] | None = None
        self._stderr_task: asyncio.Task[None] | None = None
        self._interaction_queue: asyncio.Queue[InteractionRequest] = asyncio.Queue()
        self._stderr_tail: deque[str] = deque(maxlen=config.stderr_tail_lines)
        self._turn_buffers = TurnBuffers(spool_max_size=config.max_output_bytes)
        self._turn_task: asyncio.Task[dict[str, Any]] | None = None
        self.pending_interaction: InteractionRequest | None = None
        self._stdout_limit_failures = 0
        self._closed = False
        self._remote_pid_file = (
            f"harness-acp-{uuid.uuid4().hex}.pid" if config.launch_mode == "ssh" else None
        )
        descriptor, metadata_path = tempfile.mkstemp(prefix="harness-acp-meta-", suffix=".json")
        os.close(descriptor)
        os.chmod(metadata_path, 0o600)
        self._metadata_path = metadata_path

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

    @property
    def process_info(self) -> dict[str, Any]:
        try:
            raw = Path(self._metadata_path).read_text(encoding="utf-8")
            return json.loads(raw) if raw else {}
        except (OSError, json.JSONDecodeError):
            return {}

    def _supervisor_spec(self) -> dict[str, Any]:
        return {
            "launch_mode": self.config.launch_mode,
            "argv": self.adapter.build_argv(self.config),
            "cwd": self.config.cwd,
            "harness_env": self.config.env,
            "ssh_host": self.config.ssh_host,
            "ssh_command": self.config.ssh_command,
            "ssh_args": self.config.ssh_args,
            "remote_pid_file": self._remote_pid_file,
            "terminate_grace_seconds": self.config.terminate_grace_seconds,
            "remote_cleanup_timeout_seconds": self.config.remote_cleanup_timeout_seconds,
            "metadata_path": self._metadata_path,
        }

    async def start_transport(self) -> dict[str, Any]:
        if self.running:
            return self.initialize_response
        if self._closed:
            raise AcpError("ACP client is closed")
        env = os.environ.copy()
        env[SPEC_ENV] = json.dumps(self._supervisor_spec(), separators=(",", ":"))
        try:
            self.process = await asyncio.create_subprocess_exec(
                sys.executable,
                "-m",
                "harness_acp_mcp.supervisor",
                env=env,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                limit=self.config.max_read_bytes,
                start_new_session=os.name != "nt",
            )
        except (OSError, ValueError) as exc:
            raise AcpError(f"failed to launch harness supervisor: {exc}") from exc

        self._reader_task = asyncio.create_task(
            self._read_stdout(), name=f"{self.config.harness}-acp-stdout"
        )
        self._stderr_task = asyncio.create_task(
            self._read_stderr(), name=f"{self.config.harness}-acp-stderr"
        )
        try:
            async with asyncio.timeout(self.config.startup_timeout_seconds):
                self.initialize_response = await self.request(
                    "initialize",
                    {
                        "protocolVersion": 1,
                        "clientCapabilities": {
                            "fs": {"readTextFile": False, "writeTextFile": False},
                            "terminal": False,
                            "session": {"configOptions": {"boolean": {}}},
                        },
                        "clientInfo": {
                            "name": "harness-acp-mcp",
                            "title": "Harness ACP MCP Bridge",
                            "version": "0.2.0",
                        },
                    },
                )
                self._update_model_info(self.initialize_response)
        except BaseException:
            await self.close()
            raise
        return self.initialize_response

    def auth_methods(self) -> list[dict[str, Any]]:
        result = self.initialize_response.get("result")
        methods = result.get("authMethods") if isinstance(result, dict) else None
        if not isinstance(methods, list):
            return []
        return [item for item in methods if isinstance(item, dict)]

    async def get_auth_info(self) -> AuthInfo:
        if not self.running:
            raise AcpError("harness ACP transport is not running")
        if not self.auth_methods():
            self.auth_info = AuthInfo(authenticated=True, methods=[])
            return self.auth_info
        try:
            self.auth_info = await self.adapter.get_auth_info(self, self.initialize_response)
        except AuthStatusUnsupported:
            if self.session_id:
                self.auth_info = AuthInfo(authenticated=True, methods=self.auth_methods())
            else:
                raise
        return self.auth_info

    async def authenticate(self, method_id: str) -> AuthInfo:
        available = {
            item.get("id")
            for item in self.auth_methods()
            if isinstance(item.get("id"), str)
        }
        if method_id not in available:
            choices = ", ".join(sorted(available))
            raise AcpError(f"authentication method {method_id!r} is unavailable: {choices}")
        try:
            self.auth_info = await self.adapter.authenticate(self, method_id)
        except AuthStatusUnsupported:
            self.auth_info = AuthInfo(authenticated=True, methods=self.auth_methods())
        return self.auth_info

    async def open_session(self) -> None:
        if self.session_id:
            return
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
        result = response.get("result")
        session_id = result.get("sessionId") if isinstance(result, dict) else None
        if self.config.resume_session_id and not session_id:
            session_id = self.config.resume_session_id
        if not isinstance(session_id, str) or not session_id:
            raise AcpError(f"harness did not return a sessionId: {response!r}")
        self.session_id = session_id
        if self.config.harness != "codebuddy":
            await self.set_model(self.config.model_id)
        elif self.model_id is None:
            self.model_id = self.config.model_id
            self.model_name = self._model_names.get(self.model_id, self.model_id)

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
        if isinstance(available, list):
            for item in available:
                if not isinstance(item, dict):
                    continue
                item_id = item.get("modelId") or item.get("id")
                name = item.get("name") or item.get("displayName")
                if isinstance(item_id, str) and item_id:
                    self._model_names[item_id] = name if isinstance(name, str) else item_id
        if self.model_id:
            self.model_name = self._model_names.get(self.model_id, self.model_id)

    async def request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        request_id = self._next_id
        self._next_id += 1
        future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
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
            raise AcpError("harness ACP session is not ready")
        if self.turn_active:
            raise AcpError("a harness turn is already active")
        if self.pending_interaction is not None:
            raise AcpError("an interaction is awaiting a response")
        self._turn_buffers.close()
        self._turn_buffers = TurnBuffers(spool_max_size=self.config.max_output_bytes)
        self._drain_interaction_queue()
        self._turn_task = asyncio.create_task(
            self.request(
                "session/prompt",
                {
                    "sessionId": self.session_id,
                    "prompt": [{"type": "text", "text": prompt}],
                },
            ),
            name=f"harness-prompt-{self.session_id}",
        )

    async def set_model(self, model_id: str) -> None:
        if not isinstance(model_id, str) or not model_id.strip():
            raise ValueError("model_id must not be empty")
        if not self.running or not self.session_id:
            raise AcpError("harness ACP session is not ready")
        if self.turn_active or self.pending_interaction is not None:
            raise AcpError("cannot switch model while a turn is active")
        previous_model_id = self.model_id
        await self.adapter.set_model(self, model_id.strip())
        self.model_id = model_id.strip()
        self.model_name = self._model_names.get(self.model_id, self.model_id)
        if not self.model_id:
            self.model_id = previous_model_id

    async def wait_for_turn_event(self, timeout_seconds: float) -> dict[str, Any]:
        turn_task = self._turn_task
        if turn_task is None:
            raise AcpError("no harness turn is active")
        interaction_task = asyncio.create_task(self._interaction_queue.get())
        try:
            done, _ = await asyncio.wait(
                {turn_task, interaction_task},
                timeout=timeout_seconds,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if not done:
                raise TimeoutError(f"harness turn timed out after {timeout_seconds:g} seconds")
            if turn_task in done:
                interaction_task.cancel()
                try:
                    response = await turn_task
                except asyncio.CancelledError:
                    if asyncio.current_task() is not None and asyncio.current_task().cancelling():
                        raise
                    response = {"result": {"stopReason": "cancelled"}}
                if self._turn_task is turn_task:
                    self._turn_task = None
                self.pending_interaction = None
                self._stdout_limit_failures = 0
                result = response.get("result", {})
                stop_reason = result.get("stopReason") if isinstance(result, dict) else None
                return {
                    "kind": "complete",
                    "result": {
                        "status": "cancelled" if stop_reason == "cancelled" else "completed",
                        "text": self._turn_buffers.text,
                        "stop_reason": stop_reason,
                        "tool_calls": self._turn_buffers.tool_call_summaries(),
                        "harness_session_id": self.session_id,
                    },
                }
            interaction = interaction_task.result()
            self.pending_interaction = interaction
            return {"kind": "interaction", "interaction": interaction}
        finally:
            if not interaction_task.done():
                interaction_task.cancel()

    async def wait_for_operation_event(self, task: asyncio.Task[Any]) -> dict[str, Any]:
        interaction_task = asyncio.create_task(self._interaction_queue.get())
        try:
            done, _ = await asyncio.wait(
                {task, interaction_task}, return_when=asyncio.FIRST_COMPLETED
            )
            if task in done:
                interaction_task.cancel()
                return {"kind": "complete", "result": await asyncio.shield(task)}
            interaction = interaction_task.result()
            self.pending_interaction = interaction
            return {"kind": "interaction", "interaction": interaction}
        finally:
            if not interaction_task.done():
                interaction_task.cancel()

    async def respond_interaction(self, request_id: str, response: Any) -> None:
        interaction = self.pending_interaction
        if interaction is None:
            raise AcpError("no interaction is pending")
        if interaction.request_id != request_id:
            raise AcpError("request_id does not match the pending interaction")
        if interaction.kind == "permission":
            if not isinstance(response, dict) or not isinstance(response.get("option_id"), str):
                raise AcpError("permission response requires option_id")
            option_id = response["option_id"]
            valid = {
                item.get("optionId")
                for item in interaction.options
                if isinstance(item, dict) and isinstance(item.get("optionId"), str)
            }
            if option_id not in valid:
                raise AcpError(f"unknown permission option_id: {option_id}")
            result = {"outcome": {"outcome": "selected", "optionId": option_id}}
        else:
            if not isinstance(response, dict):
                raise AcpError("information response must be an object")
            if interaction.response_style == "elicitation":
                result = {"action": "accept", "content": response}
            else:
                result = {"content": response}
        self.pending_interaction = None
        await self._send({"jsonrpc": "2.0", "id": interaction.rpc_id, "result": result})

    async def cancel_turn(self) -> None:
        async with self._cancel_lock:
            interaction = self.pending_interaction
            if interaction is not None:
                if interaction.kind == "permission":
                    reject = self.reject_option(interaction)
                    if reject:
                        with suppress(Exception):
                            await self.respond_interaction(
                                interaction.request_id, {"option_id": reject}
                            )
                elif interaction.response_style == "elicitation":
                    self.pending_interaction = None
                    with suppress(Exception):
                        await self._send(
                            {
                                "jsonrpc": "2.0",
                                "id": interaction.rpc_id,
                                "result": {"action": "cancel"},
                            }
                        )
                self.pending_interaction = None
            if self.session_id and self.running:
                with suppress(Exception):
                    await self.notify("session/cancel", {"sessionId": self.session_id})
            turn_task = self._turn_task
            if turn_task is not None:
                done, _ = await asyncio.wait(
                    {turn_task}, timeout=self.config.turn_cancel_timeout_seconds
                )
                if not done:
                    turn_task.cancel()
                with suppress(Exception, asyncio.CancelledError):
                    await turn_task
                if self._turn_task is turn_task:
                    self._turn_task = None
            self.pending_interaction = None
            self._drain_interaction_queue()

    @staticmethod
    def reject_option(interaction: InteractionRequest) -> str | None:
        for option in interaction.options:
            kind = str(option.get("kind", "")).lower()
            name = str(option.get("name", "")).lower()
            option_id = option.get("optionId")
            if (
                isinstance(option_id, str)
                and (kind.startswith("reject") or "deny" in name or "reject" in option_id.lower())
            ):
                return option["optionId"]
        return None

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self.running and self.session_id:
            with suppress(Exception):
                await self.cancel_turn()
        await self._terminate_supervisor()
        for task in (self._reader_task, self._stderr_task):
            if task is not None and not task.done():
                task.cancel()
        self._fail_pending(AcpError("harness ACP client closed"))
        self._pending.clear()
        self._turn_buffers.close()
        with suppress(FileNotFoundError):
            os.unlink(self._metadata_path)

    async def _terminate_supervisor(self) -> None:
        async with self._terminate_lock:
            process = self.process
            if process is None or process.returncode is not None:
                return
            if process.stdin is not None:
                process.stdin.close()
                with suppress(Exception):
                    await process.stdin.wait_closed()
            timeout = (
                self.config.terminate_grace_seconds
                + self.config.remote_cleanup_timeout_seconds
            )
            try:
                await asyncio.wait_for(process.wait(), timeout=timeout)
                return
            except TimeoutError:
                pass
            with suppress(ProcessLookupError):
                process.send_signal(signal.SIGTERM)
            try:
                await asyncio.wait_for(process.wait(), timeout=self.config.terminate_grace_seconds)
            except TimeoutError:
                process.kill()
                await process.wait()

    async def _send(self, message: dict[str, Any]) -> None:
        process = self.process
        if process is None or process.stdin is None or process.returncode is not None:
            raise AcpError(self._exit_message("harness process is not running"))
        data = json.dumps(message, ensure_ascii=False, separators=(",", ":")) + "\n"
        async with self._write_lock:
            try:
                process.stdin.write(data.encode())
                await process.stdin.drain()
            except (BrokenPipeError, ConnectionResetError) as exc:
                raise AcpError(self._exit_message("harness ACP stdin closed")) from exc

    async def _read_stdout(self) -> None:
        assert self.process is not None and self.process.stdout is not None
        try:
            while True:
                line, overflowed = await _readline_discarding_overflow(self.process.stdout)
                if overflowed:
                    self._stdout_limit_failures += 1
                    tolerated = (
                        self._stdout_limit_failures
                        <= self.config.stdout_overflow_retry_tolerance
                    )
                    if tolerated:
                        failure = AcpError(
                            "ACP stdout line exceeded "
                            f"max_read_bytes={self.config.max_read_bytes}; the response was "
                            "discarded and the turn was cancelled"
                        )
                    else:
                        failure = AcpError(
                            "ACP stdout repeatedly exceeded "
                            f"max_read_bytes={self.config.max_read_bytes}; the process was closed"
                        )
                    self._fail_pending(failure)
                    self.pending_interaction = None
                    self._drain_interaction_queue()
                    if tolerated:
                        if self.session_id and self.running:
                            with suppress(Exception):
                                await self.notify("session/cancel", {"sessionId": self.session_id})
                        continue
                    await self._terminate_supervisor()
                    return
                if not line:
                    break
                try:
                    message = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise AcpError(f"invalid ACP JSON: {line[:200]!r}") from exc
                await self._dispatch(message)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error("ACP stdout reader failed", exc_info=True)
            self._fail_pending(exc)
            await self._terminate_supervisor()
        else:
            self._fail_pending(AcpError(self._exit_message("harness ACP stdout closed")))

    async def _read_stderr(self) -> None:
        assert self.process is not None and self.process.stderr is not None
        try:
            while True:
                line, overflowed = await _readline_discarding_overflow(self.process.stderr)
                if overflowed:
                    self._stderr_tail.append(
                        "[discarded stderr line exceeding configured read limit]"
                    )
                    continue
                if not line:
                    break
                self._stderr_tail.append(line.decode(errors="replace").rstrip())
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error("ACP stderr reader failed", exc_info=True)
            self._fail_pending(exc)

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
                await self._interaction_queue.put(self._parse_permission(message))
            elif method in {
                "session/request_input",
                "session/request_information",
                "session/request_user_input",
                "elicitation/create",
            }:
                await self._interaction_queue.put(self._parse_information(message))
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
        elif method == "_auth/status_update":
            params = message.get("params")
            if isinstance(params, dict):
                self._last_auth_status = params.get("authStatus")

    def _parse_permission(self, message: dict[str, Any]) -> InteractionRequest:
        params = message.get("params") or {}
        tool_call = params.get("toolCall") or {}
        meta = params.get("_meta") or {}
        tool_meta = tool_call.get("_meta") or {}
        title = tool_meta.get("codebuddy.ai/toolName") or tool_call.get("title") or "Permission"
        raw_input = tool_call.get("rawInput")
        return InteractionRequest(
            rpc_id=message["id"],
            request_id=str(meta.get("codebuddy.ai/requestId") or message["id"]),
            kind="permission",
            session_id=str(params.get("sessionId") or self.session_id or ""),
            title=str(title),
            message=str(params.get("message") or f"Permission requested for {title}"),
            options=params.get("options") if isinstance(params.get("options"), list) else [],
            raw_input=raw_input if isinstance(raw_input, dict) else {},
            meta={**tool_meta, **meta},
        )

    def _parse_information(self, message: dict[str, Any]) -> InteractionRequest:
        params = message.get("params") or {}
        schema = params.get("requestedSchema") or params.get("schema")
        response_style = (
            "elicitation" if message.get("method") == "elicitation/create" else "content"
        )
        return InteractionRequest(
            rpc_id=message["id"],
            request_id=str(params.get("requestId") or message["id"]),
            kind="information",
            session_id=str(params.get("sessionId") or self.session_id or ""),
            title=str(params.get("title") or "Information requested"),
            message=str(params.get("message") or params.get("prompt") or "Provide information"),
            schema=schema if isinstance(schema, dict) else {"type": "object"},
            defaults=params.get("defaults") if isinstance(params.get("defaults"), dict) else None,
            response_style=response_style,
            meta=params.get("_meta") if isinstance(params.get("_meta"), dict) else {},
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
        if kind in {"model_update", "config_option_update"}:
            self._update_model_info({"result": {"models": update}})
            return
        if kind in {"tool_call", "tool_call_update"}:
            call_id = update.get("toolCallId")
            if not isinstance(call_id, str):
                return
            summary = self._turn_buffers.tool_calls.setdefault(call_id, {"tool_call_id": call_id})
            for source, target in (
                ("title", "title"),
                ("status", "status"),
                ("rawInput", "raw_input"),
            ):
                if source in update:
                    summary[target] = update[source]

    def _fail_pending(self, exc: BaseException) -> None:
        for future in self._pending.values():
            if not future.done():
                future.set_exception(exc)

    def _drain_interaction_queue(self) -> None:
        while not self._interaction_queue.empty():
            with suppress(asyncio.QueueEmpty):
                self._interaction_queue.get_nowait()

    def _exit_message(self, prefix: str) -> str:
        returncode = self.process.returncode if self.process else None
        detail = f"{prefix} (exit code: {returncode})"
        if self.stderr_tail:
            detail += f"\nstderr tail:\n{self.stderr_tail}"
        return detail
