from __future__ import annotations

import argparse
import asyncio
import fcntl
import json
import logging
import logging.handlers
import os
import signal
import tempfile
import time
from contextlib import suppress
from pathlib import Path
from typing import Any

from .acp import AcpError, AcpRpcError
from .adapters import AuthStatusUnsupported, default_command
from .bridge import SessionRegistry, interaction_result
from .config import Settings, load_settings
from .models import SessionConfig
from .persistence import SessionRecordStore

logger = logging.getLogger(__name__)


class HarnessDaemon:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.registry = SessionRegistry(settings)
        self.session_records = SessionRecordStore(settings.persistence.sessions_path)
        self.server: asyncio.AbstractServer | None = None
        self._reaper: asyncio.Task[None] | None = None

    async def start(self) -> None:
        socket_path = Path(self.settings.ipc.socket_path)
        socket_path.parent.mkdir(parents=True, exist_ok=True)
        os.chmod(socket_path.parent, 0o700)
        with suppress(FileNotFoundError):
            os.unlink(socket_path)
        self.server = await asyncio.start_unix_server(
            self._handle_connection,
            path=str(socket_path),
            limit=self.settings.buffers.max_read_bytes,
        )
        os.chmod(socket_path, 0o600)
        self._reaper = asyncio.create_task(self._reap_loop(), name="session-reaper")

    async def close(self) -> None:
        if self._reaper is not None:
            self._reaper.cancel()
            with suppress(asyncio.CancelledError):
                await self._reaper
        if self.server is not None:
            self.server.close()
            await self.server.wait_closed()
        await self.registry.close_all()
        with suppress(FileNotFoundError):
            os.unlink(self.settings.ipc.socket_path)

    async def _reap_loop(self) -> None:
        while True:
            await asyncio.sleep(self.settings.daemon.reap_interval_seconds)
            reaped = await self.registry.reap_idle()
            if reaped:
                logger.info("reaped %d idle sessions", reaped)

    async def _handle_connection(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        try:
            raw = await reader.readline()
            if not raw:
                return
            request = json.loads(raw)
            request_id = request.get("id") if isinstance(request, dict) else None
            if not isinstance(request, dict) or not isinstance(request.get("method"), str):
                raise ValueError("invalid daemon request")
            result = await self.dispatch(request["method"], request.get("params") or {})
            response = {"jsonrpc": "2.0", "id": request_id, "result": result}
        except BaseException as exc:
            if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                raise
            logger.warning("daemon request failed: %s", type(exc).__name__)
            response = {
                "jsonrpc": "2.0",
                "id": locals().get("request_id"),
                "error": {
                    "code": getattr(exc, "code", -32000),
                    "message": str(exc),
                    "type": type(exc).__name__,
                },
            }
        writer.write(json.dumps(response, ensure_ascii=False, separators=(",", ":")).encode())
        writer.write(b"\n")
        with suppress(BrokenPipeError, ConnectionResetError):
            await writer.drain()
        writer.close()
        with suppress(Exception):
            await writer.wait_closed()

    async def dispatch(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        handlers = {
            "ping": self._ping,
            "create_session": self._create_session,
            "authenticate": self._authenticate,
            "get_user_info": self._get_user_info,
            "set_model": self._set_model,
            "prompt": self._prompt,
            "respond_interaction": self._respond_interaction,
            "cancel_turn": self._cancel_turn,
            "close_session": self._close_session,
        }
        try:
            handler = handlers[method]
        except KeyError as exc:
            raise ValueError(f"unsupported daemon method: {method}") from exc
        return await handler(params)

    async def _ping(self, _params: dict[str, Any]) -> dict[str, Any]:
        return {
            "status": "ok",
            "pid": os.getpid(),
            "config_fingerprint": self.settings.fingerprint,
        }

    def _session_config(self, params: dict[str, Any]) -> SessionConfig:
        harness = params.get("harness")
        if harness not in {"codebuddy", "agy", "codex"}:
            raise ValueError("harness must be codebuddy, agy, or codex")
        command = params.get("command") or default_command(harness)
        model_id = params.get("model_id")
        if not isinstance(model_id, str) or not model_id.strip():
            raise ValueError("model_id must not be empty")
        cwd = params.get("cwd")
        if not isinstance(cwd, str) or not cwd.strip():
            raise ValueError("cwd must not be empty")
        process = self.settings.process
        buffers = self.settings.buffers
        return SessionConfig(
            harness=harness,
            launch_mode=params.get("launch_mode", "local"),
            cwd=cwd.strip(),
            model_id=model_id.strip(),
            command=str(command),
            args=list(params.get("args") or []),
            env=dict(params.get("env") or {}),
            ssh_host=params.get("ssh_host"),
            ssh_command=str(params.get("ssh_command") or "ssh"),
            ssh_args=list(params.get("ssh_args") or []),
            resume_session_id=params.get("resume_session_id"),
            harness_options=dict(params.get("harness_options") or {}),
            startup_timeout_seconds=float(
                params.get("startup_timeout_seconds", process.startup_timeout_seconds)
            ),
            auth_timeout_seconds=float(
                params.get("auth_timeout_seconds", self.settings.authentication.timeout_seconds)
            ),
            turn_cancel_timeout_seconds=process.turn_cancel_timeout_seconds,
            terminate_grace_seconds=process.terminate_grace_seconds,
            remote_cleanup_timeout_seconds=process.remote_cleanup_timeout_seconds,
            stdout_overflow_retry_tolerance=buffers.stdout_overflow_retry_tolerance,
            stderr_tail_lines=buffers.stderr_tail_lines,
            max_read_bytes=int(params.get("max_read_bytes", buffers.max_read_bytes)),
            max_output_bytes=int(params.get("max_output_bytes", buffers.max_output_bytes)),
        )

    async def _create_session(self, params: dict[str, Any]) -> dict[str, Any]:
        params = dict(params)
        resume_record_id = params.get("resume_record_id")
        if resume_record_id is not None:
            if not isinstance(resume_record_id, str) or not resume_record_id.strip():
                raise ValueError("resume_record_id must not be empty")
            record = await self.session_records.get(resume_record_id.strip())
            if record is None:
                raise KeyError("persisted session record not found")
            if record.get("harness") != params.get("harness"):
                raise ValueError("persisted session record belongs to a different harness")
            stored_session_id = record.get("harness_session_id")
            if not isinstance(stored_session_id, str) or not stored_session_id:
                raise ValueError("persisted session record has no resumable harness session")
            explicit_session_id = params.get("resume_session_id")
            if explicit_session_id and explicit_session_id != stored_session_id:
                raise ValueError("resume_session_id conflicts with resume_record_id")
            params["resume_session_id"] = stored_session_id
        config = self._session_config(params)
        session = await self.registry.create(config)
        try:
            await session.client.start_transport()
            try:
                info = await session.client.get_auth_info()
            except AuthStatusUnsupported:
                info = None
            if info is not None and not info.authenticated:
                session.state = "authentication_required"
                result = self._created_result(session, "authentication_required")
                result.update(info.as_dict())
                await self._persist_session(session, result)
                return result
            try:
                await session.client.open_session()
            except AcpRpcError as exc:
                detail = str(exc).lower()
                if "auth" not in detail and exc.code not in {-32000, -32001}:
                    raise
                session.state = "authentication_required"
                result = self._created_result(session, "authentication_required")
                result["authenticated"] = False
                result["auth_methods"] = session.client.auth_methods()
                result["user"] = None
                await self._persist_session(session, result)
                return result
            session.state = "ready"
            result = self._created_result(session, "ready")
            if info is not None:
                result.update(info.as_dict())
            else:
                result["authenticated"] = True
                result["auth_methods"] = session.client.auth_methods()
                result["user"] = None
            await self._persist_session(session, result)
            return result
        except BaseException:
            await self.registry.close(session.session_id)
            raise

    def _created_result(self, session: Any, status: str) -> dict[str, Any]:
        info = session.client.process_info
        launch_info = {
            "launch_mode": session.config.launch_mode,
            "cwd": session.config.cwd,
            "command": session.config.command,
            "args": session.config.args,
            "ssh_host": session.config.ssh_host,
            "environment_names": sorted(session.config.env),
            "supervisor_pid": info.get("supervisor_pid"),
            "transport_pid": info.get("transport_pid"),
            "transport_pgid": info.get("transport_pgid"),
            "remote_pid_file": info.get("remote_pid_file"),
            "started_at": info.get("started_at"),
        }
        return {
            "status": status,
            "session_id": session.session_id,
            "session_record_id": session.record_id,
            "harness": session.config.harness,
            "harness_session_id": session.client.session_id,
            "resume_session_id": session.client.session_id,
            "model_id": session.client.model_id,
            "model_name": session.client.model_name,
            "launch_info": launch_info,
        }

    async def _persist_session(self, session: Any, result: dict[str, Any]) -> None:
        existing = await self.session_records.get(session.record_id) or {}
        now = time.time()
        launch_info = result.get("launch_info") or existing.get("launch_info")
        if isinstance(launch_info, dict):
            launch_info = dict(launch_info)
            arguments = launch_info.pop("args", None)
            if isinstance(arguments, list):
                launch_info["argument_count"] = len(arguments)
        record = {
            "schema_version": 1,
            "session_record_id": session.record_id,
            "bridge_session_id": session.session_id,
            "harness": session.config.harness,
            "harness_session_id": session.client.session_id,
            "model_id": session.client.model_id,
            "status": result.get("status", session.state),
            "launch_info": launch_info,
            "created_at": existing.get("created_at", now),
            "updated_at": now,
        }
        await self.session_records.upsert(session.record_id, record)

    async def _authenticate(self, params: dict[str, Any]) -> dict[str, Any]:
        session = self.registry.get(_required_string(params, "session_id"))
        method_id = _required_string(params, "method_id")
        result = await self.registry.start_authentication(session, method_id)
        await self._persist_session(session, result)
        return result

    async def _get_user_info(self, params: dict[str, Any]) -> dict[str, Any]:
        session = self.registry.get(_required_string(params, "session_id"))
        info = await session.client.get_auth_info()
        return {"status": "ok", "session_id": session.session_id, **info.as_dict()}

    async def _set_model(self, params: dict[str, Any]) -> dict[str, Any]:
        session = self.registry.get(_required_string(params, "session_id"))
        async with session.lock:
            await session.client.set_model(_required_string(params, "model_id"))
        return {
            "status": "model_set",
            "session_id": session.session_id,
            "harness_session_id": session.client.session_id,
            "model_id": session.client.model_id,
            "model_name": session.client.model_name,
        }

    async def _prompt(self, params: dict[str, Any]) -> dict[str, Any]:
        session = self.registry.get(_required_string(params, "session_id"))
        prompt = _required_string(params, "prompt")
        timeout = float(params.get("timeout_seconds", self.settings.process.turn_timeout_seconds))
        max_output = int(params.get("max_output_bytes", session.config.max_output_bytes))
        if session.lock.locked():
            raise AcpError("this session already has an active operation")
        async with session.lock:
            try:
                await self.registry.acquire_turn(session, timeout)
                await session.client.begin_prompt(prompt)
                return await self._wait_for_turn(session, timeout, max_output)
            except BaseException:
                await session.client.cancel_turn()
                await self.registry.release_turn(session)
                raise

    async def _respond_interaction(self, params: dict[str, Any]) -> dict[str, Any]:
        session = self.registry.get(_required_string(params, "session_id"))
        timeout = float(params.get("timeout_seconds", self.settings.process.turn_timeout_seconds))
        max_output = int(params.get("max_output_bytes", session.config.max_output_bytes))
        async with session.lock:
            self.registry.disarm_interaction_timeout(session)
            await session.client.respond_interaction(
                _required_string(params, "request_id"), params.get("response")
            )
            if session.auth_task is not None and not session.client.turn_active:
                result = await self.registry.wait_for_authentication_event(session)
                await self._persist_session(session, result)
                return result
            return await self._wait_for_turn(session, timeout, max_output)

    async def _wait_for_turn(
        self, session: Any, wait_seconds: float, max_output: int
    ) -> dict[str, Any]:
        event = await session.client.wait_for_turn_event(wait_seconds)
        if event["kind"] == "complete":
            await self.registry.release_turn(session)
            return await self._externalize(
                {"session_id": session.session_id, **event["result"]}, max_output, session
            )
        self.registry.arm_interaction_timeout(session)
        return await self._externalize(
            interaction_result(session, event["interaction"]), max_output, session
        )

    async def _externalize(
        self, result: dict[str, Any], max_output: int, session: Any
    ) -> dict[str, Any]:
        serialized = json.dumps(result, ensure_ascii=False, separators=(",", ":"))
        size = len(serialized.encode())
        if size <= max_output:
            return result

        def write() -> str:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                prefix="harness-acp-result-",
                suffix=".json",
                delete=False,
            ) as output:
                output.write(serialized)
                output.write("\n")
                path = output.name
            os.chmod(path, 0o600)
            return path

        path = await asyncio.to_thread(write)
        session.output_paths.add(path)
        compact = {
            "status": result.get("status", "completed"),
            "session_id": session.session_id,
            "harness_session_id": session.client.session_id,
            "output_path": path,
            "output_bytes": size,
            "output_format": "json",
            "text_available_in_file": True,
        }
        if "interaction" in result:
            compact["interaction"] = result["interaction"]
        return compact

    async def _cancel_turn(self, params: dict[str, Any]) -> dict[str, Any]:
        session = self.registry.get(_required_string(params, "session_id"))
        await session.client.cancel_turn()
        await self.registry.release_turn(session)
        return {"status": "cancelled", "session_id": session.session_id}

    async def _close_session(self, params: dict[str, Any]) -> dict[str, Any]:
        session_id = _required_string(params, "session_id")
        session = self.registry.get(session_id)
        await self.registry.close(session_id)
        result = {
            "status": "closed",
            "session_id": session_id,
            "session_record_id": session.record_id,
        }
        await self._persist_session(session, result)
        return result


def _required_string(params: dict[str, Any], name: str) -> str:
    value = params.get(name)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must not be empty")
    return value.strip()


def configure_logging(settings: Settings) -> None:
    log_path = Path(settings.logging.path)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    handler = logging.handlers.RotatingFileHandler(
        log_path,
        maxBytes=settings.logging.max_bytes,
        backupCount=settings.logging.backup_count,
        encoding="utf-8",
    )
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    logging.basicConfig(
        level=getattr(logging, settings.logging.level, logging.INFO),
        handlers=[handler],
    )


async def run_daemon(settings: Settings) -> None:
    daemon = HarnessDaemon(settings)
    await daemon.start()
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for name in (signal.SIGTERM, signal.SIGINT):
        with suppress(NotImplementedError):
            loop.add_signal_handler(name, stop.set)
    await stop.wait()
    await daemon.close()


def _acquire_singleton(settings: Settings) -> Any:
    lock_path = Path(settings.ipc.lock_path)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(lock_path.parent, 0o700)
    lock_file = lock_path.open("a+", encoding="utf-8")
    try:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        lock_file.close()
        raise RuntimeError("a harness-acp-mcp daemon is already running") from exc
    lock_file.seek(0)
    lock_file.truncate()
    lock_file.write(str(os.getpid()))
    lock_file.flush()
    os.chmod(lock_path, 0o600)
    return lock_file


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the harness ACP MCP daemon")
    parser.add_argument("--config")
    args = parser.parse_args()
    settings = load_settings(args.config)
    configure_logging(settings)
    lock_file = _acquire_singleton(settings)
    try:
        asyncio.run(run_daemon(settings))
    finally:
        lock_file.close()


if __name__ == "__main__":
    main()
