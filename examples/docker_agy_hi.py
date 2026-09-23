"""Drive codex-codebuddy-mcp against Google Antigravity (agy_acp_server) in Docker.

`agy_acp_server` is the ACP server shipped inside `agent-harness:3`
(`/opt/agy-acp-server/agy_acp_server.par`). Four non-obvious details:

1. It aborts at startup with `Group nobody not found` because the Google
   launcher wants a group literally named ``nobody`` while Ubuntu 24.04 only
   ships ``nogroup``. This script generates a patched ``/etc/group`` and bind
   mounts it.
2. It only defines ``--debug``/``--notices``; everything else is absl flags, so
   the ``--model/--permission-mode/--acp/--acp-transport stdio`` suffix the
   bridge always appends would be rejected. Wrapping the agent in
   ``bash -c 'exec agy_acp_server'`` turns that suffix into unused shell
   positional parameters.
3. It needs ``ANTIGRAVITY_HARNESS_PATH`` pointing at ``localharness_external``,
   otherwise ``session/new`` fails with "Could not find default localharness
   binary".
4. The bridge decides "already authenticated?" by calling
   ``_codebuddy.ai/getUserInfo``. Antigravity answers that with ``result: null``
   rather than a JSON-RPC "method not found", so the bridge always reports
   "authentication required". ``auth_method_id`` must therefore always be set;
   with a persisted login the ``authenticate`` call round-trips in seconds.

Usage::

    python examples/docker_agy_hi.py [prompt]
"""

# /// script
# requires-python = ">=3.11"
# dependencies = ["mcp>=1.30,<2"]
# ///

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

IMAGE = os.environ.get("AGY_DOCKER_IMAGE", "agent-harness:3")
AUTH_DIR = os.environ.get("AGY_AUTH_DIR", "/srv/agent/auth/agy/acp0")
WORKDIR = Path(os.environ.get("AGY_WORKDIR", str(Path.home() / "agent-workspace"))).expanduser()
MODEL_ID = os.environ.get("AGY_MODEL_ID", "gemini-3.7-flash-high")
AUTH_METHOD = os.environ.get("AGY_AUTH_METHOD", "oauth-personal")
MCP_COMMAND = os.environ.get("CODEX_CODEBUDDY_MCP", "codex-codebuddy-mcp")
HARNESS = "/opt/agy-acp-server/localharness_external"


def patched_group_file() -> str:
    """Container /etc/group plus the ``nobody`` group the launcher expects."""
    base = subprocess.run(
        ["docker", "run", "--rm", IMAGE, "cat", "/etc/group"],
        capture_output=True, text=True, check=True,
    ).stdout
    if "nobody:" not in base:
        base += "nobody:x:65533:\n"
    path = Path(tempfile.gettempdir()) / "agy-patched-group"
    path.write_text(base, encoding="utf-8")
    return str(path)


def docker_agy_args(group_file: str) -> list[str]:
    return [
        "run", "--rm", "-i", "--network=host",
        "-v", f"{group_file}:/etc/group",
        "-v", f"{AUTH_DIR}:/home/master/.gemini",
        "-v", f"{WORKDIR}:{WORKDIR}",
        "-w", str(WORKDIR),
        "-e", f"ANTIGRAVITY_HARNESS_PATH={HARNESS}",
        IMAGE,
        "bash", "-c", "exec agy_acp_server",
    ]


def structured(result: Any) -> dict[str, Any]:
    if result.isError:
        raise RuntimeError(f"MCP tool failed: {result.content}")
    return result.structuredContent or {}


async def main() -> int:
    prompt = sys.argv[1] if len(sys.argv) > 1 else "hi"
    WORKDIR.mkdir(parents=True, exist_ok=True)
    group_file = patched_group_file()

    print(f"[agy] image={IMAGE} auth={AUTH_DIR} workdir={WORKDIR}", flush=True)
    print(f"[agy] patched group file={group_file}", flush=True)
    print(f"[prompt] {prompt}", flush=True)

    params = StdioServerParameters(command=MCP_COMMAND, args=[], env=dict(os.environ))
    async with stdio_client(params) as (read_stream, write_stream):
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()
            created = structured(
                await session.call_tool(
                    "create_codebuddy_session",
                    {
                        "cwd": str(WORKDIR),
                        "model_id": MODEL_ID,
                        # Swallowed by the bash wrapper; Antigravity keeps its
                        # own default model unless session/set_config_option is
                        # used.
                        "launch_mode": "local",
                        "codebuddy_command": "docker",
                        "codebuddy_args": docker_agy_args(group_file),
                        "auth_method_id": AUTH_METHOD,
                        "permission_mode": "auto",
                        "approval_mode": "compatible",
                        "startup_timeout_seconds": 300,
                        "max_read": 8 * 1024 * 1024,
                    },
                )
            )
            print("[session] " + json.dumps(created, ensure_ascii=False), flush=True)
            bridge_session_id = created["bridge_session_id"]
            try:
                result = structured(
                    await session.call_tool(
                        "prompt_codebuddy",
                        {
                            "bridge_session_id": bridge_session_id,
                            "prompt": prompt,
                            "timeout_seconds": 600,
                        },
                    )
                )
                print("[result] " + json.dumps(result, ensure_ascii=False, indent=2))
                return 0 if result.get("status") == "completed" else 1
            finally:
                await session.call_tool(
                    "close_codebuddy_session", {"bridge_session_id": bridge_session_id}
                )
                print("[session] closed", flush=True)


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
