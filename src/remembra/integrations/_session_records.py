"""The records a chat-history integration stored for one session, and only those.

LangChain's ``RemembraChatMessageHistory`` and the OpenAI Agents
``RemembraSession`` keep one memory per message, tagged with the session's
``session_id``. Other memories can carry the same ``session_id``: an app's own
note, another integration, or anything the Python SDK stored from a client
created with that ``session_id`` (it stamps ``session_id`` into every store).
Reading or clearing a session must not touch those.

Each integration therefore stamps its records with ``MARKER_KEY`` and reads
and deletes by ``session_id`` and marker together. Records stored before the
marker existed are recognised by a metadata key only that integration writes
(``langchain_message``, ``agent_item``).
"""

from __future__ import annotations

import contextlib
from typing import Any

from remembra.client.memory import MemoryError

#: Metadata key naming the integration that stored a record.
MARKER_KEY = "remembra_integration"

#: The recall API returns at most this many records per call.
PAGE = 50

#: clear() stops after this many pages (10,000 records).
MAX_PAGES = 200


def _recall(client: Any, filters: dict[str, str], limit: int) -> list[Any]:
    return list(client.recall(filters=filters, limit=limit).memories)


def _own(memory: Any, own_key: str) -> bool:
    return own_key in (memory.metadata or {})


def session_records(client: Any, session_id: str, marker: str, own_key: str, limit: int = PAGE) -> list[Any]:
    """Up to ``limit`` of this session's records, newest first. Raises MemoryError.

    Marked records come from an exact filter, so other memories under the
    same ``session_id`` never crowd them out. Unmarked (older) records are
    added only when the marked ones do not fill ``limit``.
    """
    marked = _recall(client, {"session_id": session_id, MARKER_KEY: marker}, limit)
    if len(marked) >= limit:
        return marked
    seen = {m.id for m in marked}
    older = [m for m in _recall(client, {"session_id": session_id}, limit) if _own(m, own_key) and m.id not in seen]
    return (marked + older)[:limit]


def delete_session_records(client: Any, session_id: str, marker: str, own_key: str) -> None:
    """Delete every record this integration stored for the session, and nothing else.

    A record that cannot be deleted is tried once, so the loop always ends.
    Unmarked records (stored before the marker existed) are found through
    ``session_id`` alone: if 50 or more newer memories of other code share
    that ``session_id``, unmarked records behind them are not reached.
    """
    tried: set[str] = set()

    def _delete(batch: list[Any]) -> bool:
        batch = [m for m in batch if m.id not in tried]
        for memory in batch:
            tried.add(memory.id)
            with contextlib.suppress(MemoryError):
                client.forget(memory_id=memory.id)
        return bool(batch)

    with contextlib.suppress(MemoryError):
        for _ in range(MAX_PAGES):
            if not _delete(_recall(client, {"session_id": session_id, MARKER_KEY: marker}, PAGE)):
                break
        for _ in range(MAX_PAGES):
            page = [m for m in _recall(client, {"session_id": session_id}, PAGE) if _own(m, own_key)]
            if not _delete(page):
                break
