"""An MCP server outside the agent's process tree must not become a second crew identity that blocks the agent (F2).

Found live on 0.17.5: Claude Code sessions started from the Claude desktop app get ``mcp__remembra__*`` from the
desktop app's own server (agent ``claude-desktop``), a sibling of the session's ``claude`` process, so crewd's
``whoami`` does not map it to the session's crew-hook session (``claude-code``, cc-N). The bridge then joined a
separate ``mcp`` seat (cd-N): a ``crew_claim`` made there was denied to the session's own edits by the gate
("is claimed EXCLUSIVELY by cd-1"), its checkpoints opened collisions between cd-N and cc-N, and ``crew_guard``
auto-claimed zones for the wrong identity.

Here a real crewd serves its real socket and the hook session's agent is another live process (not this test's
ancestor), so ``whoami`` answers ``no_session`` exactly as it does for the desktop app's server. The MCP seat
talks to the real crew routes.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from remembra.mcp.crew import CallerInfo, CrewBridge, local_crew
from tests.crew.test_wp9_gate import _start_crewd
from tests.crew.wp9_support import add_worktree, crew_server, make_repo, peer

pytestmark = pytest.mark.asyncio

CLAIM = dict(zone=None, mode="exclusive", task=None, to=None, baton=None, reason=None, wait_s=0)


@pytest.fixture
def hook_agent() -> Iterator[int]:
    """The hooked agent (``claude``): a live process that is not an ancestor of the MCP server (this test)."""
    proc = subprocess.Popen(["sleep", "600"])
    yield proc.pid
    proc.kill()
    proc.wait()


class _Http:
    """The SDK client's sync httpx client, served by the in-process API (called from the bridge's worker thread)."""

    def __init__(self, srv: Any, loop: asyncio.AbstractEventLoop) -> None:
        self.key = srv.key
        self.loop = loop
        self.client = httpx.AsyncClient(transport=srv.transport())

    def request(self, method: str, url: str, *, json: Any = None, params: Any = None, headers: Any = None, timeout: Any = None):
        sent = {"X-API-Key": self.key, **(headers or {})}
        coro = self.client.request(method, url, json=json, params=params, headers=sent)
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result(30)


def _caller(srv: Any, project: str, *, agent: str = "claude-desktop", local: bool = True, sid: str = "mcp-desktop") -> CallerInfo:
    client = SimpleNamespace(
        agent_id=agent,
        api_key=srv.key,
        project=None,
        base_url="http://test",
        timeout=30.0,
        _client=_Http(srv, asyncio.get_running_loop()),
    )
    return CallerInfo(
        client=client,  # type: ignore[arg-type]
        fingerprint="fp-" + agent,
        client_session_id=sid,
        agent_id=agent,
        default_project=project,
        chosen_project=project,
        local=local,
    )


async def _hook_session(d: Any, repo: Any, pid: int, sid: str = "sess-cc") -> dict[str, Any]:
    joined = await d.join(peer(pid), {"adapter": "claude-code", "client_session_id": sid, "cwd": str(repo), "agent_pid": pid})
    return {**joined, "project_id": d.sessions[joined["key"]]["project_id"]}


async def _hook_guard(d: Any, hook: dict[str, Any], path: str) -> dict[str, Any]:
    """The edit gate's question, asked as the hook session (the identity the PreToolUse hook runs as)."""
    res = await d.api_for(d.sessions[hook["key"]]).call(
        "POST",
        f"/crews/{hook['crew_id']}/guard",
        session_token=d.tokens[hook["key"]],
        json_body={"session_id": hook["session_id"], "paths": [path], "op": "write"},
    )
    assert res.ok, res.body
    return dict(res.body)


async def _mcp_claims(srv: Any) -> list[dict[str, Any]]:
    return await srv.rows(
        "SELECT c.path_glob, c.zone_id, c.state FROM crew_claims c JOIN crew_sessions s ON s.id = c.holder_session_id "
        "WHERE s.adapter = 'mcp' AND c.state IN ('active', 'offered', 'reserved', 'queued')"
    )


async def test_a_claim_from_an_mcp_server_outside_the_agents_process_does_not_lock_the_agent_out(
    tmp_path, monkeypatch, hook_agent
):
    repo = make_repo(tmp_path / "repo")
    async with crew_server(tmp_path) as srv:
        layout, d = await _start_crewd(tmp_path, srv, alive={hook_agent})
        monkeypatch.setenv("HOME", str(layout.home))
        hook = await _hook_session(d, repo, hook_agent)
        assert await asyncio.to_thread(local_crew) is None  # as for the desktop app's server: not in the agent's tree

        bridge = CrewBridge()
        caller = _caller(srv, hook["project_id"])
        text = await asyncio.to_thread(bridge.claim, caller, action="claim", paths=["docs/x.md"], **CLAIM)

        assert text.startswith("NOT CLAIMED"), text
        assert hook["callsign"] in text and "claude-desktop" in text and "remembra-crew claim" in text, text
        assert await _mcp_claims(srv) == []
        verdict = await _hook_guard(d, hook, "docs/x.md")
        assert verdict["decision"] != "deny", verdict  # the agent's own edit is not blocked by its MCP identity


async def test_every_way_to_take_a_claim_is_closed_for_the_shadowed_seat(tmp_path, monkeypatch, hook_agent):
    """crew_guard auto-claims, crew_task start claims the task's zones, a task claim does too."""
    repo = make_repo(tmp_path / "repo")
    async with crew_server(tmp_path) as srv:
        layout, d = await _start_crewd(tmp_path, srv, alive={hook_agent})
        monkeypatch.setenv("HOME", str(layout.home))
        hook = await _hook_session(d, repo, hook_agent)
        crew_id = hook["crew_id"]
        pos = next(z for z in d.snapshots[crew_id]["zones"] if z["slug"] == "pos")
        made = await d.api_for(d.sessions[hook["key"]]).call(
            "POST",
            f"/crews/{crew_id}/tasks",
            session_token=d.tokens[hook["key"]],
            json_body={"title": "Split tender", "zone_ids": [pos["id"]], "acceptance": [], "depends_on": []},
        )
        assert made.ok, made.body

        bridge = CrewBridge()
        caller = _caller(srv, hook["project_id"])
        guard = await asyncio.to_thread(bridge.guard, caller, paths=["src/app/pos/split.ts"], command=None, mcp_tool=None)
        assert guard.startswith("NOT CHECKED"), guard
        task_args = dict(title=None, status=None, zones=None, acceptance=None, phase=None, note=None)
        started = await asyncio.to_thread(bridge.task, caller, action="start", task="T-1", **task_args)
        assert started.startswith("NOT STARTED"), started
        by_task = await asyncio.to_thread(bridge.claim, caller, action="claim", paths=None, **{**CLAIM, "task": "T-1"})
        assert by_task.startswith("NOT CLAIMED"), by_task
        by_zone = await asyncio.to_thread(bridge.claim, caller, action="claim", paths=None, **{**CLAIM, "zone": "pos"})
        assert by_zone.startswith("NOT CLAIMED"), by_zone
        adopted = await asyncio.to_thread(bridge.claim, caller, action="adopt", paths=None, **{**CLAIM, "zone": "pos"})
        assert adopted.startswith("NOT ADOPTED") and "remembra-crew adopt" in adopted, adopted

        assert await _mcp_claims(srv) == []
        assert (await _hook_guard(d, hook, "src/app/pos/split.ts"))["decision"] != "deny"
        # reading stays open: the task list is the crew's, not a claim
        listed = await asyncio.to_thread(bridge.task, caller, action="list", task=None, **task_args)
        assert "T-1" in listed, listed


async def test_a_checkpoint_from_the_shadowed_seat_opens_no_collision_with_the_hook_session(tmp_path, monkeypatch, hook_agent):
    repo = make_repo(tmp_path / "repo")
    async with crew_server(tmp_path) as srv:
        layout, d = await _start_crewd(tmp_path, srv, alive={hook_agent})
        monkeypatch.setenv("HOME", str(layout.home))
        hook = await _hook_session(d, repo, hook_agent)
        crew_id = hook["crew_id"]
        pos = next(z for z in d.snapshots[crew_id]["zones"] if z["slug"] == "pos")
        assert (await d.claim(peer(hook_agent), {"key": hook["key"], "zone_id": pos["id"]}))["result"] == "granted"

        bridge = CrewBridge()
        caller = _caller(srv, hook["project_id"])
        text = await asyncio.to_thread(
            bridge.checkpoint,
            caller,
            files_changed=["src/app/pos/split.ts"],
            summary="split tender works",
            commits=None,
            tests=[{"command": "pytest", "passed": 3, "failed": 0}],
            next_step="wire the receipt",
            task=None,
        )
        assert text.startswith("CHECKPOINT ckp_"), text
        assert "FILES NOT ATTRIBUTED" in text and hook["callsign"] in text, text
        assert await srv.rows("SELECT kind, subject FROM crew_collisions") == []
        # the rest of the checkpoint is kept
        rows = await srv.rows("SELECT facts FROM crew_checkpoints ORDER BY created_at DESC LIMIT 1")
        facts = json.loads(rows[0]["facts"])
        assert facts["files_changed"] == [] and facts["notes"] == "split tender works" and facts["tests"][0]["passed"] == 3


async def test_status_names_the_hook_session_and_a_claim_taken_before_it_can_still_be_released(tmp_path, monkeypatch, hook_agent):
    """The desktop server lives for days: a claim it took before the hook session started must be visible as the
    thing blocking that session, and releasable from the same seat."""
    repo = make_repo(tmp_path / "repo")
    async with crew_server(tmp_path) as srv:
        layout, d = await _start_crewd(tmp_path, srv, alive={hook_agent})
        monkeypatch.setenv("HOME", str(layout.home))
        # the project's crew exists (an earlier session), nobody is live in it now
        first = await _hook_session(d, repo, hook_agent, "sess-earlier")
        await d.end(peer(hook_agent), {"key": first["key"], "reason": "exit"})
        for daemon in srv.daemons:
            await daemon.drain()

        bridge = CrewBridge()
        caller = _caller(srv, first["project_id"])
        got = await asyncio.to_thread(bridge.claim, caller, action="claim", paths=["docs/x.md"], **CLAIM)
        assert got.startswith("GRANTED"), got  # no hook session on this machine: the MCP seat is the agent's seat

        hook = await _hook_session(d, repo, hook_agent, "sess-now")
        assert (await _hook_guard(d, hook, "docs/x.md"))["decision"] == "deny"  # the old state: locked out
        status = await asyncio.to_thread(bridge.status, caller, project_id=None, checkout=None, verbose=False)
        assert f"CREW-HOOK SESSION ON THIS MACHINE: {hook['callsign']}" in status, status
        assert "THIS MCP SEAT HOLDS" in status and 'crew_claim(action="release"' in status, status
        assert "Claim before editing" not in status, status  # the advisory footer would send the agent back to crew_claim

        released = await asyncio.to_thread(bridge.claim, caller, action="release", paths=["docs/x.md"], **CLAIM)
        assert released.startswith("RELEASED"), released
        assert await _mcp_claims(srv) == []
        assert (await _hook_guard(d, hook, "docs/x.md"))["decision"] != "deny"


async def test_two_hook_sessions_in_the_project_are_both_named_and_none_is_called_yours(tmp_path, monkeypatch, hook_agent):
    """The desktop app's one server serves every Claude Code session it started: it cannot tell which one calls."""
    repo = make_repo(tmp_path / "repo")
    wt = add_worktree(repo, tmp_path / "wt-b", "b-work")
    other = subprocess.Popen(["sleep", "600"])
    try:
        async with crew_server(tmp_path) as srv:
            layout, d = await _start_crewd(tmp_path, srv, alive={hook_agent, other.pid})
            monkeypatch.setenv("HOME", str(layout.home))
            a = await _hook_session(d, repo, hook_agent, "sess-a")
            b = await _hook_session(d, wt, other.pid, "sess-b")
            assert a["crew_id"] == b["crew_id"] and a["callsign"] != b["callsign"]

            bridge = CrewBridge()
            caller = _caller(srv, a["project_id"])
            text = await asyncio.to_thread(bridge.claim, caller, action="claim", paths=["docs/x.md"], **CLAIM)
            assert text.startswith("NOT CLAIMED: crew-hook sessions are live"), text
            assert a["callsign"] in text and b["callsign"] in text and "one of them" in text, text
            accepted = await asyncio.to_thread(bridge.claim, caller, action="accept", paths=None, **{**CLAIM, "zone": "pos"})
            assert accepted.startswith("NOT ACCEPTED") and "hand it over" in accepted, accepted
            status = await asyncio.to_thread(bridge.status, caller, project_id=None, checkout=None, verbose=False)
            assert f"CREW-HOOK SESSIONS ON THIS MACHINE: {a['callsign']}, {b['callsign']}" in status, status
            assert await _mcp_claims(srv) == []
    finally:
        other.kill()
        other.wait()


async def test_another_agent_or_a_remote_server_keeps_its_own_seat_and_its_claims(tmp_path, monkeypatch, hook_agent):
    """Only the same program's hook session shadows a seat: Codex beside Claude Code is a real second agent, and a
    remote MCP server is not on the agent's machine."""
    repo = make_repo(tmp_path / "repo")
    async with crew_server(tmp_path) as srv:
        layout, d = await _start_crewd(tmp_path, srv, alive={hook_agent})
        monkeypatch.setenv("HOME", str(layout.home))
        hook = await _hook_session(d, repo, hook_agent)
        project = hook["project_id"]

        codex = _caller(srv, project, agent="codex", sid="mcp-codex")
        text = await asyncio.to_thread(CrewBridge().claim, codex, action="claim", paths=["docs/codex.md"], **CLAIM)
        assert text.startswith("GRANTED"), text
        status = await asyncio.to_thread(CrewBridge().status, codex, project_id=None, checkout=None, verbose=False)
        assert "CREW-HOOK SESSION" not in status and "Claim before editing" in status, status

        remote = _caller(srv, project, local=False, sid="mcp-remote")
        text = await asyncio.to_thread(CrewBridge().claim, remote, action="claim", paths=["docs/remote.md"], **CLAIM)
        assert text.startswith("GRANTED"), text
        held = sorted(r["path_glob"] for r in await _mcp_claims(srv))
        assert held == ["docs/codex.md", "docs/remote.md"], held


async def test_a_claim_in_a_crew_the_hook_session_is_not_in_says_so(tmp_path, monkeypatch, hook_agent):
    """The desktop server's default project is often not the repository's: the claim is granted there, and the answer
    says the edit gate of the hook session does not read that crew."""
    repo = make_repo(tmp_path / "repo")
    async with crew_server(tmp_path) as srv:
        layout, d = await _start_crewd(tmp_path, srv, alive={hook_agent})
        monkeypatch.setenv("HOME", str(layout.home))
        hook = await _hook_session(d, repo, hook_agent)

        caller = _caller(srv, "some-default-project")
        text = await asyncio.to_thread(CrewBridge().claim, caller, action="claim", paths=["docs/x.md"], **CLAIM)
        assert text.startswith("GRANTED"), text
        assert "NOTE:" in text and hook["project_id"] in text and "some-default-project" in text, text
        assert (await _hook_guard(d, hook, "docs/x.md"))["decision"] != "deny"


async def test_without_crewd_nothing_changes(tmp_path, monkeypatch):
    """No crewd socket (hooks not installed, or a sandbox that cannot reach it): the MCP seat works as before."""
    async with crew_server(tmp_path) as srv:
        monkeypatch.setenv("HOME", str(tmp_path / "nobody"))
        caller = _caller(srv, "solo-project")
        bridge = CrewBridge()
        text = await asyncio.to_thread(bridge.claim, caller, action="claim", paths=["docs/x.md"], **CLAIM)
        assert text.startswith("GRANTED") and "NOTE:" not in text, text
        status = await asyncio.to_thread(bridge.status, caller, project_id=None, checkout=None, verbose=False)
        assert "CREW-HOOK SESSION" not in status and "Claim before editing" in status, status


@pytest.mark.parametrize("status", ["in_progress", "claimed"])
async def test_a_task_update_that_would_claim_is_closed_for_the_shadowed_seat(tmp_path, monkeypatch, hook_agent, status):
    """crew_task(action="update", status="in_progress" | "claimed") starts / claims the task: the same lock-out."""
    repo = make_repo(tmp_path / "repo")
    async with crew_server(tmp_path) as srv:
        layout, d = await _start_crewd(tmp_path, srv, alive={hook_agent})
        monkeypatch.setenv("HOME", str(layout.home))
        hook = await _hook_session(d, repo, hook_agent)
        crew_id = hook["crew_id"]
        pos = next(z for z in d.snapshots[crew_id]["zones"] if z["slug"] == "pos")
        made = await d.api_for(d.sessions[hook["key"]]).call(
            "POST",
            f"/crews/{crew_id}/tasks",
            session_token=d.tokens[hook["key"]],
            json_body={"title": "Split tender", "zone_ids": [pos["id"]], "acceptance": [], "depends_on": []},
        )
        assert made.ok, made.body

        bridge = CrewBridge()
        caller = _caller(srv, hook["project_id"])
        args = dict(zones=None, acceptance=None, phase=None)
        out = await asyncio.to_thread(
            bridge.task, caller, action="update", task="T-1", status=status, title="Renamed", note="n", **args
        )

        assert out.startswith("NOT UPDATED"), out
        assert hook["callsign"] in out and "remembra-crew task start T-1" in out, out
        assert await _mcp_claims(srv) == []
        assert (await _hook_guard(d, hook, "src/app/pos/split.ts"))["decision"] != "deny"
        rows = await srv.rows("SELECT status, title FROM crew_tasks")
        assert [(r["status"], r["title"]) for r in rows] == [("ready", "Split tender")], rows  # refused whole, not half applied

        # an update that takes nothing stays open on the same seat
        renamed = await asyncio.to_thread(
            bridge.task, caller, action="update", task="T-1", status=None, title="Renamed", note=None, **args
        )
        assert renamed.startswith("UPDATED T-1"), renamed
        same = await asyncio.to_thread(
            bridge.task, caller, action="update", task="T-1", status="ready", title=None, note=None, **args
        )
        assert same.startswith("NOTHING TO UPDATE"), same


async def test_unblocking_a_task_the_seat_already_owns_stays_open_when_a_hook_session_appears(tmp_path, monkeypatch, hook_agent):
    """The desktop server started and blocked a task before the hook session existed: unblocking takes no new claim."""
    repo = make_repo(tmp_path / "repo")
    async with crew_server(tmp_path) as srv:
        layout, d = await _start_crewd(tmp_path, srv, alive={hook_agent})
        monkeypatch.setenv("HOME", str(layout.home))
        first = await _hook_session(d, repo, hook_agent, "sess-earlier")
        await d.end(peer(hook_agent), {"key": first["key"], "reason": "exit"})
        for daemon in srv.daemons:
            await daemon.drain()

        bridge = CrewBridge()
        caller = _caller(srv, first["project_id"])
        args = dict(zones=None, acceptance=None, phase=None)
        made = await asyncio.to_thread(
            bridge.task, caller, action="create", task=None, status=None, title="Docs", note=None, **args
        )
        assert made.startswith("CREATED T-1"), made
        started = await asyncio.to_thread(
            bridge.task, caller, action="start", task="T-1", status=None, title=None, note=None, **args
        )
        assert started.startswith("STARTED T-1"), started
        blocked = await asyncio.to_thread(
            bridge.task, caller, action="block", task="T-1", status=None, title=None, note="waiting", **args
        )
        assert blocked.startswith("BLOCKED T-1"), blocked

        hook = await _hook_session(d, repo, hook_agent, "sess-now")
        before = await _mcp_claims(srv)
        out = await asyncio.to_thread(
            bridge.task, caller, action="update", task="T-1", status="in_progress", title=None, note=None, **args
        )
        assert out.startswith("UPDATED T-1"), out
        assert await _mcp_claims(srv) == before
        assert hook["callsign"] in await asyncio.to_thread(bridge.status, caller, project_id=None, checkout=None, verbose=False)
