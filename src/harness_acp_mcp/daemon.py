from __future__ import annotations

import argparse
import asyncio
import fcntl
import ipaddress
import json
import logging
import logging.handlers
import os
import re
import shlex
import signal
import tempfile
import uuid
from contextlib import suppress
from pathlib import Path
from typing import Any

from .acp import AcpError, AcpLineTooLarge, AcpRpcError
from .adapters import AuthStatusUnsupported
from .bridge import SessionRegistry, interaction_result
from .config import Settings, create_session_timeout_seconds, load_settings
from .models import ContainerInspection, DockerMount, DockerPort, SessionConfig
from .output_log import daemon_directory

logger = logging.getLogger(__name__)
_DOCKER_ID = re.compile(r"[0-9a-f]{64}")
_PER_CALL_LIMITS = ("max_read_bytes", "max_output_bytes")
_MAX_DOCKER_MOUNTS = 64
_MAX_DOCKER_PORTS = 64
_DOCKER_ONLY_PARAMS = (
    "container_policy", "docker_id", "docker_image", "host_network", "mounts", "ports",
)
# A minimal JSON projection of exactly the inspect fields a reused container's
# ``launch_info`` reports. The whole inspect object is deliberately never requested,
# so container environment, labels, and other private state cannot reach a result.
_DOCKER_INSPECT_FORMAT = (
    '{"id":{{json .Id}},'
    '"image":{{json .Config.Image}},'
    '"image_id":{{json .Image}},'
    '"network_mode":{{json .HostConfig.NetworkMode}},'
    '"port_bindings":{{json .HostConfig.PortBindings}},'
    '"mounts":{{json .Mounts}}}'
)


class HarnessDaemon:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.registry = SessionRegistry(settings)
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
            # Each step is isolated: one failure must not stop later idle reclamation
            # or log cleanup for the lifetime of the daemon.
            try:
                reaped = await self.registry.reap_idle()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("idle session reaping failed")
            else:
                if reaped:
                    logger.info("reaped %d idle sessions", reaped)
            try:
                swept = await self.registry.sweep_output_logs()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("harness output log sweeping failed")
            else:
                if swept:
                    logger.info("swept %d expired harness output logs", swept)

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
        try:
            return await handler(params)
        except AcpLineTooLarge as exc:
            return {"status": "error", "error": exc.as_error()}

    async def _ping(self, _params: dict[str, Any]) -> dict[str, Any]:
        return {
            "status": "ok",
            "pid": os.getpid(),
            "config_fingerprint": self.settings.fingerprint,
        }

    def _session_config(self, params: dict[str, Any]) -> SessionConfig:
        hidden = {
            "command", "args", "env", "ssh_command", "ssh_args", "ssh_host",
            "launch_mode", "harness_options", "max_read_bytes", "max_output_bytes",
            "container_record_id", "resume_record_id",
        } & params.keys()
        if hidden:
            rejected = ", ".join(sorted(hidden))
            raise ValueError(f"create_session does not accept launch internals: {rejected}")
        harness = params.get("harness")
        if harness not in {"codebuddy", "agy", "codex"}:
            raise ValueError("harness must be codebuddy, agy, or codex")
        target = params.get("target", "local")
        if target not in {"local", "remote"}:
            raise ValueError("target must be local or remote")
        runtime = params.get("runtime", "direct")
        if runtime not in {"direct", "docker"}:
            raise ValueError("runtime must be direct or docker")
        docker_id = params.get("docker_id")
        policy = params.get("container_policy")
        if policy is not None and policy not in {"remove", "keep"}:
            raise ValueError("container_policy must be remove or keep")
        if docker_id is not None and (
            not isinstance(docker_id, str) or _DOCKER_ID.fullmatch(docker_id) is None
        ):
            raise ValueError("docker_id must be a full 64-character Docker container ID")
        # ``docker_image``, ``mounts``, ``ports``, and ``host_network`` are call-level
        # choices; they are never read from the daemon configuration file.
        raw_host_network = params.get("host_network")
        if raw_host_network is not None and not isinstance(raw_host_network, bool):
            raise ValueError("host_network must be boolean")
        docker_options = {
            "container_policy": policy is not None,
            "docker_id": docker_id is not None,
            "docker_image": params.get("docker_image") is not None,
            "host_network": bool(raw_host_network),
            "mounts": bool(params.get("mounts")),
            "ports": bool(params.get("ports")),
        }
        if runtime == "direct":
            present = [name for name in _DOCKER_ONLY_PARAMS if docker_options[name]]
            if present:
                raise ValueError(
                    "container options require runtime=docker: " + ", ".join(present)
                )
        docker_image: str | None = None
        mounts: tuple[DockerMount, ...] = ()
        ports: tuple[DockerPort, ...] = ()
        host_network = False
        if runtime == "docker":
            if docker_id is not None:
                # A retained container already fixes its image, mounts, published
                # ports, and network. Reusing it only starts the existing container;
                # supplying those options is rejected instead of silently ignored.
                conflicting = [
                    name
                    for name in ("docker_image", "mounts", "ports", "host_network")
                    if docker_options[name]
                ]
                if conflicting:
                    raise ValueError(
                        "a reused container (docker_id) already fixes its image, mounts, "
                        "ports, and network; remove " + ", ".join(conflicting)
                    )
            else:
                docker_image = _docker_image(params.get("docker_image"))
                mounts = _docker_mounts(params.get("mounts"))
                ports = _docker_ports(params.get("ports"))
                host_network = bool(raw_host_network)
                if host_network and ports:
                    raise ValueError("host_network cannot be combined with published ports")
        permission_mode = params.get("permission_mode", "auto")
        if permission_mode not in {"read", "edit", "auto", "bypass"}:
            raise ValueError("permission_mode must be read, edit, auto, or bypass")
        remote_host = params.get("remote_host")
        if target == "remote" and (not isinstance(remote_host, str) or not remote_host.strip()):
            raise ValueError("remote_host is required for a remote target")
        if target == "local" and remote_host is not None:
            raise ValueError("remote_host is only valid for a remote target")
        command = shlex.split(getattr(self.settings.launch, f"{harness}_command"))
        if not command:
            raise ValueError(f"launch.{harness}_command must contain a program")
        model_id = params.get("model_id")
        if not isinstance(model_id, str) or not model_id.strip():
            raise ValueError("model_id must not be empty")
        cwd = params.get("cwd")
        if not isinstance(cwd, str) or not cwd.strip():
            raise ValueError("cwd must not be empty")
        cwd = cwd.strip()
        if runtime == "docker" and not os.path.isabs(cwd):
            # For Docker, cwd is the path *inside the container* (used as the exec
            # working directory), never a host bind mount.
            raise ValueError("cwd must be an absolute container path when runtime=docker")
        resume_session_id = params.get("resume_session_id")
        if resume_session_id is not None:
            if not isinstance(resume_session_id, str) or not resume_session_id.strip():
                raise ValueError("resume_session_id must be a non-empty string")
            resume_session_id = resume_session_id.strip()
        process = self.settings.process
        buffers = self.settings.buffers
        return SessionConfig(
            harness=harness,
            launch_mode="ssh" if target == "remote" else "local",
            cwd=cwd,
            model_id=model_id.strip(),
            command=command[0],
            args=command[1:],
            runtime=runtime,
            permission_mode=permission_mode,
            acp_mode_id=(
                getattr(self.settings.launch, f"agy_{permission_mode}_mode_id")
                if harness == "agy" else None
            ),
            docker_command=self.settings.launch.docker_command,
            docker_image=docker_image,
            docker_container_name=(
                docker_id or f"harness-acp-{uuid.uuid4().hex}" if runtime == "docker" else None
            ),
            docker_id=docker_id,
            docker_mounts=mounts,
            docker_ports=ports,
            docker_host_network=host_network,
            container_policy=policy or ("keep" if docker_id else "remove"),
            reuse_container=bool(docker_id),
            ssh_host=remote_host.strip() if isinstance(remote_host, str) else None,
            ssh_command=self.settings.launch.ssh_command,
            resume_session_id=resume_session_id,
            startup_timeout_seconds=float(
                params.get("startup_timeout_seconds", process.startup_timeout_seconds)
            ),
            auth_timeout_seconds=float(
                params.get("auth_timeout_seconds", self.settings.authentication.timeout_seconds)
            ),
            turn_cancel_timeout_seconds=process.turn_cancel_timeout_seconds,
            terminate_grace_seconds=process.terminate_grace_seconds,
            remote_cleanup_timeout_seconds=process.remote_cleanup_timeout_seconds,
            stderr_tail_lines=buffers.stderr_tail_lines,
            max_read_bytes=buffers.max_read_bytes,
            max_output_bytes=buffers.max_output_bytes,
            output_log_directory=str(daemon_directory(self.settings.ipc.lock_path)),
        )

    async def _inspect_docker_instance(self, config: SessionConfig) -> ContainerInspection:
        """Read a retained container's real configuration for ``launch_info``.

        Runs ``docker inspect`` (SSH-wrapped for a remote target) with a JSON template
        that projects only the image, mounts, port bindings, and network mode, then
        validates the container ID matches the requested ``docker_id``. The returned
        values replace any request-side defaults in ``launch_info``.
        """
        command = [
            config.docker_command, "inspect", "--type", "container",
            "--format", _DOCKER_INSPECT_FORMAT, config.docker_id,
        ]
        if config.launch_mode == "ssh":
            command = [
                config.ssh_command, "--", config.ssh_host,
                shlex.join(command),
            ]
        process = await asyncio.create_subprocess_exec(
            *command,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(), timeout=config.startup_timeout_seconds,
            )
        except TimeoutError:
            process.kill()
            await process.wait()
            raise ValueError("Docker container inspection timed out") from None
        if process.returncode != 0:
            detail = stderr.decode(errors="replace").strip()[-300:]
            raise ValueError(f"Docker container does not exist or is unavailable: {detail}")
        try:
            data = json.loads(stdout.decode(errors="replace").strip())
        except json.JSONDecodeError:
            raise ValueError(
                "Docker container inspection returned malformed data"
            ) from None
        if not isinstance(data, dict) or data.get("id") != config.docker_id:
            raise ValueError(
                "Docker container does not exist or is unavailable"
            )
        return ContainerInspection(
            image=_inspect_image(data),
            mounts=_inspect_mounts(data.get("mounts")),
            ports=_inspect_ports(data.get("port_bindings")),
            host_network=data.get("network_mode") == "host",
        )

    async def _create_session(self, params: dict[str, Any]) -> dict[str, Any]:
        params = dict(params)
        config = self._session_config(params)
        budget = create_session_timeout_seconds(config.startup_timeout_seconds)
        guard = asyncio.timeout(budget)
        try:
            async with guard:
                return await self._create_session_within_budget(config)
        except TimeoutError:
            if not guard.expired():
                raise
            # Enforced here so the daemon always cleans up before the MCP caller's IPC
            # timeout expires, which otherwise leaves an orphan session behind.
            raise AcpError(
                f"session creation exceeded its {budget:g}s budget; Docker inspection, "
                "container startup, and harness initialization each have their own startup "
                "timeout"
            ) from None

    async def _create_session_within_budget(self, config: SessionConfig) -> dict[str, Any]:
        session = None
        try:
            if config.docker_id:
                config.container_inspection = await self._inspect_docker_instance(config)
            session = await self.registry.create(config)
            await session.client.start_transport()
            try:
                info = await session.client.get_auth_info()
            except AuthStatusUnsupported:
                info = None
            if info is not None and not info.authenticated:
                session.state = "authentication_required"
                result = self._created_result(session, "authentication_required")
                result.update(info.as_dict())
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
                return result
            session.state = "ready"
            result = self._created_result(session, "ready")
            if info is not None:
                result.update(info.as_dict())
            else:
                result["authenticated"] = True
                result["auth_methods"] = session.client.auth_methods()
                result["user"] = None
            return result
        except BaseException:
            if session is not None:
                with suppress(Exception, asyncio.CancelledError):
                    await self.registry.close(session.session_id)
            raise

    def _created_result(self, session: Any, status: str) -> dict[str, Any]:
        info = session.client.process_info
        launch_info = {
            "target": "remote" if session.config.launch_mode == "ssh" else "local",
            "runtime": session.config.runtime,
            "container_policy": (
                session.config.container_policy if session.config.runtime == "docker" else None
            ),
            "permission_mode": session.config.permission_mode,
            "cwd": session.config.cwd,
            "remote_host": session.config.ssh_host,
            "supervisor_pid": info.get("supervisor_pid"),
            "transport_pid": info.get("transport_pid"),
            "transport_pgid": info.get("transport_pgid"),
            "remote_pid_file": info.get("remote_pid_file"),
            "started_at": info.get("started_at"),
        }
        if session.config.runtime == "docker":
            launch_info["reused_container"] = session.config.reuse_container
            if session.config.reuse_container:
                # A reused container fixes its own image, mounts, published ports, and
                # network at creation time. Report the real values read back from
                # ``docker inspect`` instead of request defaults, which for a reuse call
                # are absent and would be misleading (for example ``host_network: false``).
                inspection = session.config.container_inspection
                if inspection is None:
                    raise AcpError(
                        "Docker container configuration is unavailable for a reused container"
                    )
                launch_info["docker_image"] = inspection.image
                launch_info["mounts"] = [mount.as_dict() for mount in inspection.mounts]
                launch_info["ports"] = [port.as_dict() for port in inspection.ports]
                launch_info["host_network"] = inspection.host_network
            else:
                # These are the creation-time values this call actually applied.
                launch_info["docker_image"] = session.config.docker_image
                launch_info["mounts"] = [
                    mount.as_dict() for mount in session.config.docker_mounts
                ]
                launch_info["ports"] = [
                    port.as_dict() for port in session.config.docker_ports
                ]
                launch_info["host_network"] = session.config.docker_host_network
        result = {
            "status": status,
            "session_id": session.session_id,
            "harness": session.config.harness,
            "harness_session_id": session.client.session_id,
            "model_id": session.client.model_id,
            "model_name": session.client.model_name,
            "output_log_path": str(session.client.output_log.path),
            "launch_info": launch_info,
        }
        if session.config.runtime == "docker" and session.config.container_policy == "keep":
            docker_id = info.get("docker_id")
            if not isinstance(docker_id, str) or _DOCKER_ID.fullmatch(docker_id) is None:
                raise AcpError("Docker container was kept but its container ID is unavailable")
            result["docker_id"] = docker_id
        return result

    async def _authenticate(self, params: dict[str, Any]) -> dict[str, Any]:
        session = self.registry.get(_required_string(params, "session_id"))
        method_id = _required_string(params, "method_id")
        result = await self.registry.start_authentication(session, method_id)
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
        _reject_per_call_limits(params)
        session = self.registry.get(_required_string(params, "session_id"))
        prompt = _required_string(params, "prompt")
        timeout = float(params.get("timeout_seconds", self.settings.process.turn_timeout_seconds))
        if session.lock.locked():
            raise AcpError("this session already has an active operation")
        async with session.lock:
            try:
                await self.registry.acquire_turn(session, timeout)
                await session.client.begin_prompt(prompt)
                return await self._wait_for_turn(session, timeout)
            except BaseException:
                await self._cancel_and_release(session)
                raise

    async def _respond_interaction(self, params: dict[str, Any]) -> dict[str, Any]:
        _reject_per_call_limits(params)
        session = self.registry.get(_required_string(params, "session_id"))
        timeout = float(params.get("timeout_seconds", self.settings.process.turn_timeout_seconds))
        async with session.lock:
            try:
                self.registry.disarm_interaction_timeout(session)
                await session.client.respond_interaction(
                    _required_string(params, "request_id"), params.get("response")
                )
                if session.auth_task is not None and not session.client.turn_active:
                    result = await self.registry.wait_for_authentication_event(session)
                    return result
                return await self._wait_for_turn(session, timeout)
            except BaseException:
                # A turn timeout, an oversized line, or a harness exit must not leave the
                # concurrency slot held forever.
                await self._cancel_and_release(session)
                raise

    async def _cancel_and_release(self, session: Any) -> None:
        with suppress(Exception, asyncio.CancelledError):
            await session.client.cancel_turn()
        # release_turn has no await points, so the slot is freed even while cancelling.
        await self.registry.release_turn(session)

    async def _wait_for_turn(self, session: Any, wait_seconds: float) -> dict[str, Any]:
        event = await session.client.wait_for_turn_event(wait_seconds)
        if event["kind"] == "complete":
            await self.registry.release_turn(session)
            return await self._externalize(
                {"session_id": session.session_id, **event["result"]},
                session.config.max_output_bytes,
                session,
            )
        self.registry.arm_interaction_timeout(session)
        return await self._externalize(
            interaction_result(session, event["interaction"]),
            session.config.max_output_bytes,
            session,
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
        self.registry.get(session_id)
        await self.registry.close(session_id)
        result = {
            "status": "closed",
            "session_id": session_id,
        }
        return result


def _docker_image(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(
            "docker_image is required when runtime=docker creates a new container"
        )
    image = value.strip()
    if image.startswith("-") or any(character in image for character in ", \t\r\n"):
        raise ValueError(
            "docker_image must be a single image reference without whitespace or commas"
        )
    if len(image) > 255:
        raise ValueError("docker_image must be at most 255 characters")
    return image


def _mount_path(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty absolute path")
    path = value.strip()
    if not os.path.isabs(path):
        raise ValueError(f"{name} must be an absolute path")
    if "," in path or any(character in path for character in "\t\r\n"):
        raise ValueError(f"{name} must not contain a comma, tab, or newline")
    return path


def _docker_mounts(value: Any) -> tuple[DockerMount, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise ValueError("mounts must be a list of mount objects")
    if len(value) > _MAX_DOCKER_MOUNTS:
        raise ValueError(f"mounts accepts at most {_MAX_DOCKER_MOUNTS} entries")
    mounts: list[DockerMount] = []
    targets: set[str] = set()
    for index, item in enumerate(value):
        if not isinstance(item, dict):
            raise ValueError(f"mounts[{index}] must be an object")
        unknown = set(item) - {"source", "target", "read_only"}
        if unknown:
            raise ValueError(
                f"mounts[{index}] has unsupported keys: {', '.join(sorted(unknown))}"
            )
        source = _mount_path(item.get("source"), f"mounts[{index}].source")
        target = _mount_path(item.get("target"), f"mounts[{index}].target")
        read_only = item.get("read_only", False)
        if not isinstance(read_only, bool):
            raise ValueError(f"mounts[{index}].read_only must be boolean")
        if target in targets:
            raise ValueError(f"mounts[{index}].target duplicates another mount target")
        targets.add(target)
        mounts.append(DockerMount(source=source, target=target, read_only=read_only))
    return tuple(mounts)


def _port_number(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} must be an integer between 1 and 65535")
    if not 1 <= value <= 65535:
        raise ValueError(f"{name} must be between 1 and 65535")
    return value


def _docker_ports(value: Any) -> tuple[DockerPort, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise ValueError("ports must be a list of port objects")
    if len(value) > _MAX_DOCKER_PORTS:
        raise ValueError(f"ports accepts at most {_MAX_DOCKER_PORTS} entries")
    ports: list[DockerPort] = []
    for index, item in enumerate(value):
        if not isinstance(item, dict):
            raise ValueError(f"ports[{index}] must be an object")
        unknown = set(item) - {"host_ip", "host_port", "container_port", "protocol"}
        if unknown:
            raise ValueError(
                f"ports[{index}] has unsupported keys: {', '.join(sorted(unknown))}"
            )
        host_port = _port_number(item.get("host_port"), f"ports[{index}].host_port")
        container_port = _port_number(
            item.get("container_port"), f"ports[{index}].container_port"
        )
        protocol = item.get("protocol", "tcp")
        if protocol not in {"tcp", "udp"}:
            raise ValueError(f"ports[{index}].protocol must be tcp or udp")
        host_ip = item.get("host_ip")
        if host_ip is not None:
            if not isinstance(host_ip, str):
                raise ValueError(f"ports[{index}].host_ip must be a string")
            try:
                ipaddress.ip_address(host_ip)
            except ValueError:
                raise ValueError(
                    f"ports[{index}].host_ip must be a valid IPv4 or IPv6 address"
                ) from None
        ports.append(
            DockerPort(
                host_port=host_port,
                container_port=container_port,
                protocol=protocol,
                host_ip=host_ip,
            )
        )
    return tuple(ports)


def _inspect_image(data: dict[str, Any]) -> str:
    """Return the container's image reference, falling back to its image ID."""
    reference = data.get("image")
    if isinstance(reference, str) and reference.strip():
        return reference.strip()
    image_id = data.get("image_id")
    if isinstance(image_id, str) and image_id.strip():
        return image_id.strip()
    raise ValueError("Docker container inspection did not report an image")


def _inspect_read_only(item: dict[str, Any]) -> bool:
    """Read a bind mount's read-only flag from ``RW`` or, secondarily, ``Mode``."""
    read_write = item.get("RW")
    if isinstance(read_write, bool):
        return not read_write
    mode = item.get("Mode")
    return isinstance(mode, str) and "ro" in mode.split(",")


def _inspect_mounts(raw: Any) -> tuple[DockerMount, ...]:
    """Project inspect ``Mounts`` down to the bind mounts the bridge can model.

    Named volumes and tmpfs mounts are skipped: the ``mounts`` option is bind-only, so
    representing them in the same ``{source, target, read_only}`` shape would be wrong.
    """
    if not isinstance(raw, list):
        return ()
    mounts: list[DockerMount] = []
    for item in raw:
        if not isinstance(item, dict) or item.get("Type") != "bind":
            continue
        source = item.get("Source")
        target = item.get("Destination")
        if not isinstance(source, str) or not source.strip():
            continue
        if not isinstance(target, str) or not target.strip():
            continue
        mounts.append(
            DockerMount(
                source=source.strip(),
                target=target.strip(),
                read_only=_inspect_read_only(item),
            )
        )
    return tuple(mounts)


def _inspect_ports(raw: Any) -> tuple[DockerPort, ...]:
    """Flatten inspect ``HostConfig.PortBindings`` into sorted published ports."""
    if not isinstance(raw, dict):
        return ()
    ports: list[DockerPort] = []
    for key, bindings in raw.items():
        if not isinstance(key, str) or "/" not in key:
            continue
        container_text, _, protocol = key.rpartition("/")
        if protocol not in {"tcp", "udp"}:
            continue
        if not container_text.isdigit():
            continue
        container_port = int(container_text)
        if not 1 <= container_port <= 65535 or not isinstance(bindings, list):
            continue
        for binding in bindings:
            if not isinstance(binding, dict):
                continue
            host_text = binding.get("HostPort")
            if not isinstance(host_text, str) or not host_text.isdigit():
                continue
            host_port = int(host_text)
            if not 1 <= host_port <= 65535:
                continue
            host_ip = binding.get("HostIp")
            host_ip = host_ip.strip() if isinstance(host_ip, str) else ""
            ports.append(
                DockerPort(
                    host_port=host_port,
                    container_port=container_port,
                    protocol=protocol,
                    host_ip=host_ip or None,
                )
            )
    ports.sort(key=lambda port: (port.container_port, port.protocol, port.host_port,
                                 port.host_ip or ""))
    return tuple(ports)


def _required_string(params: dict[str, Any], name: str) -> str:
    value = params.get(name)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must not be empty")
    return value.strip()


def _reject_per_call_limits(params: dict[str, Any]) -> None:
    rejected = [name for name in _PER_CALL_LIMITS if name in params]
    if rejected:
        raise ValueError(
            f"{', '.join(rejected)} is configured in the daemon configuration file, "
            "not per call"
        )


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
