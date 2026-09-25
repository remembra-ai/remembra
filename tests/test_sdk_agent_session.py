"""SDK (``remembra.Memory``) against the real API: AGT-3/4/5/6/7/10/11.

The SDK's sync httpx client is swapped for Starlette's ``TestClient`` (an
``httpx.Client`` over ASGI), so every call runs the production routes, the
real ``MemoryService`` and a real SQLite ``Database``. Only the vector store
and the embedder are in-process fakes.
"""

from __future__ import annotations

import os

os.environ.setdefault("REMEMBRA_AUTH_ENABLED", "false")
os.environ.setdefault("REMEMBRA_RATE_LIMIT_ENABLED", "false")

import socket

import pytest

from remembra import __version__
from remembra.client.memory import Memory, MemoryError
from remembra.client.project import normalize_project_id, parse_project_aliases
from tests.agent_api_harness import build_api
from tests.agent_api_harness import row as _row


@pytest.fixture()
def api(tmp_path):
    yield from build_api(tmp_path)


# ---------------------------------------------------------------------------
# AGT-4 provenance
# ---------------------------------------------------------------------------


def test_store_stamps_provenance_and_recall_surfaces_it(api):
    import json

    client = api["make_client"](project="alpha", agent_id="claude-code", session_id="sess-1")
    result = client.store("Deploy target is Coolify", metadata={"source": "meeting"})
    row = _row(api, result.id)
    meta = json.loads(row["metadata"])
    assert meta["agent_id"] == "claude-code"
    assert meta["session_id"] == "sess-1"
    assert meta["host"] == socket.gethostname()
    assert meta["client_version"] == __version__
    assert meta["source"] == "meeting"  # caller-supplied keys win over the stamp

    listed = client.timeline()["memories"]
    assert listed[0]["agent_id"] == "claude-code"


def test_provenance_can_be_disabled(api):
    import json

    client = api["make_client"](project="alpha", agent_id="codex", provenance=False)
    result = client.store("plain")
    assert json.loads(_row(api, result.id)["metadata"]) == {}


# ---------------------------------------------------------------------------
# AGT-5 memory types + status upsert
# ---------------------------------------------------------------------------


def test_checkpoint_and_handoff_types_round_trip(api):
    client = api["make_client"](project="alpha", agent_id="claude-code")
    cp = client.store("checkpoint: step 3 of 7 done", memory_type="checkpoint")
    assert cp.expires_at is not None
    assert _row(api, cp.id)["memory_type"] == "checkpoint"

    ho = client.store("[SESSION END] Completed X. Next: Y.", memory_type="handoff")
    brief = client.session_brief()
    assert brief["handoff"]["id"] == ho.id
    assert brief["agent_id"] == "claude-code"


def test_store_status_upsert_via_sdk(api):
    client = api["make_client"](project="alpha", agent_id="claude-code")
    first = client.store_status("deploy", "pushed, not deployed")
    second = client.store_status("deploy", "live on server")
    assert second["superseded"] == [first["memory_id"]]
    assert [s["value"] for s in client.list_status()] == ["live on server"]
    assert client.store_status("deploy", "live on server")["changed"] is False


# ---------------------------------------------------------------------------
# AGT-3 brief + inbox default sender
# ---------------------------------------------------------------------------


def test_session_brief_uses_client_agent_id_and_inbox(api):
    codex = api["make_client"](project="alpha", agent_id="codex")
    claude = api["make_client"](project="alpha", agent_id="claude-code")
    codex.send_to_inbox(to_agent="claude-code", subject="review PR", body="please review")
    brief = claude.session_brief()
    assert brief["inbox"]["unread_count"] == 1
    assert brief["inbox"]["items"][0]["from_agent"] == "codex"  # sender defaulted to client agent_id


# ---------------------------------------------------------------------------
# AGT-6 project normalization
# ---------------------------------------------------------------------------


def test_parse_and_normalize_project_ids():
    aliases = parse_project_aliases(" ClawdBot = clawbot, test=clawbot, broken, =x ")
    assert aliases == {"clawdbot": "clawbot", "test": "clawbot"}
    assert normalize_project_id("  CLAWDBOT ", aliases) == "clawbot"
    assert normalize_project_id(None, aliases) == "default"
    assert normalize_project_id("  ", aliases) == "default"
    assert normalize_project_id("Trade Mind") == "Trade-Mind"  # case preserved for non-aliases
    assert normalize_project_id("TradeMind", aliases) == "TradeMind"


def test_aliased_clients_share_one_namespace(api):
    aliases = {"clawdbot": "clawbot"}
    writer = api["make_client"](project="clawdbot", project_aliases=aliases)
    reader = api["make_client"](project="clawbot")
    assert writer.project == "clawbot"
    stored = writer.store("shared fact")
    assert _row(api, stored.id)["project_id"] == "clawbot"
    assert [m["id"] for m in reader.timeline()["memories"]] == [stored.id]
    assert [m["id"] for m in writer.timeline(project_id="ClawdBot")["memories"]] == [stored.id]


# ---------------------------------------------------------------------------
# AGT-7 timeline via SDK
# ---------------------------------------------------------------------------


def test_timeline_range_via_sdk(api):
    client = api["make_client"](project="alpha")
    client.store("one")
    assert client.timeline(start="2000-01-01", end="2000-01-02")["total"] == 0
    assert client.timeline(start="2000-01-01")["total"] == 1


# ---------------------------------------------------------------------------
# AGT-11 scoped forget
# ---------------------------------------------------------------------------


def test_forget_project_requires_explicit_project_and_is_scoped(api):
    client = api["make_client"](project="alpha")
    client.store("alpha fact")
    beta = api["make_client"](project="beta")
    beta.store("beta fact")
    with pytest.raises(MemoryError):
        client.forget_project("  ")
    client.forget_project("alpha")
    assert client.timeline()["total"] == 0
    assert beta.timeline()["total"] == 1


# ---------------------------------------------------------------------------
# AGT-10 spaces via SDK
# ---------------------------------------------------------------------------


def test_list_and_create_spaces(api):
    client = api["make_client"](project="alpha")
    created = client.create_space("fleet", description="shared by all agents")
    spaces = client.list_spaces()
    assert [s["id"] for s in spaces] == [created["id"]]
    assert spaces[0]["name"] == "fleet"


# ---------------------------------------------------------------------------
# Recall passes the new knobs through
# ---------------------------------------------------------------------------


def test_recall_forwards_retrieval_knobs():
    from unittest.mock import MagicMock

    client = Memory(base_url="http://x", project="alpha")
    response = MagicMock(status_code=200)
    response.json.return_value = {
        "context": "",
        "memories": [
            {
                "id": "m",
                "content": "c",
                "relevance": 0.5,
                "created_at": "2026-01-01T00:00:00",
                "metadata": {"source_id": "s1"},
                "memory_type": "fact",
                "staleness_warning": True,
                "age_days": 40,
            }
        ],
        "entities": [],
    }
    client._client = MagicMock()
    client._client.request.return_value = response
    result = client.recall(
        "q", retrieval_mode="debug", scope="work", as_of="2026-01-01", max_tokens=500, slim=True, include_superseded=True
    )
    payload = client._client.request.call_args.kwargs["json"]
    assert payload["retrieval_mode"] == "debug"
    assert payload["scope"] == "work"
    assert payload["as_of"] == "2026-01-01"
    assert payload["max_tokens"] == 500
    assert payload["slim"] is True
    assert payload["include_superseded"] is True
    item = result.memories[0]
    assert item.source_id == "s1" and item.staleness_warning is True and item.age_days == 40
