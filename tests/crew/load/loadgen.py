"""Crew load with concurrent memory ingestion (spec §13.4; WP-15).

    python -m tests.crew.load.loadgen --crews 50 --sessions 5 --duration 3600 [--out report.json]

Starts the crew-mode server (:mod:`tests.crew.e2e.server`, one owner and one admin key per crew) and
drives it the way crewd does, through crewd's own HTTP client (:class:`remembra.relay.crew.crewd.Api`):
every crew is one machine (host register, one heartbeat batch per host) with ``--sessions`` agent
sessions that auto-claim and release zones (``source=first_write``, the gate's row 17) and submit client
events, while memory ingestion (``POST /api/v1/memories``) runs at ``--ingest-rate`` per second across
the owners. Reaper sweeps are sampled every 10 s on top of the reaper's own 30 s loop, and a retention
pass over the whole event log runs under load near the end.

The §13.4 budgets are checked on the server's own timings (``/__e2e/metrics``: request start to
response end inside the server process):

* heartbeat p95 < 100 ms; auto-claim p95 < 300 ms;
* no ``database is locked`` and no 5xx;
* reaper sweep < 200 ms;
* no retention transaction holds ``crew.db`` for more than 50 ms.

Defaults compress the timing (heartbeat every 5 s instead of 60 s, a claim every ~3 s per session), so a
short run applies more load per minute than production. Exit status 0 when every budget holds.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import random
import secrets
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from remembra.relay.config import RelayConfig
from remembra.relay.crew.crewd import Api, Unreachable
from remembra.relay.crew.zonescompile import sha_of
from tests.crew.e2e.harness import ServerProc

BUDGETS = {"heartbeat_p95_ms": 100.0, "claim_p95_ms": 300.0, "sweep_max_ms": 200.0, "retention_tx_max_ms": 50.0}
HEARTBEAT_ROUTE = "POST /crew/heartbeat"  # route templates as the server records them
CLAIM_ROUTE = "POST /crews/{crew_id}/claims"
ZONES = ("pos", "reports", "billing", "inventory", "auth")


def zones_yaml() -> str:
    lines = ["version: 1", "zones:"]
    for z in ZONES:
        lines += [f"  {z}:", f"    title: {z.title()}", f"    include: [src/{z}/**]", "    mode: exclusive"]
    return "\n".join(lines) + "\n"


@dataclass
class Session:
    session_id: str
    token: str
    home_zone: int
    calls: int = 0


@dataclass
class Crew:
    index: int
    api: Api
    project: str
    crew_id: str = ""
    host_id: str = ""
    host_token: str = ""
    sessions: list[Session] = field(default_factory=list)
    zone_ids: list[str] = field(default_factory=list)


@dataclass
class Stats:
    attempted: dict[str, int] = field(default_factory=dict)
    client_ms: dict[str, list[float]] = field(default_factory=dict)
    statuses: dict[str, dict[int, int]] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)

    def add(self, name: str, ms: float, status: int) -> None:
        self.client_ms.setdefault(name, []).append(ms)
        bucket = self.statuses.setdefault(name, {})
        bucket[status] = bucket.get(status, 0) + 1


async def timed(stats: Stats, name: str, api: Api, method: str, path: str, **kw: Any) -> Any:
    started = time.perf_counter()
    stats.attempted[name] = stats.attempted.get(name, 0) + 1
    try:
        resp = await api.call(method, path, timeout=30.0, **kw)
    except Unreachable as e:
        stats.add(name, (time.perf_counter() - started) * 1000, 0)
        stats.errors.append(f"{name}: {e}")
        return None
    except Exception as e:
        stats.add(name, (time.perf_counter() - started) * 1000, 0)
        stats.errors.append(f"{name}: {type(e).__name__}")
        return None
    stats.add(name, (time.perf_counter() - started) * 1000, resp.status)
    if resp.status >= 500:
        stats.errors.append(f"{name}: HTTP {resp.status} {str(resp.body)[:200]}")
    return resp


async def setup_crew(crew: Crew, n_sessions: int, stats: Stats) -> None:
    reg = await timed(
        stats, "host_register", crew.api, "POST", "/crew/hosts/register",
        json_body={"host_label": f"load{crew.index}", "platform": "darwin", "crewd_version": "1.0.0"},
    )  # fmt: skip
    assert reg is not None and reg.ok, reg and reg.body
    crew.host_id, crew.host_token = reg.body["host_id"], reg.body["host_token"]
    for s in range(n_sessions):
        body = {
            "project_id": crew.project,
            "agent_id": "claude-code",
            "session_id": f"load-{crew.index}-{s}",
            "adapter": "claude-code",
            "client_kind": "hook",
            "host_id": crew.host_id,
            "checkout_fp": hashlib.sha256(f"{crew.index}:{s}".encode()).hexdigest()[:32],
            "worktree_id": f"wt{s}",
            "branch": f"work/{s}",
            "head": "a" * 40,
            "source": "startup",
        }
        joined = await timed(stats, "join", crew.api, "POST", "/crews/join", json_body=body, host_token=crew.host_token)
        assert joined is not None and joined.ok, joined and joined.body
        crew.crew_id = joined.body["crew_id"]
        crew.sessions.append(Session(joined.body["session_id"], joined.body["session_token"], s % len(ZONES)))
    text = zones_yaml()
    up = await timed(
        stats, "zones_file", crew.api, "PUT", f"/crews/{crew.crew_id}/zones/file",
        json_body={"yaml": text, "sha": sha_of(text), "branch": "main"}, session_token=crew.sessions[0].token,
    )  # fmt: skip
    assert up is not None and up.ok, up and up.body
    snap = await crew.api.call("GET", f"/crews/{crew.crew_id}/snapshot")
    by_slug = {z["slug"]: z["id"] for z in snap.body["zones"]}
    crew.zone_ids = [by_slug[z] for z in ZONES]


async def heartbeat_loop(crew: Crew, stats: Stats, stop: asyncio.Event, every: float) -> None:
    await asyncio.sleep(random.uniform(0, every))
    while not stop.is_set():
        items = [
            {
                "session_id": s.session_id,
                "token": s.token,
                "alive": True,
                "activity_age_s": random.randint(0, 20),
                "last_action": {"tool": "Edit", "age_s": 2, "path_rel": f"src/{ZONES[s.home_zone]}/x.ts"},
                "calls_since_checkpoint": s.calls,
                "footprints": [],
                "cursor": 0,
                "githook_state": "ok",
            }
            for s in crew.sessions
        ]
        await timed(
            stats, "heartbeat", crew.api, "POST", "/crew/heartbeat",
            json_body={"batch_id": "hb" + secrets.token_hex(8), "sessions": items}, host_token=crew.host_token,
        )  # fmt: skip
        try:
            await asyncio.wait_for(stop.wait(), timeout=every)
        except TimeoutError:
            pass


async def session_loop(crew: Crew, s: Session, stats: Stats, stop: asyncio.Event, claim_every: float) -> None:
    await asyncio.sleep(random.uniform(0, claim_every))
    while not stop.is_set():
        zone = s.home_zone if random.random() < 0.7 else random.randrange(len(ZONES))
        resp = await timed(
            stats, "claim", crew.api, "POST", f"/crews/{crew.crew_id}/claims",
            json_body={"zone_id": crew.zone_ids[zone], "mode": "exclusive", "wait": False, "source": "first_write"},
            session_token=s.token,
        )  # fmt: skip
        s.calls += 1
        if resp is not None and resp.status in (200, 201):
            claim_id = (resp.body or {}).get("claim", {}).get("id")
            ev = {
                "id": "ld" + secrets.token_hex(10),
                "type": "activity.commit",
                "age_s": 0,
                "payload": {
                    "sha": secrets.token_hex(20),
                    "subject_hash": secrets.token_hex(16),
                    "files": [f"src/{ZONES[zone]}/x.ts"],
                },
            }
            await timed(
                stats,
                "events",
                crew.api,
                "POST",
                f"/crews/{crew.crew_id}/events",
                json_body={"events": [ev]},
                session_token=s.token,
            )
            await asyncio.sleep(random.uniform(0.5, claim_every))
            if claim_id:
                await timed(
                    stats, "release", crew.api, "POST", f"/claims/{claim_id}/release",
                    json_body={"baton": False}, session_token=s.token,
                )  # fmt: skip
        try:
            await asyncio.wait_for(stop.wait(), timeout=random.uniform(0.5, claim_every))
        except TimeoutError:
            pass


async def ingest_loop(crews: list[Crew], stats: Stats, stop: asyncio.Event, rate: float) -> None:
    if rate <= 0:
        return
    words = [
        "invoice",
        "gct",
        "rounding",
        "tender",
        "split",
        "receipt",
        "ledger",
        "vendor",
        "stock",
        "pallet",
        "dozen",
        "case",
        "wholesale",
    ]
    n = 0
    pending: set[asyncio.Task[Any]] = set()
    try:
        while not stop.is_set():
            crew = crews[n % len(crews)]
            n += 1
            content = f"Load memory {n}: " + " ".join(random.choice(words) for _ in range(12))
            child = asyncio.create_task(
                timed(
                    stats,
                    "memory_store",
                    crew.api,
                    "POST",
                    "/memories",
                    json_body={"content": content, "project_id": crew.project},
                )
            )
            pending.add(child)
            child.add_done_callback(pending.discard)
            try:
                await asyncio.wait_for(stop.wait(), timeout=1.0 / rate)
            except TimeoutError:
                pass
    finally:
        # Observe every child before metrics/HTTP clients/server are closed.
        # Each request has a 30-second timeout; allow a bounded drain beyond it.
        remaining = list(pending)
        if remaining:
            try:
                outcomes = await asyncio.wait_for(asyncio.gather(*remaining, return_exceptions=True), timeout=35.0)
                for outcome in outcomes:
                    if isinstance(outcome, BaseException):
                        stats.errors.append(f"memory_store worker: {type(outcome).__name__}")
            except TimeoutError:
                stats.errors.append("memory_store: drain timeout")
                for child in remaining:
                    child.cancel()
                await asyncio.gather(*remaining, return_exceptions=True)


async def sweep_loop(admin: Api, stats: Stats, stop: asyncio.Event) -> None:
    while not stop.is_set():
        try:
            await asyncio.wait_for(stop.wait(), timeout=10)
        except TimeoutError:
            await timed(stats, "sweep", admin, "POST", "/__e2e/sweep", root=True)


async def run(args: argparse.Namespace) -> dict[str, Any]:
    random.seed(args.seed)
    workdir = Path(args.workdir or tempfile.mkdtemp(prefix="crew-load-"))
    server = ServerProc(workdir / "server", seed_users=args.crews, qdrant_url=os.environ.get("REMEMBRA_E2E_QDRANT_URL"))
    stats = Stats()
    stop = asyncio.Event()
    tasks: list[asyncio.Task[Any]] = []
    crews: list[Crew] = []
    admin: Api | None = None
    phase = "startup"
    failure: dict[str, str] | None = None
    metrics: dict[str, Any] = {}
    retention: dict[str, Any] | None = None
    retention_task: asyncio.Task[Any] | None = None
    elapsed_workload_s: float | None = None
    try:
        server.start()
        phase = "setup"
        keys: list[str] = list(server.info["keys"])
        crews = [
            Crew(
                i,
                Api(RelayConfig(url=server.url, api_key=keys[i], agent_id="claude-code", source="load"), agent_id="claude-code"),
                f"load-{i}",
            )
            for i in range(args.crews)
        ]
        admin = Api(RelayConfig(url=server.url, api_key=server.key, agent_id=None, source="load"), agent_id=None)
        tasks = [asyncio.create_task(setup_crew(c, args.sessions, stats)) for c in crews]
        await asyncio.gather(*tasks)
        reset = await admin.call("POST", "/__e2e/metrics/reset", root=True)
        if not reset.ok:
            raise RuntimeError(f"metrics reset failed: HTTP {reset.status}")
        phase = "workload"
        tasks = [asyncio.ensure_future(heartbeat_loop(c, stats, stop, args.heartbeat_s)) for c in crews]
        tasks += [asyncio.ensure_future(session_loop(c, s, stats, stop, args.claim_s)) for c in crews for s in c.sessions]
        tasks.append(asyncio.ensure_future(ingest_loop(crews, stats, stop, args.ingest_rate)))
        tasks.append(asyncio.ensure_future(sweep_loop(admin, stats, stop)))
        started = time.monotonic()
        while time.monotonic() - started < args.duration:
            await asyncio.sleep(1.0)
            if retention_task is None and time.monotonic() - started >= args.duration * 0.8:
                retention_task = asyncio.create_task(
                    admin.call("POST", "/__e2e/retention", root=True, params={"days": 400}, timeout=600)
                )
        stop.set()
        elapsed_workload_s = time.monotonic() - started
        outcomes = await asyncio.gather(*tasks, return_exceptions=True)
        for outcome in outcomes:
            if isinstance(outcome, BaseException):
                stats.errors.append(f"load worker: {type(outcome).__name__}")
        if retention_task is not None:
            resp = await retention_task
            retention = resp.body if resp.ok else {"error": resp.status, "body": resp.body}
        metrics = (await admin.call("GET", "/__e2e/metrics", root=True)).body
    except Exception as exc:
        # Preserve partial counts and transport errors even when setup never
        # reaches the workload. Do not retry or turn an aborted run into a pass.
        failure = {"phase": phase, "exception": type(exc).__name__}
        stats.errors.append(f"{phase}: {type(exc).__name__}")
    finally:
        stop.set()
        if retention_task is not None and not retention_task.done():
            retention_task.cancel()
            await asyncio.gather(retention_task, return_exceptions=True)
        try:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            for c in crews:
                await c.api.close()
            if admin is not None:
                await admin.close()
        finally:
            server.stop()
    report = summarize(args, metrics, retention or {}, stats)
    report["elapsed_workload_s"] = elapsed_workload_s
    report["phase"] = phase if failure else "complete"
    if failure:
        report["failure"] = failure
        report["checks"]["run_completed"] = False
        report["ok"] = False
    return report


def _p(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    return round(ordered[min(len(ordered) - 1, int(round(q * (len(ordered) - 1))))], 2)


def summarize(args: argparse.Namespace, metrics: dict[str, Any], retention: dict[str, Any], stats: Stats) -> dict[str, Any]:
    routes = metrics.get("routes") or {}
    hb = routes.get(HEARTBEAT_ROUTE) or {}
    claim = routes.get(CLAIM_ROUTE) or {}
    five_xx = {k: {s: n for s, n in v["statuses"].items() if int(s) >= 500} for k, v in routes.items()}
    five_xx = {k: v for k, v in five_xx.items() if v}
    sweeps = [x for x in metrics.get("sweeps_ms") or []]
    report = {
        "config": {k: getattr(args, k) for k in ("crews", "sessions", "duration", "heartbeat_s", "claim_s", "ingest_rate")},
        "server": {
            "vector_backend": metrics.get("vector_backend", "unknown"),
            "embedding_backend": metrics.get("embedding_backend", "unknown"),
            "heartbeat": hb,
            "claim": claim,
            "memory_store": routes.get("POST /memories") or {},
            "events": routes.get("POST /crews/{crew_id}/events") or {},
            "sweeps": {"count": len(sweeps), "max_ms": max(sweeps) if sweeps else 0.0},
            "tx_holds": metrics.get("tx_holds", {}),
            "tx_waits": metrics.get("tx_waits", {}),
            "sweep_steps": metrics.get("sweep_steps", {}),
            "database_locked": metrics.get("database_locked", 0),
            "database_locked_samples": metrics.get("database_locked_samples", []),
            "five_xx": five_xx,
            "errors": metrics.get("errors", []),
            "routes": {k: {"count": v["count"], "p95_ms": v["p95_ms"]} for k, v in routes.items()},
        },
        "retention": retention,
        "client": {name: {"count": len(v), "p95_ms": _p(v, 0.95), "max_ms": _p(v, 1.0)} for name, v in stats.client_ms.items()},
        "client_statuses": stats.statuses,
        "client_errors": stats.errors[:20],
        "requests": {
            "attempted": dict(stats.attempted),
            "completed": {name: len(values) for name, values in stats.client_ms.items()},
        },
    }
    completed = report["requests"]["completed"]
    allowed_statuses = {
        "host_register": {200, 201},
        "join": {200, 201},
        "zones_file": {200, 201},
        "heartbeat": {200},
        "claim": {200, 201, 202, 409, 423},
        "events": {200, 201},
        "release": {200, 204},
        "sweep": {200},
        "memory_store": {201},
    }
    unexpected_statuses = {
        name: {str(status): count for status, count in statuses.items() if status not in allowed_statuses.get(name, set())}
        for name, statuses in stats.statuses.items()
    }
    unexpected_statuses = {name: statuses for name, statuses in unexpected_statuses.items() if statuses}
    report["unexpected_client_statuses"] = unexpected_statuses
    memory = report["server"]["memory_store"]
    checks = {
        "heartbeat_p95": bool(hb) and hb["p95_ms"] < BUDGETS["heartbeat_p95_ms"],
        "claim_p95": bool(claim) and claim["p95_ms"] < BUDGETS["claim_p95_ms"],
        "no_database_locked": report["server"]["database_locked"] == 0,
        "no_5xx": not five_xx,
        "sweep_max": bool(sweeps) and max(sweeps) < BUDGETS["sweep_max_ms"],
        "retention_tx_max": bool(retention) and not retention.get("error")
        and float(retention.get("max_tx_ms") or 0) <= BUDGETS["retention_tx_max_ms"],
        "memory_ingested": args.ingest_rate <= 0 or (
            memory.get("count", 0) >= max(1, args.duration * args.ingest_rate * 0.8)
            and memory.get("statuses") == {"201": memory.get("count")}
            and memory.get("count") == completed.get("memory_store", 0)
        ),
        "heartbeat_workload": hb.get("count", 0) >= args.crews * (args.duration / args.heartbeat_s) * 0.8,
        "claim_workload": claim.get("count", 0) > args.crews * args.sessions,
        "requests_completed": bool(stats.attempted) and stats.attempted == completed,
        "requested_vector_backend": metrics.get("vector_backend") == args.vector_backend,
        "no_client_errors": not stats.errors,
        "no_server_errors": not report["server"]["errors"],
        "expected_client_statuses": not unexpected_statuses,
        "retention_complete": retention.get("transactions", 0) > 0
            and retention.get("events_pruned", 0) > 0 and retention.get("chain_errors") == {},
    }  # fmt: skip
    report["checks"] = checks
    report["ok"] = all(checks.values())
    return report


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Crew load with concurrent memory ingestion (§13.4)")
    p.add_argument("--crews", type=int, default=50)
    p.add_argument("--sessions", type=int, default=5)
    p.add_argument("--duration", type=float, default=3600.0, help="seconds of steady load")
    p.add_argument("--heartbeat-s", type=float, default=5.0, help="host heartbeat interval (crewd: 60 s)")
    p.add_argument("--claim-s", type=float, default=3.0, help="mean seconds between a session's auto-claims")
    p.add_argument("--ingest-rate", type=float, default=10.0, help="memory stores per second (all owners)")
    p.add_argument("--seed", type=int, default=15)
    p.add_argument(
        "--vector-backend",
        choices=["in-process-local", "isolated-server"],
        default="isolated-server" if os.environ.get("REMEMBRA_E2E_QDRANT_URL") else "in-process-local",
    )
    p.add_argument("--workdir")
    p.add_argument("--out")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    report = asyncio.run(run(args))
    text = json.dumps(report, indent=2, default=str)
    if args.out:
        Path(args.out).write_text(text)
    print(text)
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
