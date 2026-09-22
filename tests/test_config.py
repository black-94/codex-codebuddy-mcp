from __future__ import annotations

from pathlib import Path

import pytest

from harness_acp_mcp.config import load_settings


def test_example_config_matches_runtime_contract() -> None:
    settings = load_settings("config.example.yaml")

    assert settings.schema_version == 1
    assert settings.authentication.rate_limit.enabled is True
    assert settings.authentication.rate_limit.min_interval_seconds == 60
    assert settings.authentication.rate_limit.max_attempts == 10
    assert settings.process.turn_timeout_seconds == 1800
    assert settings.persistence.sessions_path.endswith("sessions.sqlite3")
    assert settings.buffers.max_read_bytes == 1024 * 1024
    assert settings.logging.max_bytes == 10 * 1024 * 1024
    assert settings.logging.backup_count == 3


def test_rate_limiting_can_be_disabled_and_reconfigured(tmp_path: Path) -> None:
    config = tmp_path / "settings.yaml"
    config.write_text(
        "schema_version: 1\n"
        "authentication:\n"
        "  timeout_seconds: 30\n"
        "  max_concurrent_targets: 4\n"
        "  rate_limit:\n"
        "    enabled: false\n"
        "    min_interval_seconds: 0\n"
        "    max_attempts: 25\n"
        "    window_seconds: 120\n",
        encoding="utf-8",
    )

    settings = load_settings(str(config))

    assert settings.authentication.timeout_seconds == 30
    assert settings.authentication.max_concurrent_targets == 4
    assert settings.authentication.rate_limit.enabled is False
    assert settings.authentication.rate_limit.min_interval_seconds == 0
    assert settings.authentication.rate_limit.max_attempts == 25


def test_unknown_configuration_is_rejected(tmp_path: Path) -> None:
    config = tmp_path / "settings.yaml"
    config.write_text("schema_version: 1\ndaemon:\n  private_machine: true\n", encoding="utf-8")

    with pytest.raises(ValueError, match="unknown configuration key"):
        load_settings(str(config))


def test_unsupported_schema_version_is_rejected(tmp_path: Path) -> None:
    config = tmp_path / "settings.yaml"
    config.write_text("schema_version: 2\n", encoding="utf-8")

    with pytest.raises(ValueError, match="schema_version"):
        load_settings(str(config))
