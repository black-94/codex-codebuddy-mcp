from __future__ import annotations

import asyncio
import os
import time
import uuid
from contextlib import suppress
from dataclasses import dataclass, field
from typing import Any

from .acp import AcpClient, AcpError
from .auth_store import AuthRateStore
from .config import Settings
from .models import InteractionRequest, SessionConfig


@dataclass(slots=True)
class BridgeSession:
    session_id: str
    record_id: str
    config: SessionConfig
    client: AcpClient
    lock: asyncio.Lock
    state: str = "starting"
    last_activity: float = field(default_factory=time.monotonic)
    turn_slot_held: bool = False
    interaction_timeout_task: asyncio.Task[None] | None = None
    auth_task: asyncio.Task[dict[str, Any]] | None = None
    output_paths: set[str] = field(default_factory=set)

    def touch(self) -> None:
        self.last_activity = time.monotonic()


class SessionRegistry:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._sessions: dict[str, BridgeSession] = {}
        self._lock = asyncio.Lock()
        self._turn_slots = asyncio.Semaphore(settings.daemon.max_concurrency)
        self._auth_slots = asyncio.Semaphore(settings.authentication.max_concurrent_targets)
        self._auth_targets: dict[str, str] = {}
        self._auth_lock = asyncio.Lock()
        self.rate_store = AuthRateStore(settings.authentication)

    async def create(self, config: SessionConfig) -> BridgeSession:
        session_id = uuid.uuid4().hex
        session = BridgeSession(
            session_id=session_id,
            record_id=uuid.uuid4().hex,
            config=config,
            client=AcpClient(config),
            lock=asyncio.Lock(),
        )
        async with self._lock:
            self._sessions[session_id] = session
        return session

    def get(self, session_id: str) -> BridgeSession:
        try:
            session = self._sessions[session_id]
        except KeyError as exc:
            raise KeyError("session not found") from exc
        session.touch()
        return session

    async def acquire_turn(self, session: BridgeSession, timeout_seconds: float) -> None:
        if session.turn_slot_held:
            return
        await asyncio.wait_for(self._turn_slots.acquire(), timeout_seconds)
        session.turn_slot_held = True
        session.touch()

    async def release_turn(self, session: BridgeSession) -> None:
        self.disarm_interaction_timeout(session)
        if session.turn_slot_held:
            session.turn_slot_held = False
            self._turn_slots.release()
        session.touch()

    def arm_interaction_timeout(self, session: BridgeSession) -> None:
        self.disarm_interaction_timeout(session)

        async def expire() -> None:
            try:
                await asyncio.sleep(self.settings.interaction.timeout_seconds)
                async with session.lock:
                    if session.client.pending_interaction is None:
                        return
                    await session.client.cancel_turn()
                    await self.release_turn(session)
            except asyncio.CancelledError:
                pass

        session.interaction_timeout_task = asyncio.create_task(
            expire(), name=f"interaction-timeout-{session.session_id}"
        )

    @staticmethod
    def disarm_interaction_timeout(session: BridgeSession) -> None:
        task = session.interaction_timeout_task
        session.interaction_timeout_task = None
        if task is not None and task is not asyncio.current_task() and not task.done():
            task.cancel()

    async def start_authentication(
        self, session: BridgeSession, method_id: str
    ) -> dict[str, Any]:
        key = self.rate_store.target_key(
            session.config.harness,
            session.config.launch_mode,
            session.config.ssh_host,
        )
        async with self._auth_lock:
            if session.auth_task is not None and not session.auth_task.done():
                task = session.auth_task
            else:
                task = None
            owner = self._auth_targets.get(key)
            if task is None and owner is not None and owner != session.session_id:
                return {
                    "status": "authentication_in_progress",
                    "session_id": session.session_id,
                    "poll_after_seconds": 2,
                }
            if task is None:
                self._auth_targets[key] = session.session_id

                async def run_authentication() -> dict[str, Any]:
                    try:
                        try:
                            info = await session.client.get_auth_info()
                        except Exception:
                            info = None
                        if info is not None and info.authenticated:
                            await session.client.open_session()
                            session.state = "ready"
                            return _session_result(session, "ready")
                        decision = await self.rate_store.check_and_record(key)
                        if not decision.allowed:
                            return {
                                "status": "authentication_rate_limited",
                                "session_id": session.session_id,
                                "retry_after_seconds": decision.retry_after_seconds,
                                "remaining_attempts": decision.remaining_attempts,
                                "window_resets_at": decision.window_resets_at,
                            }
                        async with self._auth_slots:
                            async with asyncio.timeout(session.config.auth_timeout_seconds):
                                session.state = "authenticating"
                                info = await session.client.authenticate(method_id)
                                if not info.authenticated:
                                    session.state = "authentication_required"
                                    raise AcpError(
                                        "authentication completed without a logged-in user"
                                    )
                                await session.client.open_session()
                                session.state = "ready"
                                session.touch()
                                return _session_result(session, "ready")
                    except TimeoutError:
                        await self.close(session.session_id)
                        return {
                            "status": "authentication_timed_out",
                            "session_id": session.session_id,
                        }
                    except Exception:
                        session.state = "authentication_required"
                        raise
                    finally:
                        async with self._auth_lock:
                            if self._auth_targets.get(key) == session.session_id:
                                self._auth_targets.pop(key, None)

                task = asyncio.create_task(
                    run_authentication(), name=f"authenticate-{session.session_id}"
                )
                session.auth_task = task
        return await self.wait_for_authentication_event(session)

    async def wait_for_authentication_event(self, session: BridgeSession) -> dict[str, Any]:
        task = session.auth_task
        if task is None:
            raise AcpError("no authentication operation is active")
        event = await session.client.wait_for_operation_event(task)
        if event["kind"] == "complete":
            return event["result"]
        self.arm_interaction_timeout(session)
        return interaction_result(session, event["interaction"])

    async def close(self, session_id: str) -> bool:
        async with self._lock:
            session = self._sessions.pop(session_id, None)
        if session is None:
            return False
        self.disarm_interaction_timeout(session)
        await self.release_turn(session)
        await session.client.close()
        await self.cleanup_output_paths(session)
        session.state = "closed"
        return True

    async def reap_idle(self) -> int:
        timeout = self.settings.daemon.idle_session_timeout_seconds
        if timeout <= 0:
            return 0
        now = time.monotonic()
        candidates = [
            session.session_id
            for session in self._sessions.values()
            if not session.turn_slot_held
            and session.state != "authenticating"
            and now - session.last_activity >= timeout
        ]
        await asyncio.gather(*(self.close(item) for item in candidates), return_exceptions=True)
        return len(candidates)

    async def close_all(self) -> None:
        async with self._lock:
            sessions = list(self._sessions.values())
            self._sessions.clear()
        for session in sessions:
            self.disarm_interaction_timeout(session)
            await self.release_turn(session)
        await asyncio.gather(
            *(session.client.close() for session in sessions), return_exceptions=True
        )
        await asyncio.gather(
            *(self.cleanup_output_paths(session) for session in sessions),
            return_exceptions=True,
        )

    @staticmethod
    async def cleanup_output_paths(session: BridgeSession) -> None:
        paths = tuple(session.output_paths)
        session.output_paths.clear()

        def remove() -> None:
            for path in paths:
                with suppress(FileNotFoundError):
                    os.unlink(path)

        if paths:
            await asyncio.to_thread(remove)


def interaction_result(
    session: BridgeSession, interaction: InteractionRequest
) -> dict[str, Any]:
    return {
        "status": "interaction_required",
        "session_id": session.session_id,
        "harness_session_id": session.client.session_id,
        "text": session.client.turn_text,
        "tool_calls": session.client.tool_calls,
        "interaction": interaction.as_dict(),
    }


def _session_result(session: BridgeSession, status: str) -> dict[str, Any]:
    result = {
        "status": status,
        "session_id": session.session_id,
        "harness": session.config.harness,
        "harness_session_id": session.client.session_id,
        "model_id": session.client.model_id,
        "model_name": session.client.model_name,
    }
    if session.client.auth_info is not None:
        result.update(session.client.auth_info.as_dict())
    return result
