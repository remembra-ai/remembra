"""Relay API: project identity, links, close-out, pickup brief, trail, attribution.

Production routes over a real SQLite ``Database`` and a real ``MemoryService``
(only the vector store / embedder are fakes), via ``agent_api_harness``.
"""

from __future__ import annotations

import pytest

from remembra.auth.middleware import AuthenticatedUser, get_current_user
from remembra.security.pii_detector import PIIDetector
from tests.agent_api_harness import build_api, row

GH_HTTPS = "https://github.com/Acme/Widget.git"
GH_SSH = "git@github.com:acme/widget"
ROOT = "a" * 40
SECRET = "sk-proj-" + "Zx9Qw3Er7Ty1Ui5Op2As8Df4Gh6Jk0Lz"


@pytest.fixture()
def api(tmp_path):
    yield from build_api(tmp_path)


def _as(api, **kwargs):
    user = AuthenticatedUser(user_id=kwargs.pop("user_id", "default_user"), api_key_id="k1", rate_limit_tier="standard", **kwargs)
    api["app"].dependency_overrides[get_current_user] = lambda: user
    return user


def _post(api, path, body, headers=None, status=200):
    res = api["http"].post(f"/api/v1{path}", json=body, headers=headers or {})
    assert res.status_code == status, res.text
    return res.json()


def _get(api, path, params=None, headers=None, status=200):
    res = api["http"].get(f"/api/v1{path}", params=params or {}, headers=headers or {})
    assert res.status_code == status, res.text
    return res.json()


FACTS = {
    "branch": "main",
    "head_commit": "b" * 40,
    "upstream": "origin/main",
    "unpushed_commits": 2,
    "commits": [
        {"sha": "c" * 40, "subject": "feat: widget api"},
        {"sha": "b" * 40, "subject": "fix: widget edge case"},
    ],
    "files_changed": ["src/widget.py", "tests/test_widget.py", "notes.md"],
    "uncommitted_files": ["notes.md"],
    "diff_stat": "3 files changed, 40 insertions(+), 2 deletions(-)",
    "commands": [{"cmd": "npm run build", "exit_code": 0}, {"cmd": "make deploy", "exit_code": 2}],
    "tests": [
        {"cmd": "pytest -q tests/test_widget.py", "passed": False, "summary": "1 failed, 3 passed in 0.2s"},
        {"cmd": "npm test", "passed": True, "summary": "Tests: 12 passed"},
    ],
    "errors": [f"deploy token {SECRET} rejected"],
    "todos_open": ["wire the codex adapter"],
    "notes": "halfway through the adapter work",
}


# ---------------------------------------------------------------------------
# A) project identity
# ---------------------------------------------------------------------------


def test_same_repo_any_location_resolves_to_one_project(api):
    first = _post(api, "/projects/resolve", {"git_remote": GH_HTTPS, "root_commit": ROOT, "root_path": "/Users/a/widget"})
    assert first["project_id"] == "widget" and first["created"] is True and first["kind"] == "git"
    for variant in (
        {"git_remote": GH_SSH, "root_path": "/Volumes/T7/widget"},
        {"git_remote": "ssh://git@github.com:22/ACME/widget.git/", "root_path": "/srv/checkouts/w"},
        {"git_remote": "https://token@github.com/acme/widget", "root_path": "/tmp/wt/feature-x"},
        {"root_commit": ROOT.upper(), "root_path": "/opt/no-remote-clone"},  # clone without a remote
    ):
        again = _post(api, "/projects/resolve", variant)
        assert again["project_id"] == "widget", variant
        assert again["created"] is False


def test_fork_with_shared_root_commit_gets_its_own_project(api):
    _post(api, "/projects/resolve", {"git_remote": GH_HTTPS, "root_commit": ROOT})
    fork = _post(api, "/projects/resolve", {"git_remote": "https://github.com/someone/widget", "root_commit": ROOT})
    assert fork["project_id"] != "widget" and fork["project_id"].startswith("widget-")
    assert fork["created"] is True


def test_remote_added_later_joins_the_root_commit_project(api):
    first = _post(api, "/projects/resolve", {"root_commit": ROOT, "repo_name": "gizmo", "root_path": "/x/gizmo"})
    assert first["project_id"] == "gizmo" and first["kind"] == "root"
    later = _post(api, "/projects/resolve", {"git_remote": "git@github.com:acme/gizmo-renamed.git", "root_commit": ROOT})
    assert later["project_id"] == "gizmo"


def test_hint_names_new_location_and_bind_rebinds(api):
    new = _post(api, "/projects/resolve", {"git_remote": "https://github.com/dolphy/clawbot", "hint_project": "clawdbot"})
    assert new["project_id"] == "clawdbot"
    other = _post(api, "/projects/resolve", {"git_remote": GH_HTTPS})
    assert other["project_id"] == "widget"
    # A hint alone never steals a known location...
    assert _post(api, "/projects/resolve", {"git_remote": GH_HTTPS, "hint_project": "clawdbot"})["project_id"] == "widget"
    # ...an explicit bind does.
    bound = _post(api, "/projects/resolve", {"git_remote": GH_HTTPS, "hint_project": "clawdbot", "bind": True})
    assert bound["project_id"] == "clawdbot" and bound["bound"] is True
    assert _post(api, "/projects/resolve", {"git_remote": GH_SSH})["project_id"] == "clawdbot"


def test_resolve_is_per_user(api):
    _post(api, "/projects/resolve", {"git_remote": GH_HTTPS, "hint_project": "mine"})
    _as(api, user_id="someone_else")
    assert _post(api, "/projects/resolve", {"git_remote": GH_HTTPS})["project_id"] == "widget"


def test_read_only_key_resolves_without_persisting(api):
    _as(api, role="viewer")
    res = _post(api, "/projects/resolve", {"git_remote": GH_HTTPS})
    assert res["project_id"] == "widget" and res["persisted"] is False and res["created"] is False
    _post(api, "/projects/resolve", {"git_remote": GH_HTTPS, "hint_project": "x", "bind": True}, status=403)


def test_project_scoped_key_cannot_resolve_into_other_projects(api):
    _post(api, "/projects/resolve", {"git_remote": GH_HTTPS})  # unrestricted: widget
    _as(api, project_ids=["clawdbot"])
    res = api["http"].post("/api/v1/projects/resolve", json={"git_remote": GH_HTTPS})
    assert res.status_code == 403
    # A brand-new location for a single-project key lands in its project.
    assert _post(api, "/projects/resolve", {"git_remote": "https://github.com/x/new-thing"})["project_id"] == "clawdbot"


def test_resolve_requires_some_location(api):
    _post(api, "/projects/resolve", {}, status=400)


# ---------------------------------------------------------------------------
# B) close-out
# ---------------------------------------------------------------------------


def _close(api, **overrides):
    body = {"agent_id": "claude-code", "session_id": "sess-1", "project_id": "widget", "facts": FACTS, **overrides}
    return _post(api, "/session/close", body)


def test_close_builds_one_structured_handoff(api):
    out = _close(api, end_reason="logout")
    text = out["rendered"]
    assert text.startswith("[HANDOFF] claude-code")
    assert "main@bbbbbbb" in text and "session sess-1" in text and "ended: logout" in text
    for heading in ("Done:", "Not done / open:", "Failing / errors:", "Next step:"):
        assert heading in text
    assert "ccccccc feat: widget api" in text
    assert "tests passing: npm test (Tests: 12 passed)" in text
    assert "TODO: wire the codex adapter" in text
    assert "uncommitted changes in 1 file(s): notes.md" in text
    assert "2 commit(s) not pushed to origin/main" in text
    assert "FAILING: pytest -q tests/test_widget.py (1 failed, 3 passed in 0.2s)" in text
    assert "`make deploy` exited 2" in text
    assert "Next step: fix the failing run: pytest -q tests/test_widget.py" in text
    assert out["headline"].startswith("2 commit(s), last: fix: widget edge case")

    stored = row(api, out["handoff_id"])
    assert stored["memory_type"] == "handoff"
    assert stored["content"] == text
    meta = stored["metadata"] if isinstance(stored["metadata"], dict) else __import__("json").loads(stored["metadata"])
    assert meta["relay"]["agent_id"] == "claude-code" and meta["relay"]["session_id"] == "sess-1"
    assert meta["relay"]["failing"][0].startswith("FAILING: pytest")
    # Exactly one handoff (no fact splitting).
    handoffs = _get(api, "/timeline", {"project_id": "widget", "memory_type": "handoff"})
    assert handoffs["total"] == 1


def test_close_redacts_secrets_everywhere(api):
    out = _close(api, summary=f"used key {SECRET} to deploy")
    assert SECRET not in out["rendered"]
    assert "[REDACTED:" in out["rendered"]
    assert out["redactions"]
    stored = row(api, out["handoff_id"])
    assert SECRET not in str(stored["content"]) and SECRET not in str(stored["metadata"])


def test_close_is_idempotent_per_agent_session(api):
    first = _close(api)
    same = _close(api)
    assert same["changed"] is False and same["handoff_id"] == first["handoff_id"]

    facts = {
        **FACTS,
        "todos_open": [],
        "tests": [{"cmd": "pytest -q tests/test_widget.py", "passed": True, "summary": "4 passed"}],
    }
    second = _close(api, facts=facts)
    assert second["changed"] is True and second["superseded"] == [first["handoff_id"]]
    assert row(api, first["handoff_id"])["superseded_by"] == second["handoff_id"]
    current = _get(api, "/timeline", {"project_id": "widget", "memory_type": "handoff"})
    assert current["total"] == 1 and current["memories"][0]["id"] == second["handoff_id"]

    # A different session of the same agent is a new trail entry, not an update.
    third = _close(api, session_id="sess-2")
    assert third["superseded"] == []
    assert _get(api, "/timeline", {"project_id": "widget", "memory_type": "handoff"})["total"] == 2


def test_close_upserts_last_agent_and_branch_status(api):
    _close(api)
    items = {i["key"]: i["value"] for i in _get(api, "/session/status", {"project_id": "widget"})["items"]}
    assert items["branch:widget"] == "main@bbbbbbb"
    assert items["last_agent:widget"] == "claude-code (session sess-1) on main@bbbbbbb"


def test_summary_is_grounding_checked(api):
    out = _close(api, summary="All tests pass and everything is pushed. Fixed src/widget.py and src/other.py in deadbeef1.")
    grounding = out["grounding"]
    assert grounding["status"] == "contradicted"
    issues = " | ".join(grounding["issues"])
    assert "claims tests pass" in issues
    assert "not pushed" in issues
    assert "src/other.py" in issues and "src/widget.py" not in issues
    assert "deadbeef1" in issues
    assert "Agent summary [CONTRADICTED by facts" in out["rendered"]

    clean = _close(api, session_id="s-ok", summary="Added the widget api in ccccccc; pytest still failing.")
    assert clean["grounding"]["status"] == "consistent"
    assert "unverified narrative" in clean["rendered"]


def test_close_resolves_project_from_location(api):
    out = _post(
        api,
        "/session/close",
        {"agent_id": "codex", "session_id": "s", "project": {"git_remote": GH_SSH, "root_commit": ROOT}, "facts": {}},
    )
    assert out["project_id"] == "widget" and out["resolution"]["created"] is True
    assert "Done:\n- nothing recorded" in out["rendered"]


def test_close_truncates_oversized_input_instead_of_failing(api):
    facts = {"files_changed": [f"f{i}.py" for i in range(5000)], "commands": [{"cmd": "x" * 5000, "exit_code": 1}] * 400}
    out = _close(api, facts=facts, summary="y" * 50000)
    assert out["handoff_id"]
    assert "changed 500 file(s)" in out["rendered"]


def test_close_requires_agent_and_valid_session(api):
    _post(api, "/session/close", {"session_id": "s", "facts": {}}, status=400)
    _post(api, "/session/close", {"agent_id": "a b", "session_id": "s", "facts": {}}, status=400)
    _post(api, "/session/close", {"agent_id": "codex", "session_id": "bad id!", "facts": {}}, status=400)


# ---------------------------------------------------------------------------
# G) attribution + access
# ---------------------------------------------------------------------------


def test_agent_scoped_key_attribution_is_enforced(api):
    _as(api, agent_id="codex")
    denied = api["http"].post(
        "/api/v1/session/close", json={"agent_id": "claude-code", "session_id": "s", "project_id": "widget", "facts": {}}
    )
    assert denied.status_code == 403
    denied_header = api["http"].post(
        "/api/v1/session/close",
        json={"session_id": "s", "project_id": "widget", "facts": {}},
        headers={"X-Remembra-Agent-Id": "claude-code"},
    )
    assert denied_header.status_code == 403
    out = _post(api, "/session/close", {"session_id": "s", "project_id": "widget", "facts": {}})
    assert out["agent_id"] == "codex" and out["agent_verified"] is True
    assert out["rendered"].startswith("[HANDOFF] codex")
    brief = _get(api, "/session/brief", {"project_id": "widget", "agent_id": "codex"})
    assert brief["agent_id"] == "codex"
    _get(api, "/session/brief", {"project_id": "widget", "agent_id": "claude-code"}, status=403)


def test_unscoped_key_header_and_body_must_agree(api):
    out = _post(
        api, "/session/close", {"session_id": "s", "project_id": "p", "facts": {}}, headers={"X-Remembra-Agent-Id": "gemini"}
    )
    assert out["agent_id"] == "gemini" and out["agent_verified"] is False
    res = api["http"].post(
        "/api/v1/session/close",
        json={"agent_id": "codex", "session_id": "s", "project_id": "p", "facts": {}},
        headers={"X-Remembra-Agent-Id": "gemini"},
    )
    assert res.status_code == 400


def test_project_restricted_key_cannot_close_or_brief_other_projects(api):
    _as(api, project_ids=["alpha"])
    res = api["http"].post("/api/v1/session/close", json={"agent_id": "a", "session_id": "s", "project_id": "beta", "facts": {}})
    assert res.status_code == 403
    _get(api, "/session/brief", {"project_id": "beta"}, status=403)
    _get(api, "/trail", {"project_id": "beta"}, status=403)
    assert _post(api, "/session/close", {"agent_id": "a", "session_id": "s", "facts": {}})["project_id"] == "alpha"


def test_viewer_key_cannot_close(api):
    _as(api, role="viewer")
    res = api["http"].post("/api/v1/session/close", json={"agent_id": "a", "session_id": "s", "facts": {}})
    assert res.status_code == 403


# ---------------------------------------------------------------------------
# C/D) brief, links, trail
# ---------------------------------------------------------------------------


def test_brief_by_location_leads_with_last_session(api):
    _post(api, "/projects/resolve", {"git_remote": GH_HTTPS})
    _close(api)
    brief = _get(api, "/session/brief", {"git_remote": GH_SSH, "agent_id": "codex"})
    assert brief["project_id"] == "widget"
    text = brief["rendered"]
    first_lines = text.splitlines()[:2]
    assert first_lines[0].startswith("# Remembra brief · project widget · you are codex")
    last = first_lines[1]
    assert last.startswith("Last session: claude-code, just now, on main@bbbbbbb: done: ")
    assert "NOT done: TODO: wire the codex adapter" in last
    assert "failing: FAILING: pytest -q tests/test_widget.py" in last
    assert "next: fix the failing run" in last
    assert len(text) <= 6000


def test_brief_is_capped_and_drops_recent_first(api):
    _close(api)
    http = api["http"]
    for i in range(40):
        http.post("/api/v1/memories", json={"content": f"note {i} " + "word " * 80, "project_id": "widget"})
    brief = _get(api, "/session/brief", {"project_id": "widget", "recent_n": 50})
    assert len(brief["rendered"]) <= 6000
    assert brief["rendered"].splitlines()[1].startswith("Last session: claude-code")


def test_links_show_linked_projects_latest_handoff(api):
    _post(api, "/projects/links", {"from_project": "widget", "to_project": "widget-web", "relation": "frontend_of"})
    assert (
        _post(api, "/projects/links", {"from_project": "widget", "to_project": "widget-web", "relation": "frontend_of"})[
            "created"
        ]
        is False
    )
    _close(api, project_id="widget-web", agent_id="cursor", session_id="w1")
    links = _get(api, "/projects/links", {"project_id": "widget-web"})
    assert links["items"][0]["project_id"] == "widget" and links["items"][0]["direction"] == "incoming"

    brief = _get(api, "/session/brief", {"project_id": "widget"})
    linked = brief["linked_projects"]
    assert linked[0]["project_id"] == "widget-web"
    assert linked[0]["latest_handoff"]["agent_id"] == "cursor"
    assert "Linked projects:\n- widget-web (frontend_of): cursor, just now: 2 commit(s)" in brief["rendered"]

    res = api["http"].delete(
        "/api/v1/projects/links", params={"from_project": "widget", "to_project": "widget-web", "relation": "frontend_of"}
    )
    assert res.json()["removed"] is True
    assert _get(api, "/session/brief", {"project_id": "widget"})["linked_projects"] == []


def test_links_validate_and_respect_project_scope(api):
    _post(api, "/projects/links", {"from_project": "a", "to_project": "a"}, status=400)
    _post(api, "/projects/links", {"from_project": "a", "to_project": "b", "relation": "Bad Relation"}, status=400)
    _post(api, "/projects/links", {"from_project": "a", "to_project": "secret"})
    _close(api, project_id="secret", session_id="x")
    _as(api, project_ids=["a"])
    _post(api, "/projects/links", {"from_project": "a", "to_project": "c"}, status=403)
    assert _get(api, "/projects/links", {"project_id": "a"})["items"] == []
    assert _get(api, "/session/brief", {"project_id": "a"})["linked_projects"] == []


def test_trail_lists_handoffs_and_checkpoints_newest_first(api):
    _close(api, agent_id="claude-code", session_id="s1")
    api["http"].post(
        "/api/v1/memories", json={"content": "checkpoint: halfway", "project_id": "widget", "memory_type": "checkpoint"}
    )
    _close(api, agent_id="codex", session_id="s2", facts={"branch": "feat/x", "head_commit": "d" * 40})
    trail = _get(api, "/trail", {"project": "widget"})
    assert trail["total"] == 3
    items = trail["items"]
    assert [i["agent_id"] for i in items][0] == "codex"
    assert items[0]["branch"] == "feat/x" and items[0]["head_commit"] == "d" * 40
    assert items[1]["memory_type"] == "checkpoint" and items[1]["headline"] == "checkpoint: halfway"
    assert items[2]["agent_id"] == "claude-code" and items[2]["failing"] >= 1
    by_location = _get(api, "/trail", {"git_remote": GH_HTTPS})
    assert by_location["project_id"] == "widget"


@pytest.mark.parametrize("mode", ["redact", "block"])
def test_pii_policy_applies_to_every_stored_value_without_losing_the_handoff(api, mode):
    api["app"].state.pii_detector = PIIDetector(enabled=True, mode=mode)
    facts = {"commits": [{"sha": "e" * 40, "subject": "fix: SSN 123-45-6789 leaked in logs"}], "todos_open": ["tell 123-45-6789"]}
    out = _close(api, facts=facts)
    stored = row(api, out["handoff_id"])
    assert "123-45-6789" not in stored["content"] and "123-45-6789" not in str(stored["metadata"])
    assert "Not done / open:" in stored["content"]
    assert out["redactions"].get("pii") == 2


def test_pii_detect_mode_keeps_values(api):
    api["app"].state.pii_detector = PIIDetector(enabled=True, mode="detect")
    out = _close(api, facts={"todos_open": ["call 555-867-5309 re: invoice"]})
    assert "555-867-5309" in out["rendered"]
