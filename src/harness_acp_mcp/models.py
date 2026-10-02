from __future__ import annotations

import tempfile
from dataclasses import dataclass, field
from typing import Any, Literal, TextIO

from .privacy import public_account

HarnessName = Literal["codebuddy", "agy", "codex"]
LaunchMode = Literal["local", "ssh"]


@dataclass(frozen=True, slots=True)
class DockerMount:
    """One explicit host-directory bind mount for a Docker container."""

    source: str
    target: str
    read_only: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {"source": self.source, "target": self.target, "read_only": self.read_only}


@dataclass(frozen=True, slots=True)
class DockerPort:
    """One published container port; ``host_ip`` is the optional host bind address."""

    host_port: int
    container_port: int
    protocol: Literal["tcp", "udp"] = "tcp"
    host_ip: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "host_ip": self.host_ip,
            "host_port": self.host_port,
            "container_port": self.container_port,
            "protocol": self.protocol,
        }


@dataclass(frozen=True, slots=True)
class ContainerInspection:
    """Docker configuration read back from ``docker inspect`` for a retained container.

    It mirrors the four creation-time Docker options so a reused container's
    ``launch_info`` reports the values the container actually holds instead of
    guessed defaults. Only these fields are projected out of ``docker inspect``;
    the full payload (including container environment) is never echoed.
    """

    image: str
    mounts: tuple[DockerMount, ...] = ()
    ports: tuple[DockerPort, ...] = ()
    host_network: bool = False


@dataclass(slots=True)
class SessionConfig:
    harness: HarnessName
    launch_mode: LaunchMode
    cwd: str
    model_id: str
    command: str
    runtime: Literal["direct", "docker"] = "direct"
    permission_mode: Literal["read", "edit", "auto", "bypass"] = "auto"
    acp_mode_id: str | None = None
    docker_command: str = "docker"
    docker_image: str | None = None
    docker_container_name: str | None = None
    docker_id: str | None = None
    docker_mounts: tuple[DockerMount, ...] = ()
    docker_ports: tuple[DockerPort, ...] = ()
    docker_host_network: bool = False
    container_policy: Literal["remove", "keep"] = "remove"
    reuse_container: bool = False
    # Populated for a reused container from ``docker inspect`` before the session is
    # created, so ``launch_info`` can report its real image, mounts, ports, and
    # network mode. It stays ``None`` for a newly created container.
    container_inspection: ContainerInspection | None = None
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    ssh_host: str | None = None
    ssh_command: str = "ssh"
    resume_session_id: str | None = None
    startup_timeout_seconds: float = 60.0
    auth_timeout_seconds: float = 600.0
    turn_cancel_timeout_seconds: float = 5.0
    terminate_grace_seconds: float = 5.0
    remote_cleanup_timeout_seconds: float = 10.0
    stderr_tail_lines: int = 200
    max_read_bytes: int = 100 * 1024 * 1024
    max_output_bytes: int = 128 * 1024
    output_log_directory: str | None = None


@dataclass(slots=True)
class AuthInfo:
    authenticated: bool
    methods: list[dict[str, Any]] = field(default_factory=list)
    user: dict[str, Any] | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        # ``user`` is rebuilt from the account whitelist so credentials a harness puts
        # in the account object can never reach an MCP result.
        return {
            "authenticated": self.authenticated,
            "auth_methods": self.methods,
            "user": public_account(self.user),
        }


@dataclass(slots=True)
class InteractionRequest:
    rpc_id: str | int
    request_id: str
    kind: Literal["permission", "information"]
    session_id: str
    title: str
    message: str
    options: list[dict[str, Any]] = field(default_factory=list)
    schema: dict[str, Any] | None = None
    defaults: dict[str, Any] | None = None
    response_style: Literal["content", "elicitation"] = "content"
    raw_input: dict[str, Any] = field(default_factory=dict)
    meta: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "kind": self.kind,
            "title": self.title,
            "message": self.message,
            "options": self.options,
            "schema": self.schema,
            "defaults": self.defaults,
            "raw_input": self.raw_input,
            "meta": self.meta,
        }


@dataclass(slots=True)
class TurnBuffers:
    spool_max_size: int = 64 * 1024
    tool_calls: dict[str, dict[str, Any]] = field(default_factory=dict)
    _text_file: TextIO = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self._text_file = tempfile.SpooledTemporaryFile(
            max_size=self.spool_max_size,
            mode="w+",
            encoding="utf-8",
        )

    def append_text(self, value: str) -> None:
        self._text_file.write(value)

    @property
    def text(self) -> str:
        position = self._text_file.tell()
        self._text_file.seek(0)
        value = self._text_file.read()
        self._text_file.seek(position)
        return value

    def tool_call_summaries(self) -> list[dict[str, Any]]:
        return list(self.tool_calls.values())

    def close(self) -> None:
        self._text_file.close()
