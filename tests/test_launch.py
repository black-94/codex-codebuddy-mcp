from __future__ import annotations

import asyncio
import json
import os
import shlex
import signal
import sys
import time
from dataclasses import replace
from pathlib import Path

import pytest

from harness_acp_mcp.acp import AcpClient
from harness_acp_mcp.adapters import get_adapter
from harness_acp_mcp.config import (
    AuthenticationSettings,
    IpcSettings,
    LaunchSettings,
    LoggingSettings,
    RateLimitSettings,
    Settings,
)
from harness_acp_mcp.daemon import HarnessDaemon
from harness_acp_mcp.supervisor import SPEC_ENV, _remote_command

FAKE_HARNESS = Path(__file__).with_name("fake_harness.py")


def settings(tmp_path: Path, *, docker_command: str = "docker") -> Settings:
    return Settings(
        ipc=IpcSettings(socket_path=str(tmp_path / "daemon.sock"),
                        lock_path=str(tmp_path / "daemon.lock")),
        authentication=AuthenticationSettings(
            ledger_path=str(tmp_path / "rate.sqlite3"),
            rate_limit=RateLimitSettings(enabled=False),
        ),
        logging=LoggingSettings(path=str(tmp_path / "daemon.log")),
        launch=LaunchSettings(
            codex_command=shlex.join([sys.executable, str(FAKE_HARNESS)]),
            docker_command=docker_command,
        ),
    )


def test_docker_cwd_is_a_container_path_and_requires_an_image(tmp_path: Path) -> None:
    daemon = HarnessDaemon(settings(tmp_path))
    base = {"harness": "codex", "model_id": "fake-model", "runtime": "docker"}
    with pytest.raises(ValueError, match="absolute container path"):
        daemon._session_config({**base, "cwd": "relative/path", "docker_image": "img:1"})
    with pytest.raises(ValueError, match="docker_image is required"):
        daemon._session_config({**base, "cwd": str(tmp_path)})
    # A comma is a valid character in a container working directory now that cwd is no
    # longer used as a Docker bind-mount option.
    config = daemon._session_config({
        **base, "cwd": str(tmp_path / "a,b"), "docker_image": "img:1",
    })
    assert config.cwd == str(tmp_path / "a,b")
    assert config.docker_image == "img:1"
    assert config.docker_mounts == ()
    assert config.docker_ports == ()
    assert config.docker_host_network is False


def test_daemon_builds_target_runtime_and_permission_modes(tmp_path: Path) -> None:
    daemon = HarnessDaemon(settings(tmp_path))
    config = daemon._session_config({
        "harness": "codex", "cwd": str(tmp_path), "model_id": "fake-model",
        "target": "remote", "remote_host": "ssh-alias", "runtime": "docker",
        "permission_mode": "read", "docker_image": "fake:latest",
    })
    assert config.launch_mode == "ssh"
    assert config.ssh_host == "ssh-alias"
    assert config.max_read_bytes == 100 * 1024 * 1024
    assert config.max_output_bytes == 128 * 1024
    spec = AcpClient(config)._supervisor_spec()
    assert spec["argv"][:3] == ["docker", "exec", "-i"]
    assert spec["argv"][-2:] == [sys.executable, str(FAKE_HARNESS)]
    command = _remote_command(spec)
    assert "docker rm -f" in command

    with pytest.raises(ValueError, match="launch internals"):
        daemon._session_config({
            "harness": "codex", "cwd": str(tmp_path), "model_id": "fake-model",
            "command": "untrusted-command",
        })
    with pytest.raises(ValueError, match="remote_host is required"):
        daemon._session_config({
            "harness": "codex", "cwd": str(tmp_path), "model_id": "fake-model",
            "target": "remote",
        })


def test_permission_mapping_and_unsupported_agy_mode(tmp_path: Path) -> None:
    daemon = HarnessDaemon(settings(tmp_path))
    base = {"harness": "codebuddy", "cwd": str(tmp_path), "model_id": "fake-model"}
    for requested, actual in {
        "read": "plan", "edit": "acceptEdits", "auto": "auto",
        "bypass": "bypassPermissions",
    }.items():
        config = daemon._session_config({**base, "permission_mode": requested})
        assert get_adapter("codebuddy").build_argv(config)[-4] == actual
    agy = daemon._session_config({**base, "harness": "agy", "permission_mode": "bypass"})
    with pytest.raises(ValueError, match="agy_bypass_mode_id must be configured"):
        get_adapter("agy").validate(agy)


@pytest.mark.asyncio
async def test_codex_permission_mode_is_applied_through_acp(tmp_path: Path, monkeypatch) -> None:
    log = tmp_path / "modes.jsonl"
    monkeypatch.setenv("FAKE_MODE_LOG", str(log))
    daemon = HarnessDaemon(settings(tmp_path))
    for requested, actual in (("read", "read-only"), ("bypass", "agent-full-access")):
        config = daemon._session_config({
            "harness": "codex", "cwd": str(tmp_path), "model_id": "fake-model",
            "permission_mode": requested,
        })
        client = AcpClient(config)
        try:
            await client.start_transport()
            await client.open_session()
        finally:
            await client.close()
        assert json.loads(log.read_text().splitlines()[-1])["value"] == actual


@pytest.mark.asyncio
async def test_configured_agy_mode_is_applied_through_acp(tmp_path: Path, monkeypatch) -> None:
    log = tmp_path / "agy-mode.jsonl"
    monkeypatch.setenv("FAKE_MODE_LOG", str(log))
    original = settings(tmp_path)
    launch = replace(
        original.launch,
        agy_command=shlex.join([sys.executable, str(FAKE_HARNESS)]),
        agy_read_mode_id="read-only-mode",
    )
    daemon = HarnessDaemon(replace(original, launch=launch))
    config = daemon._session_config({
        "harness": "agy", "cwd": str(tmp_path), "model_id": "fake-model",
        "permission_mode": "read",
    })
    client = AcpClient(config)
    try:
        await client.start_transport()
        await client.open_session()
    finally:
        await client.close()
    assert json.loads(log.read_text().splitlines()[-1])["modeId"] == "read-only-mode"


def docker_shim(tmp_path: Path, monkeypatch) -> tuple[Path, Path]:
    log = tmp_path / "docker-log.jsonl"
    shim = tmp_path / "docker-shim"
    shim.write_text(
        f"#!{sys.executable}\n"
        "import hashlib, json, os, sys, time\n"
        "with open(os.environ['FAKE_DOCKER_LOG'], 'a') as output:\n"
        "    output.write(json.dumps(sys.argv[1:]) + '\\n')\n"
        "args = sys.argv[1:]\n"
        "if args[0] in ('run', 'start'):\n"
        "    marker = os.environ.get('FAKE_DOCKER_RUN_MARKER')\n"
        "    if marker:\n"
        "        with open(marker, 'w') as handle:\n"
        "            handle.write(str(os.getpid()))\n"
        "    delay = float(os.environ.get('FAKE_DOCKER_RUN_DELAY', '0'))\n"
        "    if delay:\n"
        "        time.sleep(delay)\n"
        "if args[0] == 'run':\n"
        "    print(hashlib.sha256(args[args.index('--name') + 1].encode()).hexdigest())\n"
        "elif args[0] == 'inspect':\n"
        "    if os.environ.get('FAKE_DOCKER_INSPECT_FAIL') == '1':\n"
        "        sys.exit(1)\n"
        "    payload = json.loads(os.environ.get('FAKE_DOCKER_INSPECT_JSON') or '{}')\n"
        "    payload['id'] = args[-1]\n"
        "    payload.setdefault('image', 'fake:latest')\n"
        "    print(json.dumps(payload))\n"
        "if args[0] == 'exec':\n"
        "    if '--workdir' not in args:\n"
        "        sys.exit(0)\n"
        "    image_index = args.index('--workdir') + 2\n"
        "    os.execv(args[image_index + 1], args[image_index + 1:])\n",
        encoding="utf-8",
    )
    shim.chmod(0o700)
    monkeypatch.setenv("FAKE_DOCKER_LOG", str(log))
    return shim, log


async def docker_log(log: Path, minimum: int) -> list[list[str]]:
    entries: list[list[str]] = []
    for _ in range(50):
        contents = await asyncio.to_thread(log.read_text)
        entries = [json.loads(line) for line in contents.splitlines()]
        if len(entries) >= minimum:
            break
        await asyncio.sleep(0.02)
    return entries


@pytest.mark.asyncio
async def test_local_docker_session_uses_stdio_and_removes_container(
    tmp_path: Path, monkeypatch
) -> None:
    shim, log = docker_shim(tmp_path, monkeypatch)
    daemon = HarnessDaemon(settings(tmp_path, docker_command=str(shim)))
    created = await daemon.dispatch("create_session", {
        "harness": "codex", "cwd": str(tmp_path), "model_id": "fake-model",
        "runtime": "docker", "docker_image": "fake:latest",
    })
    try:
        assert created["status"] == "ready"
        assert created["launch_info"]["runtime"] == "docker"
        assert created["launch_info"]["reused_container"] is False
        assert created["launch_info"]["docker_image"] == "fake:latest"
        assert "docker_id" not in created
    finally:
        await daemon.dispatch("close_session", {"session_id": created["session_id"]})
    entries = await docker_log(log, 4)
    assert entries[0][0:2] == ["run", "--detach"]
    assert entries[0][entries[0].index("--entrypoint") + 2] == "fake:latest"
    assert entries[1][0] == "exec" and "mkdir" in entries[1] and "-p" in entries[1]
    assert entries[1][-1] == str(tmp_path)
    assert entries[2][0:2] == ["exec", "-i"]
    assert entries[-1][0:2] == ["rm", "-f"]
    assert entries[-1][2] == entries[0][entries[0].index("--name") + 1]


@pytest.mark.asyncio
async def test_kept_docker_container_can_be_reused(tmp_path: Path, monkeypatch) -> None:
    shim, log = docker_shim(tmp_path, monkeypatch)
    daemon = HarnessDaemon(settings(tmp_path, docker_command=str(shim)))
    first = await daemon.dispatch("create_session", {
        "harness": "codex", "cwd": str(tmp_path), "model_id": "fake-model",
        "runtime": "docker", "docker_image": "fake:latest",
        "container_policy": "keep",
    })
    assert first["status"] == "ready"
    docker_id = first["docker_id"]
    assert len(docker_id) == 64
    await daemon.dispatch("close_session", {"session_id": first["session_id"]})
    # The retained container's real configuration, as ``docker inspect`` would report
    # it. It intentionally differs from the first call's creation options (which had no
    # mounts or ports), proving the reuse result echoes the inspected container rather
    # than the request.
    monkeypatch.setenv("FAKE_DOCKER_INSPECT_JSON", json.dumps({
        "image": "fake:latest",
        "image_id": "sha256:" + "0" * 64,
        "network_mode": "bridge",
        "port_bindings": {"8080/tcp": [{"HostIp": "127.0.0.1", "HostPort": "18080"}]},
        "mounts": [
            {"Type": "bind", "Source": "/host/data", "Destination": "/data",
             "RW": False, "Mode": "ro"},
            {"Type": "volume", "Source": "/var/lib/docker/volumes/v/_data",
             "Destination": "/vol", "RW": True},
        ],
    }))
    second = await daemon.dispatch("create_session", {
        "harness": "codex", "cwd": str(tmp_path), "model_id": "fake-model",
        "runtime": "docker", "docker_id": docker_id,
    })
    assert second["status"] == "ready"
    assert second["launch_info"]["container_policy"] == "keep"
    assert second["launch_info"]["reused_container"] is True
    assert second["docker_id"] == docker_id
    assert second["launch_info"]["docker_image"] == "fake:latest"
    assert second["launch_info"]["mounts"] == [
        {"source": "/host/data", "target": "/data", "read_only": True}
    ]
    assert second["launch_info"]["ports"] == [
        {"host_ip": "127.0.0.1", "host_port": 18080,
         "container_port": 8080, "protocol": "tcp"}
    ]
    assert second["launch_info"]["host_network"] is False
    await daemon.dispatch("close_session", {"session_id": second["session_id"]})
    entries = await docker_log(log, 9)
    assert [item[0] for item in entries] == [
        "run", "exec", "exec", "stop", "inspect", "start", "exec", "exec", "stop",
    ]
    assert entries[4][-1] == docker_id
    assert entries[5][1] == docker_id
    assert entries[7][entries[7].index("--workdir") + 2] == docker_id


@pytest.mark.asyncio
async def test_remote_docker_session_uses_ssh_and_stdio(tmp_path: Path, monkeypatch) -> None:
    docker, log = docker_shim(tmp_path, monkeypatch)
    ssh = ssh_shim(tmp_path)
    original = settings(tmp_path, docker_command=str(docker))
    daemon = HarnessDaemon(replace(original, launch=replace(
        original.launch, ssh_command=str(ssh),
    )))
    created = await daemon.dispatch("create_session", {
        "harness": "codex", "cwd": str(tmp_path), "model_id": "fake-model",
        "target": "remote", "remote_host": "test-alias", "runtime": "docker",
        "docker_image": "fake:latest",
    })
    assert created["status"] == "ready"
    assert created["launch_info"]["target"] == "remote"
    await daemon.dispatch("close_session", {"session_id": created["session_id"]})
    entries = await docker_log(log, 4)
    assert [entry[0] for entry in entries[:4]] == ["run", "exec", "exec", "rm"]


@pytest.mark.asyncio
async def test_reuse_checks_docker_instance_before_start(tmp_path: Path, monkeypatch) -> None:
    docker, log = docker_shim(tmp_path, monkeypatch)
    monkeypatch.setenv("FAKE_DOCKER_INSPECT_FAIL", "1")
    daemon = HarnessDaemon(settings(tmp_path, docker_command=str(docker)))
    with pytest.raises(ValueError, match="does not exist or is unavailable"):
        await daemon.dispatch("create_session", {
            "harness": "codex", "cwd": str(tmp_path), "model_id": "fake-model",
            "runtime": "docker", "docker_id": "a" * 64,
        })
    entries = await docker_log(log, 1)
    assert [entry[0] for entry in entries] == ["inspect"]


def supervisor_spec(
    tmp_path: Path, shim: Path, *, name: str, policy: str, reuse: bool,
    harness_argv: list[str] | None = None,
) -> dict:
    return {
        "launch_mode": "local",
        "argv": harness_argv or [sys.executable, str(FAKE_HARNESS)],
        "cwd": str(tmp_path),
        "harness_env": {},
        "docker_command": str(shim),
        "docker_image": "fake:latest",
        "docker_id": ("a" * 64) if reuse else None,
        "docker_container_name": name,
        "docker_mounts": [],
        "docker_ports": [],
        "docker_host_network": False,
        "container_policy": policy,
        "reuse_container": reuse,
        "startup_timeout_seconds": 30,
        "ssh_host": None,
        "ssh_command": "ssh",
        "remote_pid_file": None,
        "terminate_grace_seconds": 0.2,
        "remote_cleanup_timeout_seconds": 2,
        "metadata_path": str(tmp_path / "supervisor-meta.json"),
    }


async def spawn_supervisor(spec: dict) -> asyncio.subprocess.Process:
    env = os.environ.copy()
    env[SPEC_ENV] = json.dumps(spec, separators=(",", ":"))
    return await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "harness_acp_mcp.supervisor",
        env=env,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )


async def wait_for_file(path: Path, within: float = 5.0) -> None:
    deadline = time.monotonic() + within
    while time.monotonic() < deadline:
        if await asyncio.to_thread(path.exists):
            return
        await asyncio.sleep(0.02)
    raise AssertionError(f"timed out waiting for {path}")


async def wait_for_exit(pid: int, within: float = 5.0) -> bool:
    deadline = time.monotonic() + within
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        await asyncio.sleep(0.02)
    return False


async def read_docker_log(log: Path) -> list[list[str]]:
    contents = await asyncio.to_thread(log.read_text)
    return [json.loads(line) for line in contents.splitlines()]


@pytest.mark.asyncio
async def test_sigterm_during_slow_prepare_kills_cli_and_removes_container(
    tmp_path: Path, monkeypatch
) -> None:
    shim, log = docker_shim(tmp_path, monkeypatch)
    marker = tmp_path / "run-started"
    monkeypatch.setenv("FAKE_DOCKER_RUN_MARKER", str(marker))
    monkeypatch.setenv("FAKE_DOCKER_RUN_DELAY", "30")
    name = "harness-acp-slow-prepare"
    spec = supervisor_spec(tmp_path, shim, name=name, policy="remove", reuse=False)
    process = await spawn_supervisor(spec)
    try:
        await wait_for_file(marker)
        docker_pid = int(marker.read_text())
        started = time.monotonic()
        process.send_signal(signal.SIGTERM)
        code = await asyncio.wait_for(process.wait(), timeout=10)
        elapsed = time.monotonic() - started
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()
    # The supervisor aborts the slow `docker run` instead of waiting out the pull.
    assert code == 143
    assert elapsed < 10
    assert await wait_for_exit(docker_pid)
    entries = await read_docker_log(log)
    assert entries[0][0] == "run"
    # Cleanup happens only after the preparation CLI has been killed and reaped.
    assert entries[-1][0:2] == ["rm", "-f"]
    assert entries[-1][2] == name


@pytest.mark.asyncio
async def test_sigterm_during_slow_reuse_prepare_keeps_caller_container(
    tmp_path: Path, monkeypatch
) -> None:
    shim, log = docker_shim(tmp_path, monkeypatch)
    marker = tmp_path / "start-started"
    monkeypatch.setenv("FAKE_DOCKER_RUN_MARKER", str(marker))
    monkeypatch.setenv("FAKE_DOCKER_RUN_DELAY", "30")
    name = "a" * 64
    spec = supervisor_spec(tmp_path, shim, name=name, policy="keep", reuse=True)
    process = await spawn_supervisor(spec)
    try:
        await wait_for_file(marker)
        process.send_signal(signal.SIGTERM)
        code = await asyncio.wait_for(process.wait(), timeout=10)
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()
    assert code == 143
    await asyncio.sleep(0.3)
    entries = await read_docker_log(log)
    # A reused container belongs to the caller: neither stop nor rm may be issued.
    assert [entry[0] for entry in entries] == ["start"]


@pytest.mark.asyncio
async def test_sigterm_stops_running_direct_harness(tmp_path: Path) -> None:
    name = "harness-acp-signal"
    spec = supervisor_spec(
        tmp_path,
        Path("/nonexistent-docker-shim"),
        name=name,
        policy="remove",
        reuse=False,
        harness_argv=[sys.executable, "-c", "import time; time.sleep(30)"],
    )
    spec["docker_container_name"] = None
    metadata = Path(spec["metadata_path"])
    process = await spawn_supervisor(spec)
    try:
        await wait_for_file(metadata)
        harness_pid = json.loads(await asyncio.to_thread(metadata.read_text))["transport_pid"]
        started = time.monotonic()
        process.send_signal(signal.SIGTERM)
        code = await asyncio.wait_for(process.wait(), timeout=10)
        elapsed = time.monotonic() - started
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()
    assert code == 143
    assert elapsed < 5
    assert await wait_for_exit(harness_pid)


def ssh_shim(tmp_path: Path) -> Path:
    """Simulate an SSH login shell with non-interactive job control support."""
    ssh = tmp_path / "ssh-shim"
    ssh.write_text(
        f"#!{sys.executable}\n"
        "import os, sys\n"
        "os.execv('/bin/bash', ['/bin/bash', '-c', sys.argv[-1]])\n",
        encoding="utf-8",
    )
    ssh.chmod(0o700)
    return ssh


@pytest.mark.asyncio
async def test_local_docker_uses_container_cwd_without_host_chdir(
    tmp_path: Path, monkeypatch
) -> None:
    shim, log = docker_shim(tmp_path, monkeypatch)
    daemon = HarnessDaemon(settings(tmp_path, docker_command=str(shim)))
    # A container-only path proves the supervisor never chdirs to it on the host and
    # never bind-mounts it implicitly.
    container_cwd = "/container-only/path/that/does/not/exist/on/host"
    created = await daemon.dispatch("create_session", {
        "harness": "codex", "cwd": container_cwd, "model_id": "fake-model",
        "runtime": "docker", "docker_image": "fake:latest",
    })
    try:
        assert created["status"] == "ready"
        assert created["launch_info"]["cwd"] == container_cwd
        assert created["launch_info"]["mounts"] == []
    finally:
        await daemon.dispatch("close_session", {"session_id": created["session_id"]})
    entries = await docker_log(log, 4)
    run_args, mkdir_args, exec_args = entries[0], entries[1], entries[2]
    assert not any("src=" in item for item in run_args)
    assert "--workdir" not in run_args
    assert mkdir_args[0] == "exec" and "mkdir" in mkdir_args
    assert mkdir_args[-1] == container_cwd
    assert exec_args[exec_args.index("--workdir") + 1] == container_cwd


@pytest.mark.asyncio
async def test_docker_session_publishes_explicit_mounts_and_ports(
    tmp_path: Path, monkeypatch
) -> None:
    shim, log = docker_shim(tmp_path, monkeypatch)
    daemon = HarnessDaemon(settings(tmp_path, docker_command=str(shim)))
    created = await daemon.dispatch("create_session", {
        "harness": "codex", "cwd": "/workspace", "model_id": "fake-model",
        "runtime": "docker", "docker_image": "fake:latest",
        "mounts": [{"source": str(tmp_path), "target": "/data", "read_only": True}],
        "ports": [{"host_ip": "127.0.0.1", "host_port": 18080, "container_port": 8080}],
    })
    try:
        assert created["status"] == "ready"
        assert created["launch_info"]["mounts"] == [
            {"source": str(tmp_path), "target": "/data", "read_only": True}
        ]
        assert created["launch_info"]["ports"] == [
            {"host_ip": "127.0.0.1", "host_port": 18080,
             "container_port": 8080, "protocol": "tcp"}
        ]
        assert created["launch_info"]["host_network"] is False
    finally:
        await daemon.dispatch("close_session", {"session_id": created["session_id"]})
    run_args = (await docker_log(log, 3))[0]
    assert f"type=bind,src={tmp_path},dst=/data,readonly" in run_args
    assert "127.0.0.1:18080:8080/tcp" in run_args
    assert "--network" not in run_args


@pytest.mark.asyncio
async def test_docker_session_uses_host_network(tmp_path: Path, monkeypatch) -> None:
    shim, log = docker_shim(tmp_path, monkeypatch)
    daemon = HarnessDaemon(settings(tmp_path, docker_command=str(shim)))
    created = await daemon.dispatch("create_session", {
        "harness": "codex", "cwd": "/workspace", "model_id": "fake-model",
        "runtime": "docker", "docker_image": "fake:latest", "host_network": True,
    })
    try:
        assert created["status"] == "ready"
        assert created["launch_info"]["host_network"] is True
    finally:
        await daemon.dispatch("close_session", {"session_id": created["session_id"]})
    run_args = (await docker_log(log, 3))[0]
    assert run_args[run_args.index("--network") + 1] == "host"


@pytest.mark.asyncio
async def test_remote_docker_does_not_chdir_on_host(tmp_path: Path, monkeypatch) -> None:
    docker, log = docker_shim(tmp_path, monkeypatch)
    original = settings(tmp_path, docker_command=str(docker))
    daemon = HarnessDaemon(replace(original, launch=replace(
        original.launch, ssh_command=str(ssh_shim(tmp_path)),
    )))
    container_cwd = "/container-only/path"
    created = await daemon.dispatch("create_session", {
        "harness": "codex", "cwd": container_cwd, "model_id": "fake-model",
        "target": "remote", "remote_host": "test-alias", "runtime": "docker",
        "docker_image": "fake:latest",
    })
    try:
        assert created["status"] == "ready"
    finally:
        await daemon.dispatch("close_session", {"session_id": created["session_id"]})
    entries = await docker_log(log, 4)
    assert [entry[0] for entry in entries[:4]] == ["run", "exec", "exec", "rm"]


@pytest.mark.asyncio
async def test_reused_container_creates_missing_container_cwd(tmp_path: Path, monkeypatch) -> None:
    shim, log = docker_shim(tmp_path, monkeypatch)
    daemon = HarnessDaemon(settings(tmp_path, docker_command=str(shim)))
    docker_id = "b" * 64
    created = await daemon.dispatch("create_session", {
        "harness": "codex", "cwd": "/container-only/reuse", "model_id": "fake-model",
        "runtime": "docker", "docker_id": docker_id,
    })
    try:
        assert created["status"] == "ready"
        assert created["launch_info"]["cwd"] == "/container-only/reuse"
    finally:
        await daemon.dispatch("close_session", {"session_id": created["session_id"]})
    entries = await docker_log(log, 5)
    assert [entry[0] for entry in entries[:5]] == ["inspect", "start", "exec", "exec", "stop"]
    # The reused container's working directory is created inside the container only.
    assert entries[2] == ["exec", docker_id, "mkdir", "-p", "/container-only/reuse"]
    assert entries[3][entries[3].index("--workdir") + 1] == "/container-only/reuse"


@pytest.mark.asyncio
async def test_reused_container_reports_network_mode_and_ipv6_from_inspect(
    tmp_path: Path, monkeypatch
) -> None:
    shim, log = docker_shim(tmp_path, monkeypatch)
    monkeypatch.setenv("FAKE_DOCKER_INSPECT_JSON", json.dumps({
        "image": "example/image:2",
        "image_id": "sha256:" + "0" * 64,
        "network_mode": "host",
        "port_bindings": {
            "8080/tcp": [{"HostIp": "2001:db8::1", "HostPort": "18080"}],
        },
        "mounts": None,
    }))
    daemon = HarnessDaemon(settings(tmp_path, docker_command=str(shim)))
    docker_id = "c" * 64
    created = await daemon.dispatch("create_session", {
        "harness": "codex", "cwd": "/container-only/reuse", "model_id": "fake-model",
        "runtime": "docker", "docker_id": docker_id,
    })
    try:
        assert created["status"] == "ready"
        launch_info = created["launch_info"]
        assert launch_info["reused_container"] is True
        assert launch_info["docker_image"] == "example/image:2"
        assert launch_info["mounts"] == []
        assert launch_info["ports"] == [
            {"host_ip": "2001:db8::1", "host_port": 18080,
             "container_port": 8080, "protocol": "tcp"}
        ]
        assert launch_info["host_network"] is True
    finally:
        await daemon.dispatch("close_session", {"session_id": created["session_id"]})
    entries = await docker_log(log, 5)
    # The reuse path inspects the container before starting it, projecting a JSON view.
    assert entries[0][0] == "inspect"
    assert "--format" in entries[0]
    assert entries[0][-1] == docker_id


@pytest.mark.asyncio
async def test_remote_reused_container_inspects_over_ssh(tmp_path: Path, monkeypatch) -> None:
    docker, log = docker_shim(tmp_path, monkeypatch)
    monkeypatch.setenv("FAKE_DOCKER_INSPECT_JSON", json.dumps({
        "image": "example/image:3",
        "network_mode": "bridge",
        "mounts": [],
    }))
    original = settings(tmp_path, docker_command=str(docker))
    daemon = HarnessDaemon(replace(original, launch=replace(
        original.launch, ssh_command=str(ssh_shim(tmp_path)),
    )))
    docker_id = "d" * 64
    created = await daemon.dispatch("create_session", {
        "harness": "codex", "cwd": "/container-only/reuse", "model_id": "fake-model",
        "target": "remote", "remote_host": "test-alias", "runtime": "docker",
        "docker_id": docker_id,
    })
    try:
        assert created["status"] == "ready"
        assert created["launch_info"]["target"] == "remote"
        assert created["launch_info"]["reused_container"] is True
        assert created["launch_info"]["docker_image"] == "example/image:3"
        assert created["launch_info"]["host_network"] is False
    finally:
        await daemon.dispatch("close_session", {"session_id": created["session_id"]})
    entries = await docker_log(log, 5)
    # Even a remote target inspects via SSH-wrapped Docker before starting.
    assert entries[0][0] == "inspect"
    assert entries[0][-1] == docker_id
