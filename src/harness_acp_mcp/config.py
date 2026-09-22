from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

CONFIG_ENV = "HARNESS_ACP_MCP_CONFIG"
SCHEMA_VERSION = 1
_SIZE = re.compile(r"^(?P<number>[0-9]+)\s*(?P<unit>b|kb|kib|mb|mib|gb|gib)?$", re.I)
_MULTIPLIERS = {
    "b": 1,
    "kb": 1024,
    "kib": 1024,
    "mb": 1024**2,
    "mib": 1024**2,
    "gb": 1024**3,
    "gib": 1024**3,
}


def _runtime_dir() -> Path:
    configured = os.environ.get("XDG_RUNTIME_DIR")
    if configured:
        return Path(configured) / "harness-acp-mcp"
    base = Path("/tmp") if os.name != "nt" else Path(tempfile.gettempdir())
    return base / f"harness-acp-mcp-{os.getuid()}"


def _state_dir() -> Path:
    configured = os.environ.get("XDG_STATE_HOME")
    if configured:
        return Path(configured) / "harness-acp-mcp"
    return Path.home() / ".local" / "state" / "harness-acp-mcp"


def _config_dir() -> Path:
    configured = os.environ.get("XDG_CONFIG_HOME")
    if configured:
        return Path(configured) / "harness-acp-mcp"
    return Path.home() / ".config" / "harness-acp-mcp"


@dataclass(frozen=True, slots=True)
class IpcSettings:
    socket_path: str = field(default_factory=lambda: str(_runtime_dir() / "daemon.sock"))
    lock_path: str = field(default_factory=lambda: str(_runtime_dir() / "daemon.lock"))
    connect_timeout_seconds: float = 2.0
    daemon_start_timeout_seconds: float = 10.0


@dataclass(frozen=True, slots=True)
class DaemonSettings:
    max_concurrency: int = 2
    idle_session_timeout_seconds: float = 3600.0
    reap_interval_seconds: float = 30.0


@dataclass(frozen=True, slots=True)
class RateLimitSettings:
    enabled: bool = True
    min_interval_seconds: float = 60.0
    max_attempts: int = 10
    window_seconds: float = 86400.0


@dataclass(frozen=True, slots=True)
class AuthenticationSettings:
    timeout_seconds: float = 600.0
    max_concurrent_targets: int = 2
    rate_limit: RateLimitSettings = field(default_factory=RateLimitSettings)
    ledger_path: str = field(default_factory=lambda: str(_state_dir() / "auth-rate.sqlite3"))


@dataclass(frozen=True, slots=True)
class InteractionSettings:
    timeout_seconds: float = 900.0


@dataclass(frozen=True, slots=True)
class PersistenceSettings:
    sessions_path: str = field(default_factory=lambda: str(_state_dir() / "sessions.sqlite3"))


@dataclass(frozen=True, slots=True)
class ProcessSettings:
    startup_timeout_seconds: float = 60.0
    turn_timeout_seconds: float = 1800.0
    turn_cancel_timeout_seconds: float = 5.0
    terminate_grace_seconds: float = 5.0
    remote_cleanup_timeout_seconds: float = 10.0


@dataclass(frozen=True, slots=True)
class BufferSettings:
    max_read_bytes: int = 1024 * 1024
    max_output_bytes: int = 64 * 1024
    stdout_overflow_retry_tolerance: int = 1
    stderr_tail_lines: int = 200


@dataclass(frozen=True, slots=True)
class LoggingSettings:
    path: str = field(default_factory=lambda: str(_state_dir() / "daemon.log"))
    level: str = "INFO"
    max_bytes: int = 10 * 1024 * 1024
    backup_count: int = 3


@dataclass(frozen=True, slots=True)
class Settings:
    schema_version: int = SCHEMA_VERSION
    ipc: IpcSettings = field(default_factory=IpcSettings)
    daemon: DaemonSettings = field(default_factory=DaemonSettings)
    authentication: AuthenticationSettings = field(default_factory=AuthenticationSettings)
    interaction: InteractionSettings = field(default_factory=InteractionSettings)
    persistence: PersistenceSettings = field(default_factory=PersistenceSettings)
    process: ProcessSettings = field(default_factory=ProcessSettings)
    buffers: BufferSettings = field(default_factory=BufferSettings)
    logging: LoggingSettings = field(default_factory=LoggingSettings)

    @property
    def fingerprint(self) -> str:
        payload = json.dumps(asdict(self), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode()).hexdigest()


_SCHEMA: dict[str, set[str]] = {
    "": {
        "schema_version",
        "ipc",
        "daemon",
        "authentication",
        "interaction",
        "persistence",
        "process",
        "buffers",
        "logging",
    },
    "ipc": {
        "socket_path",
        "lock_path",
        "connect_timeout_seconds",
        "daemon_start_timeout_seconds",
    },
    "daemon": {"max_concurrency", "idle_session_timeout_seconds", "reap_interval_seconds"},
    "authentication": {
        "timeout_seconds",
        "max_concurrent_targets",
        "ledger_path",
        "rate_limit",
    },
    "authentication.rate_limit": {
        "enabled",
        "min_interval_seconds",
        "max_attempts",
        "window_seconds",
    },
    "interaction": {"timeout_seconds"},
    "persistence": {"sessions_path"},
    "process": {
        "startup_timeout_seconds",
        "turn_timeout_seconds",
        "turn_cancel_timeout_seconds",
        "terminate_grace_seconds",
        "remote_cleanup_timeout_seconds",
    },
    "buffers": {
        "max_read_bytes",
        "max_output_bytes",
        "stdout_overflow_retry_tolerance",
        "stderr_tail_lines",
    },
    "logging": {"path", "level", "max_bytes", "backup_count"},
}


def _scalar(value: str) -> Any:
    value = value.strip()
    if not value:
        raise ValueError("empty YAML scalar")
    if value[:1] in {"'", '"'}:
        if value[-1:] != value[:1]:
            raise ValueError("unterminated quoted YAML scalar")
        return value[1:-1]
    lowered = value.lower()
    if lowered in {"true", "false"}:
        return lowered == "true"
    if lowered in {"null", "~"}:
        return None
    if re.fullmatch(r"-?[0-9]+", value):
        return int(value)
    if re.fullmatch(r"-?[0-9]+(?:\.[0-9]+)?", value):
        return float(value)
    return value


def _parse_yaml_mapping(text: str) -> dict[str, Any]:
    root: dict[str, Any] = {}
    stack: list[tuple[int, dict[str, Any]]] = [(-1, root)]
    for line_number, raw_line in enumerate(text.splitlines(), 1):
        content = raw_line.split("#", 1)[0].rstrip()
        if not content.strip():
            continue
        indentation = len(content) - len(content.lstrip(" "))
        if "\t" in raw_line[:indentation] or indentation % 2:
            raise ValueError(f"invalid YAML indentation on line {line_number}")
        stripped = content.strip()
        if ":" not in stripped:
            raise ValueError(f"invalid YAML mapping on line {line_number}")
        key, raw_value = (item.strip() for item in stripped.split(":", 1))
        if not key:
            raise ValueError(f"empty YAML key on line {line_number}")
        while stack[-1][0] >= indentation:
            stack.pop()
        parent = stack[-1][1]
        if key in parent:
            raise ValueError(f"duplicate YAML key {key!r} on line {line_number}")
        if raw_value:
            parent[key] = _scalar(raw_value)
        else:
            child: dict[str, Any] = {}
            parent[key] = child
            stack.append((indentation, child))
    return root


def _check_keys(data: dict[str, Any], path: str = "") -> None:
    allowed = _SCHEMA.get(path)
    if allowed is None:
        raise ValueError(f"unsupported configuration section: {path}")
    for key, value in data.items():
        if key not in allowed:
            label = f"{path}.{key}" if path else key
            raise ValueError(f"unknown configuration key: {label}")
        child_path = f"{path}.{key}" if path else key
        if isinstance(value, dict):
            _check_keys(value, child_path)


def _positive(value: Any, name: str, *, integer: bool = False) -> int | float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        raise ValueError(f"{name} must be positive")
    if integer and not isinstance(value, int):
        raise ValueError(f"{name} must be an integer")
    return value


def _nonnegative(value: Any, name: str, *, integer: bool = False) -> int | float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
        raise ValueError(f"{name} must be non-negative")
    if integer and not isinstance(value, int):
        raise ValueError(f"{name} must be an integer")
    return value


def _size(value: Any, name: str) -> int:
    if isinstance(value, int) and not isinstance(value, bool):
        return int(_positive(value, name, integer=True))
    if not isinstance(value, str) or (match := _SIZE.fullmatch(value)) is None:
        raise ValueError(f"{name} must be a positive byte count")
    amount = int(match.group("number"))
    unit = (match.group("unit") or "b").lower()
    return int(_positive(amount * _MULTIPLIERS[unit], name, integer=True))


def _path(value: Any, fallback: str, name: str) -> str:
    if value is None:
        return fallback
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a path string or null")
    return str(Path(value).expanduser())


def _section(raw: dict[str, Any], name: str) -> dict[str, Any]:
    value = raw.get(name, {})
    if not isinstance(value, dict):
        raise ValueError(f"{name} must be a mapping")
    return value


def _config_path(explicit: str | None = None) -> Path | None:
    if explicit:
        return Path(explicit).expanduser()
    configured = os.environ.get(CONFIG_ENV)
    if configured:
        return Path(configured).expanduser()
    candidate = _config_dir() / "config.yaml"
    return candidate if candidate.is_file() else None


def load_settings(explicit: str | None = None) -> Settings:
    defaults = Settings()
    path = _config_path(explicit)
    if path is None:
        return defaults
    if not path.is_file():
        raise RuntimeError(f"configured YAML file does not exist: {path}")
    raw = _parse_yaml_mapping(path.read_text(encoding="utf-8"))
    _check_keys(raw)
    version = raw.get("schema_version", SCHEMA_VERSION)
    if version != SCHEMA_VERSION:
        raise ValueError(f"schema_version must be {SCHEMA_VERSION}")

    ipc = _section(raw, "ipc")
    daemon = _section(raw, "daemon")
    authentication = _section(raw, "authentication")
    rate = authentication.get("rate_limit", {})
    if not isinstance(rate, dict):
        raise ValueError("authentication.rate_limit must be a mapping")
    interaction = _section(raw, "interaction")
    persistence = _section(raw, "persistence")
    process = _section(raw, "process")
    buffers = _section(raw, "buffers")
    logging = _section(raw, "logging")

    def positive(section: dict[str, Any], key: str, fallback: int | float) -> float:
        return float(_positive(section.get(key, fallback), key))

    def positive_int(section: dict[str, Any], key: str, fallback: int) -> int:
        return int(_positive(section.get(key, fallback), key, integer=True))

    def nonnegative(section: dict[str, Any], key: str, fallback: int | float) -> float:
        return float(_nonnegative(section.get(key, fallback), key))

    def nonnegative_int(section: dict[str, Any], key: str, fallback: int) -> int:
        return int(_nonnegative(section.get(key, fallback), key, integer=True))

    settings = Settings(
        ipc=IpcSettings(
            socket_path=_path(ipc.get("socket_path"), defaults.ipc.socket_path, "ipc.socket_path"),
            lock_path=_path(ipc.get("lock_path"), defaults.ipc.lock_path, "ipc.lock_path"),
            connect_timeout_seconds=positive(
                ipc, "connect_timeout_seconds", defaults.ipc.connect_timeout_seconds
            ),
            daemon_start_timeout_seconds=positive(
                ipc, "daemon_start_timeout_seconds", defaults.ipc.daemon_start_timeout_seconds
            ),
        ),
        daemon=DaemonSettings(
            max_concurrency=positive_int(
                daemon, "max_concurrency", defaults.daemon.max_concurrency
            ),
            idle_session_timeout_seconds=nonnegative(
                daemon,
                "idle_session_timeout_seconds",
                defaults.daemon.idle_session_timeout_seconds,
            ),
            reap_interval_seconds=positive(
                daemon, "reap_interval_seconds", defaults.daemon.reap_interval_seconds
            ),
        ),
        authentication=AuthenticationSettings(
            timeout_seconds=positive(
                authentication, "timeout_seconds", defaults.authentication.timeout_seconds
            ),
            max_concurrent_targets=positive_int(
                authentication,
                "max_concurrent_targets",
                defaults.authentication.max_concurrent_targets,
            ),
            ledger_path=_path(
                authentication.get("ledger_path"),
                defaults.authentication.ledger_path,
                "authentication.ledger_path",
            ),
            rate_limit=RateLimitSettings(
                enabled=rate.get("enabled", defaults.authentication.rate_limit.enabled),
                min_interval_seconds=nonnegative(
                    rate,
                    "min_interval_seconds",
                    defaults.authentication.rate_limit.min_interval_seconds,
                ),
                max_attempts=positive_int(
                    rate, "max_attempts", defaults.authentication.rate_limit.max_attempts
                ),
                window_seconds=positive(
                    rate, "window_seconds", defaults.authentication.rate_limit.window_seconds
                ),
            ),
        ),
        interaction=InteractionSettings(
            timeout_seconds=positive(
                interaction, "timeout_seconds", defaults.interaction.timeout_seconds
            ),
        ),
        persistence=PersistenceSettings(
            sessions_path=_path(
                persistence.get("sessions_path"),
                defaults.persistence.sessions_path,
                "persistence.sessions_path",
            )
        ),
        process=ProcessSettings(
            startup_timeout_seconds=positive(
                process, "startup_timeout_seconds", defaults.process.startup_timeout_seconds
            ),
            turn_timeout_seconds=positive(
                process, "turn_timeout_seconds", defaults.process.turn_timeout_seconds
            ),
            turn_cancel_timeout_seconds=positive(
                process,
                "turn_cancel_timeout_seconds",
                defaults.process.turn_cancel_timeout_seconds,
            ),
            terminate_grace_seconds=positive(
                process, "terminate_grace_seconds", defaults.process.terminate_grace_seconds
            ),
            remote_cleanup_timeout_seconds=positive(
                process,
                "remote_cleanup_timeout_seconds",
                defaults.process.remote_cleanup_timeout_seconds,
            ),
        ),
        buffers=BufferSettings(
            max_read_bytes=_size(
                buffers.get("max_read_bytes", defaults.buffers.max_read_bytes),
                "buffers.max_read_bytes",
            ),
            max_output_bytes=_size(
                buffers.get("max_output_bytes", defaults.buffers.max_output_bytes),
                "buffers.max_output_bytes",
            ),
            stdout_overflow_retry_tolerance=nonnegative_int(
                buffers,
                "stdout_overflow_retry_tolerance",
                defaults.buffers.stdout_overflow_retry_tolerance,
            ),
            stderr_tail_lines=positive_int(
                buffers, "stderr_tail_lines", defaults.buffers.stderr_tail_lines
            ),
        ),
        logging=LoggingSettings(
            path=_path(logging.get("path"), defaults.logging.path, "logging.path"),
            level=str(logging.get("level", defaults.logging.level)).upper(),
            max_bytes=_size(
                logging.get("max_bytes", defaults.logging.max_bytes), "logging.max_bytes"
            ),
            backup_count=nonnegative_int(
                logging, "backup_count", defaults.logging.backup_count
            ),
        ),
    )
    if not isinstance(settings.authentication.rate_limit.enabled, bool):
        raise ValueError("authentication.rate_limit.enabled must be boolean")
    return settings
