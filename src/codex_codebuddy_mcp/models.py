from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

LaunchMode = Literal["local", "ssh"]


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
    startup_timeout_seconds: float = 60.0


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
    text_parts: list[str] = field(default_factory=list)
    tool_calls: dict[str, dict[str, Any]] = field(default_factory=dict)

    @property
    def text(self) -> str:
        return "".join(self.text_parts)

    def tool_call_summaries(self) -> list[dict[str, Any]]:
        return list(self.tool_calls.values())
