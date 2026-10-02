from __future__ import annotations

import json
import os
import re
import shlex
import signal
import subprocess
import sys
import threading
import time
from contextlib import suppress
from pathlib import Path
from typing import Any, BinaryIO

SPEC_ENV = "HARNESS_ACP_SUPERVISOR_SPEC"
_DOCKER_ID = re.compile(r"[0-9a-f]{64}")

# How a container is released on cleanup. "policy" honours container_policy,
# "force_remove" deletes a container this supervisor may have half-created, and
# "skip" leaves a caller-owned (reused) container untouched.
CLEANUP_POLICY = "policy"
CLEANUP_FORCE_REMOVE = "force_remove"
CLEANUP_SKIP = "skip"


class _Terminated(Exception):
    """Raised when a termination signal arrived before the harness transport started."""


def _container_control_argv(spec: dict[str, Any], args: list[str]) -> list[str]:
    command = [spec["docker_command"], *args]
    if spec["launch_mode"] == "ssh":
        return [
            spec["ssh_command"], "--", spec["ssh_host"],
            shlex.join(command),
        ]
    return command


def _container_cleanup_args(spec: dict[str, Any], action: str = CLEANUP_POLICY) -> list[str]:
    name = spec["docker_container_name"]
    if action == CLEANUP_FORCE_REMOVE:
        return ["rm", "-f", name]
    return ["stop", "-t", "0", name] if spec["container_policy"] == "keep" else [
        "rm", "-f", name,
    ]


# ``PermissionError`` means the group no longer belongs to us: its leader was already
# reaped and the kernel reused the group id. Both cases mean there is nothing to stop.
_GROUP_GONE = (ProcessLookupError, PermissionError)


def _kill_group(pid: int) -> None:
    with suppress(*_GROUP_GONE):
        os.killpg(pid, signal.SIGKILL)


def _terminate_group(pgid: int, grace_seconds: float) -> None:
    with suppress(*_GROUP_GONE):
        os.killpg(pgid, signal.SIGTERM)
    deadline = time.monotonic() + grace_seconds
    while time.monotonic() < deadline:
        try:
            os.killpg(pgid, 0)
        except _GROUP_GONE:
            return
        time.sleep(0.05)
    with suppress(*_GROUP_GONE):
        os.killpg(pgid, signal.SIGKILL)


class _SupervisorState:
    """Shared state between the signal handler thread and the supervisor loop.

    Signal handlers run on the main thread, so this object only records what was
    observed and aborts in-flight processes. All container/remote cleanup happens
    on the main thread after ``_prepare_container`` has returned or been reaped, so
    no two Docker control commands ever race each other.
    """

    def __init__(self) -> None:
        # Reentrant: signal handlers run on the main thread and may interrupt the main
        # thread while it already holds this lock.
        self._lock = threading.RLock()
        self._terminated = False
        self._exit_code = 0
        self._prepare: subprocess.Popen[bytes] | None = None
        self._harness_pgid: int | None = None
        self._grace_seconds = 0.0
        self.prepared_ok = False

    @property
    def terminated(self) -> bool:
        return self._terminated

    @property
    def exit_code(self) -> int:
        return self._exit_code

    def request_termination(self, signum: int) -> None:
        with self._lock:
            if self._terminated:
                return
            self._terminated = True
            self._exit_code = 128 + signum

    def attach_prepare(self, process: subprocess.Popen[bytes]) -> None:
        with self._lock:
            self._prepare = process
            terminate = self._terminated
        if terminate:
            _kill_group(process.pid)

    def detach_prepare(self, process: subprocess.Popen[bytes]) -> None:
        with self._lock:
            if self._prepare is process:
                self._prepare = None

    def attach_harness(self, pgid: int, grace_seconds: float) -> None:
        with self._lock:
            self._harness_pgid = pgid
            self._grace_seconds = grace_seconds
            terminate = self._terminated
        if terminate:
            self.abort_harness()

    def abort_prepare(self) -> None:
        with self._lock:
            process = self._prepare
        if process is not None:
            _kill_group(process.pid)

    def abort_harness(self) -> None:
        with self._lock:
            pgid = self._harness_pgid
            grace_seconds = self._grace_seconds
        if pgid is not None:
            _terminate_group(pgid, grace_seconds)


def _docker_run_args(spec: dict[str, Any]) -> list[str]:
    """Build ``docker run`` arguments from validated, caller-supplied options.

    Every value is a discrete argv element; nothing is interpolated into a shell
    string. ``cwd`` is a container path and is deliberately not bind-mounted: only
    explicit ``docker_mounts`` publish a host directory.
    """
    args = ["run", "--detach", "--init", "--name", spec["docker_container_name"]]
    for mount in spec.get("docker_mounts") or []:
        option = f"type=bind,src={mount['source']},dst={mount['target']}"
        if mount.get("read_only"):
            option += ",readonly"
        args += ["--mount", option]
    for port in spec.get("docker_ports") or []:
        host_ip = port.get("host_ip")
        bind = ""
        if host_ip:
            # Docker requires IPv6 bind addresses to be bracketed: [addr]:host:container.
            bind = f"[{host_ip}]:" if ":" in host_ip else f"{host_ip}:"
        args += [
            "-p",
            f"{bind}{port['host_port']}:{port['container_port']}/{port['protocol']}",
        ]
    if spec.get("docker_host_network"):
        args += ["--network", "host"]
    args += [
        "--entrypoint", "/bin/sh", spec["docker_image"],
        "-c", "while :; do sleep 3600; done",
    ]
    return args


def _prepare_container(spec: dict[str, Any], state: _SupervisorState) -> str:
    name = spec["docker_container_name"]
    if spec["reuse_container"]:
        args = ["start", name]
    else:
        args = _docker_run_args(spec)
    command = _container_control_argv(spec, args)
    timeout = float(spec["startup_timeout_seconds"])
    # The preparation CLI is its own session leader so a termination signal can stop
    # a slow image pull or container start instead of orphaning it.
    process = subprocess.Popen(
        command,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    state.attach_prepare(process)
    try:
        try:
            stdout, stderr = process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            _kill_group(process.pid)
            process.communicate()
            if state.terminated:
                raise _Terminated() from None
            raise
    finally:
        state.detach_prepare(process)
    if state.terminated:
        raise _Terminated()
    if process.returncode:
        message = stderr.decode(errors="replace").strip()[-500:]
        raise RuntimeError(f"failed to prepare Docker container: {message}")
    docker_id = spec.get("docker_id") if spec["reuse_container"] else stdout.decode().strip()
    if not isinstance(docker_id, str) or _DOCKER_ID.fullmatch(docker_id) is None:
        raise RuntimeError("Docker did not return a full container ID")
    return docker_id


def _ensure_container_workdir(spec: dict[str, Any]) -> None:
    """Create the container working directory if it does not already exist.

    ``cwd`` is a container path. This runs ``mkdir -p`` *inside the container only* and
    never touches a host path or adds a bind mount, so ``docker exec --workdir`` can
    succeed even when the path exists in neither the host nor the image. Failure is
    ignored: an image without ``mkdir`` still attempts the harness and surfaces the
    runtime chdir error.
    """
    name = spec.get("docker_container_name")
    if not name:
        return
    command = _container_control_argv(spec, ["exec", name, "mkdir", "-p", spec["cwd"]])
    try:
        subprocess.run(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=float(spec["startup_timeout_seconds"]),
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        pass


def _cleanup_container(spec: dict[str, Any], action: str = CLEANUP_POLICY) -> None:
    if action == CLEANUP_SKIP or not spec.get("docker_container_name"):
        return
    args = _container_cleanup_args(spec, action)
    try:
        subprocess.run(
            _container_control_argv(spec, args),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=float(spec["remote_cleanup_timeout_seconds"]),
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        pass


def _remote_command(spec: dict[str, Any]) -> str:
    program: list[str] = []
    remote_env = spec.get("harness_env") or {}
    if remote_env:
        program.extend(["env", *(f"{key}={value}" for key, value in remote_env.items())])
    program.extend(spec["argv"])
    program_text = shlex.join(program)
    pid_file = shlex.quote(spec["remote_pid_file"])
    grace = float(spec["terminate_grace_seconds"])
    docker_cleanup = ""
    if spec.get("docker_container_name"):
        docker_cleanup = (
            shlex.join([spec["docker_command"], *_container_cleanup_args(spec)])
            + " >/dev/null 2>&1 || true; "
        )
    cleanup = (
        'if [ -n "$harness_pgid" ]; then '
        'kill -TERM -"$harness_pgid" 2>/dev/null || true; '
        f"sleep {grace}; "
        'kill -KILL -"$harness_pgid" 2>/dev/null || true; '
        "fi; "
        + docker_cleanup
        + 'rm -f "$pid_file"'
    )
    # For Docker, ``cwd`` is a container path and the harness runs inside the
    # container (``docker exec --workdir``); the SSH wrapper must not ``cd`` to it on
    # the host. Only a direct remote launch changes the wrapper's working directory.
    preamble: list[str] = []
    if not spec.get("docker_container_name"):
        preamble.append(f"cd {shlex.quote(spec['cwd'])} || exit 1")
    return "\n".join(
        [
            *preamble,
            "umask 077",
            'remote_tmp="${TMPDIR:-/tmp}"',
            f'pid_file="$remote_tmp"/{pid_file}',
            "harness_pid=''",
            "harness_pgid=''",
            f"trap {shlex.quote(cleanup)} EXIT",
            "trap 'exit 143' HUP TERM INT",
            "set -m || exit 1",
            f"{program_text} &",
            "harness_pid=$!",
            'harness_pgid=$(ps -o pgid= -p "$harness_pid" 2>/dev/null | tr -d " ")',
            # With job control, this single-command job leads its own process group.
            # Some restricted hosts deny ps even for a process we just launched.
            'if [ -z "$harness_pgid" ]; then harness_pgid="$harness_pid"; fi',
            'case "$harness_pgid" in ""|*[!0-9]*) exit 1 ;; esac',
            'printf "%s %s\\n" "$harness_pid" "$harness_pgid" > "$pid_file"',
            'wait "$harness_pid"',
        ]
    )


def _cleanup_remote(spec: dict[str, Any], action: str = CLEANUP_POLICY) -> None:
    pid_name = shlex.quote(spec["remote_pid_file"])
    grace = float(spec["terminate_grace_seconds"])
    docker_cleanup = ""
    if action != CLEANUP_SKIP and spec.get("docker_container_name"):
        docker_cleanup = (
            shlex.join([spec["docker_command"], *_container_cleanup_args(spec, action)])
            + " >/dev/null 2>&1 || true; "
        )
    command = (
        'remote_tmp="${TMPDIR:-/tmp}"; '
        f'pid_file="$remote_tmp"/{pid_name}; '
        'if [ -r "$pid_file" ]; then '
        'read harness_pid harness_pgid < "$pid_file"; '
        'case "$harness_pgid" in ""|*[!0-9]*) harness_pgid="" ;; esac; '
        'if [ -n "$harness_pgid" ]; then '
        'kill -TERM -"$harness_pgid" 2>/dev/null || true; '
        f"sleep {grace}; "
        'kill -KILL -"$harness_pgid" 2>/dev/null || true; '
        'fi; rm -f "$pid_file"; fi; ' + docker_cleanup
    )
    argv = [
        spec["ssh_command"],
        "--",
        spec["ssh_host"],
        command,
    ]
    try:
        subprocess.run(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=float(spec["remote_cleanup_timeout_seconds"]),
            check=False,
            start_new_session=True,
        )
    except (OSError, subprocess.TimeoutExpired):
        pass


def _copy(source: BinaryIO, target: BinaryIO, on_eof: Any | None = None) -> None:
    try:
        while True:
            chunk = os.read(source.fileno(), 64 * 1024)
            if not chunk:
                break
            view = memoryview(chunk)
            while view:
                written = os.write(target.fileno(), view)
                view = view[written:]
    except (BrokenPipeError, OSError):
        pass
    finally:
        if on_eof is not None:
            on_eof()


def _write_metadata(
    spec: dict[str, Any], child: subprocess.Popen[bytes], child_pgid: int,
    docker_id: str | None,
) -> None:
    path = spec.get("metadata_path")
    if not path:
        return
    payload = {
        "supervisor_pid": os.getpid(),
        "transport_pid": child.pid,
        "transport_pgid": child_pgid,
        "remote_pid_file": spec.get("remote_pid_file"),
        "docker_id": docker_id,
        "started_at": time.time(),
    }
    metadata = Path(path)
    metadata.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
    os.chmod(metadata, 0o600)


def run(spec: dict[str, Any]) -> int:
    mode = spec["launch_mode"]
    state = _SupervisorState()
    cleanup_lock = threading.Lock()
    cleanup_done = threading.Event()
    cleaned = False
    threads: list[threading.Thread] = []
    returncode = 0

    def container_action() -> str:
        if state.prepared_ok:
            return CLEANUP_POLICY
        # Preparation never completed, so a reused container was never entered and
        # must be left exactly as the caller supplied it.
        return CLEANUP_SKIP if spec.get("reuse_container") else CLEANUP_FORCE_REMOVE

    def perform_cleanup() -> None:
        # Reap an in-flight preparation CLI first so its container is no longer being
        # created when the name-based cleanup runs.
        state.abort_prepare()
        state.abort_harness()
        if mode == "ssh":
            _cleanup_remote(spec, container_action())
        elif spec.get("docker_container_name"):
            _cleanup_container(spec, container_action())

    def cleanup() -> None:
        nonlocal cleaned
        with cleanup_lock:
            if cleaned:
                return
            cleaned = True
        try:
            perform_cleanup()
        finally:
            cleanup_done.set()

    def handle_signal(signum: int, _frame: Any) -> None:
        # Only abort in-flight processes here; the main thread performs the container
        # cleanup after the preparation CLI has been reaped.
        state.request_termination(signum)
        state.abort_prepare()
        state.abort_harness()

    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGHUP, handle_signal)

    try:
        docker_id = None
        if spec.get("docker_container_name"):
            try:
                docker_id = _prepare_container(spec, state)
            except _Terminated:
                return state.exit_code
            state.prepared_ok = True
            if state.terminated:
                return state.exit_code
            _ensure_container_workdir(spec)
        if mode == "local":
            argv = spec["argv"]
            # A Docker harness runs inside the container via ``docker exec
            # --workdir``; the local supervisor must not chdir to that container path.
            cwd = None if spec.get("docker_container_name") else spec["cwd"]
            env = os.environ.copy()
            env.update(spec.get("harness_env") or {})
        else:
            argv = [
                spec["ssh_command"],
                "--",
                spec["ssh_host"],
                _remote_command(spec),
            ]
            cwd = None
            env = os.environ.copy()

        child = subprocess.Popen(
            argv,
            cwd=cwd,
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
            close_fds=True,
        )
        child_pgid = os.getpgid(child.pid)
        state.attach_harness(child_pgid, float(spec["terminate_grace_seconds"]))
        if state.terminated:
            return state.exit_code
        _write_metadata(spec, child, child_pgid, docker_id)

        assert child.stdin is not None
        assert child.stdout is not None
        assert child.stderr is not None
        threads = [
            threading.Thread(
                target=_copy,
                args=(sys.stdin.buffer, child.stdin, cleanup),
                daemon=True,
                name="daemon-to-harness",
            ),
            threading.Thread(
                target=_copy,
                args=(child.stdout, sys.stdout.buffer),
                daemon=True,
                name="harness-to-daemon",
            ),
            threading.Thread(
                target=_copy,
                args=(child.stderr, sys.stderr.buffer),
                daemon=True,
                name="harness-stderr",
            ),
        ]
        for thread in threads:
            thread.start()
        returncode = child.wait()
    finally:
        cleanup()
        cleanup_done.wait()
        for thread in threads[1:]:
            thread.join(timeout=1)
    return state.exit_code if state.terminated else returncode


def main() -> None:
    raw = os.environ.pop(SPEC_ENV, None)
    if not raw:
        raise SystemExit("missing supervisor specification")
    try:
        spec = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise SystemExit(f"invalid supervisor specification: {exc}") from exc
    raise SystemExit(run(spec))


if __name__ == "__main__":
    main()
