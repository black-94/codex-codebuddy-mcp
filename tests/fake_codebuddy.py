#!/usr/bin/env python3
"""Small ACP peer used by the bridge tests."""

from __future__ import annotations

import json
import os
import sys

pending_prompt_id: int | str | None = None
pending_session_id: str | None = None


def send(message: dict) -> None:
    sys.stdout.write(json.dumps(message, separators=(",", ":")) + "\n")
    sys.stdout.flush()


for raw_line in sys.stdin:
    message = json.loads(raw_line)
    method = message.get("method")
    params = message.get("params") or {}
    request_id = message.get("id")

    if method == "initialize":
        send(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {
                    "protocolVersion": 1,
                    "agentCapabilities": {},
                    "agentInfo": {"name": "fake-codebuddy", "version": "1"},
                    "authMethods": [{"id": "external", "name": "External"}],
                },
            }
        )
    elif method == "authenticate":
        send({"jsonrpc": "2.0", "id": request_id, "result": {}})
    elif method == "_codebuddy.ai/getUserInfo":
        user_info = {"userId": "fake-user"} if os.environ.get("FAKE_AUTHENTICATED") != "0" else None
        send(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {"userInfo": user_info},
            }
        )
    elif method == "session/new":
        pending_session_id = f"fake-session-{os.getpid()}"
        send(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {"sessionId": pending_session_id},
            }
        )
    elif method == "session/load":
        pending_session_id = params["sessionId"]
        send(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {"sessionId": pending_session_id},
            }
        )
    elif method == "session/prompt":
        prompt = params["prompt"][0]["text"]
        session_id = params["sessionId"]
        send(
            {
                "jsonrpc": "2.0",
                "method": "session/update",
                "params": {
                    "sessionId": session_id,
                    "update": {
                        "sessionUpdate": "agent_message_chunk",
                        "content": {"type": "text", "text": f"echo:{prompt}"},
                    },
                },
            }
        )
        if "permission" in prompt:
            pending_prompt_id = request_id
            send(
                {
                    "jsonrpc": "2.0",
                    "id": 900,
                    "method": "session/request_permission",
                    "params": {
                        "sessionId": session_id,
                        "toolCall": {
                            "toolCallId": "tool-1",
                            "rawInput": {"command": "touch sample.txt"},
                            "_meta": {"codebuddy.ai/toolName": "Bash"},
                        },
                        "options": [
                            {"kind": "allow", "name": "Allow", "optionId": "allow"},
                            {"kind": "reject", "name": "Deny", "optionId": "deny"},
                        ],
                        "_meta": {"codebuddy.ai/requestId": "permission-1"},
                    },
                }
            )
        else:
            send(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "result": {"stopReason": "end_turn"},
                }
            )
    elif request_id == 900 and pending_prompt_id is not None:
        option_id = (message.get("result") or {}).get("outcome", {}).get("optionId")
        send(
            {
                "jsonrpc": "2.0",
                "method": "session/update",
                "params": {
                    "sessionId": pending_session_id,
                    "update": {
                        "sessionUpdate": "agent_message_chunk",
                        "content": {"type": "text", "text": f";permission:{option_id}"},
                    },
                },
            }
        )
        send(
            {
                "jsonrpc": "2.0",
                "id": pending_prompt_id,
                "result": {"stopReason": "end_turn"},
            }
        )
        pending_prompt_id = None
    elif method == "session/cancel" and pending_prompt_id is not None:
        send(
            {
                "jsonrpc": "2.0",
                "id": pending_prompt_id,
                "result": {"stopReason": "cancelled"},
            }
        )
        pending_prompt_id = None
