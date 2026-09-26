"""WP-12: the dashboard's TypeScript reducer and the Python reference agree on every vector, whole state.

The vectors only assert selected paths; this runs the TypeScript reducer
(``dashboard/src/lib/crew/reducer.ts``) under vitest, has it write its final
state for every ``tests/crew/vectors/reducer`` vector, and compares each one
with ``remembra.crew.reducer.reduce`` value for value (§13.1 "Reducer vectors:
Python and TypeScript agree"). Skipped only when the dashboard toolchain is
not installed (no ``node`` or no ``dashboard/node_modules``).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from remembra.crew import reducer as R
from tests.crew.vectors.loader import reducer_vectors

REPO = Path(__file__).resolve().parents[2]
DASHBOARD = REPO / "dashboard"
VITEST = DASHBOARD / "node_modules" / ".bin" / "vitest"

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None or not VITEST.exists(),
    reason="dashboard toolchain (node + dashboard/node_modules) not installed",
)


def _typed_equal(a: Any, b: Any, where: str = "$") -> list[str]:
    """JSON equality where the JSON type must match at every level (Python's ``1 == True`` does not count)."""
    if isinstance(a, bool) or isinstance(b, bool):
        return [] if type(a) is type(b) and a == b else [f"{where}: {a!r} != {b!r}"]
    if isinstance(a, int | float) and isinstance(b, int | float):
        return [] if a == b else [f"{where}: {a!r} != {b!r}"]
    if type(a) is not type(b):
        return [f"{where}: type {type(a).__name__} != {type(b).__name__} ({a!r} vs {b!r})"]
    if isinstance(a, dict):
        out = [f"{where}: key {k!r} only in one state" for k in sorted(set(a) ^ set(b))]
        for k in sorted(set(a) & set(b)):
            out += _typed_equal(a[k], b[k], f"{where}.{k}")
        return out
    if isinstance(a, list):
        if len(a) != len(b):
            return [f"{where}: length {len(a)} != {len(b)}"]
        out = []
        for i, (x, y) in enumerate(zip(a, b, strict=True)):
            out += _typed_equal(x, y, f"{where}[{i}]")
        return out
    return [] if a == b else [f"{where}: {a!r} != {b!r}"]


def test_typescript_reducer_matches_python_reference_on_every_vector(tmp_path: Path) -> None:
    out = tmp_path / "ts-states.json"
    env = {**os.environ, "CREW_PARITY_OUT": str(out), "CI": "1"}
    proc = subprocess.run(
        [str(VITEST), "run", "src/lib/crew/__tests__/reducer.parity.test.ts"],
        cwd=DASHBOARD,
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert proc.returncode == 0, proc.stdout[-4000:] + proc.stderr[-4000:]
    ts_states = json.loads(out.read_text(encoding="utf-8"))
    vectors = reducer_vectors()
    assert sorted(ts_states) == sorted(v["name"] for v in vectors)
    failures: dict[str, list[str]] = {}
    for v in vectors:
        py_state = json.loads(json.dumps(R.reduce(v["snapshot"], v["frames"])))
        diff = _typed_equal(py_state, ts_states[v["name"]])
        if diff:
            failures[v["name"]] = diff[:10]
    assert failures == {}


def test_the_comparison_catches_a_divergent_state() -> None:
    v = reducer_vectors()[0]
    state = json.loads(json.dumps(R.reduce(v["snapshot"], v["frames"])))
    other = json.loads(json.dumps(state))
    other["last_seq"] = True  # a bool where Python has an int
    other["inbox_counts"]["extra"] = 0
    diff = _typed_equal(state, other)
    assert any("last_seq" in d for d in diff) and any("extra" in d for d in diff)
