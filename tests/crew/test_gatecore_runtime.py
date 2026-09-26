"""gatecore as the vendored gate runs it: stdlib only under ``python -I``, and inside the latency budget (§10.4, §13.1)."""

from __future__ import annotations

import json
import shutil
import statistics
import subprocess
import sys
import time
from pathlib import Path

from remembra.crew import gatecore as G
from tests.crew.gatecore_support import HOME, MemFs, snapshot

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"

CASE = {
    "tool_name": "Edit",
    "tool_input": {"file_path": "/w/yaadbooks-c/src/app/pos/cart.ts", "old_string": "a", "new_string": "b"},
    "cwd": "/w/yaadbooks-c",
}


def test_gatecore_imports_only_the_standard_library() -> None:
    probe = f"""
import sys, types, importlib.abc, json
allowed = set(sys.stdlib_module_names) | {{"remembra"}}
class Block(importlib.abc.MetaPathFinder):
    def find_spec(self, name, path=None, target=None):
        if name.split(".")[0] not in allowed:
            raise ImportError("non-stdlib import: " + name)
        return None
sys.meta_path.insert(0, Block())
for pkg, rel in (("remembra", "remembra"), ("remembra.crew", "remembra/crew")):
    mod = types.ModuleType(pkg)
    mod.__path__ = [{str(SRC)!r} + "/" + rel]
    sys.modules[pkg] = mod
import remembra.crew.gatecore as g
snap = json.loads(sys.stdin.read())
tool_input = json.loads({json.dumps(CASE["tool_input"])!r})
v = g.evaluate("Edit", tool_input, snapshot=snap, caller="cs_c", cwd="/w/yaadbooks-c", home={HOME!r},
               now="2026-09-25T20:01:00Z", mode="enforce")
assert (v.rule, v.decision) == (9, "deny"), v.as_dict()
assert g.parse_bash("git commit --no-verify -m x")["tamper"] == ["no_verify"]
print(v.hook_stdout())
"""
    out = subprocess.run(
        [sys.executable, "-I", "-c", probe], input=json.dumps(snapshot()), capture_output=True, text=True, timeout=60
    )
    assert out.returncode == 0, out.stderr
    line = out.stdout.strip()
    assert json.loads(line)["hookSpecificOutput"]["permissionDecision"] == "deny"


def _vendor(tmp: Path) -> Path:
    """Copy gatecore and schemas as a plain package, the way the gate vendors them (no site-packages, no remembra)."""
    pkg = tmp / "vendor" / "remembra" / "crew"
    pkg.mkdir(parents=True)
    (pkg.parent / "__init__.py").write_text("")
    (pkg / "__init__.py").write_text("")
    for name in ("gatecore.py", "schemas.py"):
        shutil.copy(SRC / "remembra" / "crew" / name, pkg / name)
    return tmp / "vendor"


GATE_SCRIPT = """
import time
t0 = time.perf_counter()
import json, sys
sys.path.insert(0, sys.argv[1])
from remembra.crew import gatecore as g
payload = json.loads(sys.argv[2])
with open(sys.argv[3]) as fh:
    snap = json.load(fh)
v = g.evaluate_hook_payload(payload, snapshot=snap, caller="cs_c", home=sys.argv[4], now="2026-09-25T20:01:00Z")
sys.stdout.write(v.hook_stdout() + "\\n")
sys.stderr.write("%.3f" % ((time.perf_counter() - t0) * 1000))
"""


def test_vendored_copy_runs_under_isolated_mode_without_site_packages(tmp_path: Path) -> None:
    vendor = _vendor(tmp_path)
    snap_file = tmp_path / "snap.json"
    snap_file.write_text(json.dumps(snapshot()))
    script = tmp_path / "crew-gate.py"
    script.write_text(GATE_SCRIPT)
    timings = []
    for _ in range(6):
        out = subprocess.run(
            [sys.executable, "-I", "-S", str(script), str(vendor), json.dumps(CASE), str(snap_file), HOME],
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert out.returncode == 0, out.stderr
        assert json.loads(out.stdout)["hookSpecificOutput"]["permissionDecision"] == "deny"
        timings.append(float(out.stderr))
    warm = statistics.median(timings[1:])  # the first run compiles the vendored modules
    print(f"vendored gate import+evaluate: cold {timings[0]:.1f} ms, warm median {warm:.1f} ms")
    assert warm < 180.0  # the §10.4 PreToolUse p95 budget is 90 ms for the whole hook; CI guard is 2x


def _five_kb_snapshot() -> dict:
    snap = snapshot()
    base = snap["zones"][1]
    for n in range(12):
        snap["zones"].append(
            {**base, "id": f"zn_x{n}", "slug": f"x{n}", "include_globs": [f"src/features/x{n}/**"], "parent_id": None}
        )
    assert len(json.dumps(snap)) > 5000
    return snap


def test_in_process_latency_inside_budget() -> None:
    snap = _five_kb_snapshot()
    fs = MemFs()
    samples = []
    for i in range(200):
        tool, tool_input = (
            ("Edit", CASE["tool_input"])
            if i % 2
            else ("Bash", {"command": "sed -i '' 's/a/b/' src/features/x3/a.ts && git add -A"})
        )
        t = time.perf_counter()
        v = G.evaluate(
            tool,
            tool_input,
            snapshot=snap,
            caller="cs_c",
            cwd="/w/yaadbooks-c",
            home=HOME,
            now="2026-09-25T20:01:00Z",
            mode="enforce",
            fs=fs,
            claim=lambda r: "granted",
        )
        samples.append((time.perf_counter() - t) * 1000)
        assert v.decision in ("deny", "allow")
    samples.sort()
    p95 = samples[int(len(samples) * 0.95)]
    print(f"in-process evaluate p50 {samples[len(samples) // 2]:.2f} ms, p95 {p95:.2f} ms")
    assert p95 < 45.0  # half of the hook budget; the interpreter start takes the rest


def test_read_only_bash_exits_without_reading_the_snapshot() -> None:
    class Exploding(dict):
        def get(self, *a, **k):  # type: ignore[no-untyped-def]
            raise AssertionError("snapshot read on the read-only fast path")

        def __getitem__(self, k):  # type: ignore[no-untyped-def]
            raise AssertionError("snapshot read on the read-only fast path")

    for cmd in ("git status", "cat src/app/pos/cart.ts | grep x", "npm test"):
        v = G.evaluate("Bash", {"command": cmd}, snapshot=Exploding(), caller="cs_c", cwd="/w", home=HOME)
        assert (v.rule, v.variant) == (0, "read_only")
    v = G.evaluate("mcp__supabase__list_tables", {}, snapshot=Exploding(), caller="cs_c", cwd="/w", home=HOME)
    assert (v.rule, v.variant) == (0, "read_only")
