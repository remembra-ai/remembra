"""OpenAI Agents SDK integration for Remembra.

Provides RemembraSession, an implementation of the Agents SDK ``Session``
protocol so an agent's conversation history persists in Remembra across runs —
with per-session isolation, entity resolution, and semantic recall over past
sessions.

Usage:
    from agents import Agent, Runner
    from remembra.integrations.openai_agents import RemembraSession

    session = RemembraSession(
        session_id="conversation_123",
        base_url="https://api.remembra.dev",
        api_key="rem_...",
        user_id="user_42",
    )

    agent = Agent(name="Assistant", instructions="Be helpful.")
    result = await Runner.run(agent, "Remember my name is Alice", session=session)
    # Next run recalls the history automatically:
    result = await Runner.run(agent, "What's my name?", session=session)

Requires: pip install remembra openai-agents
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from typing import Any

from remembra.client.memory import Memory, MemoryError

try:  # openai-agents is optional at import time; required to actually run a crew
    from agents.memory import SessionSettings

    _SDK_AVAILABLE = True
except ImportError:  # pragma: no cover - exercised only without the SDK
    SessionSettings = None  # type: ignore[assignment, misc]
    _SDK_AVAILABLE = False


class RemembraSession:
    """Remembra-backed conversation session for the OpenAI Agents SDK.

    Implements the SDK's ``Session`` protocol: ``get_items``, ``add_items``,
    ``pop_item``, ``clear_session``. Each turn item is stored **atomically**
    (one item = one memory, never fact-split or merged) with its full JSON
    preserved for faithful reconstruction and a monotonic sequence for
    ordering. Sessions are isolated by ``session_id`` within a
    ``(user_id, project)`` namespace.

    Args:
        session_id: Unique conversation/session ID.
        base_url: Remembra server URL.
        api_key: API key (omit for a local, auth-disabled server).
        user_id: User ID for memory isolation.
        project: Project namespace.
        ttl: Optional TTL for items (e.g. "30d").
        timeout: Request timeout in seconds.
    """

    def __init__(
        self,
        session_id: str,
        base_url: str = "http://localhost:8787",
        api_key: str | None = None,
        user_id: str = "default",
        project: str = "default",
        ttl: str | None = None,
        timeout: float = 30.0,
        session_settings: Any = None,
    ) -> None:
        self.session_id = session_id
        # Part of the Agents SDK Session protocol (default limit, etc.).
        # Defaults to a real SessionSettings when the SDK is installed;
        # stays None otherwise (the attribute only matters under the SDK).
        self.session_settings = session_settings or (SessionSettings() if _SDK_AVAILABLE else None)
        self._client = Memory(
            base_url=base_url,
            api_key=api_key,
            user_id=user_id,
            project=project,
            timeout=timeout,
        )
        self._ttl = ttl
        self._seq: int | None = None  # lazily seeded from stored max (restart-safe)

    # -- helpers ---------------------------------------------------------

    def _fetch_session_memories(self) -> list[Any]:
        """Return this session's stored memories (most-recent-first, up to 50)."""
        try:
            return list(self._client.recall(filters={"session_id": self.session_id}, limit=50).memories)
        except MemoryError:
            return []

    @staticmethod
    def _seq_of(memory: Any) -> int:
        return int((memory.metadata or {}).get("sequence", 0) or 0)

    @staticmethod
    def _item_of(memory: Any) -> dict[str, Any] | None:
        raw = (memory.metadata or {}).get("agent_item")
        if not raw:
            return None
        try:
            parsed = json.loads(raw)
            return parsed if isinstance(parsed, dict) else None
        except (json.JSONDecodeError, TypeError):
            return None

    @staticmethod
    def _readable(item: dict[str, Any]) -> str:
        """A non-empty, searchable content string for the memory record."""
        content = item.get("content")
        role = item.get("role", "item")
        if isinstance(content, str) and content.strip():
            return f"[{role}] {content}"
        return f"[{role}] {json.dumps(item)[:2000]}"

    # -- Session protocol ------------------------------------------------

    async def get_items(self, limit: int | None = None) -> list[dict[str, Any]]:
        """Return conversation items oldest-first; with ``limit``, the latest N."""
        memories = await asyncio.to_thread(self._fetch_session_memories)
        ordered = sorted(memories, key=self._seq_of)
        if limit is not None and limit >= 0:
            ordered = ordered[-limit:]
        items = [self._item_of(m) for m in ordered]
        return [i for i in items if i is not None]

    async def add_items(self, items: list[dict[str, Any]]) -> None:
        """Append items to the session, each stored atomically and in order."""
        if not items:
            return
        if self._seq is None:
            existing = await asyncio.to_thread(self._fetch_session_memories)
            self._seq = max((self._seq_of(m) for m in existing), default=0)

        for item in items:
            self._seq += 1
            metadata = {
                "session_id": self.session_id,
                "sequence": self._seq,
                "agent_item": json.dumps(item),
            }
            try:
                await asyncio.to_thread(
                    self._client.store,
                    content=self._readable(item),
                    metadata=metadata,
                    ttl=self._ttl,
                    skip_extraction=True,
                )
            except MemoryError:
                pass  # don't break the agent run

    async def pop_item(self) -> dict[str, Any] | None:
        """Remove and return the most recent item, or None if empty."""
        memories = await asyncio.to_thread(self._fetch_session_memories)
        if not memories:
            return None
        latest = max(memories, key=self._seq_of)
        item = self._item_of(latest)
        with contextlib.suppress(MemoryError):
            await asyncio.to_thread(self._client.forget, memory_id=latest.id)
        return item

    async def clear_session(self) -> None:
        """Delete only this session's items (never the user's other memory)."""

        def _clear() -> None:
            for _ in range(200):  # safety bound
                mems = self._fetch_session_memories()
                if not mems:
                    break
                for m in mems:
                    with contextlib.suppress(MemoryError):
                        self._client.forget(memory_id=m.id)

        await asyncio.to_thread(_clear)
        self._seq = None
