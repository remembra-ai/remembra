"""Drive a real crew over HTTP the way agents (API keys + session tokens) and Mani (dashboard JWT) do.

No row is inserted by hand: every entity the suite attacks is produced by the production
routers of the :class:`~tests.crew.abuse.server.RedTeamServer`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import httpx

from remembra.crew.hosts import HOST_TOKEN_HEADER
from remembra.crew.sessions import SESSION_TOKEN_HEADER

API = "/api/v1"
PROJECT = "yaadbooks"


@dataclass
class Seat:
    name: str
    id: str
    token: str
    callsign: str
    key: str
    joined: dict[str, Any] = field(default_factory=dict)

    def headers(self) -> dict[str, str]:
        return {"X-API-Key": self.key, SESSION_TOKEN_HEADER: self.token}


def ok(res: httpx.Response, *codes: int) -> dict[str, Any]:
    assert res.status_code in (codes or (200, 201)), f"{res.request.method} {res.request.url} -> {res.status_code} {res.text}"
    body = res.json()
    assert isinstance(body, dict)
    return body


class Crew:
    """One project's crew: a host, seats, zones and tasks, all through the API."""

    def __init__(self, http: httpx.Client, human: dict[str, str], agent_key: str, *, project: str = PROJECT) -> None:
        self.http = http
        self.human = human
        self.key = agent_key
        self.project = project
        self.crew_id = ""
        self.host: dict[str, str] = {}
        self.seats: dict[str, Seat] = {}
        self.zones: dict[str, str] = {}
        self.tasks: dict[str, dict[str, Any]] = {}

    def register_host(self, key: str | None = None) -> None:
        body = {"host_label": "mbp", "platform": "darwin", "crewd_version": "1"}
        out = ok(self.http.post(f"{API}/crew/hosts/register", json=body, headers={"X-API-Key": key or self.key}), 201)
        self.host = {"id": out["host_id"], "token": out["host_token"]}

    def join(self, name: str, *, agent: str = "claude-code", key: str | None = None, checkout: str | None = None) -> Seat:
        if not self.host:
            self.register_host()
        key = key or self.key
        body = {
            "project_id": self.project,
            "agent_id": agent,
            "session_id": f"client-{name}",
            "adapter": "claude-code" if agent == "claude-code" else agent,
            "client_kind": "hook",
            "host_id": self.host["id"],
            "checkout_fp": checkout or f"fp-{name}",
            "worktree_id": f"wt-{checkout or name}",
            "branch": "main",
            "head": "abc1234",
            "source": "startup",
        }
        headers = {"X-API-Key": key, HOST_TOKEN_HEADER: self.host["token"]}
        out = ok(self.http.post(f"{API}/crews/join", json=body, headers=headers), 200, 201)
        self.crew_id = out["crew_id"]
        seat = Seat(name, out["session_id"], out["session_token"], out["callsign"], key, out)
        self.seats[name] = seat
        return seat

    def zone(self, slug: str, include: list[str], *, title: str | None = None, mode: str = "exclusive") -> str:
        body = {"slug": slug, "title": title or slug, "include": include, "mode": mode}
        out = ok(self.http.post(f"{API}/crews/{self.crew_id}/zones", json=body, headers=self.human), 201)
        zone = out.get("zone", out)
        self.zones[slug] = zone["id"]
        return str(zone["id"])

    def task(
        self,
        name: str,
        by: str,
        title: str,
        zones: list[str],
        *,
        acceptance: list[dict[str, Any]] | None = None,
        body: str | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "title": title,
            "zone_ids": [self.zones[z] for z in zones],
            "acceptance": acceptance or [],
            "depends_on": [],
        }
        if body is not None:
            payload["body"] = body
        out = ok(self.http.post(f"{API}/crews/{self.crew_id}/tasks", json=payload, headers=self.seats[by].headers()), 201)
        self.tasks[name] = out["task"]
        return dict(out["task"])

    def tid(self, name: str) -> str:
        return str(self.tasks[name]["id"])

    def start(self, name: str, by: str) -> dict[str, Any]:
        return ok(
            self.http.post(f"{API}/tasks/{self.tid(name)}/start", json={"head": "abc1234"}, headers=self.seats[by].headers())
        )

    def claim(self, by: str, zone: str, *, mode: str = "exclusive") -> httpx.Response:
        body = {"zone_id": self.zones[zone], "mode": mode, "wait": False, "source": "first_write"}
        return self.http.post(f"{API}/crews/{self.crew_id}/claims", json=body, headers=self.seats[by].headers())

    def guard(self, by: str, paths: list[str], *, op: str = "write") -> httpx.Response:
        body = {"session_id": self.seats[by].id, "op": op, "paths": paths}
        return self.http.post(f"{API}/crews/{self.crew_id}/guard", json=body, headers=self.seats[by].headers())

    def say(self, by: str, body: str, *, kind: str = "chat") -> dict[str, Any]:
        payload = {"kind": kind, "body": body, "client_msg_id": f"m-{by}-{abs(hash(body)) % 10**9}"}
        return ok(
            self.http.post(f"{API}/crews/{self.crew_id}/messages", json=payload, headers=self.seats[by].headers()), 200, 201
        )

    def claims(self) -> list[dict[str, Any]]:
        out = ok(self.http.get(f"{API}/crews/{self.crew_id}/claims", headers=self.human))
        return list(out.get("claims") or out.get("items") or [])

    def stall(self, by: str, *, error: str = "billing_error", baton_ref: str | None = None) -> dict[str, Any]:
        body: dict[str, Any] = {"error": error, "error_details_class": "credits", "facts": {"uncommitted_files": []}}
        if baton_ref:
            body["baton_ref"] = baton_ref
        return ok(self.http.post(f"{API}/sessions/{self.seats[by].id}/stall", json=body, headers=self.seats[by].headers()))
