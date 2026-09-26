"""crewd ``kill -9`` soak (spec §13.4 "crewd kill -9 every 10 min for 2 h"; §13.2 "crewd kill -9 with 3
live sessions: launchd restarts it; no session reaches lost, no task stalls"; WP-15).

Real processes, temp HOME: the crew-mode server, crewd kept alive by :class:`CrewdSupervisor` (what the
LaunchAgent's ``KeepAlive`` does), and three live sessions that keep working through every kill: A
(Claude Code) in POS, B (Claude Code) in reports, and X (Codex, advisory) in its own worktree, whose POS
files the read-only fence keeps read-only because A holds POS.

Asserted: every kill is followed by a new crewd; no session goes quiet, lost or quota-blocked and no
task stalls; the agents' own-zone writes are never denied; the fence holds in X's worktree through
every restart and, when X ends, every fenced file gets its original mode back.

CI scale: a kill every 20 s for 2 min (``REMEMBRA_SOAK_KILL_EVERY_S`` / ``REMEMBRA_SOAK_DURATION_S``
give the full 600 s / 7200 s). Runs with ``REMEMBRA_CREW_SLOW=1``.
"""

from __future__ import annotations

import os
import shutil
import stat
import time
from pathlib import Path

import pytest

from tests.crew.e2e.harness import CrewdSupervisor, World, pid_alive, wait_for

pytestmark = [
    pytest.mark.skipif(shutil.which("git") is None, reason="needs git"),
    pytest.mark.skipif(os.environ.get("REMEMBRA_CREW_SLOW") != "1", reason="soak: set REMEMBRA_CREW_SLOW=1"),
]

BAD = ("session.lost", "session.quota_blocked", "task.stalled", "claim.reserved", "report.submitted")


def modes(root: Path) -> dict[str, int]:
    return {str(p.relative_to(root)): stat.S_IMODE(p.stat().st_mode) for p in sorted((root / "src").rglob("*"))}


def test_crewd_kill_9_soak(tmp_path: Path) -> None:
    every = float(os.environ.get("REMEMBRA_SOAK_KILL_EVERY_S", "20"))
    duration = float(os.environ.get("REMEMBRA_SOAK_DURATION_S", "120"))
    world = World(tmp_path)
    try:
        wts = world.setup(("wt-a", "wt-b", "wt-x"), "--include-unverified", "--agent", "claude-code", "--agent", "codex")
        original = modes(wts["wt-x"])
        sup = CrewdSupervisor(world)
        world.closers.insert(0, sup.close)
        a = world.agent("A", wts["wt-a"])
        a.start()
        crew = str(a.local_session()["crew_id"])
        feed = world.feed(crew)
        with world.human() as h:
            pos = next(z["id"] for z in world.snapshot(crew)["zones"] if z["slug"] == "pos")
            r = h.post(
                f"/crews/{crew}/tasks",
                json={"title": "POS", "zone_ids": [pos], "acceptance": [], "depends_on": []},
                headers={"Idempotency-Key": "soak-t1"},
            )
            assert r.status_code == 201, r.text
        assert "T-1 in_progress" in a.bash("remembra-crew task start T-1")["response"]["stdout"]
        b = world.agent("B", wts["wt-b"])
        b.start()
        x = world.agent("X", wts["wt-x"], adapter="codex")
        x.start()
        assert not a.write("src/app/pos/split.ts", "export const split = 0;\n")["denied"]
        assert not b.write("src/app/reports/export.ts", "export const exportCsv = () => '0';\n")["denied"]
        fenced = wts["wt-x"] / "src/app/pos/split.ts"
        assert wait_for(lambda: not (fenced.stat().st_mode & 0o222), timeout=20), oct(fenced.stat().st_mode)

        kills: list[tuple[int, float]] = []
        denied: list[str] = []
        fence_open: list[float] = []
        started = time.monotonic()
        last_kill = started
        n = 0
        while time.monotonic() - started < duration:
            n += 1
            for agent, rel in ((a, "src/app/pos/split.ts"), (b, "src/app/reports/export.ts")):
                res = agent.write(rel, f"export const v = {n};\n")
                if res["denied"]:
                    denied.append(f"{agent.name}: {res['reason']}")
            if fenced.stat().st_mode & 0o222:
                fence_open.append(time.monotonic() - started)
            if time.monotonic() - last_kill >= every:
                pid = sup.kill9()
                assert pid is not None
                kills.append((pid, time.monotonic()))
                last_kill = time.monotonic()
                assert wait_for(lambda p=pid: world.crewd_pid() not in (None, p), timeout=30), "crewd was not restarted"
            time.sleep(2.0)

        assert len(kills) >= int(duration // every) - 1, kills
        assert all(not pid_alive(pid) for pid, _ in kills)
        # the next crewd came from the supervisor (waiting on the lock) or from a hook's respawn, whichever was first
        assert denied == [], denied  # own-zone writes never blocked by a restart
        assert fence_open == [], fence_open  # X's POS files stayed read-only through every restart
        assert a.cli("renew").returncode == 0
        time.sleep(2)
        snap = world.snapshot(crew)
        states = {s["callsign"]: s["state"] for s in snap["sessions"]}
        assert set(states.values()) <= {"active", "idle"}, states
        bad = [e["type"] for e in feed.events() if e["type"] in BAD]
        assert bad == [], bad
        quiet = [e for e in feed.events() if e["type"] == "session.state_changed" and e["payload"]["to"] in ("quiet", "lost")]
        assert quiet == [], quiet

        # X ends: the fence is lifted and every file has its original mode again
        x.end("logout")
        assert wait_for(lambda: modes(wts["wt-x"]) == original, timeout=30), (modes(wts["wt-x"]), original)
    finally:
        world.close()
