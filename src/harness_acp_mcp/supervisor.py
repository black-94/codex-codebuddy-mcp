from __future__ import annotations

import json
import os
import shlex
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, BinaryIO

SPEC_ENV = "HARNESS_ACP_SUPERVISOR_SPEC"


def _remote_command(spec: dict[str, Any]) -> str:
    cwd = shlex.quote(spec["cwd"])
    program: list[str] = []
    remote_env = spec.get("harness_env") or {}
    if remote_env:
        program.extend(["env", *(f"{key}={value}" for key, value in remote_env.items())])
    program.extend(spec["argv"])
    program_text = shlex.join(program)
    pid_file = shlex.quote(spec["remote_pid_file"])
    grace = float(spec["terminate_grace_seconds"])
    cleanup = (
        'if [ -n "$harness_pgid" ]; then '
        'kill -TERM -"$harness_pgid" 2>/dev/null || true; '
        f"sleep {grace}; "
        'kill -KILL -"$harness_pgid" 2>/dev/null || true; '
        "fi; "
        'rm -f "$pid_file"'
    )
    return "\n".join(
        [
            f"cd {cwd} || exit 1",
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
            'harness_pgid=$(ps -o pgid= -p "$harness_pid" | tr -d " ")',
            'case "$harness_pgid" in ""|*[!0-9]*) exit 1 ;; esac',
            'printf "%s %s\\n" "$harness_pid" "$harness_pgid" > "$pid_file"',
            'wait "$harness_pid"',
        ]
    )


def _cleanup_remote(spec: dict[str, Any]) -> None:
    pid_name = shlex.quote(spec["remote_pid_file"])
    grace = float(spec["terminate_grace_seconds"])
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
        "fi; rm -f \"$pid_file\"; fi"
    )
    argv = [
        spec["ssh_command"],
        *spec.get("ssh_args", []),
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
    spec: dict[str, Any], child: subprocess.Popen[bytes], child_pgid: int
) -> None:
    path = spec.get("metadata_path")
    if not path:
        return
    payload = {
        "supervisor_pid": os.getpid(),
        "transport_pid": child.pid,
        "transport_pgid": child_pgid,
        "remote_pid_file": spec.get("remote_pid_file"),
        "started_at": time.time(),
    }
    metadata = Path(path)
    metadata.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
    os.chmod(metadata, 0o600)


def run(spec: dict[str, Any]) -> int:
    mode = spec["launch_mode"]
    if mode == "local":
        argv = spec["argv"]
        cwd = spec["cwd"]
        env = os.environ.copy()
        env.update(spec.get("harness_env") or {})
    else:
        argv = [
            spec["ssh_command"],
            *spec.get("ssh_args", []),
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
    _write_metadata(spec, child, child_pgid)
    cleanup_lock = threading.Lock()
    cleaned = False

    def cleanup() -> None:
        nonlocal cleaned
        with cleanup_lock:
            if cleaned:
                return
            cleaned = True
        if child_pgid is not None:
            try:
                os.killpg(child_pgid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            deadline = time.monotonic() + float(spec["terminate_grace_seconds"])
            while time.monotonic() < deadline:
                try:
                    os.killpg(child_pgid, 0)
                except ProcessLookupError:
                    break
                time.sleep(0.05)
            try:
                os.killpg(child_pgid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        if mode == "ssh":
            _cleanup_remote(spec)

    def handle_signal(_signum: int, _frame: Any) -> None:
        cleanup()

    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGHUP, handle_signal)

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
    cleanup()
    for thread in threads[1:]:
        thread.join(timeout=1)
    return returncode


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
