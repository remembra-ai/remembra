"""F15: the crew block's last line is a statement about the READER, not a constant.

Found live: Codex sessions that crew hooks never joined (they start outside a git checkout) were told
"Crew mode is automatic: claims, checkpoints and reports happen by hook." by the session brief, while
crew_status said "You are an MCP session (advisory): nothing is claimed for you automatically" seconds
later, and a baton's first "To continue" form was a remembra-crew command that refuses without a hook
session. Here: the renderer, the relay CLI's local correction and the crew CLI's refusal.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

import remembra.relay.crew.cli as crew_cli
from remembra.crew import schemas
from remembra.mcp.crew import CallerInfo, CrewBridge
from remembra.relay import handoff as H
from remembra.relay.cli import crew_footer_here
from remembra.relay.crew.gate import Layout, session_key
from tests.crew.test_wp9_gate import _start_crewd
from tests.crew.wp9_support import crew_server, make_repo, peer

AUTOMATIC = "Crew mode is automatic"
MANUAL = "nothing is claimed for you automatically"
BATON = {
    "task": "T-12",
    "task_title": "Invoice PDF",
    "zones": ["reports"],
    "from": "cc-1",
    "from_state": "quota_blocked",
    "baton_ref": "refs/remembra/baton/T-12/7",
    "dirty": 3,
}
HOOK = {"client_kind": "hook", "enforcement": "enforced"}
MCP = {"client_kind": "mcp", "enforcement": "advisory"}


def _block(**crew: object) -> str:
    return H.render_crew_block({"project_id": "p", "live": 0, **crew})


# ---------------------------------------------------------------------------
# The renderer (server side, and crew_status client side)
# ---------------------------------------------------------------------------


def test_an_unknown_reader_is_not_told_crew_mode_is_automatic():
    for crew in ({}, {"viewer": None}, {"viewer": None, "hook_live": 2, "reader_agent": "codex"}):
        block = _block(**crew)
        assert AUTOMATIC not in block, block
        assert block.split("\n")[-1] == H.CREW_FOOTER_UNKNOWN
        assert schemas.check_agent_text(block, "crew_block") == []
    # the neutral line states the condition and what to do when it does not hold
    assert "only in a session the crew hooks joined" in H.CREW_FOOTER_UNKNOWN
    assert "crew_status" in H.CREW_FOOTER_UNKNOWN and "crew_claim" in H.CREW_FOOTER_UNKNOWN


def test_a_hook_joined_reader_keeps_the_automatic_line_whatever_its_enforcement():
    # only claude-code is ever "enforced": a hook-joined Codex is (hook, advisory) and its hooks still act
    for viewer in (HOOK, {"client_kind": "hook", "enforcement": "advisory"}, {"client_kind": "hook"}):
        assert _block(viewer=viewer).split("\n")[-1] == H.CREW_FOOTER
    assert H.CREW_FOOTER == "Crew mode is automatic: claims, checkpoints and reports happen by hook."


def test_an_mcp_reader_gets_the_advisory_line_crew_status_ends_with():
    from remembra.mcp.crew import MCP_FOOTER

    block = _block(viewer=MCP, hook_live=3)
    assert block.split("\n")[-1] == MCP_FOOTER == H.CREW_FOOTER_MCP
    assert AUTOMATIC not in block and MANUAL in block
    assert schemas.check_agent_text(block, "crew_block") == []


def test_no_live_hook_session_for_the_asking_agent_is_said_by_name():
    block = _block(viewer=None, hook_live=0, reader_agent="codex")
    last = block.split("\n")[-1]
    assert last == H.crew_footer_no_hook("codex")
    assert last.startswith("No crew-hook session of codex is live in this crew")
    assert MANUAL in last and AUTOMATIC not in block
    # missing or hostile agent ids never reach the template line raw; a non-integer count decides nothing
    assert H.crew_footer_no_hook(None).startswith("No crew-hook session is live in this crew")
    assert "rm -rf" not in H.crew_footer_no_hook("x; rm -rf /")
    assert _block(viewer=None, hook_live=None).split("\n")[-1] == H.CREW_FOOTER_UNKNOWN
    assert _block(viewer=None, hook_live="0").split("\n")[-1] == H.CREW_FOOTER_UNKNOWN
    assert _block(viewer=None, hook_live=False).split("\n")[-1] == H.CREW_FOOTER_UNKNOWN
    # a joined viewer of an unknown kind (an old row without client_kind) is not called unjoined
    assert _block(viewer={"client_kind": None}, hook_live=0).split("\n")[-1] == H.CREW_FOOTER_UNKNOWN
    assert _block(viewer="junk", hook_live=0).split("\n")[-1] == H.CREW_FOOTER_UNKNOWN


def test_the_caller_can_name_the_reader_when_the_server_did_not():
    # crew_status knows its own seat; a 0.17.5 server sends no "viewer" at all
    assert _block(batons=[BATON]).count("remembra-crew adopt T-12") == 1
    hook = H.render_crew_block({"project_id": "p", "batons": [BATON]}, reader="hook")
    assert hook.split("\n")[-1] == H.CREW_FOOTER
    mcp = H.render_crew_block({"project_id": "p", "batons": [BATON], "viewer": HOOK}, reader="mcp")
    assert mcp.split("\n")[-1] == H.CREW_FOOTER_MCP


def test_every_footer_is_recognised_as_one():
    for line in (
        H.CREW_FOOTER,
        H.CREW_FOOTER_MCP,
        H.CREW_FOOTER_UNJOINED,
        H.CREW_FOOTER_UNKNOWN,
        H.crew_footer_no_hook("codex"),
        H.crew_footer_no_hook(None),
    ):
        assert H.is_crew_footer(line), line
        assert len(line) <= 300
    for line in ("", "CREW p (solo · 0 live)", "DO NOT TOUCH: zone pos → cc-1", H.crew_footer_off("codex")):
        assert not H.is_crew_footer(line), line


def test_no_hook_session_is_said_only_when_the_crew_has_none_at_all():
    # hook_live counts every agent's hook sessions: an id the hooks did not use proves nothing about the reader
    assert _block(viewer=None, hook_live=1, reader_agent="claude").split("\n")[-1] == H.CREW_FOOTER_UNKNOWN
    assert _block(viewer=None, hook_live=0, reader_agent="claude").split("\n")[-1] == H.crew_footer_no_hook("claude")


def test_the_footer_survives_the_pathological_cut_whole():
    long_task = {**BATON, "failing": ["x" * 50] * 3}
    for viewer in (None, MCP, HOOK):
        block = H.render_crew_block({"project_id": "p", "batons": [long_task, long_task], "viewer": viewer}, max_chars=420)
        assert len(block) <= 420
        assert H.is_crew_footer(block.split("\n")[-1]), block
        assert block.startswith("CREW p (solo")


def test_a_baton_offered_to_an_mcp_reader_leads_with_the_tool_it_can_run():
    lines = _block(viewer=MCP, batons=[BATON]).split("\n")
    assert lines[1].startswith("YOUR BATON (offered to you): T-12 from cc-1")
    assert lines[2].startswith('  To continue T-12: crew_claim(action="adopt", task="T-12")'), lines[2]
    # the CLI is never offered to a session it cannot find: the line says where saved work restores, with no command
    assert lines[2] == (
        '  To continue T-12: crew_claim(action="adopt", task="T-12")'
        "   (the saved work is restored only from a session the crew hooks joined)"
    )
    assert "remembra-crew adopt" not in "\n".join(lines)
    no_ref = {**BATON, "baton_ref": None, "dirty": 0}
    line = _block(viewer=MCP, batons=[no_ref]).split("\n")[2]
    assert line == '  To continue T-12: crew_claim(action="adopt", task="T-12")'
    path = {"paths": ["art/lls/cover.psd"], "from": "cc-1"}
    line = _block(viewer=MCP, batons=[path]).split("\n")[2]
    assert line == '  To continue: crew_claim(action="adopt", paths=["art/lls/cover.psd"])'
    zone = {"zones": ["reports"], "from": "cc-1", "baton_ref": "refs/remembra/baton/cs_a/1"}
    line = _block(viewer=MCP, batons=[zone]).split("\n")[2]
    assert line == (
        '  To continue: crew_claim(action="adopt", zone="reports")'
        "   (the saved work is restored only from a session the crew hooks joined)"
    )
    saved_path = {**path, "baton_ref": "refs/remembra/baton/cs_a/2"}
    for reader in (MCP, {"client_kind": "cli", "enforcement": "advisory"}):
        assert "remembra-crew adopt" not in _block(viewer=reader, batons=[BATON, zone, saved_path])
    assert schemas.check_agent_text(_block(viewer=MCP, batons=[BATON, path]), "crew_block") == []


def test_a_baton_offered_to_a_hook_reader_keeps_the_cli_form():
    lines = _block(viewer=HOOK, batons=[BATON]).split("\n")
    assert lines[2] == (
        "  To continue T-12: remembra-crew adopt T-12   (restores the saved work into this checkout)"
        '   or crew_claim(action="adopt", task="T-12")'
    )
    path = {"paths": ["art/lls/cover.psd"], "from": "cc-1"}
    assert _block(viewer=HOOK, batons=[path]).split("\n")[2] == (
        '  To continue: remembra-crew adopt --path art/lls/cover.psd   or crew_claim(action="adopt", paths=["art/lls/cover.psd"])'
    )


# ---------------------------------------------------------------------------
# The relay CLI: what this machine knows about the reading session
# ---------------------------------------------------------------------------


def _hooks(home: Path, agent: str = "codex") -> None:
    from remembra.relay.adapters import crew_hooks_installed, get_adapter

    gate = home / ".remembra" / "crew" / "bin" / "crew-gate.py"
    gate.parent.mkdir(parents=True, exist_ok=True)
    gate.write_text("# gate\n")
    config = get_adapter(agent).spec.config_file(home)
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text(f"crew-gate pretool {schemas.CREW_HOOK_MARKER}\n")
    assert crew_hooks_installed(home, agent, None) is True


def _ctx(home: Path, agent: str = "codex") -> SimpleNamespace:
    from remembra.relay.adapters import get_adapter

    return SimpleNamespace(agent=agent, home=home, cwd=home, repo=SimpleNamespace(toplevel=None), adapter=get_adapter(agent))


def _legacy_brief() -> str:
    # exactly what a 0.17.5 server renders for every reader
    return "Last session: x\nCREW clawdbot (solo · 0 live) as of 05:02 UTC\n" + H.CREW_FOOTER


def test_hooks_installed_but_this_session_never_joined_says_so(tmp_path: Path):
    _hooks(tmp_path)
    out = crew_footer_here(_ctx(tmp_path), _legacy_brief(), "sess-outside-a-checkout")
    assert AUTOMATIC not in out, out
    assert out.split("\n")[-1] == H.CREW_FOOTER_UNJOINED
    assert out.split("\n")[:2] == _legacy_brief().split("\n")[:2]
    assert "did not join this session" in H.CREW_FOOTER_UNJOINED and MANUAL in H.CREW_FOOTER_UNJOINED
    # the same against a fixed server, which could only say "unknown" or "none live for codex"
    for server_line in (H.CREW_FOOTER_UNKNOWN, H.crew_footer_no_hook("codex")):
        out = crew_footer_here(_ctx(tmp_path), "CREW p (solo · 0 live)\n" + server_line, "sess-outside-a-checkout")
        assert out.split("\n")[-1] == H.CREW_FOOTER_UNJOINED


def test_a_session_the_hooks_joined_keeps_the_automatic_line(tmp_path: Path):
    _hooks(tmp_path)
    layout = Layout(tmp_path)
    layout.sessions.mkdir(parents=True, exist_ok=True)
    layout.session_file(session_key("codex", "sess-joined")).write_text("{}")
    assert crew_footer_here(_ctx(tmp_path), _legacy_brief(), "sess-joined") == _legacy_brief()
    # even where the server could not tell (it resolved no viewer), this machine can
    out = crew_footer_here(_ctx(tmp_path), "CREW p (solo · 1 live)\n" + H.CREW_FOOTER_UNKNOWN, "sess-joined")
    assert out.split("\n")[-1] == H.CREW_FOOTER


def test_without_a_hook_session_id_the_old_servers_line_becomes_the_neutral_one(tmp_path: Path):
    _hooks(tmp_path)
    for sid in (None, ""):
        out = crew_footer_here(_ctx(tmp_path), _legacy_brief(), sid)
        assert out.split("\n")[-1] == H.CREW_FOOTER_UNKNOWN
    assert crew_footer_here(_ctx(tmp_path), _legacy_brief()).split("\n")[-1] == H.CREW_FOOTER_UNKNOWN
    # a fixed server's own verdict is left alone
    told = "CREW p (solo · 0 live)\n" + H.crew_footer_no_hook("codex")
    assert crew_footer_here(_ctx(tmp_path), told, None) == told
    # hooks not installed still wins, and a brief without a crew block is untouched
    bare = tmp_path / "bare"
    bare.mkdir()
    assert crew_footer_here(_ctx(bare), _legacy_brief(), "s").split("\n")[-1] == H.crew_footer_off("codex")
    assert crew_footer_here(_ctx(tmp_path), "Last session: x", "s") == "Last session: x"
    # an agent without crew hooks at all (undecided) is not told "automatic" either
    ctx = SimpleNamespace(agent="no-such-agent", home=tmp_path, cwd=tmp_path, repo=SimpleNamespace(toplevel=None))
    assert crew_footer_here(ctx, _legacy_brief(), "s").split("\n")[-1] == H.CREW_FOOTER_UNKNOWN


# ---------------------------------------------------------------------------
# remembra-crew: the refusal says which case it is
# ---------------------------------------------------------------------------


async def test_cli_refusal_with_crewd_running_and_no_hook_session(tmp_path, monkeypatch, capsys):
    outside = tmp_path / "not-a-checkout"
    outside.mkdir()
    repo = make_repo(tmp_path / "repo")
    async with crew_server(tmp_path) as srv:
        layout, _d = await _start_crewd(tmp_path, srv)

        def refused() -> str:
            capsys.readouterr()  # drop the server's log lines printed so far
            with pytest.raises(SystemExit) as stop:
                crew_cli._need_session(layout)
            assert stop.value.code == 1
            return next(ln for ln in capsys.readouterr().out.split("\n") if ln.startswith("REFUSED"))

        monkeypatch.chdir(outside)
        out = await asyncio.to_thread(refused)
        assert out.startswith("REFUSED: no crew session found for this command: crewd is running, but no crew hook joined"), out
        assert "this command ran outside any git checkout" in out, out
        assert "crew_status" in out and "crew_claim" in out
        # the old text blamed the sandbox and promised the tool "reaches the same session"
        assert "sandbox" not in out and "same session" not in out
        assert not out.rstrip().endswith("…"), out  # the whole sentence fits the refusal's clip

        monkeypatch.chdir(repo)
        out = await asyncio.to_thread(refused)
        assert "this command ran inside a git checkout" in out, out
        assert not out.rstrip().endswith("…"), out


async def test_cli_with_a_hook_session_still_resolves_it(tmp_path, monkeypatch):
    repo = make_repo(tmp_path / "repo")
    me = os.getpid()
    async with crew_server(tmp_path) as srv:
        layout, d = await _start_crewd(tmp_path, srv)
        c = await d.join(peer(me), {"adapter": "codex", "client_session_id": "sess-c", "cwd": str(repo), "agent_pid": me})
        who = await asyncio.to_thread(crew_cli._need_session, layout)
        assert who["session_id"] == c["session_id"] and who["ok"] is True


def test_cli_refusal_when_crewd_cannot_be_reached(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(crew_cli, "ensure_crewd", lambda *a, **k: False)  # never spawn a daemon from a test
    with pytest.raises(SystemExit) as stop:
        crew_cli._need_session(Layout(tmp_path / "nobody"))
    assert stop.value.code == 1
    out = capsys.readouterr().out
    assert out.startswith("REFUSED: no crew session found for this command: crewd cannot be reached from this shell"), out
    assert "sandbox" in out and "remembra-crew doctor" in out and "crew_status" in out
    assert "no crew hook joined" not in out
    assert not out.rstrip().endswith("…"), out


# ---------------------------------------------------------------------------
# crew_status on the agent's machine, where the seat IS the hook session
# ---------------------------------------------------------------------------


async def test_crew_status_as_the_hook_session_agrees_with_its_brief(tmp_path, monkeypatch):
    repo = make_repo(tmp_path / "repo")
    me = os.getpid()
    async with crew_server(tmp_path) as srv:
        layout, d = await _start_crewd(tmp_path, srv)
        monkeypatch.setenv("HOME", str(layout.home))
        c = await d.join(peer(me), {"adapter": "codex", "client_session_id": "sess-c", "cwd": str(repo), "agent_pid": me})
        brief = await d.brief(peer(me), {"key": c["key"]})
        assert brief["ok"] and brief["rendered"].split("\n")[-1] == H.CREW_FOOTER, brief
        assert brief["crew"]["viewer"] == {"client_kind": "hook", "enforcement": "advisory"}
        loop = asyncio.get_running_loop()

        class ViaApp:  # the agent's own HTTP client, pointed at the in-process API (the brief is not a crewd route)
            def request(self, method: str, url: str, **kw: object) -> httpx.Response:
                async def go() -> httpx.Response:
                    async with httpx.AsyncClient(transport=srv.transport()) as c:
                        headers = {**(kw.get("headers") or {}), "X-API-Key": srv.key}  # type: ignore[dict-item]
                        return await c.request(method, url, json=kw.get("json"), params=kw.get("params"), headers=headers)

                return asyncio.run_coroutine_threadsafe(go(), loop).result(30)

        client = SimpleNamespace(
            agent_id="codex", api_key="k", project=None, base_url="http://test", timeout=10, _client=ViaApp()
        )
        caller = CallerInfo(
            client=client,  # type: ignore[arg-type]
            fingerprint="fp",
            client_session_id="mcp-1",
            agent_id="codex",
            default_project="some-other-default",
            chosen_project=None,
            local=True,
        )
        text = await asyncio.to_thread(CrewBridge().status, caller, project_id=None, checkout=None, verbose=False)
        assert f"YOU: {c['callsign']} ({c['session_id']}) · hook session" in text, text
        assert "mcp, advisory" not in text and "You are an MCP session" not in text, text
        assert text.split("\n")[-1] == H.CREW_FOOTER, text
        assert text.count(H.CREW_FOOTER) == 1


# ---------------------------------------------------------------------------
# The hook-injected brief end to end: `remembra-relay brief --hook codex` against a 0.17.5 server
# ---------------------------------------------------------------------------

HOST_MARKERS = (
    "GROK_WORKSPACE_ROOT",
    "GEMINI_SESSION_ID",
    "QWEN_CODE_SESSION_ID",
    "DEVIN_PROJECT_DIR",
    "CONTINUE_PROJECT_DIR",
    "CLAUDE_CONFIG_DIR",
    "CODEX_HOME",
    "REMEMBRA_RELAY_INCLUDE_AUTOMATIONS",
)


def _hook_brief(monkeypatch, capsys, home: Path, session: str, cwd: Path) -> str:
    """Run the real ``remembra-relay brief --hook codex`` with a SessionStart payload; the server is 0.17.5."""
    import io
    import json

    from remembra.relay import cli as relay_cli

    seen: list[dict[str, str]] = []

    def old_server(request: httpx.Request) -> httpx.Response:
        seen.append(dict(request.url.params))
        if request.url.path.endswith("/session/brief"):
            return httpx.Response(200, json={"project_id": "clawdbot", "agent_id": "codex", "rendered": _legacy_brief()})
        return httpx.Response(404, json={"detail": "not found"})

    http = httpx.Client(transport=httpx.MockTransport(old_server), base_url="http://old-server")

    class Keep:
        def __enter__(self) -> httpx.Client:
            return http

        def __exit__(self, *exc: object) -> bool:
            return False

    monkeypatch.setattr(relay_cli.Context, "client", lambda self, config=None, agent=None, budget=None: Keep())
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("REMEMBRA_API_KEY", "rem_test_only")
    monkeypatch.setenv("REMEMBRA_URL", "http://old-server")
    for name in (*HOST_MARKERS, "REMEMBRA_AGENT_ID", "REMEMBRA_PROJECT", "REMEMBRA_RELAY_PROJECT", "REMEMBRA_SESSION_ID"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"session_id": session, "cwd": str(cwd), "source": "startup"})))
    capsys.readouterr()
    assert relay_cli.main(["brief", "--hook", "codex", "--agent", "codex"]) == 0
    out = capsys.readouterr().out
    assert any(p.get("session_id") == session for p in seen), seen  # the brief was asked for this hook session
    try:
        return json.dumps(json.loads(out), ensure_ascii=False).replace('\\"', '"').replace("\\n", "\n")
    except ValueError:
        return out


def test_the_hook_injected_brief_of_an_unjoined_codex_session(tmp_path, monkeypatch, capsys):
    """Codex started in a folder that is not a git checkout: `remembra-crew start` joins nothing and passes
    the brief through to the relay. Its hooks ARE installed, which is all the old check looked at."""
    home = tmp_path / "home"
    home.mkdir()
    _hooks(home)
    outside = tmp_path / "not-a-checkout"
    outside.mkdir()
    out = _hook_brief(monkeypatch, capsys, home, "sess-unjoined", outside)
    assert "CREW clawdbot (solo · 0 live)" in out, out
    assert AUTOMATIC not in out, out
    assert H.CREW_FOOTER_UNJOINED in out, out

    # the same hook for a session crewd joined (its session file exists) keeps the true line
    layout = Layout(home)
    layout.sessions.mkdir(parents=True, exist_ok=True)
    layout.session_file(session_key("codex", "sess-joined")).write_text("{}")
    out = _hook_brief(monkeypatch, capsys, home, "sess-joined", outside)
    assert H.CREW_FOOTER in out and H.CREW_FOOTER_UNJOINED not in out, out
