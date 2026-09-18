from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from codex_codebuddy_mcp.bridge import SessionRegistry
from codex_codebuddy_mcp.models import PermissionRequest, SessionConfig


@pytest.mark.asyncio
async def test_registry_enforces_owner_isolation(tmp_path: Path) -> None:
    registry = SessionRegistry()
    session = await registry.create(
        "owner-a",
        SessionConfig(launch_mode="local", cwd=str(tmp_path)),
    )
    try:
        assert registry.get(session.bridge_session_id, "owner-a") is session
        with pytest.raises(KeyError, match="not found"):
            registry.get(session.bridge_session_id, "owner-b")
    finally:
        await registry.close_all()


@pytest.mark.asyncio
async def test_registry_defaults_to_two_active_turns(tmp_path: Path) -> None:
    registry = SessionRegistry()
    sessions = [
        await registry.create(
            f"owner-{index}",
            SessionConfig(launch_mode="local", cwd=str(tmp_path)),
        )
        for index in range(3)
    ]
    try:
        assert registry.max_concurrency == 2
        await registry.acquire_turn(sessions[0], 1)
        await registry.acquire_turn(sessions[1], 1)

        waiting = asyncio.create_task(registry.acquire_turn(sessions[2], 0.05))
        with pytest.raises(TimeoutError):
            await waiting

        await registry.release_turn(sessions[0])
        await registry.acquire_turn(sessions[2], 1)
    finally:
        await registry.close_all()


@pytest.mark.asyncio
async def test_permission_timeout_cancels_turn_and_releases_slot(tmp_path: Path) -> None:
    registry = SessionRegistry(max_concurrency=1)
    first = await registry.create("owner-1", SessionConfig(launch_mode="local", cwd=str(tmp_path)))
    second = await registry.create("owner-2", SessionConfig(launch_mode="local", cwd=str(tmp_path)))
    first.client.pending_permission = PermissionRequest(
        rpc_id=1,
        request_id="permission-1",
        session_id="session-1",
        tool_name="Bash",
        raw_input={},
        options=[{"kind": "allow", "optionId": "allow"}],
        meta={},
    )
    try:
        await registry.acquire_turn(first, 1)
        registry.arm_permission_timeout(first, 0.01)
        await asyncio.sleep(0.05)
        assert first.client.pending_permission is None
        assert first.turn_slot_held is False
        await registry.acquire_turn(second, 1)
    finally:
        await registry.close_all()


@pytest.mark.asyncio
async def test_close_removes_externalized_output_files(tmp_path: Path) -> None:
    registry = SessionRegistry()
    session = await registry.create("owner", SessionConfig(launch_mode="local", cwd=str(tmp_path)))
    output_path = tmp_path / "externalized.json"
    output_path.write_text("{}", encoding="utf-8")
    session.output_paths.add(str(output_path))

    await registry.close(session.bridge_session_id, "owner")

    assert not output_path.exists()
