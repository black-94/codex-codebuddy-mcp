from __future__ import annotations

import tempfile
from dataclasses import dataclass, field
from typing import Any, Literal, TextIO

from .config import DEFAULT_APPROVAL_MODE, DEFAULT_MAX_OUTPUT, DEFAULT_MAX_READ

LaunchMode = Literal["local", "ssh"]
PermissionMode = Literal[
    "acceptEdits",
    "bypassPermissions",
    "default",
    "plan",
    "dontAsk",
    "auto",
]
ApprovalMode = Literal["elicitation", "compatible"]


@dataclass(slots=True)
class SessionConfig:
    launch_mode: LaunchMode
    cwd: str
    codebuddy_command: str = "codebuddy"
    codebuddy_args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    ssh_host: str | None = None
    ssh_command: str = "ssh"
    ssh_args: list[str] = field(default_factory=list)
    auth_method_id: str | None = None
    resume_session_id: str | None = None
    permission_mode: PermissionMode = "auto"
    startup_timeout_seconds: float = 60.0
    max_read: int = DEFAULT_MAX_READ
    max_output: int = DEFAULT_MAX_OUTPUT
    approval_mode: ApprovalMode = DEFAULT_APPROVAL_MODE
    remote_pid_file: str | None = None


@dataclass(slots=True)
class PermissionRequest:
    rpc_id: str | int
    request_id: str
    session_id: str
    tool_name: str
    raw_input: dict[str, Any]
    options: list[dict[str, Any]]
    meta: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "tool_name": self.tool_name,
            "raw_input": self.raw_input,
            "options": self.options,
            "meta": self.meta,
        }


@dataclass(slots=True)
class TurnBuffers:
    spool_max_size: int = DEFAULT_MAX_OUTPUT
    tool_calls: dict[str, dict[str, Any]] = field(default_factory=dict)
    _text_file: TextIO = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self._text_file = tempfile.SpooledTemporaryFile(
            max_size=self.spool_max_size,
            mode="w+",
            encoding="utf-8",
        )

    def append_text(self, text: str) -> None:
        self._text_file.write(text)

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
