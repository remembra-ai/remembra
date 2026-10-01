"""The load gate must reject failed or incomplete work, not only fast responses."""

from __future__ import annotations


import asyncio
from types import SimpleNamespace

import pytest

from tests.crew.load import loadgen as L


def healthy_report():
    args = L.parse_args(["--crews", "1", "--sessions", "1", "--duration", "10", "--ingest-rate", "5"])
    metrics = {
        "routes": {
            L.HEARTBEAT_ROUTE: {"count": 3, "p95_ms": 10.0, "statuses": {"200": 3}},
            L.CLAIM_ROUTE: {"count": 10, "p95_ms": 20.0, "statuses": {"201": 10}},
            "POST /memories": {"count": 50, "p95_ms": 5.0, "statuses": {"201": 50}},
        },
        "sweeps_ms": [5.0],
    }
    stats = L.Stats()
    for name, count, status in [("heartbeat", 3, 200), ("claim", 10, 201), ("memory_store", 50, 201)]:
        for _ in range(count):
            stats.add(name, 10.0, status)
    if hasattr(stats, "attempted"):
        stats.attempted = {name: len(values) for name, values in stats.client_ms.items()}
    return args, metrics, {"max_tx_ms": 5.0, "transactions": 1, "events_pruned": 1, "chain_errors": {}}, stats


def test_healthy_load_can_pass():
    args, metrics, retention, stats = healthy_report()
    assert L.summarize(args, metrics, retention, stats)["ok"]


@pytest.mark.parametrize("failure", ["client_error", "rejected_store", "missing_heartbeats", "retention_chain"])
def test_load_gate_rejects_failed_work_even_when_latency_is_good(failure):
    args, metrics, retention, stats = healthy_report()
    if failure == "client_error":
        stats.errors.append("heartbeat: connection failed")
    elif failure == "rejected_store":
        metrics["routes"]["POST /memories"]["statuses"] = {"400": 50}
        stats.statuses["memory_store"] = {400: 50}
    elif failure == "missing_heartbeats":
        metrics["routes"][L.HEARTBEAT_ROUTE]["count"] = 1
    else:
        retention["chain_errors"] = {"crew": "broken chain"}
    assert not L.summarize(args, metrics, retention, stats)["ok"]


def test_incomplete_client_requests_cannot_pass():
    args, metrics, retention, stats = healthy_report()
    stats.attempted["memory_store"] += 1
    report = L.summarize(args, metrics, retention, stats)
    assert not report["ok"] and not report["checks"]["requests_completed"]


async def test_ingestion_shutdown_waits_for_its_inflight_request():
    started, finish = asyncio.Event(), asyncio.Event()
    stop = asyncio.Event()

    class Api:
        async def call(self, *_args, **_kwargs):
            started.set()
            await finish.wait()
            return SimpleNamespace(status=201, body={})

    stats = L.Stats()
    worker = asyncio.create_task(L.ingest_loop([SimpleNamespace(api=Api(), project="p")], stats, stop, rate=1))
    try:
        await asyncio.wait_for(started.wait(), timeout=2)
        stop.set()
        await asyncio.sleep(0.01)
        assert not worker.done()  # report cannot be taken while a store is still running
        finish.set()
        await asyncio.wait_for(worker, timeout=2)
        assert stats.attempted == {"memory_store": 1}
        assert stats.statuses == {"memory_store": {201: 1}}
        assert len(stats.client_ms["memory_store"]) == 1
    finally:
        stop.set()
        finish.set()
        await worker


async def test_unexpected_client_failure_is_recorded_without_a_success():
    class Api:
        async def call(self, *_args, **_kwargs):
            raise ValueError("synthetic parse failure")

    stats = L.Stats()
    assert await L.timed(stats, "heartbeat", Api(), "POST", "/crew/heartbeat") is None
    assert stats.statuses == {"heartbeat": {0: 1}}
    assert stats.errors == ["heartbeat: ValueError"]
