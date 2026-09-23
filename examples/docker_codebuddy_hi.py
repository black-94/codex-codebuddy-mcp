"""Drive codex-codebuddy-mcp over stdio against CodeBuddy running in Docker.

The bridge is a plain MCP server. This script is a minimal MCP client that

1. starts the installed ``codex-codebuddy-mcp`` stdio server,
2. creates a bridge session whose ``codebuddy_command`` is ``docker`` so the
   ACP peer is ``codebuddy --acp --acp-transport stdio`` inside a throwaway
   ``docker run --rm`` container,
3. sends one prompt and prints the answer, and
4. closes the bridge session.

Usage::

    python examples/docker_codebuddy_hi.py [prompt]

Environment overrides::

    CODEBUDDY_DOCKER_IMAGE   image name                  (agent-harness:2)
    CODEBUDDY_DOCKER_AUTH    exported credential dir
                             (/srv/agent/auth/codebuddy/codebuddy1)
    CODEBUDDY_WORKDIR        working dir, same path in the container
                             (~/agent-workspace)
    CODEBUDDY_MODEL_ID       model ID                    (glm-5.2)
    CODEX_CODEBUDDY_MCP      bridge executable           (codex-codebuddy-mcp)
"""

# /// script
# requires-python = ">=3.11"
# dependencies = ["mcp>=1.30,<2"]
# ///

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

IMAGE = os.environ.get("CODEBUDDY_DOCKER_IMAGE", "agent-harness:2")
AUTH_DIR = os.environ.get(
    "CODEBUDDY_DOCKER_AUTH", "/srv/agent/auth/codebuddy/codebuddy1"
)
WORKDIR = Path(
    os.environ.get("CODEBUDDY_WORKDIR", str(Path.home() / "agent-workspace"))
).expanduser()
MODEL_ID = os.environ.get("CODEBUDDY_MODEL_ID", "glm-5.2")
MCP_COMMAND = os.environ.get("CODEX_CODEBUDDY_MCP", "codex-codebuddy-mcp")

# Mounted at the path CodeBuddy Code reads its login state from.
AUTH_TARGET = "/home/master/.local/share/CodeBuddyExtension"


def docker_codebuddy_args() -> list[str]:
    """Arguments placed between ``docker`` and ``codebuddy --acp ...``."""
    return [
        "run",
        "--rm",
        "-i",
        "--network=host",
        "-v",
        f"{AUTH_DIR}:{AUTH_TARGET}",
        "-v",
        f"{WORKDIR}:{WORKDIR}",
        "-w",
        str(WORKDIR),
        IMAGE,
        "codebuddy",
    ]


def structured(result: Any) -> dict[str, Any]:
    if result.isError:
        raise RuntimeError(f"MCP tool failed: {result.content}")
    return result.structuredContent or {}


async def allow_permission(session: ClientSession, payload: dict[str, Any]) -> dict[str, Any]:
    """Answer a compatible-mode permission request by choosing an allow option."""
    permission = payload["permission"]
    options = permission.get("options") or []
    chosen = next(
        (o["optionId"] for o in options if o.get("kind") == "allow" and o.get("optionId")),
        None,
    )
    if chosen is None:
        chosen = next((o["optionId"] for o in options if o.get("optionId")), None)
    if chosen is None:
        raise RuntimeError(f"permission request has no optionId: {permission}")
    print(f"[permission] {permission.get('tool_name')} -> {chosen}", flush=True)
    return structured(
        await session.call_tool(
            "respond_codebuddy_permission",
            {
                "bridge_session_id": payload["bridge_session_id"],
                "request_id": permission["request_id"],
                "option_id": chosen,
            },
        )
    )


async def main() -> int:
    prompt = sys.argv[1] if len(sys.argv) > 1 else "hi"
    WORKDIR.mkdir(parents=True, exist_ok=True)

    print(f"[docker] image={IMAGE} auth={AUTH_DIR} workdir={WORKDIR}", flush=True)
    print(f"[prompt] {prompt}", flush=True)

    params = StdioServerParameters(
        command=MCP_COMMAND,
        args=[],
        env=dict(os.environ),
    )
    async with stdio_client(params) as (read_stream, write_stream):
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()
            created = structured(
                await session.call_tool(
                    "create_codebuddy_session",
                    {
                        "cwd": str(WORKDIR),
                        "model_id": MODEL_ID,
                        "launch_mode": "local",
                        "codebuddy_command": "docker",
                        "codebuddy_args": docker_codebuddy_args(),
                        "permission_mode": "auto",
                        # This client does not advertise elicitation support, so
                        # permissions use the explicit two-step flow.
                        "approval_mode": "compatible",
                        "startup_timeout_seconds": 180,
                        "max_read": 8 * 1024 * 1024,
                    },
                )
            )
            print(
                "[session] bridge={bridge_session_id} codebuddy={codebuddy_session_id} "
                "model={model_id} ({model_name})".format(**created),
                flush=True,
            )

            bridge_session_id = created["bridge_session_id"]
            try:
                result = structured(
                    await session.call_tool(
                        "prompt_codebuddy",
                        {
                            "bridge_session_id": bridge_session_id,
                            "prompt": prompt,
                            "timeout_seconds": 900,
                        },
                    )
                )
                for _ in range(5):
                    if result.get("status") != "permission_required":
                        break
                    result = await allow_permission(session, result)

                print("[result] " + json.dumps(result, ensure_ascii=False, indent=2))
                return 0 if result.get("status") == "completed" else 1
            finally:
                await session.call_tool(
                    "close_codebuddy_session",
                    {"bridge_session_id": bridge_session_id},
                )
                print("[session] closed", flush=True)


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
