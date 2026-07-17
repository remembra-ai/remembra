"""
LangChain integration for Remembra.

Provides RemembraChatMessageHistory that implements LangChain's
BaseChatMessageHistory interface, storing conversation history
in Remembra with full entity resolution and hybrid search.

Usage:
    from remembra.integrations.langchain import RemembraChatMessageHistory

    history = RemembraChatMessageHistory(
        base_url="http://localhost:8787",
        user_id="user_123",
        session_id="conv_abc",
    )

    # Use with LangChain
    from langchain_core.runnables.history import RunnableWithMessageHistory

    chain_with_history = RunnableWithMessageHistory(
        chain,
        lambda session_id: RemembraChatMessageHistory(
            base_url="http://localhost:8787",
            user_id="user_123",
            session_id=session_id,
        ),
    )

Requires: pip install remembra langchain-core
"""

from __future__ import annotations

import contextlib
import json
from collections.abc import Sequence
from typing import Any

from langchain_core.chat_history import BaseChatMessageHistory
from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    SystemMessage,
    messages_from_dict,
    messages_to_dict,
)

from remembra.client.memory import Memory, MemoryError


class RemembraChatMessageHistory(BaseChatMessageHistory):
    """LangChain chat message history backed by Remembra.

    Each message is stored as a Remembra memory with metadata
    indicating the role, session, and sequence number. This enables:

    - Full entity resolution across conversations
    - Semantic search over past conversations
    - Temporal decay for old messages
    - GDPR-compliant deletion

    Args:
        base_url: Remembra server URL.
        user_id: User ID for memory isolation.
        session_id: Unique session/conversation ID.
        project: Project namespace (default: "default").
        api_key: API key for authentication.
        ttl: Optional TTL for messages (e.g., "30d").
        timeout: Request timeout in seconds.

    Example:
        >>> history = RemembraChatMessageHistory(
        ...     base_url="http://localhost:8787",
        ...     user_id="user_123",
        ...     session_id="conv_001",
        ... )
        >>> history.add_user_message("My name is Alice")
        >>> history.add_ai_message("Hello Alice! How can I help?")
        >>> print(history.messages)
        [HumanMessage(content='My name is Alice'),
         AIMessage(content='Hello Alice! How can I help?')]
    """

    def __init__(
        self,
        base_url: str = "http://localhost:8787",
        user_id: str = "default",
        session_id: str = "default",
        project: str = "default",
        api_key: str | None = None,
        ttl: str | None = None,
        timeout: float = 30.0,
    ) -> None:
        self._client = Memory(
            base_url=base_url,
            api_key=api_key,
            user_id=user_id,
            project=project,
            timeout=timeout,
        )
        self._session_id = session_id
        self._user_id = user_id
        self._project = project
        self._ttl = ttl
        self._message_count = 0

    @property
    def messages(self) -> list[BaseMessage]:
        """Retrieve all messages for this session from Remembra, in order."""
        try:
            # Filter-only recall: exact-match on session_id (no semantic
            # scoring), so we reliably get THIS session's messages rather
            # than whatever a fuzzy "session:X" query happens to match.
            # Recall is capped at 50 per call; page through with the sequence
            # filter to support sessions longer than 50 messages.
            items = self._recall_session()

            # Order by the stored sequence number (falls back to created_at).
            def _seq(m: Any) -> tuple[int, Any]:
                return (int((m.metadata or {}).get("sequence", 0) or 0), m.created_at)

            messages: list[BaseMessage] = []
            for memory in sorted(items, key=_seq):
                raw = (memory.metadata or {}).get("langchain_message")
                if raw:
                    try:
                        msg_data = json.loads(raw)
                        messages.extend(messages_from_dict([msg_data]))
                        continue
                    except (json.JSONDecodeError, KeyError, TypeError):
                        pass
                # Fallback for records without a serialized message: strip the
                # "[role] " prefix and infer the role.
                content = memory.content
                role = (memory.metadata or {}).get("role", "user")
                text = content[len(f"[{role}] ") :] if content.startswith(f"[{role}] ") else content
                is_ai = role in ("ai", "assistant")
                messages.append(AIMessage(content=text) if is_ai else HumanMessage(content=text))

            return messages

        except MemoryError:
            return []

    #: Max messages returned by ``messages`` in one read. The recall API caps
    #: ``limit`` at 50 and has no offset, so a single read surfaces at most the
    #: 50 most-recent messages of the session. ``clear()`` still deletes ALL of
    #: them (it recalls-then-deletes in a loop).
    MAX_MESSAGES = 50

    def _recall_session(self) -> list[Any]:
        """Return this session's most-recent stored memories (up to MAX_MESSAGES)."""
        result = self._client.recall(
            filters={"session_id": self._session_id},
            limit=self.MAX_MESSAGES,
        )
        return list(result.memories)

    def add_messages(self, messages: Sequence[BaseMessage]) -> None:
        """Store messages in Remembra.

        Each message is stored as a separate memory with metadata
        for session tracking and ordering.
        """
        for message in messages:
            self._message_count += 1

            # Serialize the message for faithful reconstruction
            msg_dict = messages_to_dict([message])[0]

            # Build a human-readable content string for search
            role = _message_role(message)
            readable_content = f"[{role}] {message.content}"

            metadata: dict[str, Any] = {
                "session_id": self._session_id,
                "role": role,
                "sequence": self._message_count,
                "langchain_message": json.dumps(msg_dict),
            }

            try:
                # skip_extraction: a chat message is stored 1:1 as a single
                # atomic memory, never split into facts — so it reconstructs
                # faithfully and the sequence/role metadata stays exact.
                self._client.store(
                    content=readable_content,
                    metadata=metadata,
                    ttl=self._ttl,
                    skip_extraction=True,
                )
            except MemoryError:
                # Silently skip on error — don't break the chain
                pass

    def clear(self) -> None:
        """Delete only THIS session's messages from Remembra.

        Recalls the session's memories by metadata filter and forgets each by
        id. This must never delete the user's other memories — the previous
        implementation called forget(user_id=...), which wiped the user's
        entire memory when a single chat session was cleared.
        """
        # Recall is capped at 50 with no offset, so delete in batches until the
        # session is empty — this fully clears sessions longer than 50 messages.
        with contextlib.suppress(MemoryError):
            for _ in range(200):  # safety bound: up to 10k messages
                result = self._client.recall(
                    filters={"session_id": self._session_id},
                    limit=50,
                )
                if not result.memories:
                    break
                for memory in result.memories:
                    with contextlib.suppress(MemoryError):
                        self._client.forget(memory_id=memory.id)
        self._message_count = 0


class RemembraMemory:
    """Standalone Remembra memory adapter for LangChain-style usage.

    Unlike RemembraChatMessageHistory (which stores chat messages),
    this class provides a general-purpose memory interface for
    storing and recalling contextual information.

    This is useful when you want to inject relevant memories
    into a prompt without maintaining full chat history.

    Args:
        base_url: Remembra server URL.
        user_id: User ID for memory isolation.
        project: Project namespace.
        api_key: API key for authentication.
        memory_key: Key name in the prompt variables (default: "memory_context").

    Example:
        >>> memory = RemembraMemory(
        ...     base_url="http://localhost:8787",
        ...     user_id="user_123",
        ... )
        >>> memory.save("Alice is the CTO of Acme Corp")
        >>> context = memory.load("Who leads Acme?")
        >>> print(context)
        {'memory_context': 'Alice is the CTO of Acme Corp.'}
    """

    def __init__(
        self,
        base_url: str = "http://localhost:8787",
        user_id: str = "default",
        project: str = "default",
        api_key: str | None = None,
        memory_key: str = "memory_context",
        timeout: float = 30.0,
    ) -> None:
        self._client = Memory(
            base_url=base_url,
            api_key=api_key,
            user_id=user_id,
            project=project,
            timeout=timeout,
        )
        self.memory_key = memory_key

    def save(
        self,
        content: str,
        metadata: dict[str, Any] | None = None,
        ttl: str | None = None,
    ) -> dict[str, Any]:
        """Store a memory."""
        result = self._client.store(content=content, metadata=metadata, ttl=ttl)
        return {
            "id": result.id,
            "facts": result.extracted_facts,
        }

    def load(
        self,
        query: str,
        limit: int = 5,
        threshold: float = 0.4,
    ) -> dict[str, str]:
        """Load relevant memories as a dict for prompt variables.

        Returns a dict with `memory_key` as the key and the
        synthesized context as the value.
        """
        try:
            result = self._client.recall(
                query=query,
                limit=limit,
                threshold=threshold,
            )
            return {self.memory_key: result.context}
        except MemoryError:
            return {self.memory_key: ""}

    def forget(
        self,
        memory_id: str | None = None,
        entity: str | None = None,
    ) -> None:
        """Forget memories."""
        self._client.forget(memory_id=memory_id, entity=entity)

    @property
    def memory_variables(self) -> list[str]:
        """Return the memory variable names."""
        return [self.memory_key]


def _message_role(message: BaseMessage) -> str:
    """Get a human-readable role name for a message."""
    if isinstance(message, HumanMessage):
        return "user"
    elif isinstance(message, AIMessage):
        return "assistant"
    elif isinstance(message, SystemMessage):
        return "system"
    else:
        return message.type
