"""Check installed console scripts and MCP startup without a real harness or account."""
from __future__ import annotations

import asyncio
import os
import signal
import subprocess
import sysconfig
import tempfile
from contextlib import suppress
from datetime import timedelta
from importlib.metadata import version
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from harness_acp_mcp import __version__

EXPECTED_TOOLS = {
    "create_session", "authenticate", "get_user_info", "set_model",
    "prompt", "respond_interaction", "cancel_turn", "close_session",
}


async def check_install() -> None:
    assert version("harness-acp-mcp") == __version__
    scripts = Path(sysconfig.get_path("scripts"))
    server = scripts / "harness-acp-mcp"
    daemon = scripts / "harness-acp-mcp-daemon"
    assert server.is_file() and daemon.is_file()
    await asyncio.to_thread(
        subprocess.run, [str(daemon), "--help"], check=True, capture_output=True, timeout=10
    )
    # Keep Unix socket paths short on both Linux and macOS; isolate all daemon state.
    with tempfile.TemporaryDirectory(prefix="acp-smoke-", dir="/tmp") as directory:
        runtime = Path(directory)
        socket = runtime / "daemon.sock"
        lock = runtime / "daemon.lock"
        config = runtime / "config.yaml"
        config.write_text(
            "ipc:\n"
            f"  socket_path: '{socket}'\n"
            f"  lock_path: '{lock}'\n"
            "authentication:\n"
            f"  ledger_path: '{runtime / 'rate.sqlite3'}'\n"
            "logging:\n"
            f"  path: '{runtime / 'daemon.log'}'\n",
            encoding="utf-8",
        )
        env = os.environ.copy()
        env.pop("PYTHONPATH", None)
        env["HARNESS_ACP_MCP_CONFIG"] = str(config)
        parameters = StdioServerParameters(command=str(server), env=env, cwd=directory)
        try:
            async with stdio_client(parameters) as (reader, writer):
                async with ClientSession(
                    reader, writer, read_timeout_seconds=timedelta(seconds=20)
                ) as client:
                    result = await client.initialize()
                    assert result.serverInfo.name == "harness-acp-mcp"
                    tools = await client.list_tools()
                    assert {tool.name for tool in tools.tools} == EXPECTED_TOOLS
                    assert socket.exists() and lock.exists(), "daemon did not auto-start"
        finally:
            if lock.exists():
                with suppress(ProcessLookupError):
                    os.kill(int(lock.read_text(encoding="utf-8")), signal.SIGTERM)
                for _ in range(200):
                    if not socket.exists():
                        break
                    await asyncio.sleep(0.05)
                else:
                    with suppress(ProcessLookupError):
                        os.kill(int(lock.read_text(encoding="utf-8")), signal.SIGKILL)
                    raise RuntimeError("smoke daemon did not shut down cleanly")
    print(f"Installed harness-acp-mcp {__version__}: console scripts and MCP handshake passed")


if __name__ == "__main__":
    asyncio.run(check_install())
