#!/usr/bin/env python3
"""Stand-in for WP-9's vendored ``crew-gate.py`` and ``remembra-crew`` hook verbs (WP-10 tests only).

Stdlib only (it runs under ``python -I``). Every call is appended as one JSON
line to ``$WP10_STUB_LOG`` (default: ``stub-gate.log`` next to this file) with
the verb, argv, the hook's stdin and the working directory, so tests can
assert exactly how the installed hooks invoked the gate. Decisions:

* ``pretool``: deny (Claude Code hook JSON) when the target path is under ``held/``;
* ``precommit``: exit 1 when a staged path is under ``held/``;
* ``trailer``: append ``Remembra-Member: stub-member`` to the message file;
* ``prepush``: exit 1 when a pushed commit touches ``held/``;
* ``fail-precommit`` marker file in the repo root makes ``precommit`` exit 3.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

ZERO = "0" * 40


def _log(entry: dict) -> None:
    path = Path(os.environ.get("WP10_STUB_LOG") or Path(__file__).with_name("stub-gate.log"))
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry) + "\n")


def _git(*args: str) -> str:
    return subprocess.run(["git", *args], capture_output=True, text=True, check=False).stdout


def main() -> int:
    argv = sys.argv[1:]
    verb = argv[0] if argv else ""
    stdin = ""
    if verb in ("pretool", "posttool", "turn", "stop", "precompact", "rewake", "start", "end", "stall", "prepush"):
        try:
            stdin = sys.stdin.read() if not sys.stdin.isatty() else ""
        except OSError:
            stdin = ""
    _log({"verb": verb, "argv": argv, "stdin": stdin, "cwd": os.getcwd(), "t": time.time()})
    if verb == "pretool":
        try:
            payload = json.loads(stdin or "{}")
        except ValueError:
            payload = {}
        target = str((payload.get("tool_input") or {}).get("file_path") or "")
        if "/held/" in target:
            reason = "BLOCKED by Remembra Crew (stub): held/ is held EXCLUSIVELY by stub-1. Work elsewhere."
            print(
                json.dumps(
                    {
                        "hookSpecificOutput": {
                            "hookEventName": "PreToolUse",
                            "permissionDecision": "deny",
                            "permissionDecisionReason": reason,
                        }
                    }
                )
            )
        return 0
    if verb == "precommit":
        if Path("fail-precommit").exists():
            return 3
        staged = _git("diff", "--cached", "--name-only").split()
        if any(p.startswith("held/") for p in staged):
            print("BLOCKED by Remembra Crew (stub): held/ is held by stub-1", file=sys.stderr)
            return 1
        return 0
    if verb == "trailer":
        if len(argv) > 1 and Path(argv[1]).is_file():
            with open(argv[1], "a", encoding="utf-8") as fh:
                fh.write("\nRemembra-Member: stub-member\n")
        return 0
    if verb == "prepush":
        for line in stdin.splitlines():
            parts = line.split()
            if len(parts) != 4 or parts[1] == ZERO:
                continue
            local, remote = parts[1], parts[3]
            rng = local if remote == ZERO else f"{remote}..{local}"
            files = _git("log", "--name-only", "--format=", rng).split()
            if any(p.startswith("held/") for p in files):
                print("BLOCKED by Remembra Crew (stub): pushed commits touch held/", file=sys.stderr)
                return 1
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
