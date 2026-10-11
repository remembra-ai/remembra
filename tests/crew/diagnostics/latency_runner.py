"""One bounded, offline-after-setup Crew latency comparison; never a release waiver.

The unchanged load test executes four times in fresh processes, once per planned
cell. Observers are process-local and restored. Only allowlisted numeric data is
published; raw test databases, server logs and generated identities stay private
to the ephemeral runner. ``self-test`` runs without starting the product or load.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import contextvars
import functools
import gc
import hashlib
import importlib.metadata
import ipaddress
import json
import math
import os
import platform
import random
import signal
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

BASE = "cb6c2f4447972a6fd976c38c63256f741aff76a2"
FIX = "8d6481dd90c48d3a2b4ae6e717d34cceaf19fcaf"
HEADS = (BASE, FIX)
SOURCE_FILES = {
    "tests/crew/e2e/harness.py": "1c44f51a3698cf821bf27d5b80e6bb5075d62eb12bbde4a342cbed916f33e5fa",
    "tests/crew/load/test_load.py": "42293aea5250623d744b6acd1871b28ab0eb28b01be07a9df7c878f9eb432953",
    "tests/crew/e2e/server.py": "ad7fcd430fdeb64291fa033059ccf8cc39a1e7260c98639f33558ebaf8c4d036",
    "tests/crew/load/loadgen.py": "99c083e320c30eb425af89be8c131efbbaf4fddf89a55bf0438168d0d32ecd9f",
    "src/remembra/crew/sessions.py": "4acddd96ad1ba2f15bb9a3d73a5295a9772c8fda3582901debd350d5380449e2",
    "src/remembra/crew/reaper.py": "7e2ff6c51519c607179beb7041dc4e6803ce1710b51d3341d11854f82b10b7ad",
    "src/remembra/storage/sqlite_tx.py": "60da44323a8e1f3be962d13c7618d06f26a1bfea63ac4c68cba9865283f515b3",
}
# Exact versions reported by the failed macOS E2E job, not the separately audited uv lock.
RESOLVED_PACKAGES = {
    "aiosqlite": "0.22.1",
    "annotated-doc": "0.0.5",
    "annotated-types": "0.8.0",
    "anyio": "4.15.1",
    "ast-serialize": "0.12.1",
    "attrs": "26.1.0",
    "backports.tarfile": "1.2.0",
    "bcrypt": "5.0.0",
    "build": "1.6.1",
    "cffi": "2.1.1",
    "charset_normalizer": "3.5.2",
    "click": "8.5.0",
    "coverage": "7.16.2",
    "cryptography": "50.0.2",
    "deprecated": "1.3.1",
    "distro": "1.9.0",
    "dnspython": "2.9.0",
    "docutils": "0.23",
    "email-validator": "2.3.0",
    "fastapi": "0.143.0",
    "grpcio": "1.84.0",
    "h11": "0.16.0",
    "h2": "4.4.1",
    "hpack": "4.2.0",
    "httpcore": "1.0.9",
    "httptools": "0.9.0",
    "httpx": "0.28.1",
    "httpx-sse": "0.4.3",
    "hyperframe": "6.1.0",
    "id": "1.6.1",
    "idna": "3.20",
    "importlib_metadata": "9.0.1",
    "iniconfig": "2.3.1",
    "jaraco.classes": "3.4.0",
    "jaraco.context": "6.1.2",
    "jaraco.functools": "4.6.0",
    "jiter": "0.17.0",
    "jsonschema": "4.26.0",
    "jsonschema-specifications": "2025.9.1",
    "keyring": "25.7.0",
    "librt": "0.16.0",
    "limits": "5.8.0",
    "markdown-it-py": "4.2.0",
    "mcp": "1.30.0",
    "mdurl": "0.1.2",
    "more-itertools": "11.1.0",
    "mypy": "2.4.0",
    "mypy_extensions": "1.1.0",
    "nh3": "0.3.7",
    "numpy": "2.4.6",
    "openai": "2.54.0",
    "opentelemetry-api": "1.45.1",
    "packaging": "26.3",
    "pathspec": "1.1.1",
    "pillow": "12.3.0",
    "pluggy": "1.6.0",
    "portalocker": "3.2.0",
    "protobuf": "7.36.2",
    "pycparser": "3.11",
    "pydantic": "2.14.0",
    "pydantic-core": "2.50.0",
    "pydantic-settings": "2.15.0",
    "pygments": "2.21.0",
    "pyjwt": "2.15.1",
    "pyotp": "2.10.0",
    "pyproject_hooks": "1.3.3",
    "pytest": "9.1.1",
    "pytest-asyncio": "1.4.0",
    "pytest-cov": "7.1.0",
    "python-dotenv": "1.2.4",
    "python-multipart": "0.0.32",
    "python-ulid": "4.0.1",
    "pyyaml": "6.0.3",
    "qdrant-client": "1.19.1",
    "qrcode": "8.2",
    "readme-renderer": "46.0",
    "redis": "8.1.0",
    "referencing": "0.37.0",
    "regex": "2026.9.29",
    "remembra": "0.17.0",
    "requests": "2.34.2",
    "requests-toolbelt": "1.0.0",
    "resend": "2.49.1",
    "rfc3986": "2.0.0",
    "rich": "15.0.0",
    "rpds-py": "2026.9.1",
    "ruff": "0.17.0",
    "slowapi": "0.1.10",
    "sniffio": "1.3.1",
    "sse-starlette": "3.5.0",
    "starlette": "1.7.0",
    "structlog": "26.1.0",
    "tiktoken": "0.14.0",
    "tqdm": "4.70.1",
    "twine": "7.0.0",
    "types-dateparser": "1.4.3.20260907",
    "typing-extensions": "4.16.0",
    "typing-inspection": "0.4.4",
    "urllib3": "2.8.0",
    "uvicorn": "0.54.0",
    "uvloop": "0.23.0",
    "watchfiles": "1.3.0",
    "websockets": "17.2",
    "wrapt": "2.5.0",
    "zipp": "4.1.1",
}
MAX_SPANS = 200000
ROUTES = {
    "POST /crew/heartbeat": "heartbeat",
    "POST /crews/{crew_id}/claims": "claim",
    "POST /claims/{claim_id}/release": "release",
    "POST /crews/{crew_id}/events": "events",
    "POST /memories": "memory_store",
}
LABELS = {
    "heartbeat",
    "session",
    "sweep",
    "consolidate",
    "factory",
    "fallback",
    "loop_lag",
    "transaction",
    "tx_enter",
    "tx_body",
    "tx_exit",
    "reaper_hosts",
    "reaper_presence",
    "reaper_leases",
    "reaper_idle_park",
    "reaper_reservations",
    "reaper_close_lost",
    "reaper_alarms",
}


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, allow_nan=False) + "\n")


def git(root, *args):
    return subprocess.check_output(["git", "-C", str(root), *args], text=True).strip()


def verify_source(root, head):
    if head not in HEADS or git(root, "rev-parse", "HEAD") != head or git(root, "status", "--porcelain"):
        raise RuntimeError("Source checkout must be clean at the pinned comparison commit")
    for name, digest in SOURCE_FILES.items():
        if hashlib.sha256((root / name).read_bytes()).hexdigest() != digest:
            raise RuntimeError("Pinned workload source changed")


def environment():
    return {
        "python": platform.python_version(),
        "platform": platform.platform(),
        "machine": platform.machine(),
        "sqlite": sqlite3.sqlite_version,
        "gc_enabled": gc.isenabled(),
        "gc_threshold": list(gc.get_threshold()),
        "packages": {name: importlib.metadata.version(name) for name in RESOLVED_PACKAGES},
    }


def clean_env(home):
    # An allowlist also removes CI tokens, external provider credentials and proxies.
    env = {key: os.environ[key] for key in ("PATH", "TMPDIR", "LANG", "LC_ALL", "SYSTEMROOT") if key in os.environ}
    env.update(
        HOME=str(home),
        PYTHONDONTWRITEBYTECODE="1",
        PYTHONUNBUFFERED="1",
        REMEMBRA_TYPESAFE_MODE="off",
        REMEMBRA_CREW_SLOW="1",
        REMEMBRA_LOAD_CREWS="10",
        REMEMBRA_LOAD_DURATION_S="60",
    )
    return env


class LoopbackOnly:
    """Block non-loopback Python INET traffic; not a claim of an OS firewall."""

    def __init__(self):
        self.denied = 0
        self.originals = []

    def check(self, sock, address):
        if sock.family not in (socket.AF_INET, socket.AF_INET6):
            return
        try:
            local = ipaddress.ip_address(address[0]).is_loopback
        except (ValueError, TypeError, IndexError):
            local = False
        if not local:
            self.denied += 1
            raise OSError("Diagnostic permits loopback only")

    def __enter__(self):
        owner = self
        for name in ("connect", "connect_ex", "sendto"):
            original = getattr(socket.socket, name)

            def wrapper(sock, *args, _name=name, _original=original, **kwargs):
                owner.check(sock, args[-1] if _name == "sendto" else args[0])
                return _original(sock, *args, **kwargs)

            self.originals.append((name, original))
            setattr(socket.socket, name, wrapper)
        self.dns = socket.getaddrinfo

        def lookup(host, *args, **kwargs):
            if host is not None:
                text = host.decode("ascii") if isinstance(host, bytes) else host
                if text == "localhost":
                    host = "127.0.0.1"
                else:
                    owner.check(SimpleNamespace(family=socket.AF_INET), (text, 0))
            return self.dns(host, *args, **kwargs)

        socket.getaddrinfo = lookup
        return self

    def __exit__(self, *_):
        socket.getaddrinfo = self.dns
        for name, original in reversed(self.originals):
            setattr(socket.socket, name, original)


class Recorder:
    """Bounded numeric inclusive spans; child spans must not be summed with parents."""

    def __init__(self, limit=MAX_SPANS):
        self.limit = limit
        self.spans = []
        self.overflow = 0
        self.active = False
        self.serial = 0
        self.parent = contextvars.ContextVar("latency_parent", default=0)
        self.origin = time.perf_counter_ns()

    @contextlib.contextmanager
    def span(self, label):
        if label not in LABELS:
            raise ValueError("Unknown diagnostic label")
        if not self.active:
            yield
            return
        self.serial += 1
        ident, parent = self.serial, self.parent.get()
        token = self.parent.set(ident)
        start = time.perf_counter_ns() - self.origin
        failed = False
        try:
            yield
        except BaseException:
            failed = True
            raise
        finally:
            end = time.perf_counter_ns() - self.origin
            self.parent.reset(token)
            if len(self.spans) < self.limit:
                self.spans.append([ident, parent, label, start, end, failed])
            else:
                self.overflow += 1

    async def lag(self):
        while True:
            expected = time.perf_counter_ns() + 10000000
            await asyncio.sleep(0.01)
            if self.active:
                self.serial += 1
                if len(self.spans) < self.limit:
                    now = time.perf_counter_ns()
                    self.spans.append(
                        [self.serial, 0, "loop_lag", expected - self.origin, max(expected, now) - self.origin, False]
                    )
                else:
                    self.overflow += 1


def async_wrapper(original, recorder, label):
    @functools.wraps(original)
    async def wrapped(*args, **kwargs):
        with recorder.span(label):
            return await original(*args, **kwargs)

    return wrapped


def sync_wrapper(original, recorder, label):
    @functools.wraps(original)
    def wrapped(*args, **kwargs):
        with recorder.span(label):
            return original(*args, **kwargs)

    return wrapped


class TimedContext:
    """Transparent context delegation, including exception suppression/cancellation."""

    def __init__(self, inner, recorder):
        self.inner, self.recorder = inner, recorder

    async def __aenter__(self):
        self.whole = self.recorder.span("transaction")
        self.whole.__enter__()
        try:
            with self.recorder.span("tx_enter"):
                result = await self.inner.__aenter__()
        except BaseException:
            self.whole.__exit__(*sys.exc_info())
            raise
        self.body = self.recorder.span("tx_body")
        self.body.__enter__()
        return result

    async def __aexit__(self, kind, value, trace):
        self.body.__exit__(kind, value, trace)
        exit_info = (kind, value, trace)
        try:
            with self.recorder.span("tx_exit"):
                return await self.inner.__aexit__(kind, value, trace)
        except BaseException:
            exit_info = sys.exc_info()
            raise
        finally:
            self.whole.__exit__(*exit_info)


def numeric_report(report):
    """Retain fixed numeric fields; never publish error strings or arbitrary labels."""

    def number(value):
        return value if type(value) in (int, float) and math.isfinite(value) else None

    def metrics(item):
        return {
            key: item[key] for key in ("count", "p50_ms", "p95_ms", "p99_ms", "max_ms") if isinstance(item.get(key), (int, float))
        }

    server = report.get("server", {})
    return {
        "config": {
            k: number(report.get("config", {}).get(k))
            for k in ("crews", "sessions", "duration", "heartbeat_s", "claim_s", "ingest_rate")
        },
        "server": {k: metrics(server.get(k, {})) for k in ("heartbeat", "claim", "memory_store", "events", "sweeps")},
        "checks": {
            k: report.get("checks", {}).get(k) is True
            for k in (
                "heartbeat_p95",
                "claim_p95",
                "no_database_locked",
                "no_5xx",
                "sweep_max",
                "retention_tx_max",
                "memory_ingested",
            )
        },
        "retention": {
            k: number(report.get("retention", {}).get(k)) for k in ("events_pruned", "transactions", "ms", "max_tx_ms")
        },
        "server_error_count": len(server.get("errors", [])),
        "client_error_count": len(report.get("client_errors", [])),
        "chain_error_count": len(report.get("retention", {}).get("chain_errors", {})),
        "ok": report.get("ok") is True,
    }


def raw_metrics(metrics):
    return {
        "request_ms": {label: [x * 1000 for x in metrics.durations.get(route, [])] for route, label in ROUTES.items()},
        "sweep_ms": [x * 1000 for x in metrics.sweeps],
        "transaction_body_ms": {key: [x * 1000 for x in metrics.tx_holds.get(key, [])] for key in ("all", "retention")},
        "http_error_count": len(metrics.errors),
        "database_locked_count": metrics.locked.count,
    }


def server_mode(args, rest):
    from tests.crew.e2e import server

    from remembra.crew.db import CrewDatabase
    from remembra.crew.reaper import CrewReaper
    from remembra.crew.sessions import CrewSessions
    from remembra.extraction import consolidator

    recorder = Recorder()
    changed = []
    lag_task = None
    cpu_start = time.process_time_ns()
    original_gc = (gc.isenabled(), gc.get_threshold())

    def replace(obj, name, replacement):
        changed.append((obj, name, getattr(obj, name)))
        setattr(obj, name, replacement)

    if args.instrument:
        for name, label in (("heartbeat", "heartbeat"), ("_heartbeat_session", "session")):
            replace(CrewSessions, name, async_wrapper(getattr(CrewSessions, name), recorder, label))
        for name in ("sweep", "_hosts", "_presence", "_leases", "_idle_park", "_reservations", "_close_lost", "_alarms"):
            replace(
                CrewReaper,
                name,
                async_wrapper(getattr(CrewReaper, name), recorder, "sweep" if name == "sweep" else "reaper" + name),
            )
        replace(
            consolidator.MemoryConsolidator,
            "consolidate",
            async_wrapper(consolidator.MemoryConsolidator.consolidate, recorder, "consolidate"),
        )
        replace(consolidator, "make_llm_client", sync_wrapper(consolidator.make_llm_client, recorder, "factory"))
        replace(
            consolidator.MemoryConsolidator,
            "_default_add",
            sync_wrapper(consolidator.MemoryConsolidator._default_add, recorder, "fallback"),
        )
        transaction = CrewDatabase.transaction

        @functools.wraps(transaction)
        def timed_transaction(self, *values, **kwargs):
            return TimedContext(transaction(self, *values, **kwargs), recorder)

        replace(CrewDatabase, "transaction", timed_transaction)

    reset = server.METRICS.reset

    def reset_metrics():
        nonlocal lag_task, cpu_start
        reset()
        recorder.spans.clear()
        recorder.overflow = 0
        recorder.origin = time.perf_counter_ns()
        recorder.active = bool(args.instrument)
        cpu_start = time.process_time_ns()
        if args.instrument and lag_task is None:
            lag_task = asyncio.create_task(recorder.lag())

    replace(server.METRICS, "reset", reset_metrics)
    try:
        with LoopbackOnly() as guard:
            result = server.main(rest)
    finally:
        recorder.active = False
        for obj, name, original in reversed(changed):
            setattr(obj, name, original)
        save(
            args.output,
            {
                "instrumented": bool(args.instrument),
                "span_fields": ["id", "parent", "label", "start_ns", "end_ns", "failed"],
                "spans": recorder.spans,
                "overflow": recorder.overflow,
                "metrics": raw_metrics(server.METRICS),
                "denied_network_attempts": guard.denied,
                "cpu_ns_process_wide": time.process_time_ns() - cpu_start,
                "gc_unchanged": original_gc == (gc.isenabled(), gc.get_threshold()),
                "inclusive_spans_not_additive": True,
            },
        )
    return result


def rewrite_server(argv, script, output, instrument):
    if len(argv) < 3 or argv[:3] != [sys.executable, "-m", "tests.crew.e2e.server"]:
        raise RuntimeError("Unexpected child command; diagnostic stopped")
    return [sys.executable, str(script), "server", "--output", str(output), *(["--instrument"] if instrument else []), *argv[3:]]


def cell_mode(args):
    root, output = args.source.resolve(), args.output.resolve()
    verify_source(root, args.head)
    os.chdir(root)
    sys.path[:0] = [str(root / "src"), str(root)]
    from tests.crew.e2e import harness
    from tests.crew.load import test_load

    original_subprocess = harness.subprocess
    script = Path(__file__).resolve()
    output.mkdir(parents=True, exist_ok=False)
    trace = output / "phases.json"

    def popen(argv, **kwargs):
        return subprocess.Popen(rewrite_server(argv, script, trace, args.instrument), **kwargs)

    harness.subprocess = SimpleNamespace(**{**vars(subprocess), "Popen": popen})
    gc_before = (gc.isenabled(), gc.get_threshold())
    outcome = "passed"
    try:
        with tempfile.TemporaryDirectory(prefix="crew-latency-cell-") as scratch, LoopbackOnly() as guard:
            try:
                # This is the existing function, unchanged, with every original assertion.
                test_load.test_crew_load_budgets_with_memory_ingestion(Path(scratch))
            except AssertionError:
                outcome = "assertion_failed"
            report_path = Path(scratch) / "load-report.json"
            if not report_path.exists():
                raise RuntimeError("No complete report; setup or integrity failure")
            report = json.loads(report_path.read_text())
        phases = json.loads(trace.read_text())
        summary = numeric_report(report)
        invalid = (
            guard.denied
            or phases["denied_network_attempts"]
            or phases["overflow"]
            or not phases["gc_unchanged"]
            or gc_before != (gc.isenabled(), gc.get_threshold())
        )
        unsafe = not summary["checks"]["no_database_locked"] or not summary["checks"]["no_5xx"] or summary["chain_error_count"]
        if outcome != "passed" and all(summary["checks"].values()):
            unsafe = True  # A subsequent count/chain/status assertion failed.
        save(
            output / "result.json",
            {
                "head": args.head,
                "instrumented": args.instrument,
                "outcome": outcome,
                "invalid_measurement": bool(invalid),
                "safety_or_integrity_failure": bool(unsafe),
                "report": summary,
                "environment": environment(),
                "remaining_assertions_not_claimed_after_failure": outcome != "passed",
            },
        )
        return 3 if invalid or unsafe else 0 if outcome == "passed" else 2
    finally:
        harness.subprocess = original_subprocess
        verify_source(root, args.head)


def compare_mode(args):
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)  # No reuse/retry of the same evidence directory.
    if platform.system() != "Darwin" or platform.machine() != "arm64" or platform.python_version() != "3.11.9":
        raise RuntimeError("Comparison requires the reviewed macOS arm64/Python3.11.9 environment")
    env_info = environment()
    if not env_info["gc_enabled"] or env_info["packages"] != RESOLVED_PACKAGES:
        raise RuntimeError("Resolved environment differs; stop before load")
    roots = {BASE: args.baseline.resolve(), FIX: args.fixed.resolve()}
    for head, root in roots.items():
        verify_source(root, head)
    changes = git(roots[FIX], "diff", "--name-only", BASE, FIX).splitlines()
    if changes != ["src/remembra/core/llm_guard.py", "tests/test_llm_guard_credentials.py"]:
        raise RuntimeError("Comparison source delta changed")
    order = [(BASE, False), (BASE, True), (FIX, True), (FIX, False)]
    orientation = random.SystemRandom().randrange(2)
    if orientation:
        order.reverse()
    manifest = {
        "cells": [{"head": head, "instrumented": enabled} for head, enabled in order],
        "orientation": orientation,
        "maximum_cells": 4,
        "duration_seconds_each": 60,
        "environment": env_info,
        "completed": [],
        "historical_causality_established": False,
        "required_checks_changed": False,
        "no_repeat_until_green": True,
    }
    save(output / "comparison.json", manifest)
    for index, (head, enabled) in enumerate(order):
        with tempfile.TemporaryDirectory(prefix="crew-latency-home-") as home:
            command = [
                sys.executable,
                str(Path(__file__).resolve()),
                "cell",
                "--source",
                str(roots[head]),
                "--head",
                head,
                "--output",
                str(output / f"cell-{index + 1}"),
                *(["--instrument"] if enabled else []),
            ]
            # Product/test output stays out of the artifact and cannot echo credentials.
            child = subprocess.Popen(
                command, env=clean_env(home), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True
            )
            try:
                code = child.wait(timeout=300)
            except subprocess.TimeoutExpired:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(child.pid, signal.SIGINT)
                try:
                    child.wait(timeout=20)
                except subprocess.TimeoutExpired:
                    pass
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(child.pid, signal.SIGKILL)
                child.wait()
                code = 3
        manifest["completed"].append({"cell": index + 1, "exit_code": code})
        save(output / "comparison.json", manifest)
        if code not in (0, 2):
            raise RuntimeError("Diagnostic cell invalid; stopped at planned safety boundary")
    for head, root in roots.items():
        verify_source(root, head)
    return 0 if all(cell["exit_code"] == 0 for cell in manifest["completed"]) else 1


class SelfTests(unittest.IsolatedAsyncioTestCase):
    def test_allowlisted_spans_and_overflow(self):
        r = Recorder(limit=1)
        r.active = True
        with r.span("heartbeat"), r.span("session"):
            pass
        self.assertEqual(len(r.spans), 1)
        self.assertEqual(r.overflow, 1)
        self.assertEqual(r.parent.get(), 0)
        with self.assertRaises(ValueError), r.span("credential_or_payload"):
            pass

    async def test_context_commit_and_rollback(self):
        r, calls = Recorder(), []
        r.active = True

        @contextlib.asynccontextmanager
        async def transaction():
            calls.append("enter")
            try:
                yield 17
            except BaseException:
                calls.append("rollback")
                raise
            else:
                calls.append("commit")

        async with TimedContext(transaction(), r) as value:
            self.assertEqual(value, 17)
        with self.assertRaises(asyncio.CancelledError):
            async with TimedContext(transaction(), r):
                raise asyncio.CancelledError
        self.assertEqual(calls, ["enter", "commit", "enter", "rollback"])
        self.assertEqual(r.parent.get(), 0)

    async def test_context_suppression_and_failed_enter(self):
        r = Recorder()
        r.active = True

        @contextlib.asynccontextmanager
        async def suppress():
            try:
                yield
            except ValueError:
                pass

        async with TimedContext(suppress(), r):
            raise ValueError("synthetic input not recorded")

        @contextlib.asynccontextmanager
        async def fail():
            raise RuntimeError("synthetic")
            yield

        with self.assertRaises(RuntimeError):
            async with TimedContext(fail(), r):
                pass
        self.assertEqual(r.parent.get(), 0)
        self.assertNotIn("synthetic", json.dumps(r.spans))

    async def check_context_exit_failure(self, exception):
        recorder, calls = Recorder(), []
        recorder.active = True

        @contextlib.asynccontextmanager
        async def transaction():
            calls.append("enter")
            yield 17
            calls.append("exit")
            raise exception

        with self.assertRaises(type(exception)) as caught:
            async with TimedContext(transaction(), recorder) as value:
                self.assertEqual(value, 17)
                calls.append("body")
        self.assertIs(caught.exception, exception)
        self.assertEqual(calls, ["enter", "body", "exit"])
        self.assertEqual(recorder.parent.get(), 0)
        self.assertEqual(
            {span[2]: span[5] for span in recorder.spans},
            {"tx_enter": False, "tx_body": False, "tx_exit": True, "transaction": True},
        )

    async def test_context_commit_failure(self):
        await self.check_context_exit_failure(RuntimeError("synthetic commit failure"))

    async def test_context_exit_cancellation(self):
        await self.check_context_exit_failure(asyncio.CancelledError())

    def test_loopback_policy(self):
        g = LoopbackOnly()
        sock = SimpleNamespace(family=socket.AF_INET)
        g.check(sock, ("127.0.0.1", 123))
        for host in ("example.com", "192.0.2.1", "169.254.169.254"):
            with self.assertRaises(OSError):
                g.check(sock, (host, 443))
        self.assertEqual(g.denied, 3)

    def test_child_command_is_exact(self):
        result = rewrite_server(
            [sys.executable, "-m", "tests.crew.e2e.server", "--port", "123"], Path("runner"), Path("out"), True
        )
        self.assertEqual(result[-2:], ["--port", "123"])
        with self.assertRaises(RuntimeError):
            rewrite_server([sys.executable, "-m", "other"], Path("runner"), Path("out"), False)

    def test_safe_output_and_environment(self):
        secret = "synthetic-sensitive-payload"
        result = numeric_report(
            {
                "config": {"crews": secret},
                "retention": {"ms": secret},
                "server": {"errors": [secret]},
                "client_errors": [secret],
                "extra": secret,
            }
        )
        self.assertNotIn(secret, json.dumps(result))
        self.assertEqual(result["server_error_count"], 1)
        old = os.environ.get("OPENAI_API_KEY")
        os.environ["OPENAI_API_KEY"] = secret
        try:
            self.assertNotIn("OPENAI_API_KEY", clean_env("/tmp/synthetic"))
        finally:
            if old is None:
                os.environ.pop("OPENAI_API_KEY")
            else:
                os.environ["OPENAI_API_KEY"] = old


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_subparsers(dest="mode", required=True)
    modes.add_parser("self-test")
    modes.add_parser("requirements")
    compare = modes.add_parser("compare")
    compare.add_argument("--baseline", type=Path, required=True)
    compare.add_argument("--fixed", type=Path, required=True)
    compare.add_argument("--output", type=Path, required=True)
    cell = modes.add_parser("cell")
    cell.add_argument("--source", type=Path, required=True)
    cell.add_argument("--head", choices=HEADS, required=True)
    for mode in (cell, modes.add_parser("server")):
        mode.add_argument("--output", type=Path, required=True)
        mode.add_argument("--instrument", action="store_true")
    args, rest = parser.parse_known_args(argv)
    if rest and args.mode != "server":
        parser.error("Unexpected arguments")
    if args.mode == "self-test":
        suite = unittest.defaultTestLoader.loadTestsFromTestCase(SelfTests)
        return 0 if unittest.TextTestRunner().run(suite).wasSuccessful() else 1
    if args.mode == "requirements":
        print("\n".join(f"{name}=={version}" for name, version in sorted(RESOLVED_PACKAGES.items()) if name != "remembra"))
        return 0
    if args.mode == "compare":
        return compare_mode(args)
    with LoopbackOnly() as guard:
        result = cell_mode(args) if args.mode == "cell" else server_mode(args, rest)
    return 3 if guard.denied else result


if __name__ == "__main__":
    sys.dont_write_bytecode = True
    try:
        sys.exit(main())
    except Exception:
        # Details can include generated fixture identities; fixed diagnostic only.
        print("Diagnostic stopped: setup, integrity, or execution boundary; inspect numeric artifacts.", file=sys.stderr)
        sys.exit(3)
