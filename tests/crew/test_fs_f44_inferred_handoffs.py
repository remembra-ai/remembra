"""F44: a handoff the server wrote for a session that never closed.

The crew outbox hands the relay facts in crew vocabulary (commits as bare
shas, tests as ``{command, passed: n, failed: n}``). They are translated
before the handoff is built, so a failing run blocks, a commit without a
subject is named by its sha, and crewd's bare-string commits no longer crash
the handler. Such a handoff is graded Incomplete (the agent did not close),
its grade says whose record it rests on, and the brief no longer explains a
missing location with a version of Remembra.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from remembra.crew.db import CrewDatabase
from remembra.crew.outbox import KIND_RELAY_HANDOFF, CrewOutboxWorker, default_handlers, relay_facts_from_crew
from remembra.crew.store import CrewStore
from remembra.relay.handoff import (
    GRADE_BASIS_REPORTED,
    GRADE_BASIS_SERVER_INFERRED,
    HEALTH_BLOCKED,
    HEALTH_INCOMPLETE,
    HEALTH_WARNINGS,
    RELAY_ROW_SOURCE,
    assess_handoff,
    build_sections,
    grade_basis,
    health_line,
    location_line,
)
from remembra.services.relay import RelayService
from tests._ret_harness import make_stack

NO_CLOSE = "session ended without a close (facts inferred by the server from its last checkpoint)"

# As SessionService._inferred_facts builds them from an MCP seat's last crew_checkpoint.
MCP_FACTS: dict[str, Any] = {
    "facts_source": "server-inferred",
    "branch": None,
    "head_commit": None,
    "uncommitted_files": ["src/a.py"],
    "dirty_count": 1,
    "last_heartbeat_age_s": 4000,
    "last_checkpoint": "wip",
    "commits": [{"sha": "4cad562"}, {"sha": "bb26933"}],
    "tests": [{"command": "pytest -q", "passed": 0, "failed": 3}],
    "todos_open": ["finish the tender split"],
}
# As crewd's checkpoint_facts sends them: bare-string commits, fingerprint tests, head / dirty.
CREWD_FACTS: dict[str, Any] = {
    "facts_source": "server-inferred",
    "branch": "feat/pos",
    "head_commit": "abc1234def",
    "uncommitted_files": [],
    "commits": ["4cad5620aa", "bb26933f11"],
    "tests": [{"fingerprint": "npm test", "passed": 12, "failed": 0}, {"fingerprint": "tsc", "passed": 0, "failed": 0}],
    "unpushed_commits": 2,
}


@pytest.fixture()
async def crew(tmp_path: Path):
    db = CrewDatabase(str(tmp_path / "crew.db"))
    await db.init_schema()
    store = CrewStore(db)
    row, _ = await store.ensure_crew("u1", "p")
    store.crew_id = row["id"]  # type: ignore[attr-defined]
    try:
        yield store
    finally:
        await db.close()


@pytest.fixture()
async def stack(tmp_path: Path):
    s = await make_stack(tmp_path)
    try:
        yield s
    finally:
        await s.close()


async def _lost_handoff(crew: CrewStore, stack: Any, facts: dict[str, Any], session: str) -> dict[str, Any]:
    """Queue a lost session's handoff as the reaper does and drain the outbox; the stored row."""
    relay = RelayService(db=stack.db, memory_service=stack.service)
    worker = CrewOutboxWorker(crew, default_handlers(main_db=stack.db, memory_service=stack.service, relay_service=relay))
    payload = {
        "user_id": "u1",
        "project_id": "yaadbooks",
        "agent_id": "codex",
        "session_id": session,
        "facts": facts,
        "end_reason": "auto:silent",
    }
    oid = await crew.enqueue_outbox(crew.crew_id, KIND_RELAY_HANDOFF, payload, dedupe_key=f"lost:{session}")
    assert await worker.run_once() == {"done": 1, "retry": 0, "failed": 0}
    handoff_id = (await crew.get_outbox(oid))["result_id"]
    cursor = await stack.db.conn.execute("SELECT id, content, metadata, source FROM memories WHERE id = ?", (handoff_id,))
    row = dict(await cursor.fetchone())
    row["metadata"] = json.loads(row["metadata"])
    return row


async def test_a_lost_mcp_session_with_a_failing_run_is_blocked_and_names_its_commits(crew, stack):
    row = await _lost_handoff(crew, stack, MCP_FACTS, "sess-mcp")
    relay = row["metadata"]["relay"]
    assert relay["facts_source"] == "server-inferred" and relay["end_reason"] == "auto:silent"
    assert relay["headline"] == "2 commit(s), last: bb26933 (no subject recorded) [2 open, 1 failing]"
    assert relay["failing"] == ["FAILING: pytest -q (0 passed, 3 failed)"]
    assert relay["done"] == ["4cad562", "bb26933"]
    assert relay["health"]["status"] == HEALTH_BLOCKED
    assert relay["health"]["missing"][:2] == ["1 failing test run(s)", NO_CLOSE]
    assert "location" not in relay
    line = health_line(row, None)
    assert line.startswith("Handoff health: Blocked (1 failing test run(s); session ended without a close")
    assert line.endswith(GRADE_BASIS_SERVER_INFERRED) and "the agent reported" not in line


async def test_a_lost_crewd_session_with_bare_string_commits_gets_a_handoff_graded_incomplete(crew, stack):
    row = await _lost_handoff(crew, stack, CREWD_FACTS, "sess-crewd")  # before F44: AttributeError, no handoff at all
    relay = row["metadata"]["relay"]
    assert relay["commits"] == [{"sha": "4cad5620aa", "subject": None}, {"sha": "bb26933f11", "subject": None}]
    assert relay["headline"].startswith("2 commit(s), last: bb26933 (no subject recorded)")
    assert "tests passing: npm test (12 passed, 0 failed)" in relay["done"]
    assert not any("tsc" in item for item in relay["done"] + relay["failing"])  # 0 passed, 0 failed: nothing ran
    assert relay["health"]["status"] == HEALTH_INCOMPLETE  # has a branch and commits, but the agent never closed
    assert relay["health"]["missing"] == [NO_CLOSE, "2 commit(s) not pushed"]


def test_crew_facts_are_translated_and_bad_shapes_are_dropped():
    assert relay_facts_from_crew({}) == {}
    out = relay_facts_from_crew(
        {
            "head": "abc1234",
            "dirty": ["a.py", 7, ""],
            "commits": ["4cad562", {"sha": "bb26933", "subject": "feat"}, {"subject": "no sha"}, None, 5, "  "],
            "tests": [
                {"command": "pytest", "passed": 4, "failed": 0},
                {"fingerprint": "npm test", "passed": 0, "failed": 2},
                {"cmd": "go test ./...", "passed": True, "summary": "ok"},
                {"command": "tsc", "passed": 0, "failed": 0},
                {"passed": 1},
                "pytest",
                None,
            ],
            "unpushed_commits": "3",
            "todos_open": "not a list",
            "task": "T-1",
        }
    )
    assert out["head_commit"] == "abc1234" and out["uncommitted_files"] == ["a.py"]
    assert out["commits"] == [{"sha": "4cad562", "subject": None}, {"sha": "bb26933", "subject": "feat"}]
    assert out["tests"] == [
        {"cmd": "pytest", "passed": True, "summary": "4 passed, 0 failed"},
        {"cmd": "npm test", "passed": False, "summary": "0 passed, 2 failed"},
        {"cmd": "go test ./...", "passed": True, "summary": "ok"},
    ]
    assert "unpushed_commits" not in out and "todos_open" not in out and out["task"] == "T-1"
    # Relay-form facts pass through unchanged (a close the relay itself built).
    relay_form = {"branch": "main", "commits": [{"sha": "a" * 40, "subject": "x"}], "tests": [{"cmd": "pytest", "passed": False}]}
    assert relay_facts_from_crew(relay_form) == {
        "branch": "main",
        "commits": [{"sha": "a" * 40, "subject": "x"}],
        "tests": [{"cmd": "pytest", "passed": False, "summary": None}],
    }


def test_only_a_server_inferred_handoff_is_graded_incomplete_for_the_missing_close():
    facts = {"branch": "main", "commits": [{"sha": "4cad562", "subject": None}], "uncommitted_files": ["a"]}
    inferred = assess_handoff(facts, {}, server_inferred=True)
    assert inferred["status"] == HEALTH_INCOMPLETE and inferred["missing"][0] == NO_CLOSE
    # The same facts from a client grade as before, even when the client claims the server's label.
    declared = assess_handoff({**facts, "facts_source": "server-inferred"}, {})
    assert declared["status"] == HEALTH_WARNINGS and NO_CLOSE not in declared["missing"]


def test_headline_names_a_commit_without_a_subject_by_its_sha():
    assert build_sections({"commits": [{"sha": "4cad562"}, {"sha": "bb26933aaa", "subject": ""}]})["headline"] == (
        "2 commit(s), last: bb26933 (no subject recorded)"
    )
    assert build_sections({"commits": [{"sha": "4cad562", "subject": "feat: x"}]})["headline"] == "1 commit(s), last: feat: x"


def test_grade_basis_names_the_servers_own_record():
    assert grade_basis({"facts_source": "server-inferred", "agent_verified": True}) == GRADE_BASIS_SERVER_INFERRED
    assert grade_basis({"facts_source": "server-inferred"}) == GRADE_BASIS_SERVER_INFERRED
    assert grade_basis({"facts_source": "agent-declared", "agent_verified": True}) == GRADE_BASIS_REPORTED
    assert "did not close" in GRADE_BASIS_SERVER_INFERRED and "not verified" in GRADE_BASIS_SERVER_INFERRED.lower()


def _handoff(relay: dict[str, Any] | None) -> dict[str, Any]:
    meta = {"source": "relay", "relay": relay} if relay is not None else {}
    return {"id": "h1", "content": "[HANDOFF] codex", "source": RELAY_ROW_SOURCE, "metadata": meta, "trust_score": 1.0}


@pytest.mark.parametrize(
    ("relay", "why"),
    [
        ({"facts_source": "server-inferred", "end_reason": "auto:silent"}, "(the session ended without a close)"),
        ({"facts_source": "agent-declared"}, "(its close did not send a location)"),
        ({"facts_source": "relay-cli:git"}, "(its close did not send a location)"),
        (None, "(a free-form handoff records no location)"),
    ],
)
def test_a_missing_location_is_not_blamed_on_an_old_version(relay, why):
    line = location_line(_handoff(relay), {"git_repo": False})
    assert line == f"This working directory is not a git repository; where the last session worked was not recorded {why}."
    assert "0.16.1" not in line
