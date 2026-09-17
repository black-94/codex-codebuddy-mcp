from __future__ import annotations

import sys
from pathlib import Path

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

FAKE_CODEBUDDY = Path(__file__).with_name("fake_codebuddy.py")


def structured(result) -> dict:
    assert result.isError is False
    assert isinstance(result.structuredContent, dict)
    return result.structuredContent


@pytest.mark.asyncio
async def test_mcp_stdio_tool_flow(tmp_path: Path) -> None:
    parameters = StdioServerParameters(
        command=sys.executable,
        args=["-m", "codex_codebuddy_mcp"],
    )
    async with stdio_client(parameters) as (read_stream, write_stream):
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()
            tools = await session.list_tools()
            names = {tool.name for tool in tools.tools}
            assert names == {
                "create_codebuddy_session",
                "prompt_codebuddy",
                "respond_codebuddy_permission",
                "cancel_codebuddy_turn",
                "close_codebuddy_session",
            }

            created = structured(
                await session.call_tool(
                    "create_codebuddy_session",
                    {
                        "cwd": str(tmp_path),
                        "codebuddy_command": sys.executable,
                        "codebuddy_args": [str(FAKE_CODEBUDDY)],
                    },
                )
            )
            assert created["status"] == "configured"
            bridge_session_id = created["bridge_session_id"]

            completed = structured(
                await session.call_tool(
                    "prompt_codebuddy",
                    {"bridge_session_id": bridge_session_id, "prompt": "from-mcp"},
                )
            )
            assert completed["status"] == "completed"
            assert completed["text"] == "echo:from-mcp"

            permission = structured(
                await session.call_tool(
                    "prompt_codebuddy",
                    {"bridge_session_id": bridge_session_id, "prompt": "needs permission"},
                )
            )
            assert permission["status"] == "permission_required"
            assert permission["permission"]["tool_name"] == "Bash"

            resumed = structured(
                await session.call_tool(
                    "respond_codebuddy_permission",
                    {
                        "bridge_session_id": bridge_session_id,
                        "request_id": permission["permission"]["request_id"],
                        "option_id": "allow",
                    },
                )
            )
            assert resumed["status"] == "completed"
            assert resumed["text"].endswith(";permission:allow")

            closed = structured(
                await session.call_tool(
                    "close_codebuddy_session",
                    {"bridge_session_id": bridge_session_id},
                )
            )
            assert closed["status"] == "closed"
