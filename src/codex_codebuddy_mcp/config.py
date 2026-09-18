from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

DEFAULT_MAX_READ = 1 * 1024 * 1024
DEFAULT_MAX_OUTPUT = 64 * 1024
DEFAULT_MAX_CONCURRENCY = 2
DEFAULT_APPROVAL_MODE = "elicitation"
DEFAULT_TURN_TIMEOUT_SECONDS = 900.0
APPROVAL_MODES = {"elicitation", "compatible"}
CONFIG_ENV = "CODEX_CODEBUDDY_MCP_CONFIG"
_SIZE_VALUE = re.compile(r"^(?P<number>[0-9]+)\s*(?P<unit>b|kb|kib|mb|mib|gb|gib)?$", re.IGNORECASE)
_SIZE_MULTIPLIERS = {
    "b": 1,
    "kb": 1024,
    "kib": 1024,
    "mb": 1024 * 1024,
    "mib": 1024 * 1024,
    "gb": 1024 * 1024 * 1024,
    "gib": 1024 * 1024 * 1024,
}
_BYTE_KEYS = {"max_read", "max_output"}
_PLAIN_INTEGER_KEYS = {"max_concurrency"}
_KNOWN_KEYS = _BYTE_KEYS | _PLAIN_INTEGER_KEYS | {"approval_mode"}


@dataclass(frozen=True, slots=True)
class BridgeDefaults:
    max_read: int = DEFAULT_MAX_READ
    max_output: int = DEFAULT_MAX_OUTPUT
    max_concurrency: int = DEFAULT_MAX_CONCURRENCY
    approval_mode: str = DEFAULT_APPROVAL_MODE


def _validate_positive_int(data: dict[str, Any], key: str, fallback: int) -> int:
    value = data.get(key, fallback)
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{key} must be a positive integer in the MCP YAML config")
    return value


def _config_path() -> Path | None:
    configured = os.environ.get(CONFIG_ENV)
    if configured:
        path = Path(configured).expanduser()
        if not path.exists():
            raise RuntimeError(f"configured MCP YAML file does not exist: {path}")
        return path
    # Editable/source checkouts may use the repository file. Installed wheels
    # use the in-code defaults unless CODEX_CODEBUDDY_MCP_CONFIG is explicit;
    # this avoids accidentally reading a same-named file above site-packages.
    candidate = Path(__file__).resolve().parents[2] / "config.yaml"
    if (candidate.parent / "pyproject.toml").is_file():
        return candidate
    return None


def load_defaults() -> BridgeDefaults:
    path = _config_path()
    if path is None or not path.exists():
        return BridgeDefaults()
    try:
        raw = _parse_yaml_mapping(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise RuntimeError(f"cannot read MCP config {path}: {exc}") from exc
    return BridgeDefaults(
        max_read=_validate_positive_int(raw, "max_read", DEFAULT_MAX_READ),
        max_output=_validate_positive_int(raw, "max_output", DEFAULT_MAX_OUTPUT),
        max_concurrency=_validate_positive_int(raw, "max_concurrency", DEFAULT_MAX_CONCURRENCY),
        approval_mode=_validate_approval_mode(raw.get("approval_mode", DEFAULT_APPROVAL_MODE)),
    )


def _validate_approval_mode(value: Any) -> str:
    if not isinstance(value, str) or value not in APPROVAL_MODES:
        choices = ", ".join(sorted(APPROVAL_MODES))
        raise ValueError(f"approval_mode must be one of {choices} in the MCP YAML config")
    return value


def _parse_yaml_mapping(text: str) -> dict[str, Any]:
    """Parse the deliberately small scalar mapping used by the bridge config.

    Keeping this parser dependency-free is useful for an MCP process launched
    directly by a host. The supported YAML subset is blank/comment lines and
    ``key: integer`` or ``key: integer+unit`` entries, which covers the public
    configuration contract (for example ``max_read: 1MB``).
    """
    result: dict[str, Any] = {}
    for line_number, line in enumerate(text.splitlines(), 1):
        content = line.split("#", 1)[0].strip()
        if not content:
            continue
        if ":" not in content:
            raise ValueError(f"invalid MCP YAML config line {line_number}: {line!r}")
        key, raw_value = (part.strip() for part in content.split(":", 1))
        if not key or not raw_value:
            raise ValueError(f"invalid MCP YAML config line {line_number}: {line!r}")
        if key not in _KNOWN_KEYS:
            raise ValueError(f"unknown MCP YAML config key on line {line_number}: {key!r}")
        if key in result:
            raise ValueError(f"duplicate MCP YAML config key on line {line_number}: {key!r}")
        if raw_value[:1] in {"'", '"'} and raw_value[-1:] == raw_value[:1]:
            raw_value = raw_value[1:-1]
        if key == "approval_mode":
            result[key] = raw_value
            continue
        if key in _PLAIN_INTEGER_KEYS:
            if not raw_value.isdecimal():
                raise ValueError(
                    f"MCP YAML config value for {key!r} must be an integer without a unit"
                )
            result[key] = int(raw_value)
            continue
        match = _SIZE_VALUE.fullmatch(raw_value)
        if match is None:
            raise ValueError(f"MCP YAML config value for {key!r} must be an integer")
        unit = (match.group("unit") or "b").lower()
        result[key] = int(match.group("number")) * _SIZE_MULTIPLIERS[unit]
    return result
