"""Which project, and so which crew, a checkout joins: crewd follows the relay's rule (0.16.1).

A git repository always gets its own project, whatever ``REMEMBRA_PROJECT`` (the MCP env) says; the
configured project names only a folder that is not a repository; ``REMEMBRA_RELAY_PROJECT`` opts into one
project for everything; when git does not answer in time neither ``git_repo`` nor the configured project is
sent. Before this fix crewd sent the configured project as a bare ``hint_project`` (no ``hint_scope``, no
``git_repo``), which the server reads as a 0.16.0 client's ``all``: every new repository joined the configured
project's crew.

Everything runs crewd against the real crew and relay routers (real crew.db and main database, auth on, a
real API key, ``tests.crew.wp9_support``) with real git repositories; a recording transport keeps what crewd
sent. The relay side is the real ``remembra-relay`` ``Context``.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
from pathlib import Path
from typing import Any

import httpx
import pytest

from remembra.relay import cli as relay_cli
from remembra.relay import hints
from remembra.relay.config import RelayConfig
from remembra.relay.crew import cli as crew_cli
from remembra.relay.crew import crewd as crewd_mod
from remembra.relay.crew.gate import Layout
from tests.crew.wp9_support import Server, crew_server, git, new_crewd, peer

A_PID, B_PID, C_PID = 42001, 42002, 42003
CONFIGURED = "clawdbot"
HINT_KEYS = ("hint_project", "hint_scope", "git_repo")
RESOLVE_PATHS = ("/api/v1/projects/resolve", "/api/v1/crews/resolve")


@pytest.fixture(autouse=True)
def _no_relay_project(monkeypatch):
    """The developer's own shell must not decide these tests."""
    monkeypatch.delenv("REMEMBRA_RELAY_PROJECT", raising=False)


def _repo(root: Path, *, commit: bool = True) -> Path:
    """A git repository named after ``root``; its root commit is its own (the content names it)."""
    root.mkdir(parents=True)
    git(root, "init", "-q", "-b", "main")
    git(root, "config", "user.email", "dev@example.com")
    git(root, "config", "user.name", "Dev")
    git(root, "config", "commit.gpgsign", "false")
    if commit:
        (root / "README.md").write_text(f"{root.name}\n")
        git(root, "add", "-A")
        git(root, "commit", "-q", "-m", f"init {root.name}")
    return root.resolve()


def _loader(srv: Server, project: str | None = CONFIGURED):
    """Like the owner's machine: the MCP env pins ``REMEMBRA_PROJECT``."""

    def load(agent: str | None, prefer: str | None) -> RelayConfig:
        return RelayConfig(url="http://test", api_key=srv.key, agent_id=agent, source="test", project=project)

    return load


class Recorder(httpx.AsyncBaseTransport):
    """Passes every request to the app and keeps the bodies crewd posted to resolve a location.

    ``refuse_hint_fields`` answers ``/crews/resolve`` like a server whose model forbids ``hint_scope`` and
    ``git_repo`` (extra fields), as 0.17.0 servers built before this fix do.
    """

    def __init__(self, inner: httpx.AsyncBaseTransport, *, refuse_hint_fields: bool = False) -> None:
        self.inner = inner
        self.refuse_hint_fields = refuse_hint_fields
        self.sent: list[tuple[str, dict[str, Any]]] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path in RESOLVE_PATHS:
            body = json.loads(request.content or b"{}")
            self.sent.append((request.url.path, body))
            extra = [k for k in ("hint_scope", "git_repo") if k in body]
            if self.refuse_hint_fields and request.url.path.endswith("/crews/resolve") and extra:
                detail = [{"loc": ["body", k], "type": "extra_forbidden", "msg": "Extra inputs are not permitted"} for k in extra]
                return httpx.Response(422, json={"detail": detail})
        return await self.inner.handle_async_request(request)

    def bodies(self, path: str) -> list[dict[str, Any]]:
        return [body for p, body in self.sent if p == path]


def _hints(body: dict[str, Any]) -> dict[str, Any]:
    return {k: body[k] for k in HINT_KEYS if k in body}


def _join_args(sid: str, cwd: Path, pid: int, **extra: Any) -> dict[str, Any]:
    return {
        "adapter": "claude-code",
        "agent_id": "claude-code",
        "client_session_id": sid,
        "cwd": str(cwd),
        "agent_pid": pid,
        "source": "startup",
        **extra,
    }


async def _bindings(srv: Server) -> dict[str, str]:
    cur = await srv.h.db.conn.execute("SELECT fingerprint, project_id FROM project_fingerprints")
    return {str(fp): str(project) for fp, project in await cur.fetchall()}


def _relay_hint_fields(
    monkeypatch: pytest.MonkeyPatch, home: Path, cwd: Path, relay_project: str | None = None
) -> dict[str, Any]:
    """What ``remembra-relay`` sends for ``cwd`` with the same configuration (``REMEMBRA_PROJECT=clawdbot``)."""
    home.mkdir(exist_ok=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("REMEMBRA_API_KEY", "rem_test_parity_only")
    monkeypatch.setenv("REMEMBRA_PROJECT", CONFIGURED)
    if relay_project:
        monkeypatch.setenv("REMEMBRA_RELAY_PROJECT", relay_project)
    else:
        monkeypatch.delenv("REMEMBRA_RELAY_PROJECT", raising=False)
    ns = relay_cli.build_parser().parse_args(["close", "--agent", "codex", "--cwd", str(cwd), "--session-id", "s1"])
    return relay_cli.Context(ns, payload={}).hint_fields()


# ---------------------------------------------------------------------------
# end to end: two repositories, two projects, two crews; a folder, the configured project
# ---------------------------------------------------------------------------


async def test_each_repository_gets_its_own_project_and_crew_and_a_folder_the_configured_one(tmp_path, monkeypatch):
    alpha = _repo(tmp_path / "alpha")
    beta = _repo(tmp_path / "beta", commit=False)  # a fresh `git init`: no commit, no remote (the live case)
    notes = (tmp_path / "notes").resolve()
    notes.mkdir()
    layout = Layout(tmp_path / "home")
    async with crew_server(tmp_path) as srv:
        rec = Recorder(srv.transport())
        d = new_crewd(layout, srv, alive={A_PID, B_PID}, config_loader=_loader(srv), transport=rec)
        a = await d.join(peer(A_PID), _join_args("sess-a", alpha, A_PID))
        b = await d.join(peer(B_PID), _join_args("sess-b", beta, B_PID))
        assert a["ok"] and b["ok"], (a, b)
        assert (a["project_id"], b["project_id"]) == ("alpha", "beta")
        assert a["crew_id"] != b["crew_id"]
        crews = await srv.rows("SELECT project_id FROM crews ORDER BY project_id")
        assert [r["project_id"] for r in crews] == ["alpha", "beta"]

        # what crewd sent for each repository: the relay's fields, so the configured project names no repository
        sent = rec.bodies("/api/v1/projects/resolve")
        assert [_hints(body) for body in sent] == [{"hint_project": CONFIGURED, "hint_scope": "folders", "git_repo": True}] * 2
        assert CONFIGURED not in (await _bindings(srv)).values()

        # a folder that is not a repository uses the configured project
        cfg = _loader(srv)("claude-code", None)
        api = d.api_for_cfg(cfg, "claude-code")
        assert await d.resolve_project(api, cfg, str(notes)) == CONFIGURED
        assert _hints(rec.bodies("/api/v1/projects/resolve")[-1]) == {
            "hint_project": CONFIGURED,
            "hint_scope": "folders",
            "git_repo": False,
        }
        bound = await _bindings(srv)
        assert sorted(v for v in bound.values() if v == CONFIGURED) == [CONFIGURED]  # the folder's path only

        # one rule: the relay sends exactly these fields for the same places
        crewd_fields = [_hints(body) for body in rec.bodies("/api/v1/projects/resolve")]
        relay_home = tmp_path / "relay-home"
        assert crewd_fields == [_relay_hint_fields(monkeypatch, relay_home, where) for where in (alpha, beta, notes)]


# ---------------------------------------------------------------------------
# REMEMBRA_RELAY_PROJECT: one project (and one crew) for everything
# ---------------------------------------------------------------------------


async def test_relay_project_keeps_every_repository_in_that_one_project(tmp_path, monkeypatch):
    alpha = _repo(tmp_path / "alpha")
    beta = _repo(tmp_path / "beta")
    gamma = _repo(tmp_path / "gamma")
    layout = Layout(tmp_path / "home")
    async with crew_server(tmp_path) as srv:
        rec = Recorder(srv.transport())
        d = new_crewd(layout, srv, alive={A_PID, B_PID, C_PID}, config_loader=_loader(srv), transport=rec)
        # the hook's environment decides (remembra-crew start forwards it) ...
        a = await d.join(peer(A_PID), _join_args("sess-a", alpha, A_PID, relay_project="team"))
        assert a["ok"] and a["project_id"] == "team", a
        assert _hints(rec.bodies("/api/v1/projects/resolve")[-1]) == {
            "hint_project": "team",
            "hint_scope": "all",
            "git_repo": True,
        }
        # ... over crewd's own: a hook without it follows the folders rule even when crewd's environment has it
        monkeypatch.setenv("REMEMBRA_RELAY_PROJECT", "team")
        b = await d.join(peer(B_PID), _join_args("sess-b", beta, B_PID, relay_project=None))
        assert b["ok"] and b["project_id"] == "beta", b
        # an older remembra-crew that does not say: crewd's own environment
        c = await d.join(peer(C_PID), _join_args("sess-c", gamma, C_PID))
        assert c["ok"] and c["project_id"] == "team" and c["crew_id"] == a["crew_id"], c
        assert _hints(rec.bodies("/api/v1/projects/resolve")[-1]) == _relay_hint_fields(
            monkeypatch, tmp_path / "relay-home", gamma, relay_project="team"
        )


# ---------------------------------------------------------------------------
# git did not answer in time: neither git_repo nor the configured project
# ---------------------------------------------------------------------------


def _slow_toplevel_git(tmp_path: Path) -> Path:
    real_git = shutil.which("git")
    assert real_git
    bindir = tmp_path / "slowbin"
    bindir.mkdir()
    shim = bindir / "git"
    shim.write_text(
        f'#!/bin/sh\nfor a in "$@"; do if [ "$a" = "--show-toplevel" ]; then exec sleep 5; fi; done\nexec "{real_git}" "$@"\n'
    )
    shim.chmod(shim.stat().st_mode | stat.S_IEXEC)
    return bindir


async def test_a_git_timeout_sends_neither_git_repo_nor_the_configured_project(tmp_path, monkeypatch):
    alpha = _repo(tmp_path / "alpha")
    layout = Layout(tmp_path / "home")
    async with crew_server(tmp_path) as srv:
        rec = Recorder(srv.transport())
        d = new_crewd(layout, srv, config_loader=_loader(srv), transport=rec)
        monkeypatch.setattr(crewd_mod, "RESOLVE_GIT_S", 0.5)
        monkeypatch.setenv("PATH", f"{_slow_toplevel_git(tmp_path)}{os.pathsep}{os.environ['PATH']}")
        cfg = _loader(srv)("claude-code", None)
        project = await d.resolve_project(d.api_for_cfg(cfg, "claude-code"), cfg, str(alpha))
        body = rec.bodies("/api/v1/projects/resolve")[-1]
        assert _hints(body) == {"hint_scope": "folders"}, body  # unknown is not "a folder"
        assert project != CONFIGURED


# ---------------------------------------------------------------------------
# SessionStart's "is there a crew here?" asks the way the join will resolve
# ---------------------------------------------------------------------------


async def test_crew_exists_answers_for_the_project_the_join_will_use(tmp_path, monkeypatch):
    alpha = _repo(tmp_path / "alpha")
    beta = _repo(tmp_path / "beta")
    gamma = _repo(tmp_path / "gamma")
    delta = _repo(tmp_path / "delta")
    layout = Layout(tmp_path / "home")
    async with crew_server(tmp_path) as srv:
        rec = Recorder(srv.transport())
        d = new_crewd(layout, srv, alive={A_PID, B_PID, C_PID}, config_loader=_loader(srv), transport=rec)
        # the configured project has a crew (a session asked for it by name) ...
        named = await d.join(peer(A_PID), _join_args("sess-a", alpha, A_PID, project_id=CONFIGURED))
        assert named["ok"] and named["project_id"] == CONFIGURED
        # ... which a new repository does not join: no crew for it yet
        ask = {"cwd": str(beta), "adapter": "claude-code", "relay_project": None}
        assert (await d.crew_exists(ask))["exists"] is False
        assert _hints(rec.bodies("/api/v1/crews/resolve")[-1]) == {
            "hint_project": CONFIGURED,
            "hint_scope": "folders",
            "git_repo": True,
        }
        # with one namespace, a new repository finds that namespace's crew, and the join lands in it
        team = await d.join(peer(B_PID), _join_args("sess-b", gamma, B_PID, relay_project="team"))
        assert team["ok"] and team["project_id"] == "team"
        found = await d.crew_exists({"cwd": str(delta), "adapter": "claude-code", "relay_project": "team"})
        assert found["exists"] is True and found["project_id"] == "team" and found["crew_id"] == team["crew_id"], found
        joined = await d.join(peer(C_PID), _join_args("sess-c", delta, C_PID, relay_project="team"))
        assert joined["crew_id"] == team["crew_id"]


async def test_crew_exists_still_answers_a_server_without_the_hint_fields(tmp_path):
    alpha = _repo(tmp_path / "alpha")
    layout = Layout(tmp_path / "home")
    async with crew_server(tmp_path) as srv:
        rec = Recorder(srv.transport(), refuse_hint_fields=True)
        d = new_crewd(layout, srv, alive={A_PID}, config_loader=_loader(srv, project=None), transport=rec)
        a = await d.join(peer(A_PID), _join_args("sess-a", alpha, A_PID))
        assert a["ok"] and a["project_id"] == "alpha"
        found = await d.crew_exists({"cwd": str(alpha), "adapter": "claude-code"})
        assert found["exists"] is True and found["crew_id"] == a["crew_id"], found
        first, second = rec.bodies("/api/v1/crews/resolve")
        assert _hints(first) == {"hint_scope": "folders", "git_repo": True}
        assert _hints(second) == {}  # asked again the way crewd asked before


# ---------------------------------------------------------------------------
# remembra-crew start forwards the hook's REMEMBRA_RELAY_PROJECT
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("relay_project", ["team", None])
def test_session_start_forwards_the_hooks_relay_project(tmp_path, monkeypatch, capsys, relay_project):
    repo = _repo(tmp_path / "plain")
    layout = Layout(tmp_path / "home")
    if relay_project:
        monkeypatch.setenv("REMEMBRA_RELAY_PROJECT", relay_project)
    asked: list[tuple[str, dict[str, Any]]] = []

    def fake_rpc_ex(_layout: Layout, op: str, args: dict[str, Any] | None = None, **_: Any) -> tuple[dict[str, Any], bool]:
        asked.append((op, dict(args or {})))
        return {"ok": True, "exists": True}, True

    def fake_rpc(_layout: Layout, op: str, args: dict[str, Any] | None = None, **_: Any) -> dict[str, Any]:
        asked.append((op, dict(args or {})))
        return {"ok": False, "error": "stop_here"}

    monkeypatch.setattr(crew_cli, "rpc_ex", fake_rpc_ex)
    monkeypatch.setattr(crew_cli, "rpc", fake_rpc)
    monkeypatch.setattr(crew_cli, "ensure_crewd", lambda *a, **k: True)
    monkeypatch.setattr(
        crew_cli, "read_stdin", lambda *a, **k: json.dumps({"session_id": "s-1", "cwd": str(repo), "source": "startup"})
    )
    assert crew_cli.main(["start", "--hook", "claude-code", "--agent-pid", str(os.getpid())], layout=layout) == 0
    assert [op for op, _ in asked] == ["crew_exists", "join"]
    assert all("relay_project" in args and args["relay_project"] == relay_project for _, args in asked), asked
    assert "stop_here" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# the shared rule itself
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("project", "git_repo", "relay_project", "expected"),
    [
        ("clawdbot", True, None, {"git_repo": True, "hint_scope": "folders", "hint_project": "clawdbot"}),
        ("clawdbot", False, None, {"git_repo": False, "hint_scope": "folders", "hint_project": "clawdbot"}),
        ("clawdbot", None, None, {"hint_scope": "folders"}),  # git timed out: no configured project
        ("default", True, None, {"git_repo": True, "hint_scope": "folders"}),  # `default` does not count
        (None, False, None, {"git_repo": False, "hint_scope": "folders"}),
        ("claw bot", True, None, {"git_repo": True, "hint_scope": "folders", "hint_project": "clawdbot"}),  # alias
        ("clawdbot", True, "team", {"git_repo": True, "hint_scope": "all", "hint_project": "team"}),
        ("clawdbot", None, "team", {"hint_scope": "all", "hint_project": "team"}),  # one namespace: git not needed
        ("clawdbot", True, "  ", {"git_repo": True, "hint_scope": "folders", "hint_project": "clawdbot"}),
    ],
)
def test_hint_fields(project, git_repo, relay_project, expected):
    config = RelayConfig(
        url="http://x", api_key=None, agent_id=None, source="test", project=project, project_aliases="claw-bot=clawdbot"
    )
    assert hints.hint_fields(config, git_repo, relay_project) == expected


def test_only_a_refused_hint_field_is_asked_again():
    refused = {"detail": [{"loc": ["body", "hint_scope"], "type": "extra_forbidden", "msg": "Extra inputs are not permitted"}]}
    assert crewd_mod.refused_fields(crewd_mod.Resp(422, refused), hints.HINT_KEYS)
    other = {"detail": [{"loc": ["body", "root_path"], "type": "string_too_long", "msg": "too long"}]}
    assert not crewd_mod.refused_fields(crewd_mod.Resp(422, other), hints.HINT_KEYS)
    assert not crewd_mod.refused_fields(crewd_mod.Resp(422, {"detail": {"error": "validation_error"}}), hints.HINT_KEYS)
    assert not crewd_mod.refused_fields(crewd_mod.Resp(404, refused), hints.HINT_KEYS)
