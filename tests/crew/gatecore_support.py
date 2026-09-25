"""Shared helpers for the gatecore tests (WP-3): an in-memory filesystem and snapshot builders."""

from __future__ import annotations

import copy
import posixpath
from collections.abc import Iterable, Mapping
from typing import Any

from remembra.crew import gatecore as G
from remembra.crew import schemas as S
from tests.crew.vectors.loader import load

CONCRETE = load("guard/concrete.json")
HOME = CONCRETE["home"]
NOW = "2026-09-25T20:01:00.000Z"
SERVER_CODES = {201: "granted", 409: "conflict", 429: "rate_limited", "timeout": "timeout", "cap": "cap"}


class MemFs:
    """In-memory filesystem: files with text, directories, hard-link inodes; ``realpath`` follows symlinks."""

    def __init__(
        self,
        files: Mapping[str, str] | Iterable[str] = (),
        dirs: Iterable[str] = (),
        links: Mapping[str, str] | None = None,
        inodes: Mapping[str, tuple[int, int, int]] | None = None,
    ) -> None:
        if isinstance(files, Mapping):
            self.files = dict(files)
        else:
            self.files = {f: "" for f in files}
        self.dirs = set(dirs)
        self.links = dict(links or {})
        self.inodes = dict(inodes or {})
        self.reads: list[str] = []

    def realpath(self, path: str) -> str:
        path = posixpath.normpath(path)
        for _ in range(16):
            for src, dst in self.links.items():
                if path == src or path.startswith(src + "/"):
                    path = posixpath.normpath(dst + path[len(src) :])
                    break
            else:
                return path
        return path

    def exists(self, path: str) -> bool:
        return path in self.files or path in self.dirs

    def is_dir(self, path: str) -> bool:
        return path in self.dirs

    def inode(self, path: str) -> tuple[int, int, int] | None:
        return self.inodes.get(path)

    def read_text(self, path: str, limit: int = 262_144) -> str | None:
        self.reads.append(path)
        return self.files.get(path)


def snapshot(**changes: Any) -> dict[str, Any]:
    """A deep copy of the concrete-vector snapshot with top-level keys replaced."""
    snap = copy.deepcopy(CONCRETE["snapshot"])
    snap.update(copy.deepcopy(changes))
    return snap


def find(items: list[dict[str, Any]], ident: str) -> dict[str, Any]:
    return next(x for x in items if x["id"] == ident)


def toplevel(snap: Mapping[str, Any], caller: str) -> str:
    return next(c["toplevel"] for c in snap["checkouts"] if c["session_id"] == caller)


def run(
    tool: str,
    tool_input: Mapping[str, Any],
    *,
    caller: str = "cs_c",
    snap: Mapping[str, Any] | None = None,
    fs: Any = None,
    mode: str | None = "enforce",
    now: str = NOW,
    cwd: str | None = None,
    claim: Any = None,
    **kw: Any,
) -> G.Verdict:
    snap = snap if snap is not None else snapshot()
    return G.evaluate(
        tool,
        tool_input,
        snapshot=snap,
        caller=caller,
        cwd=cwd or toplevel(snap, caller),
        home=HOME,
        now=now,
        mode=mode,
        fs=fs if fs is not None else MemFs(),
        claim=claim,
        human="Mani",
        **kw,
    )


def concrete_evaluate(case: Mapping[str, Any], ctx: Mapping[str, Any]) -> dict[str, Any]:
    """The adapter the WP-0a runner calls: snapshot + tool call (+ mocked claim response) → outcome and facts."""
    snap = ctx["snapshot"]
    top = toplevel(snap, case["caller"])
    fs = MemFs(posixpath.join(top, f) for f in case["existing_files"])
    server = case["server"]

    def claim(req: G.ClaimRequest) -> str:
        return SERVER_CODES[server["claim"]]

    verdict = G.evaluate(
        case["tool_name"],
        case["tool_input"],
        snapshot=snap,
        caller=case["caller"],
        cwd=case["cwd"],
        home=ctx["home"],
        now=case["now"],
        mode=case["mode"],
        fs=fs,
        claim=claim if server else None,
        human="Mani",
    )
    if verdict.reason is not None:
        assert S.check_agent_text(verdict.reason, "deny") == [], (case["name"], verdict.reason)
    return {"rule": verdict.rule, "decision": verdict.decision, "variant": verdict.variant, "facts": verdict.facts}
