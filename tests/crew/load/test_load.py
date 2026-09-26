"""§13.4 load budgets at CI scale (WP-15): ``tests.crew.load.loadgen`` for a minute.

10 crews × 5 sessions with heartbeats every 5 s, an auto-claim every ~3 s per session and memory ingestion
at 5 stores/s, then a retention pass under load. The budgets are the spec's (server-side timings):
heartbeat p95 < 100 ms, auto-claim p95 < 300 ms, no ``database is locked``, no 5xx, reaper sweep < 200 ms,
no retention transaction held > 50 ms. The full run is ``python -m tests.crew.load.loadgen`` (50 × 5, 1 h;
the crew-e2e workflow runs it nightly). Runs with ``REMEMBRA_CREW_SLOW=1``.
"""

from __future__ import annotations

import asyncio
import json
import os

import pytest

from tests.crew.load import loadgen

pytestmark = pytest.mark.skipif(os.environ.get("REMEMBRA_CREW_SLOW") != "1", reason="load: set REMEMBRA_CREW_SLOW=1")


def test_crew_load_budgets_with_memory_ingestion(tmp_path):
    crews = int(os.environ.get("REMEMBRA_LOAD_CREWS", "10"))
    duration = os.environ.get("REMEMBRA_LOAD_DURATION_S", "60")
    args = loadgen.parse_args(
        ["--crews", str(crews), "--sessions", "5", "--duration", duration, "--ingest-rate", "5", "--workdir", str(tmp_path)]
    )
    report = asyncio.run(loadgen.run(args))
    (tmp_path / "load-report.json").write_text(json.dumps(report, indent=2, default=str))
    server = report["server"]
    assert report["checks"] == {k: True for k in report["checks"]}, json.dumps(report, indent=2, default=str)[:6000]
    assert server["heartbeat"]["count"] >= crews * (float(duration) / 5) * 0.8
    assert server["claim"]["count"] > crews * 5
    assert report["retention"]["events_pruned"] > 0 and report["retention"]["transactions"] > 0
    assert report["retention"]["chain_errors"] == {}, report["retention"]  # verify under live writes
    assert server["memory_store"]["statuses"] == {"201": server["memory_store"]["count"]}
