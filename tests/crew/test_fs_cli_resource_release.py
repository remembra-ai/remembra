"""Fault survey F26: ``remembra-crew release`` finds every kind of claim the CLI can take.

A deploy lock claimed as ``deploy:backtohello-production`` showed in ``whereami`` as "zone None" and
``release`` refused every spelling of its name ("REFUSED: you hold no claim on …"), while ``status``
listed it under YOU: release matched a claim id, a zone slug or a raw path, never ``resource``.

These call the real ``cmd_release`` / ``cmd_whereami`` / ``cmd_claim`` with the session, the on-disk
snapshot and the crewd API call replaced (no daemon, no network).
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import pytest

from remembra.relay.crew import cli
from remembra.relay.crew.gate import Layout

SID = "cs_me"
OTHER = "cs_other"
ZONE = {"id": "zn_api", "slug": "api", "include_globs": ["app/api/**"]}


def _claim(cid: str, **kw: Any) -> dict[str, Any]:
    base = {
        "id": cid,
        "zone_id": None,
        "path_glob": None,
        "resource": None,
        "holder_session_id": SID,
        "state": "active",
        "mode": "exclusive",
        "epoch": 1,
        "lease_expires_at": "2026-11-10T05:26:12Z",
    }
    return {**base, **kw}


LOCK = _claim("clm_lock", resource="deploy:backtohello-production")


class Crewd:
    """The three things the CLI asks crewd for: who am I, the snapshot file, and an API call."""

    def __init__(self, claims: list[dict[str, Any]], *, after: list[dict[str, Any]] | None = None, ok: bool = True) -> None:
        self.claims = claims
        self.after = after  # the snapshot's claims once a write went through (None: it never refreshes)
        self.ok = ok
        self.calls: list[tuple[str, str, Any]] = []
        self.reads = 0

    def snapshot(self, layout: Any, crew_id: str) -> dict[str, Any]:
        self.reads += 1
        return {"zones": [ZONE], "claims": [dict(c) for c in self.claims]}

    def api(self, layout: Any, method: str, path: str, *, json_body: Any = None, **kw: Any) -> dict[str, Any]:
        self.calls.append((method, path, json_body))
        if not self.ok:
            return {
                "ok": False,
                "status": 409,
                "body": {"detail": {"error": "claim_not_live", "message": "This claim is released."}},
            }
        if self.after is not None:
            self.claims = self.after
        return {"ok": True, "status": 200, "body": {"claim": {"id": path.split("/")[2], "mode": "exclusive", "epoch": 1}}}


@pytest.fixture()
def crewd(monkeypatch, tmp_path):
    def install(claims: list[dict[str, Any]], **kw: Any) -> Crewd:
        fake = Crewd(claims, **kw)
        who = {"session_id": SID, "crew_id": "crw_1", "callsign": "cc-3", "project_id": "bth", "toplevel": "", "key": "k"}
        monkeypatch.setattr(cli, "_need_session", lambda layout: who)
        monkeypatch.setattr(cli, "_snapshot", fake.snapshot)
        monkeypatch.setattr(cli, "_api", fake.api)
        monkeypatch.setattr(cli, "RELEASE_SETTLE_S", 0.3, raising=False)
        return fake

    return install


LAYOUT = Layout(Path("/nonexistent-remembra-home"))


def release(target: str, baton: bool = False) -> int:
    return cli.cmd_release(argparse.Namespace(target=target, baton=baton), LAYOUT)


def test_a_resource_lock_is_released_by_its_name(crewd, capsys):
    fake = crewd([LOCK], after=[])
    assert release("deploy:backtohello-production") == 0
    assert fake.calls == [("POST", "/claims/clm_lock/release", {"baton": False})]
    out = capsys.readouterr().out
    assert out.strip() == "RELEASED deploy:backtohello-production", out


def test_a_reserved_lock_is_still_released_by_its_name(crewd, capsys):
    """The stuck lock was ``reserved`` by the time it was looked at; the server lets its holder end it."""
    fake = crewd([{**LOCK, "state": "reserved"}], after=[])
    assert release("deploy:backtohello-production") == 0
    assert fake.calls == [("POST", "/claims/clm_lock/release", {"baton": False})]


@pytest.mark.parametrize(
    "target", ["api", "docs/guide/", "app/api/assets/[id]/route.ts", "deploy:prod", "schema:main", "mcp:stripe"]
)
def test_whatever_claim_takes_release_gives_back_by_the_same_name(crewd, capsys, target):
    """Round trip: the row the server stores for ``remembra-crew claim <target>`` is found by ``release <target>``."""
    fake = crewd([])
    assert cli.cmd_claim(argparse.Namespace(target=target, mode="exclusive", wait=0, task=None, reason=None), LAYOUT) == 0
    sent = fake.calls.pop()[2]
    stored = _claim("clm_new", **{k: sent.get(k) for k in ("zone_id", "path_glob", "resource")})
    fake.claims = [_claim("clm_other", path_glob="docs/other.md"), stored]
    capsys.readouterr()
    assert release(target) == 0
    assert fake.calls == [("POST", "/claims/clm_new/release", {"baton": False})]


def test_a_live_claim_is_picked_before_an_ended_row_of_the_same_name(crewd, capsys):
    ended = _claim("clm_old", path_glob="docs/a.md", state="released")
    live = _claim("clm_live", path_glob="docs/a.md")
    fake = crewd([ended, live], after=[ended])
    assert release("docs/a.md") == 0
    assert fake.calls == [("POST", "/claims/clm_live/release", {"baton": False})]


def test_another_sessions_lock_is_not_released_and_the_answer_says_what_you_hold(crewd, capsys):
    theirs = _claim("clm_theirs", resource="deploy:staging", holder_session_id=OTHER)
    fake = crewd([LOCK, theirs, _claim("clm_api", zone_id="zn_api"), _claim("clm_doc", path_glob="docs/a.md")])
    assert release("deploy:staging") == 1
    assert fake.calls == []
    out = capsys.readouterr().out
    assert out.startswith("REFUSED: you hold no claim on deploy:staging."), out
    assert "You hold: deploy:backtohello-production (clm_lock), zone api (clm_api), docs/a.md (clm_doc)" in out, out


def test_a_session_that_holds_nothing_gets_the_plain_refusal(crewd, capsys):
    crewd([])
    assert release("None") == 1
    assert capsys.readouterr().out.strip() == "REFUSED: you hold no claim on None", "unchanged wording"


def test_whereami_names_a_resource_lock_and_a_path_claim(crewd, capsys):
    crewd([LOCK, _claim("clm_api", zone_id="zn_api"), _claim("clm_doc", path_glob="docs/a.md")])
    assert cli.cmd_whereami(argparse.Namespace(json=False), LAYOUT) == 0
    out = capsys.readouterr().out
    assert "holding deploy:backtohello-production (active, epoch 1, lease until 2026-11-10T05:26:12Z)" in out, out
    assert "holding zone api (active" in out and "holding path docs/a.md (active" in out, out
    assert "None" not in out, out

    assert cli.cmd_whereami(argparse.Namespace(json=True), LAYOUT) == 0
    claims = json.loads(capsys.readouterr().out)["claims"]
    assert [(c["id"], c["zone"], c["resource"], c["path_glob"]) for c in claims] == [
        ("clm_lock", None, "deploy:backtohello-production", None),
        ("clm_api", "api", None, None),
        ("clm_doc", None, None, "docs/a.md"),
    ]
    assert {"state", "epoch", "lease_expires_at"} <= set(claims[0])  # the keys it had before are kept


def test_release_waits_for_the_local_snapshot_and_says_so_when_it_lags(crewd, capsys):
    """``status`` reads the on-disk snapshot, which crewd refreshes in the background after a write."""
    fake = crewd([LOCK], after=None)  # never refreshes within the wait
    assert release("deploy:backtohello-production") == 0
    out = capsys.readouterr().out.strip().split("\n")
    assert out[0] == "RELEASED deploy:backtohello-production", out
    assert len(out) == 2 and "remembra-crew status" in out[1] and "a minute" in out[1], out
    assert fake.reads >= 3  # it polled the snapshot file, not the server


def test_a_baton_release_is_settled_once_the_snapshot_shows_it_reserved(crewd, capsys):
    fake = crewd([LOCK], after=[{**LOCK, "state": "reserved"}])
    assert release("deploy:backtohello-production", baton=True) == 0
    assert fake.calls == [("POST", "/claims/clm_lock/release", {"baton": True})]
    out = capsys.readouterr().out.strip()
    assert out == "RELEASED deploy:backtohello-production (held as a baton for the next pickup)", out


def test_a_refused_release_is_reported_and_not_waited_on(crewd, capsys):
    fake = crewd([LOCK], ok=False)
    assert release("deploy:backtohello-production") == 1
    assert capsys.readouterr().out.strip() == "REFUSED: This claim is released."
    assert fake.reads == 1
