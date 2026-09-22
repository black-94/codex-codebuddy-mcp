from __future__ import annotations

import re
from abc import ABC
from typing import Any

from .models import AuthInfo, HarnessName, SessionConfig

_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_DETACH_ARGS = {"--bg", "--background", "--tmux", "--tmux-classic", "--serve"}
_CODEBUDDY_MANAGED = {
    "--acp",
    "--acp-transport",
    "--model",
    "--permission-mode",
    "--input-format",
    "--output-format",
    "--print",
    "-p",
}


class AuthStatusUnsupported(RuntimeError):
    pass


class HarnessAdapter(ABC):
    name: HarnessName
    default_command: str

    def validate(self, config: SessionConfig) -> None:
        if not config.cwd.strip():
            raise ValueError("cwd must not be empty")
        if not config.model_id.strip():
            raise ValueError("model_id must not be empty")
        if config.launch_mode not in {"local", "ssh"}:
            raise ValueError("launch_mode must be local or ssh")
        if config.launch_mode == "ssh" and not config.ssh_host:
            raise ValueError("ssh_host is required for SSH launch mode")
        if not config.command.strip():
            raise ValueError("command must not be empty")
        for name in config.env:
            if not _ENV_NAME.fullmatch(name):
                raise ValueError(f"invalid environment variable name: {name!r}")
        for value in config.args:
            if value.split("=", 1)[0] in _DETACH_ARGS or value == "setsid":
                raise ValueError(f"argument may detach from the managed process group: {value}")

    def build_argv(self, config: SessionConfig) -> list[str]:
        self.validate(config)
        return [config.command, *config.args]

    async def get_auth_info(
        self, client: Any, initialize_response: dict[str, Any]
    ) -> AuthInfo:
        methods = _auth_methods(initialize_response)
        try:
            response = await client.request("authentication/status", {})
        except Exception as exc:
            if getattr(exc, "code", None) == -32601:
                raise AuthStatusUnsupported from exc
            raise
        return _normalize_auth_status(response, methods)

    async def authenticate(self, client: Any, method_id: str) -> AuthInfo:
        await client.request("authenticate", {"methodId": method_id})
        return await self.get_auth_info(client, client.initialize_response)

    async def set_model(self, client: Any, model_id: str) -> None:
        await client.request(
            "session/set_model",
            {"sessionId": client.session_id, "modelId": model_id},
        )


class CodeBuddyAdapter(HarnessAdapter):
    name: HarnessName = "codebuddy"
    default_command = "codebuddy"

    def validate(self, config: SessionConfig) -> None:
        super().validate(config)
        for value in config.args:
            if value.split("=", 1)[0] in _CODEBUDDY_MANAGED:
                raise ValueError(f"CodeBuddy argument is managed by the adapter: {value}")
        mode = config.harness_options.get("permission_mode", "auto")
        if mode not in {
            "acceptEdits",
            "bypassPermissions",
            "default",
            "plan",
            "dontAsk",
            "auto",
        }:
            raise ValueError(f"unsupported CodeBuddy permission mode: {mode!r}")

    def build_argv(self, config: SessionConfig) -> list[str]:
        self.validate(config)
        mode = str(config.harness_options.get("permission_mode", "auto"))
        return [
            config.command,
            *config.args,
            "--model",
            config.model_id,
            "--permission-mode",
            mode,
            "--acp",
            "--acp-transport",
            "stdio",
        ]

    async def get_auth_info(
        self, client: Any, initialize_response: dict[str, Any]
    ) -> AuthInfo:
        methods = _auth_methods(initialize_response)
        try:
            response = await client.request("_codebuddy.ai/getUserInfo", {})
        except Exception as exc:
            if getattr(exc, "code", None) == -32601:
                raise AuthStatusUnsupported from exc
            raise
        result = response.get("result")
        user = result.get("userInfo") if isinstance(result, dict) else None
        return AuthInfo(
            authenticated=isinstance(user, dict) and bool(user),
            methods=methods,
            user=user if isinstance(user, dict) else None,
            raw=result if isinstance(result, dict) else {},
        )


class CodexAdapter(HarnessAdapter):
    name: HarnessName = "codex"
    default_command = "codex-acp"


class AgyAdapter(HarnessAdapter):
    name: HarnessName = "agy"
    default_command = "agy_acp_server"


_ADAPTERS: dict[HarnessName, HarnessAdapter] = {
    "codebuddy": CodeBuddyAdapter(),
    "codex": CodexAdapter(),
    "agy": AgyAdapter(),
}


def get_adapter(name: HarnessName) -> HarnessAdapter:
    try:
        return _ADAPTERS[name]
    except KeyError as exc:  # pragma: no cover - typing normally prevents this
        raise ValueError(f"unsupported harness: {name!r}") from exc


def default_command(name: HarnessName) -> str:
    return get_adapter(name).default_command


def _auth_methods(response: dict[str, Any]) -> list[dict[str, Any]]:
    result = response.get("result")
    methods = result.get("authMethods") if isinstance(result, dict) else None
    return [item for item in methods if isinstance(item, dict)] if isinstance(methods, list) else []


def _normalize_auth_status(
    response: dict[str, Any], methods: list[dict[str, Any]]
) -> AuthInfo:
    result = response.get("result")
    if not isinstance(result, dict):
        raise AuthStatusUnsupported("authentication/status returned no object")
    candidate = result.get("authStatus", result)
    if not isinstance(candidate, dict):
        raise AuthStatusUnsupported("authentication/status returned no status")
    explicit = candidate.get("authenticated")
    kind = str(candidate.get("kind", "")).lower()
    status_type = str(candidate.get("type", "")).lower()
    if isinstance(explicit, bool):
        authenticated = explicit
    elif kind:
        authenticated = kind not in {"none", "unauthenticated", "auth_required", "unknown"}
    elif status_type in {"api-key", "chat-gpt", "gateway"}:
        authenticated = True
    elif status_type in {"none", "unauthenticated", "auth-required", "unknown"}:
        authenticated = False
    else:
        raise AuthStatusUnsupported("authentication status has no reliable login indicator")
    account = candidate.get("account")
    if not isinstance(account, dict) and isinstance(candidate.get("email"), str):
        account = {"email": candidate["email"]}
    return AuthInfo(
        authenticated=authenticated,
        methods=methods,
        user=account if isinstance(account, dict) else None,
        raw=candidate,
    )
