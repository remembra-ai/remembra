"""The load gate must reject failed or incomplete work, not only fast responses."""

from __future__ import annotations


import asyncio
import json
from types import SimpleNamespace

import pytest

from tests.crew.load import loadgen as L


def healthy_report():
    args = L.parse_args(["--crews", "1", "--sessions", "1", "--duration", "10", "--ingest-rate", "5"])
    metrics = {
        "vector_backend": "in-process-local",
        "event_loop_backend": "uvloop",
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


def test_instrumented_cpu_profile_cannot_certify_capacity():
    args, metrics, retention, stats = healthy_report()
    metrics["profiled"] = True
    report = L.summarize(args, metrics, retention, stats)
    assert report["server"]["profiled"]
    assert not report["checks"]["unprofiled_server"] and not report["ok"]
    assert all(value for key, value in report["checks"].items() if key != "unprofiled_server")


def test_local_report_cannot_satisfy_requested_server_backend():
    args, metrics, retention, stats = healthy_report()
    args.vector_backend = "isolated-server"
    report = L.summarize(args, metrics, retention, stats)
    assert not report["checks"]["requested_vector_backend"] and not report["ok"]


def test_asyncio_report_cannot_satisfy_requested_production_loop():
    args, metrics, retention, stats = healthy_report()
    metrics["event_loop_backend"] = "asyncio"
    report = L.summarize(args, metrics, retention, stats)
    assert not report["checks"]["requested_event_loop"] and not report["ok"]


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


def test_setup_failure_is_written_before_nonzero_exit(monkeypatch, tmp_path, capsys):
    closed, stopped = [], []

    class Server:
        def __init__(self, *_args, **_kwargs):
            self.info = {"keys": ["synthetic"]}
            self.url, self.key = "http://example.invalid", "synthetic"

        def start(self):
            return self

        def stop(self):
            stopped.append(True)

    class Api:
        def __init__(self, *_args, **_kwargs):
            pass

        async def call(self, *_args, **_kwargs):
            raise L.Unreachable("synthetic transport timeout")

        async def close(self):
            closed.append(True)

    monkeypatch.setattr(L, "ServerProc", Server)
    monkeypatch.setattr(L, "Api", Api)
    out = tmp_path / "report.json"
    assert L.main(["--crews", "1", "--sessions", "5", "--duration", "3600", "--out", str(out), "--workdir", str(tmp_path)]) == 1
    report = json.loads(out.read_text())
    assert report["ok"] is False
    assert report["phase"] == "setup"
    assert report["failure"] == {"phase": "setup", "exception": "AssertionError"}
    assert report["config"]["duration"] == 3600 and report["config"]["sessions"] == 5
    assert report["requests"]["attempted"] == {"host_register": 1}
    assert report["client_statuses"] == {"host_register": {"0": 1}}
    assert "synthetic transport timeout" in report["client_errors"][0]
    assert len(closed) == 2 and stopped == [True]
    assert json.loads(capsys.readouterr().out) == report
