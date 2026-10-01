"""WP-9 crewd wiring against the real routers: the read-only fence for an advisory (Codex) session,
the transcript detector turning a limit message into ``quota_blocked``, zones.yml sync rules
(default-branch commit applied, loosening held for a human), re-join on compact/resume, the
git-hook check event, presence frames, redaction of outbound payloads and baton retention.
"""

from __future__ import annotations

import json
import os
import stat
import time

from remembra.crew import schemas as S
from remembra.relay.crew import baton as B
from remembra.relay.crew.gate import Layout, read_json
from tests.crew.wp9_support import add_worktree, client_events, crew_server, git, make_repo, new_crewd, peer

A_PID, X_PID = 51001, 51002


def _join(sid, cwd, pid, adapter="claude-code", source="startup", transcript=None):
    return {
        "adapter": adapter,
        "agent_id": adapter,
        "client_session_id": sid,
        "cwd": str(cwd),
        "agent_pid": pid,
        "source": source,
        "transcript_path": transcript,
    }


async def test_read_only_fence_for_an_advisory_session_in_its_own_worktree(tmp_path):
    repo = make_repo(tmp_path / "repo")
    wt_x = add_worktree(repo, tmp_path / "wt-codex", "codex-work")
    layout = Layout(tmp_path / "home")
    async with crew_server(tmp_path) as srv:
        d = new_crewd(layout, srv, alive={A_PID, X_PID})
        a = await d.join(peer(A_PID), _join("sess-a", repo, A_PID))
        x = await d.join(peer(X_PID), _join("sess-x", wt_x, X_PID, adapter="codex"))
        assert d.sessions[x["key"]]["enforcement"] == "advisory"
        pos = next(z for z in d.snapshots[a["crew_id"]]["zones"] if z["slug"] == "pos")
        assert (await d.claim(peer(A_PID), {"key": a["key"], "zone_id": pos["id"]}))["result"] == "granted"
        await d.sync_snapshot(a["crew_id"])
        target = wt_x / "src/app/pos/split.ts"
        assert stat.S_IMODE(target.stat().st_mode) & 0o222 == 0
        try:
            target.write_text("apply_patch\n")
            raise AssertionError("the fence did not block the write")
        except PermissionError:
            pass
        # the holder's own checkout is never fenced
        assert os.access(repo / "src/app/pos/split.ts", os.W_OK)
        assert d.doctor()["sessions"][1]["fenced"] >= 3
        # the holder releases: the next sync restores the modes
        claim = next(c for c in d.snapshots[a["crew_id"]]["claims"] if c["zone_id"] == pos["id"])
        rel = await d.api_for(d.sessions[a["key"]]).call(
            "POST", f"/claims/{claim['id']}/release", json_body={}, session_token=d.tokens[a["key"]]
        )
        assert rel.ok, rel.body
        await d.sync_snapshot(a["crew_id"])
        assert os.access(target, os.W_OK)
        target.write_text("now allowed\n")


async def test_transcript_detector_reports_quota_blocked_with_a_baton(tmp_path):
    repo = make_repo(tmp_path / "repo")
    layout = Layout(tmp_path / "home")
    transcript = tmp_path / "rollout.jsonl"
    now = [time.time()]
    async with crew_server(tmp_path) as srv:
        d = new_crewd(layout, srv, alive={X_PID}, clock=lambda: now[0])
        x = await d.join(peer(X_PID), _join("sess-x", repo, X_PID, adapter="codex", transcript=str(transcript)))
        (repo / "src/app/reports/export.ts").write_text("half\n")
        stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now[0]))
        transcript.write_text(
            json.dumps(
                {
                    "timestamp": stamp,
                    "type": "event_msg",
                    "payload": {"type": "error", "message": "You've hit your usage limit. Try again in 4 hours."},
                }
            )
            + "\n"
        )
        assert await d.detect() == []  # the process is alive and it has been quiet < 5 min
        now[0] += 301
        assert await d.detect() == [x["key"]]
        rows = await srv.rows("SELECT state, limit_source FROM crew_sessions WHERE id = ?", (x["session_id"],))
        assert rows[0]["state"] == "quota_blocked"
        assert any(r.startswith(f"refs/remembra/baton/{x['session_id']}/") for r in B.list_batons(repo))
        assert await d.detect() == []  # reported once


async def test_zones_sync_applies_default_branch_commits_and_holds_loosening(tmp_path):
    repo = make_repo(tmp_path / "repo")
    layout = Layout(tmp_path / "home")
    async with crew_server(tmp_path) as srv:
        d = new_crewd(layout, srv, alive={A_PID})
        a = await d.join(peer(A_PID), _join("sess-a", repo, A_PID))
        crew_id = a["crew_id"]
        # a tightening change committed on main is uploaded and applied
        text = (
            (repo / ".remembra/zones.yml")
            .read_text()
            .replace("  reports:\n", "  billing:\n    include: [src/app/billing/**]\n  reports:\n")
        )
        (repo / ".remembra/zones.yml").write_text(text)
        git(repo, "commit", "-qam", "add billing zone")
        await d.check_zones()
        await d.sync_snapshot(crew_id)
        assert "billing" in {z["slug"] for z in d.snapshots[crew_id]["zones"]}
        # deleting the pos zone on main is loosening: pending until a human approves; pos stays enforced
        text = (repo / ".remembra/zones.yml").read_text()
        cut = text.index("  pos:")
        end = text.index("  billing:")
        (repo / ".remembra/zones.yml").write_text(text[:cut] + text[end:])
        git(repo, "commit", "-qam", "drop pos")
        await d.check_zones()
        await d.sync_snapshot(crew_id)
        assert "pos" in {z["slug"] for z in d.snapshots[crew_id]["zones"]}
        assert d.snapshots[crew_id]["pending_zone_changes"]
        # an uncommitted working-copy edit is never uploaded
        before = d.zones_uploaded[crew_id]
        (repo / ".remembra/zones.yml").write_text("version: 1\nzones: {}\n")
        await d.check_zones()
        assert d.zones_uploaded[crew_id] == before
        compiled = read_json(layout.snapshots / f"{crew_id}.zones.json")
        assert compiled["ok"] and compiled["source"] == "default_branch"


async def test_rejoin_on_compact_keeps_the_session_and_token(tmp_path):
    repo = make_repo(tmp_path / "repo")
    layout = Layout(tmp_path / "home")
    async with crew_server(tmp_path) as srv:
        d = new_crewd(layout, srv, alive={A_PID})
        a = await d.join(peer(A_PID), _join("sess-a", repo, A_PID))
        token = d.tokens[a["key"]]
        again = await d.join(peer(A_PID), _join("sess-a", repo, A_PID, source="compact"))
        assert again["rejoined"] and again["session_id"] == a["session_id"] and d.tokens[a["key"]] == token
        # a restarted crewd (token on disk) re-joins the same session too
        d2 = new_crewd(layout, srv, alive={A_PID})
        assert d2.sessions[a["key"]]["session_id"] == a["session_id"]
        third = await d2.join(peer(A_PID), _join("sess-a", repo, A_PID, source="resume"))
        assert third["session_id"] == a["session_id"]


async def test_githook_missing_event_and_presence_frames_and_redaction(tmp_path):
    repo = make_repo(tmp_path / "repo")
    layout = Layout(tmp_path / "home")
    sent: list[str] = []

    class FakeLink:
        async def send(self, text: str) -> None:
            sent.append(text)

        async def close(self) -> None:
            return None

    async def connect(url, headers):
        assert url == "ws://test/ws" and headers["X-API-Key"]
        return FakeLink()

    async with crew_server(tmp_path) as srv:
        d = new_crewd(layout, srv, alive={A_PID}, ws_connect=connect)
        a = await d.join(peer(A_PID), _join("sess-a", repo, A_PID))
        sess = d.sessions[a["key"]]
        sess["githook_state"] = "ok"
        d.check_githooks()
        assert sess["githook_state"] == "missing"
        missing = [e for e in await client_events(srv, layout, a["crew_id"], d) if e["type"] == "githook.missing"]
        assert {m["payload"]["hook"] for m in missing} == {"pre-commit", "prepare-commit-msg", "pre-push"}
        # presence: at most one frame per session per 5 s, schema-valid, command metadata only
        await d.activity(peer(A_PID), {"key": a["key"], "phase": "start", "tool": "Bash", "verb": "curl"})
        assert await d.send_presence() == 1
        await d.activity(peer(A_PID), {"key": a["key"], "phase": "end", "tool": "Bash", "verb": "curl", "read_only": True})
        assert await d.send_presence() == 0
        frame = json.loads(sent[0])
        assert S.validate_ws_frame(frame) == []
        assert frame["lanes"][0]["last_action"] == {
            "tool": "Bash",
            "verb": "curl",
            "age_s": frame["lanes"][0]["last_action"]["age_s"],
        }
        # a test fingerprint carrying a secret is redacted before it leaves the host
        await d.on_test(sess, {"argv": ["pytest", "--token=sk-ant-api03-" + "x" * 40], "passed": 3, "failed": 0})
        events = [e for e in await client_events(srv, layout, a["crew_id"], d) if e["type"] == "activity.test_verdict_changed"]
        assert events and "sk-ant-api03" not in json.dumps(events)
        await d.checkpoint(sess, "test", force=True)
        ck = await srv.rows("SELECT facts FROM crew_checkpoints WHERE session_id = ?", (a["session_id"],))
        assert ck and "sk-ant-api03" not in ck[-1]["facts"]
        observed = json.loads(ck[-1]["facts"])["tests"][0]["observed_at"]
        await d.checkpoint(sess, "turn", force=True)
        later = await srv.rows("SELECT facts FROM crew_checkpoints WHERE session_id = ?", (a["session_id"],))
        assert json.loads(later[-1]["facts"])["tests"][0]["observed_at"] == observed
        assert str(layout.home) not in json.dumps(events)


async def test_baton_retention_sweep(tmp_path):
    repo = make_repo(tmp_path / "repo")
    layout = Layout(tmp_path / "home")
    async with crew_server(tmp_path) as srv:
        d = new_crewd(layout, srv, alive={A_PID})
        await d.join(peer(A_PID), _join("sess-a", repo, A_PID))
        (repo / "README.md").write_text("dirty\n")
        baton = B.create_baton_ref(repo, "T-7")
        assert baton is not None
        d.batons[baton.ref] = {"toplevel": str(repo), "remote": None, "closed_at": time.time() - B.REF_TTL_S - 5}
        d.batons["refs/remembra/baton/T-8/1"] = {"toplevel": str(repo), "closed_at": None}
        assert d.sweep_batons() == [baton.ref]
        assert baton.ref not in B.list_batons(repo)
        assert "refs/remembra/baton/T-8/1" in d.batons
