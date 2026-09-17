from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass
from typing import Any

from .acp import AcpClient
from .models import PermissionRequest, SessionConfig


@dataclass(slots=True)
class BridgeSession:
    bridge_session_id: str
    owner_key: str
    config: SessionConfig
    client: AcpClient
    lock: asyncio.Lock

    @property
    def state(self) -> str:
        if self.client.pending_permission is not None:
            return "permission_required"
        if self.client.turn_active:
            return "running"
        if self.client.running:
            return "ready"
        return "configured"


class SessionRegistry:
    def __init__(self) -> None:
        self._sessions: dict[str, BridgeSession] = {}
        self._lock = asyncio.Lock()

    async def create(self, owner_key: str, config: SessionConfig) -> BridgeSession:
        bridge_session_id = uuid.uuid4().hex
        session = BridgeSession(
            bridge_session_id=bridge_session_id,
            owner_key=owner_key,
            config=config,
            client=AcpClient(config),
            lock=asyncio.Lock(),
        )
        async with self._lock:
            self._sessions[bridge_session_id] = session
        return session

    def get(self, bridge_session_id: str, owner_key: str) -> BridgeSession:
        session = self._sessions.get(bridge_session_id)
        if session is None or session.owner_key != owner_key:
            raise KeyError("bridge session not found")
        return session

    async def close(self, bridge_session_id: str, owner_key: str) -> bool:
        session = self.get(bridge_session_id, owner_key)
        async with self._lock:
            self._sessions.pop(bridge_session_id, None)
        await session.client.close()
        return True

    async def close_all(self) -> None:
        async with self._lock:
            sessions = list(self._sessions.values())
            self._sessions.clear()
        await asyncio.gather(
            *(session.client.close() for session in sessions), return_exceptions=True
        )


def permission_result(session: BridgeSession, permission: PermissionRequest) -> dict[str, Any]:
    return {
        "status": "permission_required",
        "bridge_session_id": session.bridge_session_id,
        "codebuddy_session_id": session.client.session_id,
        "text": session.client.turn_text,
        "tool_calls": session.client.tool_calls,
        "permission": permission.as_dict(),
    }
