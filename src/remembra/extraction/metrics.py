"""In-process counters for ingest-pipeline failures.

A deliberately tiny hook: code calls :func:`incr` at every failure point that
used to be swallowed silently (entity extraction, Jev errors, extraction
fallbacks). The observability stream (REL-8) can export :func:`snapshot` as
Prometheus counters without touching the call sites.
"""

from __future__ import annotations

import threading
from collections import Counter

_lock = threading.Lock()
_counters: Counter[str] = Counter()


def incr(name: str, amount: int = 1) -> None:
    with _lock:
        _counters[name] += amount


def get(name: str) -> int:
    with _lock:
        return _counters[name]


def snapshot() -> dict[str, int]:
    with _lock:
        return dict(_counters)


def reset() -> None:
    """Test helper."""
    with _lock:
        _counters.clear()
