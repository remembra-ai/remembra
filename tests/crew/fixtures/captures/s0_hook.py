#!/usr/bin/env python3
"""Spike S0 capture hook (stdlib only, run by Claude Code as a command hook).

Usage (from a project-level ``.claude/settings.json`` written by ``s0_harness``)::

    python3 s0_hook.py <capture_dir> <EventName> <action> [arg ...]

The hook always records the raw stdin payload it received to
``<capture_dir>/<NN>-<EventName>.json`` (``NN`` is a monotonic counter so the
firing order is preserved), then performs ``action``:

``record``
    No stdout, exit 0 (the "allow / no-op" contract of spec §8.2).
``deny <reason>``
    PreToolUse deny: ``hookSpecificOutput.permissionDecision = "deny"``.
``ctx <text>``
    ``hookSpecificOutput.additionalContext = text`` with **no** decision.
``envfile <NAME> <value>``
    Append ``export NAME=value`` to ``$CLAUDE_ENV_FILE`` (SessionStart only).
``sleepctx <seconds> <text>``
    Sleep, write a ``<EventName>.done`` marker, then emit ``additionalContext``.
    Used to observe ``async`` hooks.
``rewake <seconds> <text>``
    Sleep, write a marker, print ``text`` to stderr and exit 2. Used with
    ``asyncRewake``.
``rewake_once <seconds> <text>``
    Like ``rewake`` but a no-op once the marker exists (prevents a Stop loop).

The hook never raises: any internal error is reported on stderr with exit 0,
matching the "hooks exit 0 on internal errors" rule of the spec.
"""

from __future__ import annotations

import json
import os
import sys
import time


def _next_path(capture_dir: str, event: str) -> str:
    os.makedirs(capture_dir, exist_ok=True)
    # O_EXCL on a per-slot sequence file gives one global firing order across
    # events, even when hooks run concurrently.
    for n in range(1, 1000):
        try:
            fd = os.open(os.path.join(capture_dir, f".seq-{n:03d}"), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            continue
        os.close(fd)
        return os.path.join(capture_dir, f"{n:02d}-{event}.json")
    raise RuntimeError("too many captures")


def _emit(event: str, **fields: str) -> None:
    out = {"hookSpecificOutput": {"hookEventName": event, **fields}}
    sys.stdout.write(json.dumps(out) + "\n")
    sys.stdout.flush()


def _marker(capture_dir: str, event: str) -> None:
    with open(os.path.join(capture_dir, f"{event}.done"), "w", encoding="utf-8") as fh:
        fh.write(str(time.time()))


def main(argv: list[str]) -> int:
    if len(argv) < 3:
        sys.stderr.write("s0_hook: usage: s0_hook.py <capture_dir> <Event> <action> [args]\n")
        return 0
    capture_dir, event, action, *args = argv
    raw = sys.stdin.read()
    try:
        payload: object = json.loads(raw) if raw.strip() else None
    except ValueError:
        payload = {"_unparsed_stdin": raw}
    record = {
        "event": event,
        "action": action,
        "received_at": time.time(),
        "env": {
            "CLAUDE_PROJECT_DIR": os.environ.get("CLAUDE_PROJECT_DIR"),
            "CLAUDE_ENV_FILE_set": bool(os.environ.get("CLAUDE_ENV_FILE")),
        },
        "payload": payload,
    }
    with open(_next_path(capture_dir, event), "w", encoding="utf-8") as fh:
        json.dump(record, fh, indent=2, sort_keys=True)

    if action == "record":
        return 0
    if action == "deny":
        _emit(event, permissionDecision="deny", permissionDecisionReason=args[0])
        return 0
    if action == "ctx":
        _emit(event, additionalContext=args[0])
        return 0
    if action == "envfile":
        env_file = os.environ.get("CLAUDE_ENV_FILE")
        if env_file:
            with open(env_file, "a", encoding="utf-8") as fh:
                fh.write(f"export {args[0]}={args[1]}\n")
        return 0
    if action == "sleepctx":
        time.sleep(float(args[0]))
        _marker(capture_dir, event)
        _emit(event, additionalContext=args[1])
        return 0
    if action == "rewake_once":
        # Only the first firing rewakes; later firings (e.g. the Stop after the rewake turn) are no-ops.
        if os.path.exists(os.path.join(capture_dir, f"{event}.done")):
            return 0
        action = "rewake"
    if action == "rewake":
        time.sleep(float(args[0]))
        _marker(capture_dir, event)
        sys.stderr.write(args[1] + "\n")
        return 2
    sys.stderr.write(f"s0_hook: unknown action {action!r}\n")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except Exception as exc:  # pragma: no cover - defensive, hooks must not crash Claude Code
        sys.stderr.write(f"s0_hook: internal error: {exc}\n")
        sys.exit(0)
