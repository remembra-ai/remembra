"""M2: the server port of the "why?" verdict, and ``GET /api/v1/trail/diagnosis``.

* :func:`remembra.marshal.diagnosis.diagnose_agent` passes every case of
  ``tests/fixtures/marshal/diagnosis_cases.json``, the file vitest runs through
  the dashboard's ``diagnoseAgent``, with the same partial-field comparison.
* JavaScript rounding (``Math.round`` rounds halves up) and time parsing
  (a time with no zone is UTC) are ported exactly.
* The route answers with the port's verdict on the caller's own reads, for a
  dashboard login and for API keys, writes nothing (no pickup, no binding), and
  shows a project-restricted key only its projects and no key names.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from remembra.auth.middleware import AuthenticatedUser
from remembra.marshal import words
from remembra.marshal.diagnosis import (
    DiagnosisInput,
    KeyEvidence,
    Verdict,
    agent_meta,
    canonical_agent_id,
    diagnose_agent,
    js_round,
    parse_server_time,
    relative_time,
)
from remembra.services.marshal_diagnosis import gather_diagnosis_input
from tests.marshal_desk_harness import desk_app

ROOT = Path(__file__).resolve().parents[1]
FIXTURE: dict[str, Any] = json.loads((ROOT / "tests" / "fixtures" / "marshal" / "diagnosis_cases.json").read_text())
NOW = parse_server_time(FIXTURE["now"])
assert NOW is not None


def _run(case: dict[str, Any]) -> Verdict:
    i = case["input"]
    return diagnose_agent(
        DiagnosisInput(
            agent_id=i["agent_id"],
            keys=[KeyEvidence.from_mapping(k) for k in i["keys"]],
            trail=i["trail"],
            agent_trail=i["agent_trail"],
            summary_agent=i.get("summary_agent"),
            now=NOW,
            server_url=FIXTURE["server_url"],
        )
    )


@pytest.mark.parametrize("case", FIXTURE["cases"], ids=[c["name"] for c in FIXTURE["cases"]])
def test_the_port_passes_every_fixture_case(case: dict[str, Any]) -> None:
    v = _run(case)
    got = {
        "code": v.code,
        "proven": v.proven,
        "verdict": v.verdict,
        "detail": v.detail,
        "lines": [f"{line.label}: {line.text}" for line in v.lines],
        "causes": list(v.causes),
        "unverified": v.unverified,
        "fix": v.fix.text if v.fix else None,
        "then": v.then,
        "commands": list(v.commands),
        "caveat": v.caveat,
        "doc": v.doc,
    }
    for field, want in case["expect"].items():
        assert got[field] == want, field


def test_the_fixture_reaches_every_verdict() -> None:
    codes = {_run(c).code for c in FIXTURE["cases"]}
    assert codes == set(words.SHARED_RULES) | set(words.DASHBOARD_ONLY)


def test_as_dict_uses_the_typescript_field_names_with_nulls() -> None:
    case = next(c for c in FIXTURE["cases"] if c["expect"].get("code") == "CODEX_TRUST_MISSING")
    d = _run(case).as_dict()
    assert list(d) == [
        "code",
        "proven",
        "lines",
        "verdict",
        "detail",
        "causes",
        "unverified",
        "fix",
        "then",
        "check",
        "commands",
        "caveat",
        "doc",
    ]
    assert d["fix"]["commands"] == [
        {
            "prompt": ">",
            "text": "/hooks",
            "label": "The Codex CLI command that lists hooks to trust",
            "caption": "in the Codex CLI:",
        }
    ]
    assert d["check"]["commands"][0] == {
        "prompt": "$",
        "text": "remembra-relay doctor --agent codex",
        "label": "Doctor for Codex",
        "caption": None,
    }
    assert d["lines"][0]["failed"] is False and d["unverified"] is None and d["caveat"] is None


def test_rounding_is_javascripts_half_up() -> None:
    assert [js_round(x) for x in (0.5, 1.5, 2.5, 89.5, -0.5, -2.5)] == [1, 2, 3, 90, 0, -2]
    assert [round(x) for x in (0.5, 2.5)] == [0, 2]  # what a naive port would get
    now = datetime(2026, 9, 26, 12, 0, tzinfo=UTC)
    # 2.5 minutes: JavaScript says 3m (Python's round would say 2m).
    assert relative_time(now - timedelta(seconds=150), now) == "3m ago"
    # 89.5 minutes -> 90 minutes -> 1.5 hours -> 2h.
    assert relative_time(now - timedelta(minutes=89, seconds=30), now) == "2h ago"
    # 150 minutes -> 2.5 hours: 3h, never 2h.
    assert relative_time(now - timedelta(minutes=150), now) == "3h ago"
    assert relative_time(now - timedelta(seconds=44), now) == "just now"
    assert relative_time(now - timedelta(hours=30), now) == "yesterday"
    assert relative_time(now - timedelta(days=3), now) == "3d ago"
    assert relative_time(now - timedelta(days=9), now) == "Sep 17"
    assert relative_time(now - timedelta(days=400), now) == "Aug 22, 2025"
    assert relative_time(None, now) == "unknown time" and relative_time("not a time", now) == "unknown time"


def test_server_times_without_a_zone_are_utc() -> None:
    assert parse_server_time("2026-09-26 09:00:00") == datetime(2026, 9, 26, 9, tzinfo=UTC)
    assert parse_server_time("2026-09-26T09:00:00+02:00") == datetime(2026, 9, 26, 7, tzinfo=UTC)
    assert parse_server_time("2026-09-26T09:00:00Z") == datetime(2026, 9, 26, 9, tzinfo=UTC)
    assert parse_server_time("") is None and parse_server_time(None) is None


def test_agent_names_follow_the_dashboard() -> None:
    assert canonical_agent_id("Claude") == "claude-code" and canonical_agent_id("codex-cli") == "codex"
    assert canonical_agent_id("My-Agent") == "My-Agent"  # an unknown id is kept as written
    assert agent_meta("my_custom.agent").name == "My Custom Agent"
    assert agent_meta("cursor").verified is False and agent_meta("codex").detach_close is True
    assert agent_meta("").name == "Unattributed"


# ---------------------------------------------------------------------------
# The route
# ---------------------------------------------------------------------------


def _jwt_principal(user_id: str) -> AuthenticatedUser:
    return AuthenticatedUser(user_id=user_id, api_key_id="jwt_auth", rate_limit_tier="standard")


async def test_the_route_answers_the_ports_verdict_and_writes_nothing(tmp_path) -> None:
    async with desk_app(tmp_path) as h:
        uid = await h.create_user("diag@example.com")
        headers = h.jwt(uid, "diag@example.com")
        claude = await h.api_key(uid, name="relay (mac, 2026-09-20)", agent_id="claude-code")
        await h.seed_handoff(claude, agent="claude-code", project="widget")
        await h.seed_handoff(claude, agent="claude-code", project="widget")
        await h.api_key(uid, name="ci")  # never used

        pickups, before = await h.count("relay_pickups"), await h.total_changes()
        res = await h.http.get("/api/v1/trail/diagnosis", params={"agent_id": "codex"}, headers=headers)
        assert res.status_code == 200, res.text
        body = res.json()
        assert await h.count("relay_pickups") == pickups and await h.total_changes() == before

        assert body["agent_id"] == "codex" and body["window"] == {"trail_limit": 100, "agent_limit": 5}
        assert body["generated_at"].endswith("Z")
        now = parse_server_time(body["generated_at"])
        assert now is not None
        expected = diagnose_agent(await gather_diagnosis_input(h.app.state, _jwt_principal(uid), "codex", now))
        assert body["verdict"] == expected.as_dict()
        assert body["verdict"]["code"] == "CODEX_TRUST_MISSING" and body["verdict"]["proven"] is False
        assert body["evidence"] == {
            "keys": {
                "active": 2,
                "used": 1,
                "newest_used_at": body["evidence"]["keys"]["newest_used_at"],
                "newest_name": "relay (mac, 2026-09-20)",
            },
            "entries": {"count": 0, "handoffs": 0, "newest_at": None},
            "pickups": {"briefs": 0, "others_handoffs": 2, "trail_entries": 2},
        }
        assert body["evidence"]["keys"]["newest_used_at"].endswith("Z")

        # Claude Code itself is connected: HANDED_OFF, with its own entries counted.
        res = await h.http.get("/api/v1/trail/diagnosis", params={"agent_id": "Claude"}, headers=headers)
        assert res.status_code == 200
        assert res.json()["agent_id"] == "claude-code" and res.json()["verdict"]["code"] == "HANDED_OFF"
        assert res.json()["evidence"]["entries"]["count"] == 2


async def test_pickups_without_a_close_are_picks_up_never_closes(tmp_path) -> None:
    async with desk_app(tmp_path) as h:
        uid = await h.create_user("pick@example.com")
        claude = await h.api_key(uid, agent_id="claude-code")
        codex = await h.api_key(uid, agent_id="codex")
        await h.seed_handoff(claude, agent="claude-code")
        await h.seed_pickup(codex, reader="codex")
        res = await h.http.get("/api/v1/trail/diagnosis", params={"agent_id": "codex"}, headers=h.jwt(uid, "pick@example.com"))
        verdict = res.json()["verdict"]
        assert verdict["code"] == "PICKS_UP_NEVER_CLOSES" and verdict["proven"] is True
        assert verdict["verdict"] == "Codex read 1 brief but never handed off: its close hasn't reached Remembra."
        assert res.json()["evidence"]["pickups"]["briefs"] == 1


async def test_api_keys_get_the_verdict_and_a_restricted_key_sees_only_its_projects(tmp_path) -> None:
    async with desk_app(tmp_path) as h:
        uid = await h.create_user("keys@example.com")
        claude = await h.api_key(uid, name="relay laptop", agent_id="claude-code")
        await h.seed_handoff(claude, agent="claude-code", project="widget")
        await h.seed_handoff(claude, agent="claude-code", project="gadget")
        codex = await h.api_key(uid, name="codex key", agent_id="codex")
        restricted = await h.api_key(uid, name="widget only", project_ids=["widget"])

        for key in (codex, restricted):
            # The key's own last-used stamp is the one write an API-key read makes (measured on a plain read).
            await h.http.get("/api/v1/trail/summary", headers=key)
            before = await h.total_changes()
            await h.http.get("/api/v1/trail/summary", headers=key)
            stamp = await h.total_changes() - before
            before, pickups = await h.total_changes(), await h.count("relay_pickups")
            res = await h.http.get("/api/v1/trail/diagnosis", params={"agent_id": "codex"}, headers=key)
            assert res.status_code == 200, res.text
            assert await h.total_changes() - before == stamp and await h.count("relay_pickups") == pickups
            body = res.json()
            if key is codex:
                assert body["evidence"]["pickups"]["trail_entries"] == 2
                assert body["evidence"]["keys"]["newest_name"] is not None
            else:
                # A key restricted to widget reads widget's entries only, and no key names.
                assert body["evidence"]["pickups"]["trail_entries"] == 1
                assert body["evidence"]["keys"]["newest_name"] is None
                assert "relay laptop" not in res.text and "codex key" not in res.text


async def test_a_malformed_agent_id_is_400_and_a_missing_one_422(tmp_path) -> None:
    async with desk_app(tmp_path) as h:
        uid = await h.create_user("bad@example.com")
        headers = h.jwt(uid, "bad@example.com")
        for bad in ("-codex", "codex agent", "codex;rm", "x" * 129):
            res = await h.http.get("/api/v1/trail/diagnosis", params={"agent_id": bad}, headers=headers)
            assert res.status_code in (400, 422), (bad, res.status_code)
            if len(bad) <= 128:
                assert res.status_code == 400
        assert (await h.http.get("/api/v1/trail/diagnosis", headers=headers)).status_code == 422
        assert (await h.http.get("/api/v1/trail/diagnosis", params={"agent_id": "codex"})).status_code == 401
