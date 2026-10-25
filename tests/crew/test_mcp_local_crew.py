"""crew_claim on the agent's machine acts as the agent's crew-hook session, through crewd.

Found live on 0.17.3: a hooked Claude Code session's MCP ``crew_claim`` landed in the configured default
project's crew as a separate ``mcp`` seat, so the claim was never the one crewd saves and offers when the
agent dies; and a sandboxed Codex could not run ``remembra-crew adopt`` (no ``ps``, no crewd socket), while
the MCP adopt it fell back to took the claim but restored nothing.

Here a real crewd serves its real socket; this test process is the agent (its pid is the session's agent
pid), so the MCP bridge's ``whoami`` resolves the hook session as it does for a stdio MCP server, which is
a child of the agent. The bridge runs in a worker thread, as in the MCP server.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from remembra.mcp.crew import CallerInfo, CrewBridge, local_crew
from tests.crew.test_wp9_gate import _start_crewd
from tests.crew.wp9_support import add_worktree, crew_server, make_repo, peer

pytestmark = pytest.mark.asyncio

LOST_PID = 41901


def _caller(local: bool = True, chosen: str | None = None) -> CallerInfo:
    client = SimpleNamespace(agent_id="codex", api_key="k", project=None)
    return CallerInfo(
        client=client,  # type: ignore[arg-type]
        fingerprint="fp",
        client_session_id="mcp-1",
        agent_id="codex",
        # as _crew_caller: the chosen project, else the configured default (which must not win over the hook session)
        default_project=chosen or "some-other-default",
        chosen_project=chosen,
        local=local,
    )


async def _lost_path_baton(d, repo: Path, path: str, text: str) -> dict:
    a = await d.join(
        peer(LOST_PID), {"adapter": "claude-code", "client_session_id": "sess-a", "cwd": str(repo), "agent_pid": LOST_PID}
    )
    got = await d.api_for(d.sessions[a["key"]]).call(
        "POST",
        f"/crews/{a['crew_id']}/claims",
        session_token=d.tokens[a["key"]],
        json_body={"path_glob": path, "mode": "exclusive", "wait": False, "source": "first_write"},
    )
    assert got.ok, got.body
    (repo / path).parent.mkdir(parents=True, exist_ok=True)
    (repo / path).write_text(text)
    stalled = await d.stall(peer(LOST_PID), {"key": a["key"], "error": "billing_error"})
    assert stalled.get("baton_ref"), stalled
    return a


async def test_mcp_adopt_on_the_agents_machine_restores_the_saved_work(tmp_path, monkeypatch):
    repo = make_repo(tmp_path / "repo")
    wt_c = add_worktree(repo, tmp_path / "wt-c", "c-work")
    me = os.getpid()
    async with crew_server(tmp_path) as srv:
        layout, d = await _start_crewd(tmp_path, srv, alive={LOST_PID})
        monkeypatch.setenv("HOME", str(layout.home))
        await _lost_path_baton(d, repo, "docs/plan.md", "# Launch plan X\n")
        c = await d.join(peer(me), {"adapter": "codex", "client_session_id": "sess-c", "cwd": str(wt_c), "agent_pid": me})
        local = await asyncio.to_thread(local_crew)
        assert local is not None and local.who["session_id"] == c["session_id"], local

        bridge = CrewBridge()
        text = await asyncio.to_thread(
            bridge.claim,
            _caller(),
            action="adopt",
            zone=None,
            paths=["docs/plan.md"],
            mode="exclusive",
            task=None,
            to=None,
            baton=None,
            reason=None,
            wait_s=0,
        )
        assert text.startswith("ADOPTED docs/plan.md: you now hold"), text
        assert "saved work is restored into this checkout: 1 file(s) from refs/remembra/baton/" in text, text
        assert (wt_c / "docs/plan.md").read_text() == "# Launch plan X\n"
        held = await srv.rows("SELECT holder_session_id, state FROM crew_claims WHERE path_glob = 'docs/plan.md'")
        assert {"holder_session_id": c["session_id"], "state": "active"} in held, held
        restored = await srv.rows("SELECT restored FROM crew_batons WHERE to_session = ?", (c["session_id"],))
        assert restored == [{"restored": 1}], restored


async def test_mcp_claim_on_the_agents_machine_is_held_by_the_hook_session(tmp_path, monkeypatch):
    """The claim lands in this repository's crew as the hook session, not as an mcp seat in the configured
    default project: it is the claim crewd keeps and offers as a baton if the agent dies."""
    repo = make_repo(tmp_path / "repo")
    me = os.getpid()
    async with crew_server(tmp_path) as srv:
        layout, d = await _start_crewd(tmp_path, srv)
        monkeypatch.setenv("HOME", str(layout.home))
        c = await d.join(peer(me), {"adapter": "claude-code", "client_session_id": "sess-c", "cwd": str(repo), "agent_pid": me})
        text = await asyncio.to_thread(
            CrewBridge().claim,
            _caller(),
            action="claim",
            zone=None,
            paths=["docs/notes.md"],
            mode="exclusive",
            task=None,
            to=None,
            baton=None,
            reason=None,
            wait_s=0,
        )
        assert text.startswith("GRANTED"), text
        rows = await srv.rows("SELECT holder_session_id, crew_id FROM crew_claims WHERE path_glob = 'docs/notes.md'")
        assert rows == [{"holder_session_id": c["session_id"], "crew_id": c["crew_id"]}], rows
        sessions = await srv.rows("SELECT adapter FROM crew_sessions")
        assert all(s["adapter"] != "mcp" for s in sessions), sessions  # no second, disconnected seat


async def test_without_a_hook_session_or_off_the_machine_the_mcp_seat_is_used(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "nobody"))
    assert await asyncio.to_thread(local_crew) is None  # no crewd socket
    called: list[str] = []

    class Probe(CrewBridge):
        def seat(self, caller, project_id=None, checkout=None):  # type: ignore[override]
            called.append("seat")
            raise RuntimeError("seat path")

    for caller in (_caller(local=False), _caller(local=True)):
        with pytest.raises(RuntimeError, match="seat path"):
            Probe().claim(
                caller,
                action="claim",
                zone=None,
                paths=["docs/x.md"],
                mode="exclusive",
                task=None,
                to=None,
                baton=None,
                reason=None,
                wait_s=0,
            )
    assert called == ["seat", "seat"]


async def test_a_project_the_agent_chose_itself_keeps_the_mcp_seat(tmp_path, monkeypatch):
    repo = make_repo(tmp_path / "repo")
    me = os.getpid()
    async with crew_server(tmp_path) as srv:
        layout, d = await _start_crewd(tmp_path, srv)
        monkeypatch.setenv("HOME", str(layout.home))
        await d.join(peer(me), {"adapter": "claude-code", "client_session_id": "sess-c", "cwd": str(repo), "agent_pid": me})

        class Probe(CrewBridge):
            def seat(self, caller, project_id=None, checkout=None):  # type: ignore[override]
                raise RuntimeError("seat path")

        with pytest.raises(RuntimeError, match="seat path"):
            await asyncio.to_thread(
                Probe().claim,
                _caller(chosen="another-project"),
                action="claim",
                zone=None,
                paths=["docs/x.md"],
                mode="exclusive",
                task=None,
                to=None,
                baton=None,
                reason=None,
                wait_s=0,
            )


class _NoBrief(CrewBridge):
    """crew_status without the session-brief block (served by the API key route, not crewd, in production)."""

    def _crew_block(self, http, caller, seat):  # type: ignore[override]
        return ""


async def test_status_and_claim_are_one_session_for_the_agent(tmp_path, monkeypatch):
    """Codex review P1: after a claim, crew_status must show it as this agent's, not a second session's."""
    repo = make_repo(tmp_path / "repo")
    me = os.getpid()
    async with crew_server(tmp_path) as srv:
        layout, d = await _start_crewd(tmp_path, srv)
        monkeypatch.setenv("HOME", str(layout.home))
        c = await d.join(peer(me), {"adapter": "claude-code", "client_session_id": "sess-c", "cwd": str(repo), "agent_pid": me})
        bridge = _NoBrief()
        args = dict(zone=None, mode="exclusive", task=None, to=None, baton=None, reason=None, wait_s=0)
        got = await asyncio.to_thread(bridge.claim, _caller(), action="claim", paths=["docs/notes.md"], **args)
        assert got.startswith("GRANTED"), got
        status = await asyncio.to_thread(bridge.status, _caller(), project_id=None, checkout=None, verbose=False)
        you = next(line for line in status.splitlines() if line.startswith("YOU"))
        assert c["callsign"] in you and "docs/notes.md" in you, status
        released = await asyncio.to_thread(bridge.claim, _caller(), action="release", paths=["docs/notes.md"], **args)
        assert released.startswith("RELEASED"), released
        rows = await srv.rows("SELECT state FROM crew_claims WHERE path_glob = 'docs/notes.md'")
        assert all(r["state"] != "active" for r in rows), rows


async def test_a_project_named_in_crew_status_keeps_later_claims_there(tmp_path, monkeypatch):
    """Codex review P1: crew_status(project_id=B) then crew_claim must not act in the hook session's crew A."""
    repo = make_repo(tmp_path / "repo")
    me = os.getpid()
    async with crew_server(tmp_path) as srv:
        layout, d = await _start_crewd(tmp_path, srv)
        monkeypatch.setenv("HOME", str(layout.home))
        await d.join(peer(me), {"adapter": "claude-code", "client_session_id": "sess-c", "cwd": str(repo), "agent_pid": me})
        bridge = CrewBridge()
        caller = _caller()
        scope = (caller.fingerprint, caller.client_session_id)
        local_project = (await asyncio.to_thread(local_crew)).who["project_id"]
        assert await asyncio.to_thread(bridge._local_seat, caller, scope, None) is not None
        assert await asyncio.to_thread(bridge._local_seat, caller, scope, "project-b") is None  # named in this call
        bridge._current[scope] = "project-b"  # what crew_status(project_id="project-b") leaves
        assert await asyncio.to_thread(bridge._local_seat, caller, scope, None) is None
        assert await asyncio.to_thread(bridge._local_seat, caller, scope, local_project) is not None  # named back


async def test_a_task_baton_restore_refused_by_a_dirty_checkout_is_retried_by_the_same_call(tmp_path, monkeypatch):
    """Codex review P2: the second crew_claim adopt restores the saved work once the checkout is clean."""
    repo = make_repo(tmp_path / "repo")
    wt_c = add_worktree(repo, tmp_path / "wt-c", "c-work")
    me = os.getpid()
    async with crew_server(tmp_path) as srv:
        layout, d = await _start_crewd(tmp_path, srv, alive={LOST_PID})
        monkeypatch.setenv("HOME", str(layout.home))
        a = await d.join(
            peer(LOST_PID), {"adapter": "claude-code", "client_session_id": "sess-a", "cwd": str(repo), "agent_pid": LOST_PID}
        )
        crew_id = a["crew_id"]
        pos = next(z for z in d.snapshots[crew_id]["zones"] if z["slug"] == "pos")
        api, tok = d.api_for(d.sessions[a["key"]]), d.tokens[a["key"]]
        created = await api.call(
            "POST",
            f"/crews/{crew_id}/tasks",
            session_token=tok,
            json_body={"title": "Split tender", "zone_ids": [pos["id"]], "acceptance": [], "depends_on": []},
        )
        task = created.body["task"]
        from tests.crew.wp9_support import git

        assert (
            await api.call(
                "POST", f"/tasks/{task['id']}/start", session_token=tok, json_body={"head": git(repo, "rev-parse", "HEAD")}
            )
        ).ok
        await d.sync_snapshot(crew_id)
        (repo / "src/app/pos/tender.ts").write_text("export const tender = 'wip';\n")
        assert (await d.stall(peer(LOST_PID), {"key": a["key"], "error": "billing_error"})).get("baton_ref")
        await d.join(peer(me), {"adapter": "codex", "client_session_id": "sess-c", "cwd": str(wt_c), "agent_pid": me})
        (wt_c / "scratch.txt").write_text("in the way\n")  # uncommitted change: the restore is refused
        args = dict(zone=None, paths=None, mode="exclusive", to=None, baton=None, reason=None, wait_s=0)
        first = await asyncio.to_thread(CrewBridge().claim, _caller(), action="adopt", task="T-1", **args)
        assert first.startswith("ADOPTED T-1") and "NOT restored yet" in first, first
        (wt_c / "scratch.txt").unlink()
        second = await asyncio.to_thread(CrewBridge().claim, _caller(), action="adopt", task="T-1", **args)
        assert "saved work is restored into this checkout" in second, second
        assert (wt_c / "src/app/pos/tender.ts").read_text() == "export const tender = 'wip';\n"


async def test_several_paths_of_one_baton_are_adopted_together_and_restored_once(tmp_path, monkeypatch):
    """Codex review P2: the brief may name several paths of one lost session; the adopt takes them all."""
    repo = make_repo(tmp_path / "repo")
    wt_c = add_worktree(repo, tmp_path / "wt-c", "c-work")
    me = os.getpid()
    async with crew_server(tmp_path) as srv:
        layout, d = await _start_crewd(tmp_path, srv, alive={LOST_PID})
        monkeypatch.setenv("HOME", str(layout.home))
        a = await d.join(
            peer(LOST_PID), {"adapter": "claude-code", "client_session_id": "sess-a", "cwd": str(repo), "agent_pid": LOST_PID}
        )
        for path in ("docs/a.md", "notes/b.md"):
            got = await d.api_for(d.sessions[a["key"]]).call(
                "POST",
                f"/crews/{a['crew_id']}/claims",
                session_token=d.tokens[a["key"]],
                json_body={"path_glob": path, "mode": "exclusive", "wait": False, "source": "first_write"},
            )
            assert got.ok, got.body
            (repo / path).parent.mkdir(parents=True, exist_ok=True)
            (repo / path).write_text(f"{path}\n")
        assert (await d.stall(peer(LOST_PID), {"key": a["key"], "error": "billing_error"})).get("baton_ref")
        c = await d.join(peer(me), {"adapter": "codex", "client_session_id": "sess-c", "cwd": str(wt_c), "agent_pid": me})
        args = dict(zone=None, mode="exclusive", task=None, to=None, baton=None, reason=None, wait_s=0)
        text = await asyncio.to_thread(CrewBridge().claim, _caller(), action="adopt", paths=["docs/a.md", "notes/b.md"], **args)
        assert text.startswith("ADOPTED docs/a.md, notes/b.md: you now hold 2 claim(s)"), text
        assert "restored into this checkout: 2 file(s)" in text, text
        held = await srv.rows(
            "SELECT path_glob FROM crew_claims WHERE holder_session_id = ? AND state = 'active'", (c["session_id"],)
        )
        assert sorted(r["path_glob"] for r in held) == ["docs/a.md", "notes/b.md"]


async def test_a_project_chosen_later_moves_the_session_off_the_hook_seat(tmp_path, monkeypatch):
    """Codex review P1: once the hook seat was used, a project chosen later (session_brief) still wins."""
    repo = make_repo(tmp_path / "repo")
    me = os.getpid()
    async with crew_server(tmp_path) as srv:
        layout, d = await _start_crewd(tmp_path, srv)
        monkeypatch.setenv("HOME", str(layout.home))
        await d.join(peer(me), {"adapter": "claude-code", "client_session_id": "sess-c", "cwd": str(repo), "agent_pid": me})
        seen: list[str | None] = []

        class Probe(CrewBridge):
            def _join_body(self, caller, project, client_session_id, checkout):  # type: ignore[override]
                seen.append(project)
                raise RuntimeError("mcp seat join")

        bridge = Probe()
        first, _ = await asyncio.to_thread(bridge.seat, _caller())
        assert first.local is not None
        local_project = first.project_id
        bridge.note_project(_caller(), "project-b")  # session_brief chose B (A was in use)
        with pytest.raises(RuntimeError, match="mcp seat join"):
            await asyncio.to_thread(bridge.seat, _caller(chosen="project-b"))
        assert seen == ["project-b"]
        # and back: session_brief chooses A again (Codex review, third pass): the hook seat, not B's
        bridge.note_project(_caller(), local_project)
        back, _ = await asyncio.to_thread(bridge.seat, _caller(chosen=local_project))
        assert back.local is not None and back.project_id == local_project
        assert seen == ["project-b"]


async def test_a_reserved_resource_is_adopted_over_the_crew_routes(tmp_path, monkeypatch):
    """Codex review P2: zone="deploy:vercel" is a resource, not a zone slug: it is not sent to crewd's adopt."""
    called: list[str] = []

    class Probe(CrewBridge):
        def _local_adopt(self, *a, **kw):  # type: ignore[override]
            called.append("local")
            return "local"

        def _adopt(self, http, seat, *, zone, paths, task):  # type: ignore[override]
            called.append(f"crew routes {zone}")
            return "ok"

    repo = make_repo(tmp_path / "repo")
    me = os.getpid()
    async with crew_server(tmp_path) as srv:
        layout, d = await _start_crewd(tmp_path, srv)
        monkeypatch.setenv("HOME", str(layout.home))
        await d.join(peer(me), {"adapter": "claude-code", "client_session_id": "sess-c", "cwd": str(repo), "agent_pid": me})
        args = dict(paths=None, mode="exclusive", task=None, to=None, baton=None, reason=None, wait_s=0)
        await asyncio.to_thread(Probe().claim, _caller(), action="adopt", zone="deploy:vercel", **args)
        await asyncio.to_thread(Probe().claim, _caller(), action="adopt", zone="pos", **args)
    assert called == ["crew routes deploy:vercel", "local"]
