"""The chat-history integrations touch only what they stored for their session (truth audit P-250).

``RemembraChatMessageHistory.clear()`` (LangChain) and
``RemembraSession.clear_session()`` (OpenAI Agents) promise to delete only that
session's messages. They recalled every memory in the user/project whose
metadata ``session_id`` matched and deleted all of them, so an app note, or
anything the Python SDK stored from a client created with the same
``session_id`` (it stamps ``session_id`` into every store), was deleted too.
``messages`` showed such notes as chat turns, and ``pop_item`` could delete
one.

Real routes, ``MemoryService`` and SQLite (``agent_api_harness``; only the
vector store and the embedder are fakes).
"""

from __future__ import annotations

import json
from typing import Any

import pytest

pytest.importorskip("langchain_core")

from langchain_core.messages import AIMessage, HumanMessage, messages_to_dict  # noqa: E402

from remembra.integrations.langchain import RemembraChatMessageHistory  # noqa: E402
from remembra.integrations.openai_agents import RemembraSession  # noqa: E402
from tests.agent_api_harness import build_api  # noqa: E402


@pytest.fixture()
def api(tmp_path):
    yield from build_api(tmp_path)


def _contents(api: dict[str, Any]) -> set[str]:
    return {m["content"] for m in api["http"].get("/api/v1/memories", params={"limit": 100}).json()}


def _history(api: dict[str, Any], session_id: str) -> RemembraChatMessageHistory:
    history = RemembraChatMessageHistory(session_id=session_id)
    history._client = api["make_client"]()
    return history


def _agents_session(api: dict[str, Any], session_id: str) -> RemembraSession:
    session = RemembraSession(session_id=session_id)
    session._client = api["make_client"]()
    return session


def _others(api: dict[str, Any], session_id: str) -> set[str]:
    """Memories other code stored under the same session_id, plus one unrelated note."""
    app = api["make_client"]()
    app.store("App note: invoice 17 is for this thread", metadata={"session_id": session_id}, skip_extraction=True)
    # The SDK stamps its own session_id into every store (provenance).
    agent = api["make_client"](session_id=session_id)
    agent.store("Agent note: the customer prefers email", skip_extraction=True)
    app.store("Unrelated: the office closes at 6pm", skip_extraction=True)
    return {
        "App note: invoice 17 is for this thread",
        "Agent note: the customer prefers email",
        "Unrelated: the office closes at 6pm",
    }


# --------------------------------------------------------------------------
# LangChain
# --------------------------------------------------------------------------


def test_langchain_clear_deletes_only_the_history_it_stored(api) -> None:
    history = _history(api, "thread-42")
    history.add_user_message("My name is Alice")
    history.add_ai_message("Hello Alice")
    others = _others(api, "thread-42")

    assert [type(m) for m in history.messages] == [HumanMessage, AIMessage]
    assert [m.content for m in history.messages] == ["My name is Alice", "Hello Alice"]

    history.clear()

    assert history.messages == []
    assert _contents(api) == others


def test_langchain_clear_reaches_history_behind_many_newer_notes(api) -> None:
    """More than one recall page (50) of other memories share the session_id."""
    history = _history(api, "thread-9")
    history.add_user_message("first")
    history.add_ai_message("second")
    app = api["make_client"]()
    notes = {f"note {i}" for i in range(55)}
    for note in sorted(notes):
        app.store(note, metadata={"session_id": "thread-9"}, skip_extraction=True)

    assert [m.content for m in history.messages] == ["first", "second"]
    history.clear()

    assert history.messages == []
    assert _contents(api) == notes


def test_langchain_reads_and_clears_history_stored_before_the_marker(api) -> None:
    """Messages stored by earlier versions carry only session_id, role, sequence and langchain_message."""
    app = api["make_client"]()
    for seq, message in enumerate([HumanMessage(content="old question"), AIMessage(content="old answer")], start=1):
        metadata = {
            "session_id": "thread-old",
            "role": "human" if seq == 1 else "ai",
            "sequence": seq,
            "langchain_message": json.dumps(messages_to_dict([message])[0]),
        }
        app.store(f"[{metadata['role']}] {message.content}", metadata=metadata, skip_extraction=True)
    others = _others(api, "thread-old")
    history = _history(api, "thread-old")

    assert [m.content for m in history.messages] == ["old question", "old answer"]
    history.clear()

    assert history.messages == []
    assert _contents(api) == others


def test_langchain_keeps_order_across_history_objects(api) -> None:
    """RunnableWithMessageHistory builds a new history object for every call.

    Each one numbered its messages from 1 again, so a three-turn conversation
    read back as q0 q1 q2 a0 a1 a2.
    """
    for turn in range(3):
        history = _history(api, "thread-7")
        history.add_user_message(f"q{turn}")
        history.add_ai_message(f"a{turn}")

    assert [m.content for m in _history(api, "thread-7").messages] == ["q0", "a0", "q1", "a1", "q2", "a2"]


# --------------------------------------------------------------------------
# OpenAI Agents
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_agents_clear_session_deletes_only_the_items_it_stored(api) -> None:
    session = _agents_session(api, "conv-7")
    await session.add_items([{"role": "user", "content": "hi"}, {"role": "assistant", "content": "hello"}])
    others = _others(api, "conv-7")

    assert [i["content"] for i in await session.get_items()] == ["hi", "hello"]
    await session.clear_session()

    assert await session.get_items() == []
    assert _contents(api) == others


@pytest.mark.asyncio
async def test_agents_pop_item_never_deletes_another_memory(api) -> None:
    session = _agents_session(api, "conv-8")
    await session.add_items([{"role": "user", "content": "hi"}, {"role": "assistant", "content": "hello"}])
    app = api["make_client"]()
    # Another writer that also numbers its records.
    app.store("App log line 99", metadata={"session_id": "conv-8", "sequence": 99}, skip_extraction=True)

    assert await session.pop_item() == {"role": "assistant", "content": "hello"}
    assert await session.pop_item() == {"role": "user", "content": "hi"}
    assert await session.pop_item() is None
    assert _contents(api) == {"App log line 99"}


@pytest.mark.asyncio
async def test_agents_clear_session_reaches_items_behind_many_newer_notes(api) -> None:
    session = _agents_session(api, "conv-9")
    await session.add_items([{"role": "user", "content": "first"}])
    app = api["make_client"]()
    notes = {f"note {i}" for i in range(55)}
    for note in sorted(notes):
        app.store(note, metadata={"session_id": "conv-9"}, skip_extraction=True)

    assert [i["content"] for i in await session.get_items()] == ["first"]
    await session.clear_session()

    assert await session.get_items() == []
    assert _contents(api) == notes
