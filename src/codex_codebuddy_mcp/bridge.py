from __future__ import annotations

import asyncio
import logging
import os
import uuid
from contextlib import suppress
from dataclasses import dataclass, field
from typing import Any

from .acp import AcpClient
from .models import PermissionRequest, SessionConfig

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class BridgeSession:
    bridge_session_id: str
    owner_key: object
    config: SessionConfig
    client: AcpClient
    lock: asyncio.Lock
    turn_slot_held: bool = False
    permission_timeout_task: asyncio.Task[None] | None = None
    output_paths: set[str] = field(default_factory=set)


class SessionRegistry:
    def __init__(self, max_concurrency: int = 2) -> None:
        if max_concurrency <= 0:
            raise ValueError("max_concurrency must be positive")
        self._sessions: dict[str, BridgeSession] = {}
        self._lock = asyncio.Lock()
        self._turn_slots = asyncio.Semaphore(max_concurrency)
        self.max_concurrency = max_concurrency

    async def acquire_turn(self, session: BridgeSession, timeout_seconds: float) -> None:
        if session.turn_slot_held:
            return
        await asyncio.wait_for(self._turn_slots.acquire(), timeout_seconds)
        session.turn_slot_held = True

    async def release_turn(self, session: BridgeSession) -> None:
        self.disarm_permission_timeout(session)
        if session.turn_slot_held:
            session.turn_slot_held = False
            self._turn_slots.release()

    def arm_permission_timeout(self, session: BridgeSession, timeout_seconds: float) -> None:
        """Cancel a compatible-mode turn whose permission is never answered."""
        self.disarm_permission_timeout(session)

        async def expire_permission() -> None:
            try:
                await asyncio.sleep(timeout_seconds)
                async with session.lock:
                    if session.client.pending_permission is None or not session.turn_slot_held:
                        return
                    logger.warning(
                        "permission response timed out for bridge session %s; cancelling turn",
                        session.bridge_session_id,
                    )
                    try:
                        await session.client.cancel_turn()
                    finally:
                        await self.release_turn(session)
            except asyncio.CancelledError:
                pass
            finally:
                if session.permission_timeout_task is asyncio.current_task():
                    session.permission_timeout_task = None

        session.permission_timeout_task = asyncio.create_task(
            expire_permission(),
            name=f"codebuddy-permission-timeout-{session.bridge_session_id}",
        )

    @staticmethod
    def disarm_permission_timeout(session: BridgeSession) -> None:
        task = session.permission_timeout_task
        session.permission_timeout_task = None
        if task is not None and task is not asyncio.current_task() and not task.done():
            task.cancel()

    @staticmethod
    async def cleanup_output_paths(session: BridgeSession) -> None:
        paths = tuple(session.output_paths)
        session.output_paths.clear()

        def remove_files() -> None:
            for path in paths:
                with suppress(FileNotFoundError):
                    os.unlink(path)

        if paths:
            await asyncio.to_thread(remove_files)

    async def create(self, owner_key: object, config: SessionConfig) -> BridgeSession:
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

    def get(self, bridge_session_id: str, owner_key: object) -> BridgeSession:
        session = self._sessions.get(bridge_session_id)
        same_owner = False
        if session is not None:
            if isinstance(session.owner_key, str) and isinstance(owner_key, str):
                same_owner = session.owner_key == owner_key
            else:
                same_owner = session.owner_key is owner_key
        if session is None or not same_owner:
            raise KeyError("bridge session not found")
        return session

    async def close(self, bridge_session_id: str, owner_key: object) -> bool:
        session = self.get(bridge_session_id, owner_key)
        async with self._lock:
            self._sessions.pop(bridge_session_id, None)
        await self.release_turn(session)
        await session.client.close()
        await self.cleanup_output_paths(session)
        return True

    async def close_all(self) -> None:
        async with self._lock:
            sessions = list(self._sessions.values())
            self._sessions.clear()
        for session in sessions:
            await self.release_turn(session)
        await asyncio.gather(
            *(session.client.close() for session in sessions), return_exceptions=True
        )
        await asyncio.gather(
            *(self.cleanup_output_paths(session) for session in sessions),
            return_exceptions=True,
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
