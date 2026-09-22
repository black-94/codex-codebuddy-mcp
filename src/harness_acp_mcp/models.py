from __future__ import annotations

import tempfile
from dataclasses import dataclass, field
from typing import Any, Literal, TextIO

HarnessName = Literal["codebuddy", "agy", "codex"]
LaunchMode = Literal["local", "ssh"]


@dataclass(slots=True)
class SessionConfig:
    harness: HarnessName
    launch_mode: LaunchMode
    cwd: str
    model_id: str
    command: str
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    ssh_host: str | None = None
    ssh_command: str = "ssh"
    ssh_args: list[str] = field(default_factory=list)
    resume_session_id: str | None = None
    harness_options: dict[str, Any] = field(default_factory=dict)
    startup_timeout_seconds: float = 60.0
    auth_timeout_seconds: float = 600.0
    turn_cancel_timeout_seconds: float = 5.0
    terminate_grace_seconds: float = 5.0
    remote_cleanup_timeout_seconds: float = 10.0
    stdout_overflow_retry_tolerance: int = 1
    stderr_tail_lines: int = 200
    max_read_bytes: int = 1024 * 1024
    max_output_bytes: int = 64 * 1024


@dataclass(slots=True)
class AuthInfo:
    authenticated: bool
    methods: list[dict[str, Any]] = field(default_factory=list)
    user: dict[str, Any] | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "authenticated": self.authenticated,
            "auth_methods": self.methods,
            "user": self.user,
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
