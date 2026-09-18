from __future__ import annotations

import pytest

import codex_codebuddy_mcp.config as config_module
from codex_codebuddy_mcp.config import CONFIG_ENV, load_defaults


def test_repository_yaml_defaults() -> None:
    defaults = load_defaults()
    assert defaults.max_read == 1 * 1024 * 1024
    assert defaults.max_output == 64 * 1024
    assert defaults.max_concurrency == 2
    assert defaults.approval_mode == "elicitation"
    assert defaults.timeout_seconds == 900
    assert defaults.startup_timeout_seconds == 60
    assert defaults.turn_cancel_timeout_seconds == 5
    assert defaults.local_process_terminate_timeout_seconds == 5
    assert defaults.remote_ssh_cleanup_timeout_seconds == 10
    assert defaults.stdout_overflow_retry_tolerance == 1
    assert defaults.stderr_tail_buffer_size == 80


def test_config_path_can_override_defaults(tmp_path, monkeypatch) -> None:
    config_path = tmp_path / "bridge.yaml"
    config_path.write_text(
        "max_read: 2MB\n"
        "max_output: 8KB\n"
        "max_concurrency: 4\n"
        "approval_mode: compatible\n"
        "timeout_seconds: 12.5\n"
        "startup_timeout_seconds: 3\n"
        "turn_cancel_timeout_seconds: 0.5\n"
        "local_process_terminate_timeout_seconds: 1.25\n"
        "remote_ssh_cleanup_timeout_seconds: 2\n"
        "stdout_overflow_retry_tolerance: 3\n"
        "stderr_tail_buffer_size: 12\n",
        encoding="utf-8",
    )
    monkeypatch.setenv(CONFIG_ENV, str(config_path))

    defaults = load_defaults()
    assert defaults.max_read == 2 * 1024 * 1024
    assert defaults.max_output == 8192
    assert defaults.max_concurrency == 4
    assert defaults.approval_mode == "compatible"
    assert defaults.timeout_seconds == 12.5
    assert defaults.startup_timeout_seconds == 3
    assert defaults.turn_cancel_timeout_seconds == 0.5
    assert defaults.local_process_terminate_timeout_seconds == 1.25
    assert defaults.remote_ssh_cleanup_timeout_seconds == 2
    assert defaults.stdout_overflow_retry_tolerance == 3
    assert defaults.stderr_tail_buffer_size == 12


def test_installed_layout_does_not_probe_above_site_packages(tmp_path, monkeypatch) -> None:
    installed_module = tmp_path / "lib" / "site-packages" / "codex_codebuddy_mcp" / "config.py"
    installed_module.parent.mkdir(parents=True)
    accidental_config = installed_module.parents[2] / "config.yaml"
    accidental_config.write_text("max_concurrency: 99\n", encoding="utf-8")
    monkeypatch.delenv(CONFIG_ENV, raising=False)
    monkeypatch.setattr(config_module, "__file__", str(installed_module))

    assert config_module._config_path() is None
    assert load_defaults().max_concurrency == 2


def test_concurrency_rejects_byte_units(tmp_path, monkeypatch) -> None:
    config_path = tmp_path / "bridge.yaml"
    config_path.write_text("max_concurrency: 2MB\n", encoding="utf-8")
    monkeypatch.setenv(CONFIG_ENV, str(config_path))

    with pytest.raises(ValueError, match="without a unit"):
        load_defaults()


def test_timeout_rejects_units(tmp_path, monkeypatch) -> None:
    config_path = tmp_path / "bridge.yaml"
    config_path.write_text("timeout_seconds: 2m\n", encoding="utf-8")
    monkeypatch.setenv(CONFIG_ENV, str(config_path))

    with pytest.raises(ValueError, match="number of seconds"):
        load_defaults()


def test_stdout_overflow_tolerance_allows_zero(tmp_path, monkeypatch) -> None:
    config_path = tmp_path / "bridge.yaml"
    config_path.write_text("stdout_overflow_retry_tolerance: 0\n", encoding="utf-8")
    monkeypatch.setenv(CONFIG_ENV, str(config_path))

    assert load_defaults().stdout_overflow_retry_tolerance == 0
