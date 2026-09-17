from __future__ import annotations

from pathlib import Path

import pytest

from codex_codebuddy_mcp.bridge import SessionRegistry
from codex_codebuddy_mcp.models import SessionConfig


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
