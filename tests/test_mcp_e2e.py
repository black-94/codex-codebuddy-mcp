from __future__ import annotations

import asyncio
import json
import stat
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
                "switch_codebuddy_model",
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
                        "model_id": "fake-model-id",
                        "codebuddy_command": sys.executable,
                        "codebuddy_args": [str(FAKE_CODEBUDDY)],
                        "approval_mode": "compatible",
                        "max_read": 4 * 1024 * 1024,
                        "max_output": 64 * 1024,
                    },
                )
            )
            assert created["status"] == "created"
            assert created["codebuddy_session_id"].startswith("fake-session-")
            assert created["model_id"] == "fake-model-id"
            assert created["model_name"] == "Fake Model"
            assert created["approval_mode"] == "compatible"
            assert created["max_read"] == 4 * 1024 * 1024
            assert created["max_output"] == 64 * 1024
            bridge_session_id = created["bridge_session_id"]

            switched = structured(
                await session.call_tool(
                    "switch_codebuddy_model",
                    {
                        "bridge_session_id": bridge_session_id,
                        "model_id": "fake-fast-id",
                    },
                )
            )
            assert switched == {
                "status": "model_switched",
                "bridge_session_id": bridge_session_id,
                "codebuddy_session_id": created["codebuddy_session_id"],
                "model_id": "fake-fast-id",
                "model_name": "Fake Fast Model",
            }

            completed = structured(
                await session.call_tool(
                    "prompt_codebuddy",
                    {"bridge_session_id": bridge_session_id, "prompt": "from-mcp"},
                )
            )
            assert completed["status"] == "completed"
            assert completed["text"] == "echo:from-mcp"
            assert "model_name" not in completed

            large = structured(
                await session.call_tool(
                    "prompt_codebuddy",
                    {
                        "bridge_session_id": bridge_session_id,
                        "prompt": "large-output",
                        "max_output": 128,
                    },
                )
            )
            assert large["text_available_in_file"] is True
            output_path = Path(large["output_path"])
            output_mode = await asyncio.to_thread(lambda: stat.S_IMODE(output_path.stat().st_mode))
            assert output_mode == 0o600
            stored = json.loads(await asyncio.to_thread(output_path.read_text, encoding="utf-8"))
            assert len(stored["text"]) == 10000
            await asyncio.to_thread(output_path.unlink)

            permission = structured(
                await session.call_tool(
                    "prompt_codebuddy",
                    {
                        "bridge_session_id": bridge_session_id,
                        "prompt": "needs permission",
                        "max_output": 128,
                    },
                )
            )
            assert permission["status"] == "permission_required"
            assert permission["permission"]["tool_name"] == "Bash"
            permission_output_path = Path(permission["output_path"])
            assert await asyncio.to_thread(permission_output_path.exists)
            assert "model_name" not in permission

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
            assert not await asyncio.to_thread(permission_output_path.exists)

            recoverable = structured(
                await session.call_tool(
                    "create_codebuddy_session",
                    {
                        "cwd": str(tmp_path),
                        "model_id": "fake-model-id",
                        "codebuddy_command": sys.executable,
                        "codebuddy_args": [str(FAKE_CODEBUDDY)],
                        "approval_mode": "compatible",
                        "max_read": 1024,
                    },
                )
            )
            recoverable_id = recoverable["bridge_session_id"]

            oversized = await session.call_tool(
                "prompt_codebuddy",
                {"bridge_session_id": recoverable_id, "prompt": "large-output"},
            )
            assert oversized.isError is True
            assert "response was discarded" in str(oversized.content)
            assert "compress the answer" in str(oversized.content)

            retried = structured(
                await session.call_tool(
                    "prompt_codebuddy",
                    {"bridge_session_id": recoverable_id, "prompt": "short answer"},
                )
            )
            assert retried["status"] == "completed"
            assert retried["text"] == "echo:short answer"

            await session.call_tool(
                "close_codebuddy_session",
                {"bridge_session_id": recoverable_id},
            )
