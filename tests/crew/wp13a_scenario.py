"""WP-13a scenario: a real crew driven over real HTTP, for the Mission Control live check.

Everything goes through the production routers of a running server (the
``LiveServer`` of ``test_dashboard_live``): an agent API key registers a host
and joins three sessions, a dashboard login creates the zones, the sessions
create and start tasks (claims), heartbeat with presence, checkpoint and hit
the guard; then cc-1 runs out of credits (a StopFailure-style stall with a
baton ref), which reserves POS and stalls its task: the pickup slot.

No row is inserted by hand, so the events, the snapshot and the list the
dashboard reads are exactly what the services produce.
"""

from __future__ import annotations

from typing import Any

import httpx

from remembra.crew.hosts import HOST_TOKEN_HEADER
from remembra.crew.sessions import SESSION_TOKEN_HEADER

API = "/api/v1"
PROJECT = "yaadbooks"


class Scenario:
    def __init__(self, base_url: str, jwt: str, api_key: str) -> None:
        self.http = httpx.Client(base_url=base_url, timeout=20.0)
        self.human = {"Authorization": f"Bearer {jwt}"}
        self.key = {"X-API-Key": api_key}
        self.host: dict[str, str] = {}
        self.crew_id = ""
        self.sessions: dict[str, dict[str, Any]] = {}
        self.zones: dict[str, str] = {}
        self.tasks: dict[str, dict[str, Any]] = {}

    def close(self) -> None:
        self.http.close()

    # -- helpers ---------------------------------------------------------------------

    def _ok(self, res: httpx.Response, *codes: int) -> dict[str, Any]:
        assert res.status_code in (codes or (200, 201)), f"{res.request.method} {res.request.url} -> {res.status_code} {res.text}"
        return res.json()

    def as_session(self, name: str) -> dict[str, str]:
        return {**self.key, SESSION_TOKEN_HEADER: self.sessions[name]["token"]}

    def sid(self, name: str) -> str:
        return str(self.sessions[name]["id"])

    # -- steps -----------------------------------------------------------------------

    def register_host(self) -> None:
        body = {"host_label": "mbp", "platform": "darwin", "crewd_version": "1"}
        out = self._ok(self.http.post(f"{API}/crew/hosts/register", json=body, headers=self.key), 201)
        self.host = {"id": out["host_id"], "token": out["host_token"]}

    def join(self, name: str, *, agent: str, checkout: str, model: str | None = None) -> None:
        body = {
            "project_id": PROJECT,
            "agent_id": agent,
            "session_id": f"client-{name}",
            "adapter": agent,
            "client_kind": "hook",
            "host_id": self.host["id"],
            "checkout_fp": checkout,
            "worktree_id": f"wt-{checkout}",
            "branch": "main",
            "head": "abc1234",
            "source": "startup",
        }
        if model:
            body["model"] = model
        headers = {**self.key, HOST_TOKEN_HEADER: self.host["token"]}
        out = self._ok(self.http.post(f"{API}/crews/join", json=body, headers=headers), 201)
        self.crew_id = out["crew_id"]
        self.sessions[name] = {"id": out["session_id"], "token": out["session_token"], "callsign": out["callsign"]}

    def zone(self, slug: str, title: str, include: list[str]) -> None:
        body = {"slug": slug, "title": title, "include": include, "mode": "exclusive"}
        out = self._ok(self.http.post(f"{API}/crews/{self.crew_id}/zones", json=body, headers=self.human), 201)
        zone = out.get("zone", out)
        self.zones[slug] = zone["id"]

    def task(self, name: str, by: str, title: str, zones: list[str], phase: str) -> None:
        body = {"title": title, "zone_ids": [self.zones[z] for z in zones], "acceptance": [], "depends_on": [], "phase": phase}
        out = self._ok(self.http.post(f"{API}/crews/{self.crew_id}/tasks", json=body, headers=self.as_session(by)), 201)
        self.tasks[name] = out["task"]

    def start(self, name: str, by: str) -> dict[str, Any]:
        tid = self.tasks[name]["id"]
        return self._ok(self.http.post(f"{API}/tasks/{tid}/start", json={"head": "abc1234"}, headers=self.as_session(by)))

    def heartbeat(self, batch: str, lanes: dict[str, dict[str, Any]]) -> dict[str, Any]:
        sessions = []
        for name, lane in lanes.items():
            sessions.append(
                {
                    "session_id": self.sid(name),
                    "token": self.sessions[name]["token"],
                    "alive": True,
                    "activity_age_s": lane.get("age", 2),
                    "last_action": lane.get("action"),
                    "calls_since_checkpoint": lane.get("calls", 0),
                    "limit": lane.get("limit"),
                    "footprints": [],
                    "cursor": 0,
                    "githook_state": lane.get("githook", "ok"),
                }
            )
        headers = {**self.key, HOST_TOKEN_HEADER: self.host["token"]}
        return self._ok(self.http.post(f"{API}/crew/heartbeat", json={"batch_id": batch, "sessions": sessions}, headers=headers))

    def checkpoint(self, by: str, trigger: str, facts: dict[str, Any]) -> None:
        body = {"session_id": self.sid(by), "trigger": trigger, "facts": facts}
        self._ok(self.http.post(f"{API}/crews/{self.crew_id}/checkpoints", json=body, headers=self.as_session(by)), 200, 201)

    def guard(self, by: str, path: str) -> dict[str, Any]:
        body = {"session_id": self.sid(by), "op": "write", "paths": [path]}
        return self._ok(self.http.post(f"{API}/crews/{self.crew_id}/guard", json=body, headers=self.as_session(by)))

    def stall(self, by: str, *, error: str, baton_ref: str, dirty: list[str], unpushed: int) -> dict[str, Any]:
        body = {
            "error": error,
            "error_details_class": "credits",
            "facts": {"uncommitted_files": dirty, "unpushed_commits": unpushed},
            "baton_ref": baton_ref,
        }
        return self._ok(self.http.post(f"{API}/sessions/{self.sid(by)}/stall", json=body, headers=self.as_session(by)))

    # -- the whole story ---------------------------------------------------------------

    def run(self) -> dict[str, Any]:
        self.register_host()
        self.join("a", agent="claude-code", checkout="fp-a", model="opus")
        self.join("b", agent="codex", checkout="fp-b")
        self.join("c", agent="claude-code", checkout="fp-c", model="sonnet")
        self.zone("pos", "POS section", ["src/app/pos/**"])
        self.zone("reports", "Reports", ["src/app/reports/**"])
        self.zone("payroll", "Payroll", ["src/app/payroll/**"])
        self.task("t1", "a", "POS split tender", ["pos"], "Phase 2 · POS")
        self.task("t2", "b", "Reports export", ["reports"], "Phase 3 · Reports")
        self.task("t3", "c", "Payroll ledger", ["payroll"], "Phase 4 · Payroll")
        self.start("t1", "a")
        self.start("t2", "b")
        self.heartbeat(
            "hb-1",
            {
                "a": {"action": {"tool": "Edit", "path_rel": "src/app/pos/tender.ts", "age_s": 3}, "calls": 12},
                "b": {"action": {"tool": "Bash", "path_rel": None, "verb": "npm test", "age_s": 5}, "calls": 31},
                "c": {"action": {"tool": "Read", "path_rel": "src/app/pos/cart.ts", "age_s": 8}, "calls": 4},
            },
        )
        self.checkpoint("a", "commit", {"branch": "main", "head": "abc1234", "commits": ["abc1234"]})
        self.checkpoint("a", "test", {"tests": [{"command": "npm test -- pos", "passed": 12, "failed": 1}]})
        self.checkpoint("b", "test", {"tests": [{"command": "npm test -- reports", "passed": 8, "failed": 0}]})
        guard = self.guard("c", "src/app/pos/cart.ts")
        stall = self.stall(
            "a",
            error="billing_error",
            baton_ref="refs/remembra/baton/T-1/1",
            dirty=["src/app/pos/tender.ts", "src/app/pos/split.ts", "src/app/pos/receipt.ts"],
            unpushed=2,
        )
        return {
            "crew_id": self.crew_id,
            "project": PROJECT,
            "sessions": {n: {"id": s["id"], "callsign": s["callsign"]} for n, s in self.sessions.items()},
            "tasks": {n: t["id"] for n, t in self.tasks.items()},
            "zones": self.zones,
            "guard": guard.get("decision"),
            "stall_state": stall.get("state"),
        }
