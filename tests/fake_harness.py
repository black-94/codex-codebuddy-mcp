#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import subprocess
import sys
import time

authenticated = os.environ.get("FAKE_AUTHENTICATED", "1") == "1"
current_model = "fake-model"
session_id = "fake-session"
pending_prompt: int | str | None = None
pending_kind: str | None = None
pending_auth: int | str | None = None
child: subprocess.Popen[bytes] | None = None

pid_file = os.environ.get("FAKE_CHILD_PID_FILE")
if pid_file:
    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(300)"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    with open(pid_file, "w", encoding="utf-8") as output:
        output.write(f"{os.getpid()} {child.pid}\n")
    if os.environ.get("FAKE_EXIT_AFTER_CHILD") == "1":
        raise SystemExit(0)


def send(message: dict) -> None:
    sys.stdout.write(json.dumps(message, separators=(",", ":")) + "\n")
    sys.stdout.flush()


for raw in sys.stdin:
    message = json.loads(raw)
    method = message.get("method")
    params = message.get("params") or {}
    request_id = message.get("id")
    if method == "initialize":
        methods = [] if os.environ.get("FAKE_NO_AUTH") == "1" else [
            {"id": "browser", "name": "Browser"}
        ]
        send(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {
                    "protocolVersion": 1,
                    "agentInfo": {"name": "fake-harness", "version": "1"},
                    "agentCapabilities": {},
                    "authMethods": methods,
                    "models": {
                        "availableModels": [
                            {"modelId": "fake-model", "name": "Fake Model"},
                            {"modelId": "fake-fast", "name": "Fake Fast"},
                        ],
                        "currentModelId": current_model,
                    },
                },
            }
        )
    elif method == "authentication/status":
        status = (
            {"kind": "chatgpt", "account": {"email": "user@example.invalid"}}
            if authenticated
            else {"kind": "none"}
        )
        send({"jsonrpc": "2.0", "id": request_id, "result": {"authStatus": status}})
    elif method == "_codebuddy.ai/getUserInfo":
        user = {"userId": "fake-user"} if authenticated else None
        send({"jsonrpc": "2.0", "id": request_id, "result": {"userInfo": user}})
    elif method == "authenticate":
        if os.environ.get("FAKE_AUTH_INTERACTION") == "1":
            pending_auth = request_id
            send(
                {
                    "jsonrpc": "2.0",
                    "id": 902,
                    "method": "elicitation/create",
                    "params": {
                        "message": "Enter authentication value",
                        "requestedSchema": {
                            "type": "object",
                            "properties": {"value": {"type": "string"}},
                            "required": ["value"],
                        },
                    },
                }
            )
        else:
            delay = float(os.environ.get("FAKE_AUTH_DELAY", "0"))
            if delay:
                time.sleep(delay)
            authenticated = True
            send({"jsonrpc": "2.0", "id": request_id, "result": {}})
    elif method in {"session/new", "session/load"}:
        if not authenticated and os.environ.get("FAKE_NO_AUTH") != "1":
            send(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "error": {"code": -32000, "message": "authentication required"},
                }
            )
        else:
            if method == "session/load":
                session_id = params["sessionId"]
            send({"jsonrpc": "2.0", "id": request_id, "result": {"sessionId": session_id}})
    elif method == "session/set_model":
        current_model = params["modelId"]
        send({"jsonrpc": "2.0", "id": request_id, "result": {"modelId": current_model}})
    elif method == "session/prompt":
        text = params["prompt"][0]["text"]
        send(
            {
                "jsonrpc": "2.0",
                "method": "session/update",
                "params": {
                    "sessionId": session_id,
                    "update": {
                        "sessionUpdate": "agent_message_chunk",
                        "content": {"type": "text", "text": f"echo:{text}"},
                    },
                },
            }
        )
        if text == "permission":
            pending_prompt = request_id
            pending_kind = "permission"
            send(
                {
                    "jsonrpc": "2.0",
                    "id": 900,
                    "method": "session/request_permission",
                    "params": {
                        "sessionId": session_id,
                        "toolCall": {"title": "Command", "rawInput": {"command": "printf ok"}},
                        "options": [
                            {"kind": "allow", "name": "Allow", "optionId": "allow"},
                            {"kind": "reject", "name": "Deny", "optionId": "deny"},
                        ],
                    },
                }
            )
        elif text in {"information", "elicitation"}:
            pending_prompt = request_id
            pending_kind = text
            send(
                {
                    "jsonrpc": "2.0",
                    "id": 901,
                    "method": (
                        "elicitation/create" if text == "elicitation" else "session/request_input"
                    ),
                    "params": {
                        "sessionId": session_id,
                        "requestId": "info-1",
                        "message": "Choose a value",
                        "schema": {
                            "type": "object",
                            "properties": {"value": {"type": "string"}},
                            "required": ["value"],
                        },
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
    elif request_id == 902 and pending_auth is not None:
        result = message.get("result", {})
        if result.get("action") == "accept":
            authenticated = True
        send({"jsonrpc": "2.0", "id": pending_auth, "result": {}})
        pending_auth = None
    elif request_id in {900, 901} and pending_prompt is not None:
        if pending_kind == "permission":
            value = message.get("result", {}).get("outcome", {}).get("optionId")
        elif pending_kind == "elicitation":
            result = message.get("result", {})
            value = result.get("content", {}).get("value")
            if result.get("action") != "accept":
                value = result.get("action")
        else:
            value = message.get("result", {}).get("content", {}).get("value")
        send(
            {
                "jsonrpc": "2.0",
                "method": "session/update",
                "params": {
                    "sessionId": session_id,
                    "update": {
                        "sessionUpdate": "agent_message_chunk",
                        "content": {"type": "text", "text": f";answer:{value}"},
                    },
                },
            }
        )
        send(
            {
                "jsonrpc": "2.0",
                "id": pending_prompt,
                "result": {"stopReason": "end_turn"},
            }
        )
        pending_prompt = None
        pending_kind = None
    elif method == "session/cancel" and pending_prompt is not None:
        send(
            {
                "jsonrpc": "2.0",
                "id": pending_prompt,
                "result": {"stopReason": "cancelled"},
            }
        )
        pending_prompt = None
