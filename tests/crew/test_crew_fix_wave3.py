"""Fix wave 3 (review findings on the local runtime): every case runs the real code path.

* a missing ``crew-gate.py`` never blocks an agent (the hook commands exit 0, crewd restores it,
  uninstall removes hooks before the gate);
* a baton adopted into a fenced advisory worktree is restored (fence lifted first), a failed
  restore rolls the checkout back, and the CLI never resends a non-idempotent op after a lost reply;
* a hook payload over the read cap is never silently skipped;
* a symlinked ``~/.claude/settings.json`` (dotfiles) and a symlinked git hook stay symlinks;
* the fence writes its restore record before the first chmod, and is lifted when a checkout
  becomes shared;
* SessionStart in an unrelated repo stays inside one deadline when crewd or the server hangs.

Temp HOME, temp repos and in-process or subprocess runs only; nothing touches the real config.
"""

from __future__ import annotations

import io
import json
import os
import shutil
import socket
import stat
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest

from remembra.crew import gatecore
from remembra.relay.adapters.crew_hooks import CrewCommands, crew_specs
from remembra.relay.config import RelayConfig
from remembra.relay.crew import baton as B
from remembra.relay.crew import cli
from remembra.relay.crew import fence as F
from remembra.relay.crew import gate as gate_mod
from remembra.relay.crew import githooks, install
from remembra.relay.crew import outbox as O
from remembra.relay.crew.crewd import Crewd, Peer
from remembra.relay.crew.gate import Layout, read_json, vendor_gate, verify_gate
from tests.crew.test_wp10_install import _base_args, _run, _seed_home, _tty
from tests.crew.wp9_support import add_worktree, crew_server, git, make_repo, new_crewd, peer
from tests.crew.wp10_support import STUB_GATE, make_crew_env

PY = sys.executable
A_PID, C_PID = 42001, 42003


def _sh(command: str, stdin: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    """Run a hook command the way Claude Code does (``sh -c``)."""
    return subprocess.run(["/bin/sh", "-c", command], input=stdin, capture_output=True, text=True, timeout=30, env=env)


def _claude_hook_commands(settings: Path) -> dict[str, str]:
    data = json.loads(settings.read_text())
    out = {}
    for event, groups in data["hooks"].items():
        for group in groups:
            for h in group["hooks"]:
                if "# remembra-crew" in h["command"] and "crew-gate.py" in h["command"]:
                    out.setdefault(event, h["command"])
    return out


# ---------------------------------------------------------------------------
# 1. A missing crew-gate.py never blocks an agent
# ---------------------------------------------------------------------------


def test_python_exits_2_on_a_missing_script_which_is_why_the_hooks_guard_it(tmp_path):
    res = subprocess.run([PY, "-I", str(tmp_path / "nope" / "crew-gate.py"), "pretool"], capture_output=True, timeout=30)
    assert res.returncode == 2  # a *blocking* error for a Claude Code hook


@pytest.mark.parametrize("verb", ["pretool", "turn", "stop", "posttool", "precompact", "rewake"])
def test_gate_hook_command_exits_0_when_the_gate_is_missing_and_runs_it_when_present(tmp_path, monkeypatch, verb):
    log = tmp_path / "stub.log"
    monkeypatch.setenv("WP10_STUB_LOG", str(log))
    payload = json.dumps({"session_id": "s1", "tool_name": "Write", "tool_input": {"file_path": "/x/held/a.ts"}})
    spec = crew_specs()["claude-code"]
    hook = next(h for h in spec.hooks if h.verb == verb)
    missing = CrewCommands(python=PY, gate=str(tmp_path / "gone" / "crew-gate.py"), crew="/nonexistent/remembra-crew")
    res = _sh(missing.command("claude-code", hook), payload)
    assert (res.returncode, res.stdout) == (0, ""), res.stderr
    assert not log.exists()
    present = CrewCommands(python=PY, gate=str(STUB_GATE), crew="/nonexistent/remembra-crew")
    res = _sh(present.command("claude-code", hook), payload)
    assert res.returncode == 0, res.stderr
    assert [json.loads(line)["verb"] for line in log.read_text().splitlines()] == [verb]
    if verb == "pretool":  # the stub denies held/ paths: the gate's stdout still reaches the agent
        assert json.loads(res.stdout)["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_tamper_detection_still_finds_the_guarded_commands(tmp_path):
    spec = crew_specs()["claude-code"]
    cmds = CrewCommands(python=PY, gate=str(tmp_path / "g" / "crew-gate.py"), crew="/opt/remembra-crew")
    from remembra.relay.adapters.crew_hooks import render

    text, _ = render(None, spec, cmds)
    entries = gatecore.crew_hook_entries(text)
    assert len(entries) == len(spec.hooks)
    assert any(e[0] == "PreToolUse" and "test ! -f" in e[2] for e in entries)
    assert gatecore.marker_entries(text) == {e[2] for e in entries}


def test_deleting_remembra_home_leaves_every_installed_agent_hook_harmless(tmp_path, monkeypatch):
    env = make_crew_env(tmp_path, monkeypatch)
    home = env["home"]
    settings = _seed_home(home)
    stdin, master = _tty()
    try:
        code, out = _run(env, _base_args(env, "--apply", "--yes", "--no-service", "--gate-source", str(STUB_GATE)), stdin=stdin)
    finally:
        stdin.close()
        os.close(master)
    assert code == 0, out
    commands = _claude_hook_commands(settings)
    assert set(commands) >= {"PreToolUse", "UserPromptSubmit", "Stop", "PostToolUse", "PreCompact"}
    shutil.rmtree(home / ".remembra")  # "reset" by hand
    payload = json.dumps({"session_id": "s1", "tool_name": "Bash", "tool_input": {"command": "echo hi"}})
    for event, command in commands.items():
        res = _sh(command, payload, env={**os.environ, "HOME": str(home)})
        assert (res.returncode, res.stdout) == (0, ""), (event, res.returncode, res.stderr)


def test_crewd_restores_a_deleted_gate_while_the_manifest_has_agent_hooks(tmp_path):
    layout = Layout(tmp_path / "home")
    vendor_gate(layout)
    manifest = layout.root / "install.json"
    manifest.write_text(json.dumps({"version": 1, "agents": {"claude-code": [str(tmp_path / "settings.json")]}}))
    layout.gate_script.unlink()
    d = Crewd(layout, restore_gate=True)
    d.load_state()
    res = d.check_gate()
    assert res["restored"] is True and res["installed"] is False
    assert verify_gate(layout).ok
    assert d.events_log[-1]["type"] == "gate.tampered"
    # after a full uninstall (no agents recorded) a missing gate stays missing
    manifest.write_text(json.dumps({"version": 1, "agents": {}}))
    layout.gate_script.unlink()
    res = d.check_gate()
    assert res["restored"] is False and not layout.gate_script.exists()


def _install_with_git_hooks(env: dict[str, Any], repo: Path) -> None:
    stdin, master = _tty()
    try:
        code, out = _run(env, _base_args(env, "--apply", "--yes", "--git-hooks", "--repo", str(repo)), stdin=stdin)
    finally:
        stdin.close()
        os.close(master)
    assert code == 0, out


def test_uninstall_removes_the_hooks_before_the_gate_and_a_failed_run_leaves_no_hook_on_a_missing_gate(tmp_path, monkeypatch):
    env = make_crew_env(tmp_path, monkeypatch)
    home = env["home"]
    settings = _seed_home(home)
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    _install_with_git_hooks(env, repo)
    gate = install.gate_path(home)
    assert gate.exists()
    plan = install.build_plan(install.ConnectOptions(home=home, uninstall=True, platform="darwin"))
    paths = [op.path for op in plan.ops]
    assert paths.index(settings) < paths.index(gate)
    assert paths.index(repo / ".git" / "hooks" / "pre-commit") < paths.index(gate)
    assert paths.index(install.manifest_path(home)) < paths.index(gate)
    # the gate cannot be removed (read-only bin/): the run stops, but only after the hooks are gone
    gate.parent.chmod(0o500)
    try:
        stdin, master = _tty("yes\n")
        try:
            code, out = _run(env, ["--uninstall", "--apply"], stdin=stdin)
        finally:
            stdin.close()
            os.close(master)
        assert code == 1 and "Stopped:" in out, out
        assert gate.exists()
        assert "# remembra-crew" not in settings.read_text()
        assert not (repo / ".git" / "hooks" / "pre-commit").exists()
        assert install.read_manifest(home)["agents"] == {}  # crewd no longer restores the gate
    finally:
        gate.parent.chmod(0o700)
    stdin, master = _tty("yes\n")
    try:
        code, out = _run(env, ["--uninstall", "--apply"], stdin=stdin)
    finally:
        stdin.close()
        os.close(master)
    assert code == 0, out
    assert not gate.exists()


# ---------------------------------------------------------------------------
# 4. Symlinked config files stay symlinks
# ---------------------------------------------------------------------------


def test_a_symlinked_claude_settings_is_written_through_and_stays_a_link(tmp_path, monkeypatch):
    env = make_crew_env(tmp_path, monkeypatch)
    home = env["home"]
    dotfiles = tmp_path / "dotfiles"
    dotfiles.mkdir()
    target = dotfiles / "claude-settings.json"
    target.write_text(
        json.dumps({"model": "opus", "hooks": {"Notification": [{"hooks": [{"type": "command", "command": "say"}]}]}})
    )
    link = home / ".claude" / "settings.json"
    link.parent.mkdir()
    link.symlink_to(target)
    code, out = _run(env, _base_args(env, "--no-service"))
    assert code == 0 and "is a symlink: the change is written to its target" in out, out
    stdin, master = _tty()
    try:
        code, out = _run(env, _base_args(env, "--apply", "--yes", "--no-service"), stdin=stdin)
    finally:
        stdin.close()
        os.close(master)
    assert code == 0, out
    assert link.is_symlink() and os.readlink(link) == str(target)
    data = json.loads(target.read_text())
    assert data["model"] == "opus" and "# remembra-crew" in json.dumps(data["hooks"]["PreToolUse"])
    assert list(link.parent.glob("settings.json.bak-crew-*")), "the pre-install content is backed up"
    stdin, master = _tty("yes\n")
    try:
        code, out = _run(env, ["--uninstall", "--apply"], stdin=stdin)
    finally:
        stdin.close()
        os.close(master)
    assert code == 0, out
    assert link.is_symlink() and os.readlink(link) == str(target)
    assert "# remembra-crew" not in target.read_text() and json.loads(target.read_text())["model"] == "opus"


def test_a_symlinked_git_hook_is_chained_as_a_symlink_and_restored_as_one(tmp_path, monkeypatch):
    make_crew_env(tmp_path, monkeypatch)
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    shared = tmp_path / "shared-hooks" / "pre-commit"
    shared.parent.mkdir()
    shared.write_text("#!/bin/sh\necho shared-hook-ran\n")
    shared.chmod(0o755)
    active = repo / ".git" / "hooks" / "pre-commit"
    active.symlink_to(shared)
    plan = githooks.plan_install(repo, githooks.GateCommand(PY, str(STUB_GATE)), hooks=("pre-commit",))
    for op in plan.ops:
        githooks.apply_op(op)
    prev = active.with_name("pre-commit" + githooks.PREV_SUFFIX)
    assert prev.is_symlink() and os.readlink(prev) == str(shared)
    assert not active.is_symlink() and githooks.is_crew_script(active.read_text())
    assert shared.read_text() == "#!/bin/sh\necho shared-hook-ran\n", "the shared hook itself is never rewritten"
    for op in githooks.plan_uninstall(repo).ops:
        githooks.apply_op(op)
    assert active.is_symlink() and os.readlink(active) == str(shared)
    assert not prev.exists() and not prev.is_symlink()


def test_apply_op_refuses_a_symlink_into_a_missing_directory(tmp_path):
    link = tmp_path / "settings.json"
    link.symlink_to(tmp_path / "nowhere" / "x.json")
    with pytest.raises(OSError, match="symlink"):
        githooks.apply_op(githooks.FileOp(link, None, "{}\n", 0o600, []))
    assert link.is_symlink()


# ---------------------------------------------------------------------------
# 5, 6. The fence: write-ahead record, lifted when the checkout is shared
# ---------------------------------------------------------------------------

POS_ZONE = {"id": "zn_pos", "slug": "pos", "include_globs": ["src/app/pos/**"], "exclude_globs": []}


def _writable(path: Path) -> bool:
    return bool(stat.S_IMODE(path.stat().st_mode) & stat.S_IWUSR)


def test_fence_record_is_on_disk_before_the_first_chmod_so_a_crash_is_undone_at_start(tmp_path, monkeypatch):
    repo = make_repo(tmp_path / "repo")
    for i in range(6):
        (repo / f"src/app/pos/f{i}.ts").write_text("x\n")
    fence_dir = tmp_path / "fence"
    real_chmod = os.chmod
    calls = {"n": 0}

    class Killed(BaseException):
        pass

    def dying_chmod(path, mode, *a, **kw):
        calls["n"] += 1
        if calls["n"] == 4:
            # the record must already hold every path this run is about to fence
            record = json.loads(next(fence_dir.glob("*.json")).read_text())
            assert {"src/app/pos/f0.ts", "src/app/pos/split.ts", "src/app/pos"} <= set(record["entries"])
            raise Killed()
        return real_chmod(path, mode, *a, **kw)

    monkeypatch.setattr(os, "chmod", dying_chmod)
    with pytest.raises(Killed):
        F.Fence(fence_dir).apply(str(repo), [POS_ZONE], case_insensitive=False)
    monkeypatch.setattr(os, "chmod", real_chmod)
    assert not all(_writable(p) for p in (repo / "src/app/pos").glob("*.ts"))
    swept = F.Fence(fence_dir).sweep()  # crewd start after the crash
    assert swept.errors == []
    assert all(_writable(p) for p in (repo / "src/app/pos").glob("*.ts"))
    assert _writable(repo / "src/app/pos") and not list(fence_dir.glob("*.json"))


def _fence_session(key: str, sid: str, top: Path, enforcement: str) -> dict[str, Any]:
    return {
        "key": key,
        "session_id": sid,
        "crew_id": "crw_1",
        "toplevel": str(top),
        "enforcement": enforcement,
        "callsign": key,
        "case_insensitive": False,
    }


def test_fence_is_lifted_when_another_session_joins_the_advisory_checkout(tmp_path):
    repo = make_repo(tmp_path / "repo")
    layout = Layout(tmp_path / "home")
    d = Crewd(layout, restore_gate=False)
    d.load_state()
    snap = {
        "settings": {},
        "zones": [POS_ZONE],
        "claims": [{"id": "c1", "zone_id": "zn_pos", "holder_session_id": "cs_holder", "state": "active", "mode": "exclusive"}],
    }
    d.sessions["codex"] = _fence_session("codex", "cs_codex", repo, "advisory")
    d.apply_fence("crw_1", snap)
    assert not _writable(repo / "src/app/pos/split.ts")
    d.sessions["cc2"] = _fence_session("cc2", "cs_cc2", repo, "enforce")  # Claude Code starts in the same worktree
    d.apply_fence("crw_1", snap)
    assert _writable(repo / "src/app/pos/split.ts") and _writable(repo / "src/app/pos")
    assert d.fence.fenced(str(repo)) == {}
    d.apply_fence("crw_1", snap)  # every later sync keeps it lifted
    assert _writable(repo / "src/app/pos/split.ts")


# ---------------------------------------------------------------------------
# 2. Adopt into a fenced worktree; transactional restore; no blind resend
# ---------------------------------------------------------------------------


def _join(sid: str, cwd: Path, pid: int, adapter: str) -> dict[str, Any]:
    return {
        "adapter": adapter,
        "agent_id": adapter,
        "client_session_id": sid,
        "cwd": str(cwd),
        "agent_pid": pid,
        "source": "startup",
    }


async def test_adopt_into_a_fenced_advisory_worktree_restores_the_work(tmp_path):
    repo = make_repo(tmp_path / "repo")
    wt_c = add_worktree(repo, tmp_path / "wt-c", "c-work")
    layout = Layout(tmp_path / "home")
    async with crew_server(tmp_path) as srv:
        d = new_crewd(layout, srv, alive={A_PID, C_PID})
        a = await d.join(peer(A_PID), _join("sess-a", repo, A_PID, "claude-code"))
        crew_id = a["crew_id"]
        pos = next(z for z in d.snapshots[crew_id]["zones"] if z["slug"] == "pos")
        tok_a = d.tokens[a["key"]]
        api = d.api_for(d.sessions[a["key"]])
        created = await api.call(
            "POST",
            f"/crews/{crew_id}/tasks",
            session_token=tok_a,
            json_body={"title": "POS split tender", "zone_ids": [pos["id"]], "acceptance": [], "depends_on": []},
        )
        task = created.body["task"]
        started = await api.call(
            "POST", f"/tasks/{task['id']}/start", session_token=tok_a, json_body={"head": git(repo, "rev-parse", "HEAD")}
        )
        assert started.ok, started.body
        (repo / "src/app/pos/tender.ts").write_text("export const tender = 'wip';\n")
        (repo / "src/app/pos/cart.ts").write_text("export const cart = 'new';\n")
        stalled = await d.stall(peer(A_PID), {"key": a["key"], "error": "billing_error"})
        assert stalled["baton_ref"]
        # Codex (advisory) in its own worktree: A's reserved zone is fenced read-only there
        c = await d.join(peer(C_PID), _join("sess-c", wt_c, C_PID, "codex"))
        assert d.sessions[c["key"]]["enforcement"] == "advisory"
        await d.sync_snapshot(crew_id)
        assert not _writable(wt_c / "src/app/pos/tender.ts") and not _writable(wt_c / "src/app/pos")
        res = await d.adopt(peer(C_PID), {"task": "T-1"})
        assert res["ok"] and res["restore"]["restored"] is True, res
        assert (wt_c / "src/app/pos/tender.ts").read_text() == "export const tender = 'wip';\n"
        assert (wt_c / "src/app/pos/cart.ts").read_text() == "export const cart = 'new';\n"
        assert sorted(git(wt_c, "status", "--porcelain").splitlines()) == ["?? src/app/pos/cart.ts", "M src/app/pos/tender.ts"]
        await d.sync_snapshot(crew_id)  # C holds pos now: nothing of it is fenced again
        assert _writable(wt_c / "src/app/pos/tender.ts")


def test_a_restore_that_fails_part_way_puts_the_checkout_back(tmp_path):
    repo = make_repo(tmp_path / "repo")
    wt_c = add_worktree(repo, tmp_path / "wt-c", "c-work")
    (wt_c / "c.txt").write_text("c's commit\n")
    git(wt_c, "add", "c.txt")
    git(wt_c, "commit", "-q", "-m", "c")  # HEAD differs from the baton's parent: restore switches branch
    c_head = git(wt_c, "rev-parse", "HEAD")
    (repo / "README.md").write_text("changed by the baton\n")
    (repo / "src/app/pos/tender.ts").write_text("saved\n")
    (repo / "src/app/pos/new.ts").write_text("brand new\n")
    baton = B.create_baton_ref(repo, "T-3")
    assert baton is not None
    (wt_c / "src/app/pos").chmod(0o555)  # e.g. a fence nobody lifted
    try:
        result = B.restore_baton(wt_c, baton.ref, retry_command="remembra-crew adopt T-3 --restore-only")
    finally:
        (wt_c / "src/app/pos").chmod(0o755)
    assert result.restored is False and result.reason == "restore_failed", result
    assert result.rolled_back is True and result.error
    assert result.next_command == "remembra-crew adopt T-3 --restore-only"
    assert git(wt_c, "status", "--porcelain") == ""
    assert git(wt_c, "symbolic-ref", "--short", "HEAD") == "c-work" and git(wt_c, "rev-parse", "HEAD") == c_head
    assert (wt_c / "README.md").read_text() == "hi\n" and not (wt_c / "src/app/pos/new.ts").exists()
    assert git(wt_c, "branch", "--list", "remembra/*") == ""
    # once the cause is gone the retry restores everything
    again = B.restore_baton(wt_c, baton.ref)
    assert again.restored is True and (wt_c / "src/app/pos/tender.ts").read_text() == "saved\n"


def test_restore_handles_more_paths_than_one_command_line_holds(tmp_path, monkeypatch):
    repo = make_repo(tmp_path / "repo")
    wt_c = add_worktree(repo, tmp_path / "wt-c", "c-work")
    many = repo / "gen"
    many.mkdir()
    for i in range(30):
        (many / f"f{i:03}.ts").write_text(f"{i}\n")
    baton = B.create_baton_ref(repo, "T-9")
    assert baton is not None
    monkeypatch.setattr(B, "PATHSPEC_CHUNK", 7)  # several chunks
    result = B.restore_baton(wt_c, baton.ref)
    assert result.restored and len(result.files) == 30
    assert all((wt_c / "gen" / f"f{i:03}.ts").read_text() == f"{i}\n" for i in range(30))


async def test_crewd_answers_an_unexpected_op_failure_instead_of_dropping_the_reply(tmp_path):
    d = Crewd(Layout(tmp_path / "home"), restore_gate=False)
    d.load_state()

    async def boom(peer_: Peer, args: Any) -> dict[str, Any]:
        raise B.GitError("git checkout failed: unable to unlink old 'src/pos/cart.ts': Permission denied")

    async def crash(peer_: Peer, args: Any) -> dict[str, Any]:
        raise RuntimeError("bug")

    d.adopt = boom  # type: ignore[method-assign]
    res = await d.handle("adopt", {}, Peer(None, None))
    assert res["ok"] is False and res["error"] == "git_failed" and "Permission denied" in res["message"]
    d.adopt = crash  # type: ignore[method-assign]
    res = await d.handle("adopt", {}, Peer(None, None))
    assert res == {"ok": False, "error": "internal_error", "message": "crewd adopt failed (RuntimeError)"}


HANG = object()


class FakeCrewd:
    """A unix-socket stand-in for crewd: ``answer(op, args)`` returns a reply, ``None`` (close) or HANG."""

    def __init__(self, layout: Layout, answer: Callable[[str, dict[str, Any]], Any]) -> None:
        self.path = layout.socket
        self.path.parent.mkdir(parents=True, exist_ok=True)
        os.chmod(self.path.parent, 0o700)
        if self.path.exists():
            self.path.unlink()
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.bind(str(self.path))
        self.sock.listen(16)
        self.answer = answer
        self.ops: list[str] = []
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self._loop, daemon=True)
        self.thread.start()

    def _loop(self) -> None:
        self.sock.settimeout(0.1)
        while not self.stop.is_set():
            try:
                conn, _ = self.sock.accept()
            except OSError:
                continue
            threading.Thread(target=self._serve, args=(conn,), daemon=True).start()

    def _serve(self, conn: socket.socket) -> None:
        with conn:
            buf = b""
            while b"\n" not in buf:
                chunk = conn.recv(65536)
                if not chunk:
                    return
                buf += chunk
            req = json.loads(buf.split(b"\n", 1)[0])
            self.ops.append(req["op"])
            reply = self.answer(req["op"], req.get("args") or {})
            if reply is HANG:
                self.stop.wait(30)
                return
            if reply is not None:
                conn.sendall(json.dumps(reply).encode() + b"\n")

    def close(self) -> None:
        self.stop.set()
        self.sock.close()
        self.thread.join(2)
        if self.path.exists():
            self.path.unlink()


def test_cli_never_resends_a_non_idempotent_op_after_a_lost_reply(tmp_path):
    layout = Layout(tmp_path / "home")
    fake = FakeCrewd(layout, lambda op, args: {"ok": True} if op == "ping" else None)
    try:
        res = cli._call(layout, "adopt", {"task": "T-1"}, timeout=2.0)
        assert res["ok"] is False and res["error"] == "no_reply", res
        assert fake.ops.count("adopt") == 1
        res = cli._call(layout, "status", timeout=2.0)  # read-only: one retry after checking crewd is up
        assert fake.ops.count("status") == 2 and res["error"] == "crewd_unreachable"
    finally:
        fake.close()


def test_cli_adopt_explains_a_rolled_back_restore(tmp_path, monkeypatch, capsys):
    layout = Layout(tmp_path / "home")
    monkeypatch.setattr(cli, "_need_session", lambda _layout: {"key": "k1"})
    monkeypatch.setattr(
        cli,
        "_call",
        lambda *a, **k: {
            "ok": True,
            "task_ref": "T-1",
            "baton_ref": "refs/remembra/baton/T-1/1",
            "restore": {
                "restored": False,
                "reason": "restore_failed",
                "error": "git checkout failed: Permission denied",
                "rolled_back": True,
                "next_command": "remembra-crew adopt T-1 --restore-only",
            },
        },
    )
    assert cli.main(["adopt", "T-1"], layout=layout) == 0
    out = capsys.readouterr().out
    assert "Saved work NOT restored: git checkout failed: Permission denied." in out
    assert "put back as it was" in out and "remembra-crew adopt T-1 --restore-only" in out
    assert "Commit or move them" not in out


# ---------------------------------------------------------------------------
# 7. SessionStart deadline
# ---------------------------------------------------------------------------


def _start(layout: Layout, cwd: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[int, float, list[float]]:
    briefs: list[float] = []
    monkeypatch.setattr(
        cli, "read_stdin", lambda *a, **k: json.dumps({"session_id": "s-1", "cwd": str(cwd), "source": "startup"})
    )
    monkeypatch.setattr(cli, "relay_brief_passthrough", lambda adapter, agent, payload, *, timeout=0: briefs.append(timeout))
    t = time.monotonic()
    code = cli.main(["start", "--hook", "claude-code", "--agent-pid", str(os.getpid())], layout=layout)
    return code, time.monotonic() - t, briefs


def test_session_start_does_not_ask_twice_when_crewd_times_out(tmp_path, monkeypatch):
    repo = make_repo(tmp_path / "plain", zones=None)
    layout = Layout(tmp_path / "home")
    monkeypatch.setattr(cli, "START_DEADLINE_S", 3.0)
    fake = FakeCrewd(layout, lambda op, args: {"ok": True} if op == "ping" else HANG)
    try:
        code, elapsed, briefs = _start(layout, repo, monkeypatch)
    finally:
        fake.close()
    assert code == 0
    assert fake.ops == ["crew_exists"], fake.ops  # no ping + second crew_exists after the timeout
    assert elapsed < 2.5, elapsed  # crew_exists got at most half the budget
    assert len(briefs) == 1 and 1.0 <= briefs[0] <= 1.6, briefs  # the plain brief keeps the rest


def test_session_start_asks_again_only_after_starting_crewd(tmp_path, monkeypatch):
    repo = make_repo(tmp_path / "plain", zones=None)
    layout = Layout(tmp_path / "home")
    started: list[FakeCrewd] = []

    def fake_ensure(_layout: Layout, *, wait: float = 0) -> bool:
        started.append(FakeCrewd(layout, lambda op, args: {"ok": True, "exists": False}))
        return True

    monkeypatch.setattr(cli, "ensure_crewd", fake_ensure)
    try:
        code, elapsed, briefs = _start(layout, repo, monkeypatch)
    finally:
        for f in started:
            f.close()
    assert code == 0 and len(started) == 1 and started[0].ops == ["crew_exists"]
    assert len(briefs) == 1 and briefs[0] > 5.0 and elapsed < cli.START_DEADLINE_S


async def test_crewd_caches_no_crew_and_unreachable_answers(tmp_path):
    repo = make_repo(tmp_path / "plain", zones=None)
    now = [1000.0]
    seen: list[str] = []
    mode = {"answer": "404"}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        if mode["answer"] == "down":
            raise httpx.ConnectError("black hole", request=request)
        return httpx.Response(404, json={"detail": {"error": "not_found"}})

    d = Crewd(
        Layout(tmp_path / "home"),
        config_loader=lambda agent, prefer: RelayConfig(url="http://test", api_key="k", agent_id=agent, source="test"),
        transport=httpx.MockTransport(handler),
        clock=lambda: now[0],
        restore_gate=False,
    )
    d.load_state()
    args = {"cwd": str(repo), "adapter": "claude-code"}
    assert (await d.crew_exists(args))["exists"] is False
    assert (await d.crew_exists(args))["exists"] is False
    assert len(seen) == 1  # the second SessionStart in this repo does not wait on the network
    now[0] += 301
    await d.crew_exists(args)
    assert len(seen) == 2
    d.crew_exists_cache.clear()
    mode["answer"] = "down"
    other = make_repo(tmp_path / "other", zones=None)
    assert (await d.crew_exists(args))["error"] == "unreachable"
    assert (await d.crew_exists({"cwd": str(other), "adapter": "claude-code"}))["error"] == "unreachable"
    assert len(seen) == 3  # an unreachable server is not asked again for a minute, from any repo
    now[0] += 61
    await d.crew_exists(args)
    assert len(seen) == 4


# ---------------------------------------------------------------------------
# 3. Hook payloads over the read cap are never silently skipped
# ---------------------------------------------------------------------------


def test_read_stdin_ex_reports_what_it_left_unread(monkeypatch):
    r, w = os.pipe()
    os.write(w, b"x" * 100)
    os.close(w)
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(os.fdopen(r, "rb")))
    assert gate_mod.read_stdin_ex(limit=50) == ("x" * 50, True)
    r, w = os.pipe()
    os.write(w, b"y" * 100)
    os.close(w)
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(os.fdopen(r, "rb")))
    assert gate_mod.read_stdin_ex(limit=100) == ("y" * 100, False)


def test_salvage_reads_structure_never_values():
    content = 'x","session_id":"evil","file_path":"/etc/passwd"}' * 50
    raw = json.dumps(
        {"session_id": "s-1", "tool_name": "Write", "tool_input": {"file_path": "/r/.remembra/zones.yml", "content": content}}
    )
    got = gate_mod.salvage_payload(raw[: len(raw) // 2])
    assert got == {"session_id": "s-1", "tool_name": "Write", "tool_input": {"file_path": "/r/.remembra/zones.yml"}}
    late = json.dumps({"session_id": "s-1", "tool_name": "Write", "tool_input": {"content": content, "file_path": "/r/a.ts"}})
    assert "tool_input" not in gate_mod.salvage_payload(late[:-20])


@pytest.fixture()
def sleeper():
    proc = subprocess.Popen(["sleep", "600"])
    yield proc.pid
    proc.kill()
    proc.wait()


def _run_gate(layout: Layout, payload_text: str, cwd: Path) -> subprocess.CompletedProcess[str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith(("REMEMBRA_", "PYTHON"))}
    env["HOME"] = str(layout.home)
    return subprocess.run(
        [PY, "-I", str(layout.gate_script), "pretool", "--hook", "claude-code"],
        input=payload_text,
        capture_output=True,
        text=True,
        cwd=str(cwd),
        env=env,
        timeout=120,
    )


def _pretool(sid: str, cwd: Path, tool: str, tool_input: dict[str, Any]) -> str:
    return json.dumps(
        {
            "session_id": sid,
            "cwd": str(cwd),
            "hook_event_name": "PreToolUse",
            "tool_name": tool,
            "tool_input": tool_input,
            "permission_mode": "default",
        }
    )


def _decision(res: subprocess.CompletedProcess[str]) -> tuple[str | None, str]:
    assert res.returncode == 0, res.stderr
    if not res.stdout:
        return None, ""
    out = json.loads(res.stdout)["hookSpecificOutput"]
    return out["permissionDecision"], out["permissionDecisionReason"]


async def test_oversized_payloads_still_get_the_crew_policy_and_zone_denies(tmp_path, sleeper):
    import asyncio

    repo = make_repo(tmp_path / "repo")
    wt_b = add_worktree(repo, tmp_path / "wt-b", "b")
    me = os.getpid()
    big = "x" * (gate_mod.STDIN_MAX_CHARS + 1024)
    async with crew_server(tmp_path) as srv:
        layout = Layout(tmp_path / "home")
        vendor_gate(layout)
        d = new_crewd(layout, srv, alive={sleeper})
        await d.start_server()
        a = await d.join(
            peer(sleeper), {"adapter": "claude-code", "client_session_id": "sess-a", "cwd": str(repo), "agent_pid": sleeper}
        )
        await d.join(peer(me), {"adapter": "claude-code", "client_session_id": "sess-b", "cwd": str(wt_b), "agent_pid": me})
        crew_id = a["crew_id"]
        pos = next(z for z in d.snapshots[crew_id]["zones"] if z["slug"] == "pos")
        assert (await d.claim(peer(sleeper), {"key": a["key"], "zone_id": pos["id"]}))["result"] == "granted"
        await d.sync_snapshot(crew_id)

        async def gate(tool: str, tool_input: dict[str, Any]) -> tuple[str | None, str]:
            text = _pretool("sess-b", wt_b, tool, tool_input)
            return _decision(await asyncio.to_thread(_run_gate, layout, text, wt_b))

        # 5 MB: over the old 4 MB cap, now read and parsed in full
        decision, reason = await gate("Write", {"file_path": str(wt_b / ".remembra/zones.yml"), "content": "y" * 5_000_000})
        assert decision == "deny", reason
        # over the cap: crew-policy (rule 2) and a zone held by another session still deny
        decision, reason = await gate("Write", {"file_path": str(wt_b / ".remembra/zones.yml"), "content": big})
        assert decision == "deny", reason
        decision, reason = await gate("Write", {"file_path": str(wt_b / "src/app/pos/split.ts"), "content": big})
        assert decision == "deny" and 'zone "pos"' in reason, reason
        # a shell command cut off mid-string cannot be checked: refused, with the reason
        decision, reason = await gate("Bash", {"command": f"echo {big}"})
        assert decision == "deny" and "too large for the crew gate" in reason
        # a free path in the caller's own worktree is still allowed
        decision, _ = await gate("Write", {"file_path": str(wt_b / "src/app/reports/export.ts"), "content": big})
        assert decision is None
        # the gate.error is spooled, then delivered by crewd's outbox flush (POST /crews/{id}/events)
        await d.drain()
        errors = [e.body["payload"] for e in O.read_entries(layout.outbox, ("event",)) if e.body["type"] == "gate.error"]
        rows = await srv.rows("SELECT payload FROM crew_events WHERE crew_id = ? AND type = 'gate.error'", (crew_id,))
        errors += [json.loads(r["payload"]) for r in rows]
        assert {"stage": "payload", "error_class": "payload_oversized"} in errors
        assert (wt_b / "src/app/pos/split.ts").read_text() == "export const split = 1;\n"
        assert read_json(layout.session_file(a["key"])) is not None
