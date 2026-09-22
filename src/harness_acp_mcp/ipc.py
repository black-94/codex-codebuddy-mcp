from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import time
import uuid
from contextlib import suppress

from .config import Settings


class DaemonRpcError(RuntimeError):
    def __init__(self, error: dict[str, object]) -> None:
        self.error = error
        self.code = error.get("code")
        super().__init__(str(error.get("message") or "daemon request failed"))


class DaemonClient:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._start_lock = asyncio.Lock()

    async def ensure_daemon(self) -> dict[str, object]:
        async with self._start_lock:
            try:
                result = await self.call("ping", {}, ensure=False, request_timeout=1.0)
            except (OSError, TimeoutError, ConnectionError):
                self._spawn_daemon()
                deadline = time.monotonic() + self.settings.ipc.daemon_start_timeout_seconds
                while True:
                    try:
                        result = await self.call(
                            "ping", {}, ensure=False, request_timeout=0.5
                        )
                        break
                    except (OSError, TimeoutError, ConnectionError):
                        if time.monotonic() >= deadline:
                            raise RuntimeError(
                                "daemon did not become ready before startup timeout"
                            ) from None
                        await asyncio.sleep(0.05)
            fingerprint = result.get("config_fingerprint")
            if fingerprint != self.settings.fingerprint:
                raise RuntimeError(
                    "running daemon uses a different configuration; stop it before changing config"
                )
            return result

    def _spawn_daemon(self) -> None:
        subprocess.Popen(
            [sys.executable, "-m", "harness_acp_mcp.daemon"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            start_new_session=os.name != "nt",
            env=os.environ.copy(),
        )

    async def call(
        self,
        method: str,
        params: dict[str, object],
        *,
        ensure: bool = True,
        request_timeout: float | None = None,
    ) -> dict[str, object]:
        if ensure:
            await self.ensure_daemon()
        effective_timeout = (
            request_timeout or self.settings.process.turn_timeout_seconds + 30
        )
        async with asyncio.timeout(effective_timeout):
            reader, writer = await asyncio.open_unix_connection(
                self.settings.ipc.socket_path,
                limit=self.settings.buffers.max_read_bytes,
            )
            request = {
                "jsonrpc": "2.0",
                "id": uuid.uuid4().hex,
                "method": method,
                "params": params,
            }
            writer.write(json.dumps(request, ensure_ascii=False, separators=(",", ":")).encode())
            writer.write(b"\n")
            await writer.drain()
            raw = await reader.readline()
            writer.close()
            with suppress(Exception):
                await writer.wait_closed()
        if not raw:
            raise ConnectionError("daemon closed the connection without a response")
        response = json.loads(raw)
        if "error" in response:
            raise DaemonRpcError(response["error"])
        result = response.get("result")
        if not isinstance(result, dict):
            raise ConnectionError("daemon returned an invalid result")
        return result
