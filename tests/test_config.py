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


def test_config_path_can_override_defaults(tmp_path, monkeypatch) -> None:
    config_path = tmp_path / "bridge.yaml"
    config_path.write_text(
        "max_read: 2MB\nmax_output: 8KB\nmax_concurrency: 4\napproval_mode: compatible\n",
        encoding="utf-8",
    )
    monkeypatch.setenv(CONFIG_ENV, str(config_path))

    defaults = load_defaults()
    assert defaults.max_read == 2 * 1024 * 1024
    assert defaults.max_output == 8192
    assert defaults.max_concurrency == 4
    assert defaults.approval_mode == "compatible"


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
