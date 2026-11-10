"""F32: tool results that carry stored rows fit a client's result limit.

``session_brief`` is the call the instructions tell every agent to make first. Called that way (no
``compact``) it returned the server's whole brief (the handoff row with its content and the same items
again under ``metadata.relay``, five more full rows, and the rendered text twice, last): 19 to 67 KB in
busy projects. Claude Code replaces a result of about 49 KB with a 2 KB preview, and Codex truncates it,
so the agent saw the head of the handoff JSON and never the brief text, its inbox count or the closing
reminder. The brief's own data block was also re-wrapped, which rewrote its tags to ``[remembra-data>``.
``recall_memories`` and ``timeline`` had the same unbounded rows.

The response keeps its keys (other code reads ``inbox`` and ``handoff`` from it); it is slimmed, led by
the text brief, and cut to a budget.

The MCP server here runs against the production routes (``mcp_env``); recall, which needs a vector
index on the server, runs against a fake SDK client built from the real result types.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import MagicMock

import pytest

import remembra.mcp.server as server
from remembra.client.types import MemoryItem, RecallResult
from remembra.security.untrusted import DATA_CLOSE, DATA_OPEN, TOOL_PREAMBLE, unwrap_untrusted
from tests.agent_api_harness import seed
from tests.test_mcp_agent_tools import api, mcp_env  # noqa: F401 - fixtures

DONE = [f"{i:02d} " + "refactored the importer and reran the suite; " * 3 for i in range(87)]  # 87 items, ~135 chars each
NOT_DONE = [f"TODO {i}: wire the exporter to the new schema" for i in range(14)]
FAILING = [f"tests/test_export.py::test_case_{i} fails on the new schema" for i in range(12)]


def _framed(raw: str) -> dict[str, Any]:
    """One untrusted-data block holding one JSON object, with only the server's own tags in it."""
    lines = raw.splitlines()
    assert lines[0] == TOOL_PREAMBLE and lines[1] == DATA_OPEN and lines[-1] == DATA_CLOSE, raw[:300]
    low = raw.lower()
    assert low.count("<remembra-data") == 1 and low.count("</remembra-data") == 1
    assert "[remembra-data" not in raw  # nothing of the server's own was neutralized
    out = json.loads(unwrap_untrusted(raw))
    assert isinstance(out, dict)
    return out


def _busy_project(env: dict[str, Any]) -> None:
    """Project alpha as a long session leaves it: one large relay handoff and five 3 KB checkpoints."""
    now = datetime.now(UTC)
    body = "## Done\n" + "\n".join(f"- {d}" for d in DONE) + "\n## Not done\n" + "\n".join(f"- {d}" for d in NOT_DONE)
    relay = {"done": DONE, "not_done": NOT_DONE, "failing": FAILING, "next_step": "port the exporter", "branch": "main"}
    seed(
        env,
        "handoff-big",
        body,
        now - timedelta(minutes=5),
        memory_type="handoff",
        metadata={"agent_id": "codex", "memory_type": "handoff", "source_id": "relay:close", "relay": relay},
    )
    for i in range(5):
        seed(
            env,
            f"cp-{i}",
            f"checkpoint {i}: " + "ran the migration dry run and compared row counts. " * 60,
            now - timedelta(minutes=10 + i),
            memory_type="checkpoint",
            # the first two carry a handoff-sized metadata, the rest a checkpoint's few fields
            metadata={"agent_id": "codex", "memory_type": "checkpoint", **({"relay": {"done": DONE[:20]}} if i < 2 else {})},
        )


def test_session_brief_as_the_instructions_say_to_call_it_fits_and_leads_with_the_brief(mcp_env):  # noqa: F811
    _busy_project(mcp_env)
    raw = server.session_brief()  # exactly what the instructions say to call: no compact
    assert len(raw) <= server.BRIEF_DETAIL_BUDGET_CHARS, len(raw)
    out = _framed(raw)  # one block, and none of the brief's own tags rewritten to "[remembra-data>"
    assert out["status"] == "ok" and out["project_id"] == "alpha" and out["handoff_id"] == "handoff-big"
    # What an agent needs first comes first: a client that shows only the start of a result shows the brief.
    assert list(out)[:6] == ["status", "project_id", "agent_id", "handoff_id", "inbox_unread", "brief"]
    assert raw.index('"brief"') < 400
    assert out["rendered"] == out["brief"]  # still returned while the result fits with it (it goes first)
    # Every key the default response had is still there.
    assert {"handoff", "inbox", "status_items", "recent", "known_agents", "warnings", "linked_projects"} <= set(out)

    lines = out["brief"].splitlines()
    assert lines[0].startswith("# Remembra brief") and "Last session: codex" in out["brief"]
    assert lines.count(server._BRIEF_RECORDED_OPEN) == 1 and lines.count(server._BRIEF_RECORDED_CLOSE) == 1
    assert lines.index(server._BRIEF_RECORDED_OPEN) < lines.index(server._BRIEF_RECORDED_CLOSE) < len(lines) - 1
    assert lines[-1].startswith("Before you finish")

    handoff = out["handoff"]
    assert handoff["id"] == "handoff-big" and handoff["agent_id"] == "codex" and handoff["memory_type"] == "handoff"
    assert handoff["content_truncated"] is True and handoff["content_chars"] > server.BRIEF_HANDOFF_CONTENT_CHARS
    assert len(handoff["content"]) <= server.BRIEF_HANDOFF_CONTENT_CHARS + 3
    relay = handoff["metadata"]["relay"]
    assert relay["done"] == DONE[: server.RELAY_LIST_ITEMS] and relay["done_total"] == 87
    assert relay["not_done"] == NOT_DONE[: server.RELAY_LIST_ITEMS] and relay["not_done_total"] == 14
    assert relay["failing_total"] == 12 and relay["next_step"] == "port the exporter" and relay["branch"] == "main"

    assert [m["id"] for m in out["recent"]] == [f"cp-{i}" for i in range(5)]
    for m in out["recent"]:
        assert set(m) >= {"id", "memory_type", "agent_id", "created_at", "content"}
        assert len(m["content"]) <= server.BRIEF_RECENT_CONTENT_CHARS + 3 and m["content_truncated"] is True
    assert ["metadata" in m for m in out["recent"]] == [False, False, True, True, True]  # bulky metadata goes
    assert out["recent"][2]["metadata"] == {"agent_id": "codex", "memory_type": "checkpoint"}
    assert out["truncated"] == ["handoff.content", "handoff.metadata", "recent.content", "recent.metadata"]
    assert "metadata_truncated" not in handoff  # said once, under "truncated"


def test_session_brief_compact_is_unchanged_and_keeps_its_own_block(mcp_env):  # noqa: F811
    _busy_project(mcp_env)
    raw = server.session_brief(compact=True)
    assert len(raw) <= 8_000, len(raw)
    out = json.loads(raw)
    assert set(out) == {"status", "project_id", "agent_id", "brief", "handoff_id", "inbox_unread", "warnings"}
    lines = out["brief"].splitlines()
    assert "[remembra-data" not in raw
    assert lines.count(DATA_OPEN) == 1 and lines.count(DATA_CLOSE) == 1
    assert lines[-1].startswith("Before you finish") and lines.index(DATA_CLOSE) < len(lines) - 1
    # The two forms carry the same brief; only the block's tag lines differ.
    embedded = _framed(server.session_brief())["brief"].splitlines()
    assert [ln for ln in embedded if not ln.startswith("---")] == [ln for ln in lines if ln not in (DATA_OPEN, DATA_CLOSE)]


def test_session_brief_of_a_small_project_is_unchanged_in_substance(mcp_env):  # noqa: F811
    stored = json.loads(unwrap_untrusted(server.store_memory("[SESSION END] did X, next Y", memory_type="handoff")))
    server.store_memory("parser half done", memory_type="checkpoint")
    out = _framed(server.session_brief())
    assert out["handoff"]["id"] == stored["id"] and out["handoff"]["content"] == "[SESSION END] did X, next Y"
    assert "content_truncated" not in out["handoff"] and isinstance(out["handoff"]["metadata"], dict)
    assert [m["content"] for m in out["recent"]] == ["parser half done"]
    assert "truncated" not in out and isinstance(out["recent"][0]["metadata"], dict)  # nothing was cut
    assert out["handoff_id"] == stored["id"] and out["inbox_unread"] == 0
    assert "[SESSION END] did X, next Y" in out["brief"]


def test_session_brief_never_exceeds_the_cap_whatever_the_server_sends(monkeypatch):
    """An old or future server can put anything in the brief: fields are dropped, largest first, until it fits."""
    big_meta = {"relay": {"done": ["d" * 5_000] * 40}, "blob": "b" * 30_000}
    rendered = "# Remembra brief\n" + DATA_OPEN + "\nx\n" + DATA_CLOSE + "\nBefore you finish: close."
    brief = {
        "project_id": "alpha",
        "agent_id": "claude-code",
        "rendered": rendered,
        "handoff": {"id": "h1", "content": "c" * 50_000, "metadata": big_meta},
        "recent": [{"id": f"r{i}", "content": "r" * 9_000, "metadata": {"k": "v" * 9_000}} for i in range(5)],
        "linked_projects": [{"project_id": f"p{i}", "latest_handoff": {"headline": "h" * 4_000}} for i in range(40)],
        "status_items": [{"key": f"k{i}", "value": "v" * 2_000} for i in range(60)],
        "crew": {"block": "z" * 40_000},
        "inbox": {"unread_count": 3, "items": [{"id": i, "body_preview": "p" * 200} for i in range(10)]},
        "warnings": ["w" * 300] * 5,
        "future_field": "f" * 70_000,
    }
    client = MagicMock()
    client.session_brief.return_value = brief
    monkeypatch.setattr(server, "_get_client", lambda: client)
    monkeypatch.setattr(server, "_session_projects", {})
    raw = server.session_brief(project_id="alpha")
    assert len(raw) <= server.BRIEF_DETAIL_BUDGET_CHARS, len(raw)
    out = _framed(raw)
    assert out["status"] == "ok" and out["project_id"] == "alpha" and out["handoff_id"] == "h1" and out["inbox_unread"] == 3
    assert out["handoff"]["id"] == "h1"  # the handoff's identity survives every cut
    assert out["brief"].splitlines()[-1] == "Before you finish: close."  # and so does the text brief
    assert "future_field" in out["truncated"] and "crew" in out["truncated"]
    for dropped in out["truncated"]:
        assert dropped.split(".")[0] in brief

    # A text brief far over the server's own cap is clipped rather than sent whole.
    client.session_brief.return_value = {"project_id": "alpha", "rendered": "t" * 90_000}
    long = server.session_brief(project_id="alpha")
    assert len(long) <= server.BRIEF_DETAIL_BUDGET_CHARS and _framed(long)["truncated"] == ["brief", "rendered"]

    # Fields of the wrong type are cut like any other (nothing is assumed about a field's shape).
    wrong = {"project_id": "alpha", "handoff": "h" * 90_000, "inbox": ["i" * 90_000], "recent": "r" * 9}
    client.session_brief.return_value = wrong
    odd = server.session_brief(project_id="alpha")
    assert len(odd) <= server.BRIEF_DETAIL_BUDGET_CHARS
    assert _framed(odd)["truncated"] == ["inbox", "handoff"] and _framed(odd)["recent"] == "r" * 9

    # Missing and empty fields (a server without a handoff, one that sends no text brief or no linked projects).
    for sparse in ({}, {"handoff": None, "recent": None, "inbox": None}, {"handoff": "not a row", "recent": ["x", None]}):
        client.session_brief.return_value = dict(sparse)
        got = _framed(server.session_brief(project_id="alpha"))
        assert got["status"] == "ok" and got["handoff_id"] is None and got["inbox_unread"] == 0
        assert got["brief"] is None and got["warnings"] == [] and "truncated" not in got
        compact = json.loads(server.session_brief(project_id="alpha", compact=True))
        assert compact["brief"] is None and compact["warnings"] == [] and compact["handoff_id"] is None


def test_timeline_is_budgeted_and_pages_from_where_it_stopped(mcp_env):  # noqa: F811
    for i in range(20):
        meta = {"agent_id": "codex", "relay": {"done": [f"item {n} " + "x" * 120 for n in range(30)], "next_step": "go"}}
        seed(mcp_env, f"t{i:02d}", f"memory {i:02d} " + "body " * 1_000, datetime(2026, 3, 1, 9, i), metadata=meta)
    raw = server.timeline(limit=20)
    assert len(raw) <= server.RESULT_BUDGET_CHARS, len(raw)
    out = _framed(raw)
    got = out["memories"]
    assert out["total"] == 20 and out["count"] == len(got) and 1 <= len(got) < 20
    assert out["truncated"] is True and out["requested"] == 20 and out["next_offset"] == len(got)
    assert "full=true" in out["hint"]
    first = got[0]
    assert first["id"] == "t00" and first["content"].startswith("memory 00 ")
    assert first["content_truncated"] is True and first["content_chars"] > server.MEMORY_CONTENT_CHARS
    assert len(first["content"]) <= server.MEMORY_CONTENT_CHARS + 3
    assert first["agent_id"] == "codex"
    done = first["metadata"]["relay"]["done"]
    assert first["metadata"]["relay"]["done_total"] == 30 and first["metadata_truncated"] is True
    assert done == [f"item {n} " + "x" * 120 for n in range(len(done))]
    assert len(done) in (server.MEMORY_TIGHT_LIST_ITEMS, server.RELAY_LIST_ITEMS)
    assert first["metadata"]["relay"]["next_step"] == "go"

    # Paging with next_offset reaches every memory exactly once, in order.
    seen = [m["id"] for m in got]
    offset = out["next_offset"]
    while offset is not None:
        page = _framed(server.timeline(limit=20, offset=offset))
        assert len(server.timeline(limit=20, offset=offset)) <= server.RESULT_BUDGET_CHARS
        assert page["memories"], page
        seen += [m["id"] for m in page["memories"]]
        offset = page.get("next_offset")
    assert seen == [f"t{i:02d}" for i in range(20)]

    # full=true is the old, unclipped answer for a caller that asks for it.
    full = json.loads(unwrap_untrusted(server.timeline(limit=20, full=True)))
    assert len(full["memories"]) == 20 and "truncated" not in full
    assert all(len(m["content"]) > 5_000 and "content_truncated" not in m for m in full["memories"])
    assert len(full["memories"][0]["metadata"]["relay"]["done"]) == 30


def test_timeline_small_results_are_unchanged(mcp_env):  # noqa: F811
    seed(mcp_env, "d1", "day 1", datetime(2026, 3, 1, 9, 0), metadata={"agent_id": "codex", "note": "n"})
    out = _framed(server.timeline())
    assert out["count"] == 1 and out["total"] == 1 and "truncated" not in out and "next_offset" not in out
    (m,) = out["memories"]
    assert m["content"] == "day 1" and m["metadata"] == {"agent_id": "codex", "note": "n"} and "content_truncated" not in m
    empty = _framed(server.timeline(start_date="2030-01-01"))
    assert empty["memories"] == [] and empty["count"] == 0 and "truncated" not in empty


def _recall_client(n: int, size: int, context: str) -> MagicMock:
    client = MagicMock()
    client.recall.return_value = RecallResult(
        context=context,
        memories=[
            MemoryItem(
                id=f"m{i}",
                content=f"memory {i} " + "detail " * (size // 7),
                relevance=0.9 - i / 100,
                created_at=datetime(2026, 9, 1),
                metadata={"agent_id": "codex", "relay": {"done": ["d" * 200] * 25}},
                memory_type="handoff",
            )
            for i in range(n)
        ],
        entities=[],
    )
    return client


def test_recall_is_budgeted_best_matches_first(monkeypatch):
    monkeypatch.setattr(server, "_get_client", lambda: _recall_client(20, 5_000, "ctx " * 20_000))
    raw = server.recall_memories(query="exporter", limit=20)
    assert len(raw) <= server.RESULT_BUDGET_CHARS, len(raw)
    out = _framed(raw)
    got = out["memories"]
    assert [m["id"] for m in got] == [f"m{i}" for i in range(len(got))] and 1 <= len(got) < 20  # ranked order kept
    assert out["count"] == len(got) and out["matched"] == 20 and out["truncated"] is True and "full=true" in out["hint"]
    assert out["context_truncated"] is True and len(out["context"]) <= server.RECALL_CONTEXT_CHARS + 3
    assert got[0]["content_truncated"] is True and got[0]["relevance"] == 0.9
    assert got[0]["metadata"]["relay"]["done_total"] == 25

    full = json.loads(unwrap_untrusted(server.recall_memories(query="exporter", limit=20, full=True)))
    assert len(full["memories"]) == 20 and len(full["context"]) == 80_000 and "truncated" not in full
    assert len(full["memories"][0]["metadata"]["relay"]["done"]) == 25

    slim = _framed(server.recall_memories(query="exporter", limit=20, slim=True))
    assert slim["count"] == 20 and len(json.dumps(slim)) <= server.RESULT_BUDGET_CHARS and slim["context_truncated"] is True


def test_recall_small_results_are_unchanged(monkeypatch):
    client = _recall_client(2, 70, "short context")
    for item in client.recall.return_value.memories:
        item.metadata = {"agent_id": "codex"}
    monkeypatch.setattr(server, "_get_client", lambda: client)
    out = _framed(server.recall_memories(query="exporter"))
    assert out["count"] == 2 and out["context"] == "short context"
    assert "truncated" not in out and "context_truncated" not in out and "matched" not in out
    assert all("content_truncated" not in m and m["metadata"] == {"agent_id": "codex"} for m in out["memories"])
    # one memory larger than the whole budget is still returned (clipped), never an empty answer
    monkeypatch.setattr(server, "_get_client", lambda: _recall_client(1, 400_000, ""))
    big = server.recall_memories(query="exporter")
    assert len(big) <= server.RESULT_BUDGET_CHARS and len(_framed(big)["memories"]) == 1


@pytest.mark.parametrize("value", [None, "", [], {}, 0])
def test_slimming_handles_rows_without_content_or_metadata(value: Any):
    row = server._slim_row({"id": "x", "content": value, "metadata": value}, content_chars=10)
    assert row["id"] == "x" and "content_truncated" not in row
    assert server._slim_metadata(value) == (value if isinstance(value, dict) else {})


def test_rows_are_cut_tighter_only_when_that_keeps_more_of_them(mcp_env):  # noqa: F811
    """Twelve 5 KB memories: eight fit at 2,000 characters each, all twelve only at the tight size."""
    for i in range(12):
        seed(mcp_env, f"w{i:02d}", f"memory {i} " + "body " * 1_000, datetime(2026, 4, 1, 9, i), metadata={"agent_id": "codex"})
    raw = server.timeline(limit=12)
    assert len(raw) <= server.RESULT_BUDGET_CHARS
    out = _framed(raw)
    assert [m["id"] for m in out["memories"]] == [f"w{i:02d}" for i in range(12)] and "truncated" not in out
    assert all(len(m["content"]) == server.MEMORY_TIGHT_CONTENT_CHARS + 3 and m["content_chars"] > 5_000 for m in out["memories"])
    # Eight of them fit at the normal size, so they keep it.
    eight = _framed(server.timeline(limit=8))
    assert eight["count"] == 8 and "truncated" not in eight
    assert all(len(m["content"]) == server.MEMORY_CONTENT_CHARS + 3 for m in eight["memories"])
