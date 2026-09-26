"""§13.6: one session flooding guard and claims → other sessions' auto-claims unaffected.

Runs against the red-team server with every production limiter on (the crew per-session,
per-user and per-host buckets and the app's slowapi per-user limits). Both sessions belong
to the same account and the same API key — the case the per-user limiter alone got wrong
(D11: a starved agent's auto-claim 429 turns into fail-open).

The flooder exhausts its own guard and claims buckets (and, for good measure, messages,
tasks and a slowapi-limited read route). The victim session then hits the guard on a leaf
zone it has not claimed: the server auto-claims it (rule 17) and a direct claim is granted.
"""

from __future__ import annotations

from collections import Counter

from tests.crew.abuse.driver import API, Crew
from tests.crew.abuse.server import RedTeamServer


def _flood(send: object, n: int) -> Counter[int]:
    codes: Counter[int] = Counter()
    for i in range(n):
        codes[send(i).status_code] += 1  # type: ignore[operator]
    return codes


def test_one_session_flooding_guard_and_claims_cannot_starve_another_sessions_auto_claims(limited_server: RedTeamServer) -> None:
    s = limited_server
    c = Crew(s.http, s.login(), s.admin_key)
    flooder = c.join("flooder")
    victim = c.join("victim")
    for slug in ("pos", "reports", "billing", "payroll"):
        c.zone(slug, [f"src/app/{slug}/**"])

    # The flooder: guard at 300/min per session, claims at 60/min per session, then 429.
    guard = _flood(lambda i: c.guard("flooder", [f"src/app/pos/f{i % 7}.ts"]), 330)
    assert guard[200] == 300 and guard[429] == 30, guard
    claims = _flood(lambda i: c.claim("flooder", ("pos", "billing")[i % 2]), 75)
    assert claims[429] == 15 and sum(claims.values()) - claims[429] == 60, claims
    last = c.claim("flooder", "payroll")
    assert last.status_code == 429 and last.json()["detail"]["error"] == "rate_limited"
    assert int(last.headers["Retry-After"]) >= 1 and last.json()["detail"]["retry_after_s"] >= 1

    # ...and every other bucket it can reach, plus a slowapi-limited route of the same account.
    msgs = _flood(
        lambda i: s.http.post(
            f"{API}/crews/{c.crew_id}/messages",
            json={"kind": "chat", "body": f"spam {i}", "client_msg_id": f"spam-{i}"},
            headers=flooder.headers(),
        ),
        25,
    )
    assert msgs[429] == 5, msgs
    reads = _flood(lambda i: s.http.get(f"{API}/crews/{c.crew_id}/members", headers={"X-API-Key": s.admin_key}), 125)
    assert reads[429] >= 1, reads  # the app limiter is live for this account

    # The victim (same account, same key, its own session token) is unaffected.
    res = c.guard("victim", ["src/app/reports/export.ts"])
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["decision"] == "allow" and [a["zone_id"] for a in body["auto_claimed"]] == [c.zones["reports"]], body
    assert body["auto_claimed"][0]["holder_session_id"] == victim.id
    granted = c.claim("victim", "payroll")
    assert granted.status_code == 201, granted.text
    for i in range(5):  # sustained, not a one-off
        assert c.guard("victim", [f"src/app/reports/r{i}.ts"]).status_code == 200
    held = {cl["zone_id"] for cl in c.claims() if cl["holder_session_id"] == victim.id and cl["state"] == "active"}
    assert {c.zones["reports"], c.zones["payroll"]} <= held

    # The flooder is still refused: its buckets did not reset because the victim spent from its own.
    assert c.guard("flooder", ["src/app/pos/f0.ts"]).status_code == 429
