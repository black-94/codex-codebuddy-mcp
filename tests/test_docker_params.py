from __future__ import annotations

import shlex
import sys
from pathlib import Path

import pytest

from harness_acp_mcp.config import (
    AuthenticationSettings,
    IpcSettings,
    LaunchSettings,
    LoggingSettings,
    RateLimitSettings,
    Settings,
)
from harness_acp_mcp.daemon import (
    HarnessDaemon,
    _inspect_image,
    _inspect_mounts,
    _inspect_ports,
)
from harness_acp_mcp.supervisor import (
    _docker_run_args,
    _ensure_container_workdir,
    _remote_command,
)

BASE = {"harness": "codex", "model_id": "fake-model", "runtime": "docker"}


def daemon(tmp_path: Path) -> HarnessDaemon:
    return HarnessDaemon(
        Settings(
            ipc=IpcSettings(
                socket_path=str(tmp_path / "daemon.sock"),
                lock_path=str(tmp_path / "daemon.lock"),
            ),
            authentication=AuthenticationSettings(
                ledger_path=str(tmp_path / "rate.sqlite3"),
                rate_limit=RateLimitSettings(enabled=False),
            ),
            logging=LoggingSettings(path=str(tmp_path / "daemon.log")),
            launch=LaunchSettings(codex_command="codex-acp"),
        )
    )


def test_missing_image_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="docker_image is required"):
        daemon(tmp_path)._session_config({**BASE, "cwd": "/work"})


def test_new_container_builds_from_the_supplied_image(tmp_path: Path) -> None:
    config = daemon(tmp_path)._session_config(
        {**BASE, "cwd": "/work", "docker_image": "example/image:1"}
    )
    assert config.docker_image == "example/image:1"
    assert config.reuse_container is False
    assert config.container_policy == "remove"
    assert config.docker_mounts == ()
    assert config.docker_ports == ()
    assert config.docker_host_network is False


@pytest.mark.parametrize(
    "extra",
    [
        {"docker_image": "example/image:1"},
        {"mounts": [{"source": "/a", "target": "/b"}]},
        {"ports": [{"host_port": 1, "container_port": 2}]},
        {"host_network": True},
        {"docker_id": "a" * 64},
        {"container_policy": "keep"},
    ],
)
def test_direct_runtime_rejects_docker_only_parameters(tmp_path: Path, extra: dict) -> None:
    with pytest.raises(ValueError, match="require runtime=docker"):
        daemon(tmp_path)._session_config(
            {"harness": "codex", "model_id": "fake-model", "cwd": str(tmp_path), **extra}
        )


@pytest.mark.parametrize(
    "extra",
    [
        {"docker_image": "example/image:1"},
        {"mounts": [{"source": "/a", "target": "/b"}]},
        {"ports": [{"host_port": 1, "container_port": 2}]},
        {"host_network": True},
    ],
)
def test_reused_container_rejects_image_mounts_ports_and_network(
    tmp_path: Path, extra: dict
) -> None:
    base = {**BASE, "cwd": "/work", "docker_id": "a" * 64}
    with pytest.raises(ValueError, match="reused container"):
        daemon(tmp_path)._session_config({**base, **extra})


def test_reused_container_needs_no_image_and_defaults_to_keep(tmp_path: Path) -> None:
    config = daemon(tmp_path)._session_config(
        {**BASE, "cwd": "/work", "docker_id": "a" * 64}
    )
    assert config.reuse_container is True
    assert config.docker_image is None
    assert config.container_policy == "keep"
    assert config.docker_container_name == "a" * 64


def test_mounts_are_validated(tmp_path: Path) -> None:
    engine = daemon(tmp_path)
    base = {**BASE, "cwd": "/work", "docker_image": "example/image:1"}
    with pytest.raises(ValueError, match="mounts must be a list"):
        engine._session_config({**base, "mounts": {"source": "/a", "target": "/b"}})
    with pytest.raises(ValueError, match=r"mounts\[0\]\.source must be an absolute path"):
        engine._session_config({**base, "mounts": [{"source": "rel", "target": "/b"}]})
    with pytest.raises(ValueError, match=r"mounts\[0\]\.target must not contain a comma"):
        engine._session_config({**base, "mounts": [{"source": "/a", "target": "/b,c"}]})
    with pytest.raises(ValueError, match="unsupported keys"):
        engine._session_config(
            {**base, "mounts": [{"source": "/a", "target": "/b", "mode": "ro"}]}
        )
    with pytest.raises(ValueError, match="read_only must be boolean"):
        engine._session_config(
            {**base, "mounts": [{"source": "/a", "target": "/b", "read_only": "yes"}]}
        )
    with pytest.raises(ValueError, match="duplicates another mount target"):
        engine._session_config({
            **base,
            "mounts": [
                {"source": "/a", "target": "/b"},
                {"source": "/c", "target": "/b"},
            ],
        })

    config = engine._session_config({
        **base,
        "mounts": [{"source": "/host", "target": "/data", "read_only": True}],
    })
    assert config.docker_mounts[0].source == "/host"
    assert config.docker_mounts[0].target == "/data"
    assert config.docker_mounts[0].read_only is True


def test_ports_are_validated_and_conflict_with_host_network(tmp_path: Path) -> None:
    engine = daemon(tmp_path)
    base = {**BASE, "cwd": "/work", "docker_image": "example/image:1"}
    with pytest.raises(ValueError, match="ports must be a list"):
        engine._session_config({**base, "ports": {"host_port": 1}})
    with pytest.raises(ValueError, match=r"ports\[0\]\.host_port must be between 1 and 65535"):
        engine._session_config({**base, "ports": [{"host_port": 0, "container_port": 2}]})
    with pytest.raises(ValueError, match="protocol must be tcp or udp"):
        engine._session_config(
            {**base, "ports": [{"host_port": 1, "container_port": 2, "protocol": "sctp"}]}
        )
    with pytest.raises(ValueError, match="host_ip must be a valid"):
        engine._session_config(
            {**base, "ports": [{"host_port": 1, "container_port": 2, "host_ip": "nope"}]}
        )
    with pytest.raises(ValueError, match="host_network must be boolean"):
        engine._session_config({**base, "host_network": "yes"})
    with pytest.raises(ValueError, match="host_network cannot be combined"):
        engine._session_config({
            **base, "host_network": True,
            "ports": [{"host_port": 1, "container_port": 2}],
        })

    config = engine._session_config({
        **base,
        "ports": [
            {"host_port": 8080, "container_port": 80, "host_ip": "127.0.0.1"},
            {"host_port": 5353, "container_port": 53, "protocol": "udp"},
        ],
    })
    assert config.docker_ports[0].host_ip == "127.0.0.1"
    assert config.docker_ports[0].protocol == "tcp"
    assert config.docker_ports[1].protocol == "udp"
    assert config.docker_ports[1].host_ip is None


def test_docker_run_args_build_explicit_mounts_and_ports() -> None:
    spec = {
        "docker_container_name": "harness-acp-test",
        "docker_image": "example/image:1",
        "cwd": "/container/work",
        "docker_mounts": [
            {"source": "/host/a", "target": "/data", "read_only": False},
            {"source": "/host/b", "target": "/ro", "read_only": True},
        ],
        "docker_ports": [
            {"host_ip": "127.0.0.1", "host_port": 8080,
             "container_port": 80, "protocol": "tcp"},
            {"host_ip": None, "host_port": 5353, "container_port": 53, "protocol": "udp"},
        ],
        "docker_host_network": False,
    }
    args = _docker_run_args(spec)
    assert args[:4] == ["run", "--detach", "--init", "--name"]
    assert "type=bind,src=/host/a,dst=/data" in args
    assert "type=bind,src=/host/b,dst=/ro,readonly" in args
    assert "127.0.0.1:8080:80/tcp" in args
    assert "5353:53/udp" in args
    assert "--network" not in args
    # cwd is a container path and must never become an implicit bind mount.
    assert not any("src=/container/work" in item for item in args)
    assert args[-1] == "while :; do sleep 3600; done"
    assert "example/image:1" in args


def test_docker_run_args_support_host_network() -> None:
    args = _docker_run_args({
        "docker_container_name": "harness-acp-test",
        "docker_image": "example/image:1",
        "cwd": "/container/work",
        "docker_mounts": [],
        "docker_ports": [],
        "docker_host_network": True,
    })
    assert args[args.index("--network") + 1] == "host"


def test_docker_run_args_bracket_ipv6_host_addresses() -> None:
    args = _docker_run_args({
        "docker_container_name": "harness-acp-test",
        "docker_image": "example/image:1",
        "cwd": "/container/work",
        "docker_mounts": [],
        "docker_ports": [
            {"host_ip": "2001:db8::1", "host_port": 8080,
             "container_port": 80, "protocol": "tcp"},
            {"host_ip": "::1", "host_port": 9090, "container_port": 90, "protocol": "udp"},
            {"host_ip": "127.0.0.1", "host_port": 7070,
             "container_port": 70, "protocol": "tcp"},
            {"host_ip": None, "host_port": 6060, "container_port": 60, "protocol": "tcp"},
        ],
        "docker_host_network": False,
    })
    assert "[2001:db8::1]:8080:80/tcp" in args
    assert "[::1]:9090:90/udp" in args
    assert "127.0.0.1:7070:70/tcp" in args
    assert "6060:60/tcp" in args
    # An unbracketed IPv6 form would be ambiguous for Docker and must not appear.
    assert not any("2001:db8::1:8080" in item for item in args)


def test_ipv6_host_ip_round_trips_from_session_config(tmp_path: Path) -> None:
    config = daemon(tmp_path)._session_config({
        **BASE, "cwd": "/work", "docker_image": "example/image:1",
        "ports": [{"host_ip": "2001:db8::1", "host_port": 8080, "container_port": 80}],
    })
    args = _docker_run_args({
        "docker_container_name": "harness-acp-test",
        "docker_image": config.docker_image,
        "cwd": config.cwd,
        "docker_mounts": [m.as_dict() for m in config.docker_mounts],
        "docker_ports": [p.as_dict() for p in config.docker_ports],
        "docker_host_network": config.docker_host_network,
    })
    assert "[2001:db8::1]:8080:80/tcp" in args


def test_inspect_image_prefers_reference_then_image_id() -> None:
    assert _inspect_image(
        {"image": "example/image:1", "image_id": "sha256:x"}
    ) == "example/image:1"
    assert _inspect_image({"image": "", "image_id": "sha256:y"}) == "sha256:y"
    with pytest.raises(ValueError, match="did not report an image"):
        _inspect_image({})


def test_inspect_mounts_keeps_only_binds_and_reads_read_only() -> None:
    mounts = _inspect_mounts([
        {"Type": "bind", "Source": "/host/a", "Destination": "/data",
         "RW": True, "Mode": ""},
        {"Type": "bind", "Source": "/host/b", "Destination": "/ro",
         "RW": False, "Mode": "ro"},
        # ``RW`` absent: fall back to a comma-separated ``Mode`` containing "ro".
        {"Type": "bind", "Source": "/host/c", "Destination": "/mode-ro", "Mode": "ro,z"},
        # A named volume and a tmpfs are not bind mounts and must be dropped.
        {"Type": "volume", "Source": "/var/lib/docker/volumes/v/_data",
         "Destination": "/vol", "RW": True},
        {"Type": "tmpfs", "Source": "", "Destination": "/tmp", "RW": True},
        {"Type": "bind", "Destination": "/no-source", "RW": True},
    ])
    assert [mount.as_dict() for mount in mounts] == [
        {"source": "/host/a", "target": "/data", "read_only": False},
        {"source": "/host/b", "target": "/ro", "read_only": True},
        {"source": "/host/c", "target": "/mode-ro", "read_only": True},
    ]
    assert _inspect_mounts(None) == ()


def test_inspect_ports_flatten_sort_and_keep_ipv6() -> None:
    ports = _inspect_ports({
        "8080/tcp": [{"HostIp": "127.0.0.1", "HostPort": "18080"}],
        "53/udp": [{"HostIp": "", "HostPort": "5353"}],
        "9090/tcp": [
            {"HostIp": "::1", "HostPort": "19090"},
            {"HostIp": "2001:db8::1", "HostPort": "29090"},
        ],
        # An unpublished binding and an unknown protocol carry no usable port.
        "80/tcp": None,
        "81/sctp": [{"HostIp": "", "HostPort": "18081"}],
    })
    assert [port.as_dict() for port in ports] == [
        {"host_ip": None, "host_port": 5353, "container_port": 53, "protocol": "udp"},
        {"host_ip": "127.0.0.1", "host_port": 18080,
         "container_port": 8080, "protocol": "tcp"},
        {"host_ip": "::1", "host_port": 19090, "container_port": 9090, "protocol": "tcp"},
        {"host_ip": "2001:db8::1", "host_port": 29090,
         "container_port": 9090, "protocol": "tcp"},
    ]
    assert _inspect_ports(None) == ()


def test_ensure_container_workdir_runs_mkdir_inside_container(monkeypatch) -> None:
    captured: dict[str, list[str]] = {}

    def fake_run(argv, **kwargs):
        captured["argv"] = argv

    monkeypatch.setattr("harness_acp_mcp.supervisor.subprocess.run", fake_run)
    _ensure_container_workdir({
        "launch_mode": "local",
        "cwd": "/container-only/work",
        "docker_command": "docker",
        "docker_container_name": "harness-acp-x",
        "startup_timeout_seconds": 5,
    })
    assert captured["argv"] == [
        "docker", "exec", "harness-acp-x", "mkdir", "-p", "/container-only/work",
    ]


def test_ensure_container_workdir_wraps_remote_in_ssh(monkeypatch) -> None:
    captured: dict[str, list[str]] = {}

    def fake_run(argv, **kwargs):
        captured["argv"] = argv

    monkeypatch.setattr("harness_acp_mcp.supervisor.subprocess.run", fake_run)
    _ensure_container_workdir({
        "launch_mode": "ssh",
        "ssh_command": "ssh",
        "ssh_host": "placeholder.invalid",
        "cwd": "/container-only/work",
        "docker_command": "docker",
        "docker_container_name": "harness-acp-x",
        "startup_timeout_seconds": 5,
    })
    argv = captured["argv"]
    assert argv[:3] == ["ssh", "--", "placeholder.invalid"]
    assert "docker exec harness-acp-x mkdir -p /container-only/work" in argv[-1]


def test_ensure_container_workdir_is_skipped_for_direct_runtime(monkeypatch) -> None:
    called: list[object] = []
    monkeypatch.setattr(
        "harness_acp_mcp.supervisor.subprocess.run",
        lambda *args, **kwargs: called.append(args),
    )
    _ensure_container_workdir({"docker_container_name": None, "cwd": "/work"})
    assert called == []


def remote_spec(tmp_path: Path, *, docker_name: str | None) -> dict:
    host_cwd = str(tmp_path)
    return {
        "cwd": host_cwd,
        "argv": [sys.executable, "-c", "pass"],
        "harness_env": {},
        "remote_pid_file": "harness-acp-test.pid",
        "terminate_grace_seconds": 0.01,
        "remote_cleanup_timeout_seconds": 1,
        "ssh_command": "ssh",
        "ssh_host": "placeholder.invalid",
        "docker_command": "docker",
        "docker_container_name": docker_name,
        "container_policy": "remove",
        "reuse_container": False,
    }


def test_remote_direct_wrapper_changes_directory(tmp_path: Path) -> None:
    command = _remote_command(remote_spec(tmp_path, docker_name=None))
    assert f"cd {shlex.quote(str(tmp_path))} || exit 1" in command


def test_remote_docker_wrapper_never_changes_directory(tmp_path: Path) -> None:
    spec = remote_spec(tmp_path, docker_name="harness-acp-test")
    # A container path that does not exist on the host would break a ``cd``.
    spec["cwd"] = "/container-only/work"
    spec["argv"] = [
        "docker", "exec", "-i", "--workdir", "/container-only/work",
        "harness-acp-test", sys.executable, "-c", "pass",
    ]
    command = _remote_command(spec)
    assert "cd " not in command
    assert "--workdir /container-only/work" in command or "--workdir" in command
