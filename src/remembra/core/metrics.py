"""Minimal Prometheus-compatible metrics registry.

Deliberately dependency-free (no ``prometheus_client``) — Remembra only needs
a handful of counters and gauges, rendered in the Prometheus text exposition
format (version 0.0.4) at ``GET /metrics``.

Thread-safe: all mutation happens under a single lock, so counters can be
incremented from worker threads (e.g. ``asyncio.to_thread``) as well as the
event loop.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterable

LabelValues = tuple[tuple[str, str], ...]


def _escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


def _fmt_labels(labels: LabelValues) -> str:
    if not labels:
        return ""
    return "{" + ",".join(f'{k}="{_escape(v)}"' for k, v in labels) + "}"


def _fmt_value(value: float) -> str:
    if value == int(value):
        return str(int(value))
    return repr(float(value))


class _Metric:
    kind = "untyped"

    def __init__(self, name: str, help_text: str, label_names: Iterable[str] = ()) -> None:
        self.name = name
        self.help = help_text
        self.label_names = tuple(label_names)
        self._values: dict[LabelValues, float] = {}
        self._lock = threading.Lock()

    def _key(self, labels: dict[str, str]) -> LabelValues:
        if set(labels) != set(self.label_names):
            raise ValueError(f"{self.name}: expected labels {self.label_names}, got {tuple(labels)}")
        return tuple((n, str(labels[n])) for n in self.label_names)

    def get(self, **labels: str) -> float:
        with self._lock:
            return self._values.get(self._key(labels), 0.0)

    def samples(self) -> list[tuple[LabelValues, float]]:
        with self._lock:
            return sorted(self._values.items())

    def reset(self) -> None:
        with self._lock:
            self._values.clear()

    def render(self) -> list[str]:
        lines = [f"# HELP {self.name} {self.help}", f"# TYPE {self.name} {self.kind}"]
        for labels, value in self.samples():
            lines.append(f"{self.name}{_fmt_labels(labels)} {_fmt_value(value)}")
        return lines


class Counter(_Metric):
    kind = "counter"

    def inc(self, amount: float = 1.0, **labels: str) -> None:
        if amount < 0:
            raise ValueError("counters can only increase")
        key = self._key(labels)
        with self._lock:
            self._values[key] = self._values.get(key, 0.0) + amount


class Gauge(_Metric):
    kind = "gauge"

    def set(self, value: float, **labels: str) -> None:
        key = self._key(labels)
        with self._lock:
            self._values[key] = float(value)


class Registry:
    def __init__(self) -> None:
        self._metrics: dict[str, _Metric] = {}
        self._collectors: list[Callable[[], None]] = []
        self._lock = threading.Lock()

    def counter(self, name: str, help_text: str, label_names: Iterable[str] = ()) -> Counter:
        with self._lock:
            existing = self._metrics.get(name)
            if existing is not None:
                if not isinstance(existing, Counter):
                    raise ValueError(f"{name} already registered as {existing.kind}")
                return existing
            metric = Counter(name, help_text, label_names)
            self._metrics[name] = metric
            return metric

    def gauge(self, name: str, help_text: str, label_names: Iterable[str] = ()) -> Gauge:
        with self._lock:
            existing = self._metrics.get(name)
            if existing is not None:
                if not isinstance(existing, Gauge):
                    raise ValueError(f"{name} already registered as {existing.kind}")
                return existing
            metric = Gauge(name, help_text, label_names)
            self._metrics[name] = metric
            return metric

    def add_collector(self, fn: Callable[[], None]) -> None:
        """Register a callback run just before rendering (to refresh gauges)."""
        with self._lock:
            self._collectors.append(fn)

    def remove_collector(self, fn: Callable[[], None]) -> None:
        with self._lock:
            if fn in self._collectors:
                self._collectors.remove(fn)

    def render(self) -> str:
        with self._lock:
            collectors = list(self._collectors)
            metrics = [self._metrics[k] for k in sorted(self._metrics)]
        for fn in collectors:
            try:
                fn()
            except Exception:  # a broken collector must never break /metrics
                pass
        lines: list[str] = []
        for metric in metrics:
            lines.extend(metric.render())
        return "\n".join(lines) + "\n"


REGISTRY = Registry()

# ---------------------------------------------------------------------------
# Remembra metrics (names are part of the ops contract — see docs/DEPLOYING.md)
# ---------------------------------------------------------------------------

EMBEDDING_ERRORS = REGISTRY.counter(
    "remembra_embedding_errors_total",
    "Embedding provider failures by provider and error kind.",
    ("provider", "kind"),
)
LLM_ERRORS = REGISTRY.counter(
    "remembra_llm_errors_total",
    "LLM (extraction/consolidation) failures by component and error kind.",
    ("component", "kind"),
)
LLM_FALLBACKS = REGISTRY.counter(
    "remembra_llm_fallbacks_total",
    "Times an LLM step fell back to a non-LLM default (component).",
    ("component",),
)
STORE_FAILURES = REGISTRY.counter(
    "remembra_store_failures_total",
    "Store requests that failed, by reason.",
    ("reason",),
)
RECALL_FAILURES = REGISTRY.counter(
    "remembra_recall_failures_total",
    "Recall requests that failed, by reason.",
    ("reason",),
)
RECALL_DEGRADED = REGISTRY.counter(
    "remembra_recall_degraded_total",
    "Recall requests answered in a degraded mode (e.g. keyword_only), by mode.",
    ("mode",),
)
BREAKER_STATE = REGISTRY.gauge(
    "remembra_circuit_breaker_state",
    "Circuit breaker state: 0=closed, 1=half_open, 2=open.",
    ("name",),
)
BREAKER_OPENS = REGISTRY.counter(
    "remembra_circuit_breaker_opens_total",
    "Times a circuit breaker opened, by breaker and triggering error kind.",
    ("name", "kind"),
)
PENDING_EMBEDDINGS = REGISTRY.gauge(
    "remembra_pending_embeddings",
    "Rows in the pending_embeddings queue by status.",
    ("status",),
)
PENDING_EMBEDDINGS_PROCESSED = REGISTRY.counter(
    "remembra_pending_embeddings_processed_total",
    "Pending-embedding queue items processed by outcome.",
    ("outcome",),
)
RECONCILE_DRIFT = REGISTRY.gauge(
    "remembra_reconcile_drift",
    "SQLite<->Qdrant drift found by the last reconcile run, by type.",
    ("type",),
)
ALERTS_SENT = REGISTRY.counter(
    "remembra_alerts_sent_total",
    "Operator alerts sent, by event and outcome.",
    ("event", "outcome"),
)
BACKGROUND_TASKS = REGISTRY.gauge(
    "remembra_background_tasks",
    "Currently running tracked background tasks.",
)


def recall_degraded(mode: str) -> None:
    """Record one recall answered in a degraded mode (wave-2 wiring point)."""
    RECALL_DEGRADED.inc(mode=mode)
