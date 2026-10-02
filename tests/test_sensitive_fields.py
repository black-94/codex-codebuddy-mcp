from __future__ import annotations

import asyncio
import json
import shlex
import sys
import uuid
from pathlib import Path

import pytest

from harness_acp_mcp.acp import AcpClient
from harness_acp_mcp.adapters import CodeBuddyAdapter, _normalize_auth_status
from harness_acp_mcp.config import (
    AuthenticationSettings,
    DaemonSettings,
    IpcSettings,
    LaunchSettings,
    LoggingSettings,
    RateLimitSettings,
    Settings,
)
from harness_acp_mcp.daemon import HarnessDaemon
from harness_acp_mcp.models import AuthInfo, SessionConfig
from harness_acp_mcp.privacy import public_account, redact_sensitive

FAKE_HARNESS = Path(__file__).with_name("fake_harness.py")


def secret(prefix: str) -> str:
    # Generated per run so no token-like literal is committed and no real credential
    # is ever involved.
    return f"{prefix}-{uuid.uuid4().hex}"


def test_public_account_keeps_only_whitelisted_identity_fields() -> None:
    token = secret("tk")
    account = {
        "userId": "u1",
        "email": "user@example.invalid",
        "token": token,
        "accessToken": token,
        "refreshToken": token,
        "apiKey": token,
        "profile": {"secret": token},
    }
    filtered = public_account(account)
    assert filtered == {"userId": "u1", "email": "user@example.invalid"}
    assert token not in json.dumps(filtered)


@pytest.mark.parametrize(
    "value",
    [None, "string", [{"userId": "u"}], {"token": "x"}, {"unknown": "value"}],
)
def test_public_account_returns_none_when_nothing_safe_remains(value: object) -> None:
    assert public_account(value) is None


def test_redact_sensitive_is_recursive_and_non_mutating() -> None:
    token = secret("sk")
    payload = {
        "result": {
            "userInfo": {
                "userId": "u1",
                "token": token,
                "nested": [{"password": token, "note": "keep"}],
            }
        }
    }
    redacted = redact_sensitive(payload)
    serialized = json.dumps(redacted)
    assert token not in serialized
    assert redacted["result"]["userInfo"]["userId"] == "u1"
    assert redacted["result"]["userInfo"]["nested"][0]["note"] == "keep"
    # The caller's object is not modified.
    assert payload["result"]["userInfo"]["token"] == token


@pytest.mark.parametrize(
    "template",
    [
        "auth accessToken={token}",
        "token={token}",
        "Authorization: Bearer {token}",
        "note token: {token} and trailing text",
    ],
)
def test_redact_sensitive_masks_credentials_in_free_text(template: str) -> None:
    token = secret("tk")
    redacted = redact_sensitive(template.format(token=token))
    assert token not in redacted
    assert "[redacted]" in redacted


def test_redact_sensitive_redacts_embedded_json_text() -> None:
    token = secret("tk")
    text = json.dumps({"result": {"userInfo": {"userId": "u", "token": token}}})
    redacted = redact_sensitive(text)
    assert token not in redacted
    assert json.loads(redacted)["result"]["userInfo"]["userId"] == "u"


def test_redact_sensitive_leaves_plain_text_unchanged() -> None:
    assert redact_sensitive("echo:hello") == "echo:hello"


def test_auth_info_as_dict_filters_account_credentials() -> None:
    token = secret("tk")
    info = AuthInfo(
        authenticated=True,
        methods=[{"id": "browser"}],
        user={"userId": "u1", "accessToken": token},
    )
    public = info.as_dict()
    assert public["authenticated"] is True
    assert public["user"] == {"userId": "u1"}
    assert token not in json.dumps(public)


@pytest.mark.asyncio
async def test_codebuddy_adapter_drops_tokens_and_keeps_login_boolean() -> None:
    token = secret("tk")

    class StubClient:
        async def request(self, method: str, params: dict) -> dict:
            return {"result": {"userInfo": {"userId": "u1", "token": token, "accessToken": token}}}

    info = await CodeBuddyAdapter().get_auth_info(StubClient(), {})
    assert info.authenticated is True
    assert info.user == {"userId": "u1"}
    assert token not in json.dumps(info.as_dict())

    class TokenOnlyClient:
        async def request(self, method: str, params: dict) -> dict:
            return {"result": {"userInfo": {"token": token}}}

    # A user object holding only credentials is still authenticated; nothing leaks.
    only_credentials = await CodeBuddyAdapter().get_auth_info(TokenOnlyClient(), {})
    assert only_credentials.authenticated is True
    assert only_credentials.user is None
    assert token not in json.dumps(only_credentials.as_dict())


def test_normalize_auth_status_filters_account_credentials() -> None:
    token = secret("tk")
    response = {
        "result": {
            "authStatus": {
                "kind": "chatgpt",
                "account": {"email": "user@example.invalid", "accessToken": token},
            }
        }
    }
    info = _normalize_auth_status(response, [])
    assert info.authenticated is True
    assert info.user == {"email": "user@example.invalid"}
    assert token not in json.dumps(info.as_dict())


def isolated_settings(tmp_path: Path) -> Settings:
    return Settings(
        ipc=IpcSettings(
            socket_path=str(tmp_path / "daemon.sock"),
            lock_path=str(tmp_path / "daemon.lock"),
        ),
        daemon=DaemonSettings(idle_session_timeout_seconds=0),
        authentication=AuthenticationSettings(
            ledger_path=str(tmp_path / "rate.sqlite3"),
            rate_limit=RateLimitSettings(enabled=False),
        ),
        logging=LoggingSettings(path=str(tmp_path / "daemon.log")),
        launch=LaunchSettings(
            codebuddy_command=shlex.join([sys.executable, str(FAKE_HARNESS)]),
            codex_command=shlex.join([sys.executable, str(FAKE_HARNESS)]),
        ),
    )


@pytest.mark.asyncio
async def test_create_session_and_get_user_info_never_expose_or_persist_tokens(
    tmp_path: Path, monkeypatch
) -> None:
    token = secret("tk")
    monkeypatch.setenv("FAKE_USER_TOKEN", token)
    daemon = HarnessDaemon(isolated_settings(tmp_path))
    created = await daemon.dispatch("create_session", {
        "harness": "codebuddy", "cwd": str(tmp_path), "model_id": "fake-model",
    })
    try:
        assert created["status"] == "ready"
        assert created["authenticated"] is True
        assert created["user"] == {"userId": "fake-user"}
        assert token not in json.dumps(created)

        info = await daemon.dispatch("get_user_info", {"session_id": created["session_id"]})
        assert info["authenticated"] is True
        assert info["user"] == {"userId": "fake-user"}
        assert token not in json.dumps(info)

        log = Path(created["output_log_path"])
        contents = await asyncio.to_thread(log.read_text)
        assert token not in contents
        assert "[redacted]" in contents
    finally:
        await daemon.registry.close_all()


@pytest.mark.asyncio
async def test_output_log_redacts_generic_status_credentials(
    tmp_path: Path, monkeypatch
) -> None:
    token = secret("sk")
    monkeypatch.setenv("FAKE_STATUS_TOKEN", token)
    daemon = HarnessDaemon(isolated_settings(tmp_path))
    created = await daemon.dispatch("create_session", {
        "harness": "codex", "cwd": str(tmp_path), "model_id": "fake-model",
    })
    try:
        assert created["status"] == "ready"
        assert created["user"] == {"email": "user@example.invalid"}
        assert token not in json.dumps(created)
        log = Path(created["output_log_path"])
        contents = await asyncio.to_thread(log.read_text)
        assert token not in contents
    finally:
        await daemon.registry.close_all()


@pytest.mark.asyncio
async def test_stderr_credentials_are_redacted_in_tail_log_and_errors(
    tmp_path: Path, monkeypatch
) -> None:
    token = secret("tk")
    monkeypatch.setenv("FAKE_STDERR_TOKEN", token)
    client = AcpClient(
        SessionConfig(
            harness="codex",
            launch_mode="local",
            cwd=str(tmp_path),
            model_id="fake-model",
            command=sys.executable,
            args=[str(FAKE_HARNESS)],
            startup_timeout_seconds=5,
            terminate_grace_seconds=0.1,
            remote_cleanup_timeout_seconds=0.1,
        )
    )
    await client.start_transport()
    try:
        for _ in range(100):
            if "accessToken" in client.stderr_tail:
                break
            await asyncio.sleep(0.02)
        # The credential-looking stderr line is captured, but masked.
        assert "accessToken" in client.stderr_tail
        assert token not in client.stderr_tail
        assert token not in client._exit_message("boom")
        contents = await asyncio.to_thread(client.output_log.path.read_text)
        assert token not in contents
        assert "[redacted]" in contents
    finally:
        await client.close()
