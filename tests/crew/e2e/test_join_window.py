"""The join window, end to end with the crew rate limiter on (as in production).

Seen live on 2026-09-28: session A held zone ``api``; session B joined from its own worktree and its
PreToolUse Edit in ``api`` was allowed (exit 0, empty stdout) for 10-13 s. The server allows one
snapshot read per 10 s per API key, agent and crew; B's join read was refused (429), and crewd put
B's checkout into the local snapshot only with a successful read, so the gate took every path in B's
checkout for "outside every checkout" (rule 0: allow) until a later read.

Here everything is a real process: the crew API with its rate limiter, ``remembra-crewd``, the gate
and ``remembra-crew start`` installed by the real ``connect``, and two model-free "Claude Code"
sessions running the installed hooks. B starts right after A's claim (well inside the 10 s window)
and must be refused on its very first write in A's zone, while a free zone stays writable.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from tests.crew.e2e.harness import World

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="needs git")


@pytest.fixture
def world(tmp_path: Path):
    w = World(tmp_path, crew_rate_limits=True)
    try:
        yield w
    finally:
        w.close()


def test_a_session_joining_inside_the_snapshot_window_is_denied_on_its_first_write(world: World) -> None:
    wts = world.setup(("wt-a", "wt-b"))
    a = world.agent("a", wts["wt-a"])
    a.start()
    a.prompt("split the bill")
    first = a.edit("src/app/pos/split.ts", "[total]", "[total, 0]")
    assert not first["denied"] and first["ok"], json.dumps(first)[:2000]
    crew_id = str(a.local_session()["crew_id"])

    b = world.agent("b", wts["wt-b"])
    b.start()
    assert b.local_session()["crew_id"] == crew_id
    snap = json.loads(world.layout.snapshot_file(crew_id).read_text())

    b.prompt("split the bill too")
    res = b.edit("src/app/pos/split.ts", "[total]", "[total, 1]")
    assert res["denied"], json.dumps(res)[:2000]
    # why: B's checkout was in the local snapshot as soon as its join returned, whatever the server read gave
    assert str(wts["wt-b"]) in {c["toplevel"] for c in snap["checkouts"]}, snap["checkouts"]
    assert 'zone "pos"' in str(res["reason"]), res["reason"]
    assert (wts["wt-b"] / "src/app/pos/split.ts").read_text() == "export const split = (total: number) => [total];\n"
    # a zone nobody holds stays writable for B
    free = b.edit("src/app/reports/export.ts", "''", "'csv'")
    assert not free["denied"] and free["ok"], json.dumps(free)[:2000]
    for agent in (b, a):
        agent.end()
