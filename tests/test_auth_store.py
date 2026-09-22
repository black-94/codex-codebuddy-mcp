from __future__ import annotations

from pathlib import Path

import pytest

from harness_acp_mcp.auth_store import AuthRateStore
from harness_acp_mcp.config import AuthenticationSettings, RateLimitSettings


@pytest.mark.asyncio
async def test_rate_limit_is_atomic_and_persistent(tmp_path: Path) -> None:
    settings = AuthenticationSettings(
        ledger_path=str(tmp_path / "rate.sqlite3"),
        rate_limit=RateLimitSettings(
            enabled=True,
            min_interval_seconds=0,
            max_attempts=2,
            window_seconds=3600,
        ),
    )
    key = AuthRateStore.target_key("codex", "local", None)
    first = await AuthRateStore(settings).check_and_record(key)
    second = await AuthRateStore(settings).check_and_record(key)
    limited = await AuthRateStore(settings).check_and_record(key)

    assert first.allowed is True
    assert second.allowed is True
    assert limited.allowed is False
    assert limited.remaining_attempts == 0


@pytest.mark.asyncio
async def test_rate_limit_can_be_disabled_without_creating_state(tmp_path: Path) -> None:
    path = tmp_path / "disabled.sqlite3"
    settings = AuthenticationSettings(
        ledger_path=str(path),
        rate_limit=RateLimitSettings(enabled=False),
    )
    store = AuthRateStore(settings)

    results = await store.check_and_record("opaque-key"), await store.check_and_record(
        "opaque-key"
    )

    assert all(item.allowed for item in results)
    assert not path.exists()


def test_target_key_does_not_store_target_text() -> None:
    key = AuthRateStore.target_key("agy", "ssh", "<ssh-target>")

    assert len(key) == 64
    assert "ssh-target" not in key
