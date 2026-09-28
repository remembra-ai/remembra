"""``remembra-crew``: the Crew mode command line (spec §8.1, §8.2, §8.3 AGENTS.md block).

Hook commands (read the agent's hook payload from stdin; never fail the agent; exit 0)::

    remembra-crew start --hook claude-code --agent claude-code   # SessionStart: join + brief + crew block
    remembra-crew end   --hook claude-code --agent claude-code   # SessionEnd: ≤300 ms, crewd finishes the rest
    remembra-crew stall --hook claude-code --agent claude-code   # StopFailure: baton ref + stalled report

These hooks replace ``remembra-relay brief`` / ``close`` in the agent's config, so they keep the relay's
hook order (0.16.1): a Codex automation or sub-agent thread is skipped and a hook another agent runs is
routed (:func:`relay_hook_verdict`) before any crew work; a session that is not a crew session gets the
plain ``remembra-relay brief`` / ``close``, which drops a repeated end, skips an empty close and then sends
(:func:`relay_brief_passthrough`, :func:`relay_close_passthrough`); crewd skips an empty close of a crew
session too.

Agent and human commands (act as the caller's crew session, resolved by crewd from the process
tree; they never read a token)::

    remembra-crew status | whereami | doctor | renew
    remembra-crew claim <zone|path|schema:db> [--mode exclusive|shared|watch] [--task T-n] [--wait N]
    remembra-crew release <zone|claim id> [--baton]
    remembra-crew adopt T-n [--restore-only]
    remembra-crew task list | create --title … [--zone slug …] | start T-n | update T-n --status … | block T-n --reason … |
                       unblock T-n | release T-n [--baton]
    remembra-crew checkpoint [--summary …] [--next …]
    remembra-crew report T-n [--done …] [--not-done …] [--failing …] [--next …] [--follow-up …] [--summary …]
    remembra-crew say "text" [--kind chat|question|answer|note|request_release|decision] [--to @callsign] [--wait N]
    remembra-crew watch [--since N] [--once]
    remembra-crew zones [show|push]
    remembra-crew bypass --session <callsign> [--code RCB-…]   # humans at an interactive terminal only (D34)

Agent-authored text (task titles, messages) is printed only inside the ``<remembra-data>`` block.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import uuid
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Final

from remembra.crew import schemas as S
from remembra.relay.crew.gate import (
    Layout,
    parse_payload,
    read_json,
    read_stdin,
    respawn_crewd,
    rpc,
    rpc_ex,
    rpc_send,
    session_key,
)
from remembra.relay.hints import RELAY_PROJECT_ENV

CREWD_START_WAIT_S: Final = 3.0
START_BUDGET_S: Final = 9.0
# One deadline for the whole SessionStart hook (§10.4: 10 s hard; the hook's own timeout is 15 s). Every
# step gets what is left, so a black-holed server can never push start past it.
START_DEADLINE_S: Final = 8.0
# The plain relay close run for a session that is not a crew session: inline for Claude Code (whose relay
# close has its own ~10 s budget), detached by the relay itself for agents that do not wait. Kept under the
# crew SessionEnd hook's 20 s timeout.
END_PASSTHROUGH_S: Final = 17.0
CREW_EXISTS_TIMEOUT_S: Final = 5.0
MIN_STEP_S: Final = 0.3
HOOK_OUTPUT: Final[Mapping[str, str]] = {
    "claude-code": "hook-json",
    "gemini": "hook-json",
    "qwen": "hook-json",
    "cursor": "cursor-json",
}
HOOK_COMMANDS: Final = ("start", "end", "stall")


def _err(message: str) -> None:
    try:
        sys.stderr.write(f"remembra-crew: {message}\n")
    except Exception:
        pass


def agent_text(value: Any) -> str:
    """One agent- or repo-authored value under the brief's trust policy (R-14,
    :func:`remembra.relay.handoff.police_item`); server-template prefixes stay outside it."""
    from remembra.relay.handoff import police_item

    return police_item(str(value or ""))


def data_block(items: Sequence[str]) -> str:
    """Items clipped and neutralised inside the one data block (§11), the trust policy's notes kept
    at the end of each line (the agent-authored parts went through :func:`agent_text`)."""
    from remembra.relay.handoff import data_line

    clean = [data_line(i, S.DATA_ITEM_CLIP) for i in items if i]
    if not clean:
        return ""
    return S.DATA_OPEN + "\n" + "\n".join(clean) + "\n" + S.DATA_CLOSE


def ensure_crewd(layout: Layout, *, wait: float = CREWD_START_WAIT_S) -> bool:
    """crewd answers ping, or is started (supervisor's argv from ``bin/crewd.json``, else this Python)."""
    if rpc(layout, "ping", timeout=0.5):
        return True
    if not respawn_crewd(layout, force=True):
        layout.run.mkdir(parents=True, exist_ok=True)
        env = {k: v for k, v in os.environ.items() if not k.startswith("REMEMBRA_CREW")}
        try:
            subprocess.Popen(  # noqa: S603
                [sys.executable, "-m", "remembra.relay.crew.crewd"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
                env=env,
            )
        except OSError:
            return False
    end = time.monotonic() + wait
    while time.monotonic() < end:
        if rpc(layout, "ping", timeout=0.3):
            return True
        time.sleep(0.1)
    return False


# Ops that are safe to send twice. Anything else (adopt, claim, release, stall, end, report, bypass…)
# is resent only when the first request never reached crewd: a lost reply may mean it already ran.
RETRY_SAFE_OPS: Final = frozenset({"ping", "status", "whoami", "crew_exists", "events_page", "doctor", "brief"})


def _call(layout: Layout, op: str, args: Mapping[str, Any] | None = None, *, timeout: float = 20.0) -> dict[str, Any]:
    res, sent = rpc_ex(layout, op, args, timeout=timeout)
    if res is None and (not sent or op in RETRY_SAFE_OPS) and ensure_crewd(layout):
        res, sent_again = rpc_ex(layout, op, args, timeout=timeout)
        sent = sent or sent_again
    if res is None and sent and op not in RETRY_SAFE_OPS:
        return {
            "ok": False,
            "error": "no_reply",
            "message": f"crewd took the {op} request but did not answer; it may have run."
            " Check `remembra-crew status` before trying again",
        }
    if res is None:
        return {"ok": False, "error": "crewd_unreachable", "message": "crewd is not running (remembra-crew doctor)"}
    return res


def _print_json(data: Any) -> None:
    print(json.dumps(data, indent=2, default=str))


def _fail(res: Mapping[str, Any]) -> int:
    msg = res.get("message") or res.get("error") or f"HTTP {res.get('status')}"
    body = res.get("body")
    if isinstance(body, dict):
        detail = body.get("detail", body)
        if isinstance(detail, dict):
            msg = detail.get("message") or detail.get("error") or msg
    print(f"REFUSED: {S.clip_item(str(msg), 300)}")
    return 1


# ===========================================================================
# Hook commands
# ===========================================================================


def _hook_payload(args: argparse.Namespace) -> dict[str, Any]:
    return parse_payload(read_stdin()) if args.hook else {}


def _emit_start(adapter: str, text: str) -> None:
    mode = HOOK_OUTPUT.get(adapter, "text")
    if mode == "hook-json":
        try:
            sys.stdout.write(S.hook_session_start(text) + "\n")
        except ValueError:
            sys.stdout.write(
                S.hook_session_start("Remembra crew: the brief could not be shown (it broke the text rules).") + "\n"
            )
    elif mode == "cursor-json":
        sys.stdout.write(json.dumps({"additional_context": text}) + "\n")
    else:
        sys.stdout.write(text + "\n")


def _env_file(join: Mapping[str, Any]) -> None:
    """``$CLAUDE_ENV_FILE`` (S0 GO): project and member for later Bash calls; the session id is never exported."""
    path = os.environ.get("CLAUDE_ENV_FILE")
    if not path:
        return
    try:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(f"export REMEMBRA_PROJECT_ID={json.dumps(str(join.get('project_id') or ''))}\n")
            fh.write(f"export REMEMBRA_MEMBER={json.dumps(str(join.get('member_key') or ''))}\n")
    except OSError as e:
        _err(f"could not write CLAUDE_ENV_FILE ({e.__class__.__name__})")


def _toplevel(cwd: str) -> str | None:
    try:
        res = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"], cwd=cwd, capture_output=True, text=True, timeout=3, check=False
        )  # noqa: S603,S607
    except (OSError, subprocess.SubprocessError):
        return None
    return res.stdout.strip() or None if res.returncode == 0 else None


def crew_enabled_locally(layout: Layout, toplevel: str) -> bool:
    """Crew mode for this checkout without asking the server: a ``.remembra/`` directory, or the owner's config
    (``~/.remembra/crew/config.json``: ``{"crew_all_repos": true}`` or ``{"crew_repos": ["/abs/path", …]}``)."""
    if (Path(toplevel) / ".remembra").is_dir():
        return True
    config = read_json(layout.config_file) or {}
    if config.get("crew_all_repos") is True:
        return True
    repos = config.get("crew_repos") or []
    real = os.path.realpath(toplevel)
    return any(isinstance(r, str) and os.path.realpath(os.path.expanduser(r)) == real for r in repos)


def relay_hook_verdict(adapter: str, payload: Mapping[str, Any], verb: str) -> tuple[str, str] | None:
    """Steps 1 and 2 of the relay's hook order (0.16.1) for a crew hook: ``("skip", why)``, ``("route", host)``
    or None when the hook is this adapter's own session.

    1. **skip**: a Codex automation or sub-agent thread (:mod:`remembra.relay.background`) joins no crew,
       gets no brief and leaves no handoff (``REMEMBRA_RELAY_INCLUDE_AUTOMATIONS=1`` keeps automations).
    2. **route**: a hook another agent runs (:func:`remembra.relay.hosts.detect_host`: Cursor, Grok,
       Devin or Continue running Claude Code's hook file, a hook ``kimi migrate`` copied) is not a session
       of this adapter's agent, so it joins no crew here; that agent's own hooks handle its session.

    Each writes one line to relay.log, as ``remembra-relay brief`` / ``close`` do. Never raises: an error
    here lets the hook go on. Only a hook with a payload is checked (the markers are in it).
    """
    if not payload:
        return None
    try:
        from remembra.relay import background, hosts
        from remembra.relay.adapters import get_adapter

        spec_adapter = get_adapter(adapter)
        if spec_adapter is None:
            return None
        home = Path(os.environ.get("HOME") or Path.home())
        skipped = background.skip_hook_session(spec_adapter, payload, verb, home)
        if skipped:
            return "skip", skipped
        host = hosts.detect_host(spec_adapter, payload, os.environ, home)
        if host is not None:
            from remembra.relay import outbox

            outbox.log(home, f"crew {verb} --hook {adapter}: run by {host}, not a crew session of {adapter}")
            return "route", host
    except Exception as e:
        _err(f"hook check failed: {e.__class__.__name__}")
    return None


def _hook_ack(adapter: str) -> None:
    """What a hook prints when it has nothing to say: ``{}`` for Cursor, which logs empty stdout as a failed hook."""
    if HOOK_OUTPUT.get(adapter) == "cursor-json":
        sys.stdout.write("{}\n")


def relay_brief_passthrough(adapter: str, agent: str, payload: Mapping[str, Any], *, timeout: float = START_BUDGET_S) -> None:
    """Not a crew checkout: the plain relay brief (the SessionStart hook replaces ``remembra-relay brief``, §8.2).

    ``remembra-relay brief`` prints its own notice when the brief is unavailable, and nothing when there is
    nothing to print (a skipped or routed hook, a brief this session already has); its output is passed on as
    it is. Only a brief that could not run at all (timed out, not started) prints the notice here.
    """
    fmt = {"hook-json": "hook-json", "cursor-json": "cursor-json"}.get(HOOK_OUTPUT.get(adapter, "text"), "text")
    out: str | None = None
    if timeout >= MIN_STEP_S:
        try:
            res = subprocess.run(  # noqa: S603
                [sys.executable, "-m", "remembra.relay.cli", "brief", "--hook", adapter, "--agent", agent, "--format", fmt],
                input=json.dumps(dict(payload)),
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
            out = res.stdout if res.returncode == 0 else None
        except (OSError, subprocess.SubprocessError):
            out = None
    if out is None:
        _emit_start(adapter, "Remembra brief unavailable. Call the session_brief tool.")
    elif out.strip():
        sys.stdout.write(out if out.endswith("\n") else out + "\n")
    else:
        _hook_ack(adapter)


def relay_close_passthrough(adapter: str, agent: str, payload: Mapping[str, Any], *, timeout: float = END_PASSTHROUGH_S) -> None:
    """Not a crew session: the plain relay close (the SessionEnd hook replaces ``remembra-relay close``, §8.2).

    ``remembra-relay close`` keeps its own order: skip a Codex automation or sub-agent thread, route a hook
    another agent runs, drop a repeat of the same end, skip an empty close, then send (queued when the server
    cannot be reached, detached for agents that do not wait for their end hook). Its stdout (Cursor's ``{}``)
    is passed on.
    """
    try:
        res = subprocess.run(  # noqa: S603
            [sys.executable, "-m", "remembra.relay.cli", "close", "--hook", adapter, "--agent", agent],
            input=json.dumps(dict(payload)),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as e:
        _err(f"relay close failed: {e.__class__.__name__}")
        _hook_ack(adapter)
        return
    if res.stdout.strip():
        sys.stdout.write(res.stdout if res.stdout.endswith("\n") else res.stdout + "\n")
    else:
        _hook_ack(adapter)


def relay_close_event(adapter: str, payload: Mapping[str, Any]) -> bool:
    """Whether the relay's own hooks would close on this event (Claude Code: a StopFailure on a usage or
    billing limit), so a crew hook on the same event runs the plain relay close for a non-crew session."""
    try:
        import re

        from remembra.relay.adapters import get_adapter

        spec_adapter = get_adapter(adapter)
        event = str(payload.get("hook_event_name") or "")
        if spec_adapter is None or not event:
            return False
        value = str(payload.get("error") or "")
        for extra in spec_adapter.spec.extra_close_events:
            if extra.event != event:
                continue
            if extra.matcher is None or re.fullmatch(extra.matcher, value):
                return True
    except Exception:
        return False
    return False


def cmd_start(args: argparse.Namespace, layout: Layout) -> int:
    from remembra.relay.crew.crewd import find_agent_pid

    adapter = args.hook or "claude-code"
    started = time.monotonic()
    deadline = started + START_DEADLINE_S

    def left(reserve: float = 0.0) -> float:
        return max(0.0, deadline - time.monotonic() - reserve)

    try:
        payload = _hook_payload(args)
        # The relay's order first: a Codex automation or sub-agent thread, or a hook another agent runs,
        # joins no crew and prints no brief (that agent's own hooks brief its session).
        if relay_hook_verdict(adapter, payload, "brief"):
            _hook_ack(adapter)
            return 0
        sid = payload.get("session_id") or args.session_id
        cwd = payload.get("cwd") or args.cwd or os.getcwd()
        if not sid:
            _emit_start(adapter, "Remembra crew: no session id in the hook payload; crew rules are not active for this session.")
            return 0
        agent_pid = args.agent_pid or find_agent_pid(os.getpid(), adapter) or os.getppid()
        plain = payload or {"session_id": sid, "cwd": str(cwd)}
        top = _toplevel(str(cwd))
        if top is None:
            relay_brief_passthrough(adapter, args.agent or adapter, plain, timeout=left())
            return 0
        # crewd resolves this checkout's project under the relay's rule with THIS session's environment (a
        # supervised crewd does not have it): REMEMBRA_RELAY_PROJECT, sent even when unset.
        relay_project = os.environ.get(RELAY_PROJECT_ENV)
        enabled = bool(args.crew) or crew_enabled_locally(layout, top)
        if not enabled:
            # a crew may already exist for this project (created elsewhere): join it; otherwise the plain brief.
            # The brief keeps at least half the budget; crew_exists is asked again only when crewd was not
            # running (never after a timeout: a black-holed server would just time out twice).
            ask = {"cwd": str(cwd), "adapter": adapter, "agent_id": args.agent or adapter, "relay_project": relay_project}
            budget = min(CREW_EXISTS_TIMEOUT_S, left() / 2)
            exists, sent = rpc_ex(layout, "crew_exists", ask, timeout=budget) if budget >= MIN_STEP_S else (None, True)
            if exists is None and not sent and ensure_crewd(layout, wait=min(CREWD_START_WAIT_S, left() / 4)):
                budget = min(CREW_EXISTS_TIMEOUT_S, left() / 2)
                if budget >= MIN_STEP_S:
                    exists = rpc(layout, "crew_exists", ask, timeout=budget)
            enabled = bool(exists and exists.get("exists"))
        if not enabled:
            relay_brief_passthrough(adapter, args.agent or adapter, plain, timeout=left())
            return 0
        if not ensure_crewd(layout, wait=min(CREWD_START_WAIT_S, left())):
            _emit_start(
                adapter, "Remembra crew unavailable: crewd did not start (run remembra-crew doctor). Crew rules are not active."
            )
            return 0
        join = rpc(
            layout,
            "join",
            {
                "adapter": adapter,
                "agent_id": args.agent or adapter,
                "client_session_id": str(sid),
                "cwd": str(cwd),
                "agent_pid": int(agent_pid),
                "source": str(payload.get("source") or args.source or "startup"),
                "model": payload.get("model") if isinstance(payload.get("model"), str) else None,
                "transcript_path": payload.get("transcript_path"),
                "project_id": args.project,
                "relay_project": relay_project,
            },
            timeout=max(MIN_STEP_S, left(reserve=0.5)),
        )
        if not join or not join.get("ok"):
            reason = (join or {}).get("message") or (join or {}).get("error") or "no answer from crewd"
            _emit_start(
                adapter,
                f"Remembra crew unavailable: {S.clip_item(str(reason), 200)}. Crew rules are not active for this session.",
            )
            return 0
        _env_file(join)
        brief = rpc(layout, "brief", {"key": join["key"]}, timeout=left()) if left() >= MIN_STEP_S else None
        brief = brief or {}
        text = str(brief.get("rendered") or "").strip()
        extra: list[str] = []
        if not text:
            extra.append(
                f"Crew: you are {join.get('callsign')} in crew {join.get('crew_id')}. "
                "Crew mode is automatic: claims, checkpoints and reports happen by hook."
            )
        if join.get("shared_checkout"):
            extra.append("Crew: another session works in this same checkout; one worktree per agent is recommended.")
        if join.get("githook_state") == "missing":
            extra.append("Crew: commit gate: missing in this checkout (run remembra-crew doctor).")
        if join.get("observe_only"):
            extra.append("Crew: this session joined in observe-only mode (plan limit): it cannot claim zones.")
        full = "\n".join([t for t in (text, *extra) if t])
        _emit_start(adapter, full[: S.TEXT_CAPS["session_start"]])
    except Exception as e:  # never break the agent's session start
        _err(f"start failed: {e.__class__.__name__}: {e}")
        _emit_start(adapter, "Remembra crew unavailable (internal error). Crew rules are not active for this session.")
    return 0


def _spool_hook(layout: Layout, kind: str, key: str, args: dict[str, Any]) -> None:
    from remembra.relay.crew import outbox

    sess = read_json(layout.session_file(key)) or {}
    try:
        outbox.spool(
            layout.outbox,
            kind,
            {"op": "end" if kind == "leave" else "stall", "args": args},
            session_key=key,
            crew_id=str(sess.get("crew_id") or ""),
            name=f"{kind}-{key}",
        )
    except (OSError, ValueError):
        pass
    respawn_crewd(layout)


def cmd_end(args: argparse.Namespace, layout: Layout) -> int:
    """SessionEnd fast path (≤300 ms): hand off to crewd (or spool) and return.

    A session that is not a crew session (it never joined: not a crew checkout, or skipped or routed at
    start) gets the plain relay close instead, with the relay's whole order.
    """
    try:
        payload = _hook_payload(args)
        adapter = args.hook or "claude-code"
        verdict = relay_hook_verdict(adapter, payload, "close")
        if verdict is not None and verdict[0] == "skip":
            # A Codex sub-agent thread carries its PARENT's session id: it must not end the parent's crew session.
            _hook_ack(adapter)
            return 0
        sid = payload.get("session_id") or args.session_id
        key = session_key(adapter, str(sid)) if sid and verdict is None else ""
        if not key or not layout.session_file(key).exists():
            if payload:  # an empty payload is an orphaned end hook (Gemini's third SessionEnd): nothing to do
                # the relay routes a hook another agent runs itself: it files that agent's handoff
                relay_close_passthrough(adapter, args.agent or adapter, payload)
            else:
                _hook_ack(adapter)
            return 0
        body = {
            "key": key,
            "reason": str(payload.get("reason") or args.reason or "other")[:64],
            "transcript_path": payload.get("transcript_path"),
        }
        if not rpc_send(layout, "end", body):
            _spool_hook(layout, "leave", key, {k: v for k, v in body.items() if k != "key"})
    except Exception as e:
        _err(f"end failed: {e.__class__.__name__}")
    return 0


def cmd_stall(args: argparse.Namespace, layout: Layout) -> int:
    """StopFailure: crewd saves the baton ref and records the stall (the hook survives ``-p`` exit, S0).

    Not a crew session: the plain relay close when the relay's own hooks would close on this stop (a usage
    or billing limit), so the handoff is written when the work stops, as without Crew mode.
    """
    try:
        payload = _hook_payload(args)
        adapter = args.hook or "claude-code"
        verdict = relay_hook_verdict(adapter, payload, "close")
        if verdict is not None and verdict[0] == "skip":
            return 0
        sid = payload.get("session_id") or args.session_id
        key = session_key(adapter, str(sid)) if sid and verdict is None else ""
        if not key or not layout.session_file(key).exists():
            if payload and relay_close_event(adapter, payload):
                relay_close_passthrough(adapter, args.agent or adapter, payload)
            return 0
        body = {
            "key": key,
            "error": str(payload.get("error") or args.error or "unknown"),
            "last_assistant_message": str(payload.get("last_assistant_message") or "")[:2000],
        }
        if not rpc_send(layout, "stall", body):
            _spool_hook(layout, "stall", key, {k: v for k, v in body.items() if k != "key"})
    except Exception as e:
        _err(f"stall failed: {e.__class__.__name__}")
    return 0


# ===========================================================================
# Session helpers
# ===========================================================================


def _whoami(layout: Layout) -> dict[str, Any] | None:
    res = _call(layout, "whoami", timeout=3.0)
    return res if res.get("ok") else None


def _snapshot(layout: Layout, crew_id: str) -> dict[str, Any]:
    from remembra.relay.crew.snapshot import load_snapshot

    return load_snapshot(layout.snapshot_file(crew_id)) or {}


def _api(
    layout: Layout,
    method: str,
    path: str,
    *,
    json_body: Any = None,
    params: Mapping[str, Any] | None = None,
    if_match: str | None = None,
    idem: str | None = None,
    timeout: float = 20.0,
) -> dict[str, Any]:
    return _call(
        layout,
        "api",
        {
            "method": method,
            "path": path,
            "json": json_body,
            "params": dict(params or {}),
            "if_match": if_match,
            "idem": idem,
            "timeout": timeout,
        },
        timeout=timeout + 2.0,
    )


def _zone_by_slug(snap: Mapping[str, Any], slug: str) -> dict[str, Any] | None:
    return next((dict(z) for z in snap.get("zones") or () if z.get("slug") == slug), None)


def _task_by_ref(layout: Layout, crew_id: str, ref: str) -> dict[str, Any] | None:
    res = _api(layout, "GET", f"/crews/{crew_id}/tasks")
    if not res.get("ok"):
        return None
    n = ref.upper().removeprefix("T-").removeprefix("T")
    for t in (res.get("body") or {}).get("tasks") or []:
        if str(t.get("number")) == n or t.get("id") == ref:
            return dict(t)
    return None


def _need_session(layout: Layout) -> dict[str, Any]:
    who = _whoami(layout)
    if who is None:
        raise SystemExit(_fail({"message": "this command runs inside a crew session (an agent started by a crew hook)"}))
    return who


# ===========================================================================
# status / whereami / doctor / renew
# ===========================================================================


def cmd_status(args: argparse.Namespace, layout: Layout) -> int:
    from remembra.crew import gatecore as G
    from remembra.relay.crew.snapshot import server_now

    who = _whoami(layout)
    if who is None:
        res = _call(layout, "status", timeout=3.0)
        if args.json:
            _print_json(res)
            return 0 if res.get("ok") else 1
        if not res.get("ok"):
            return _fail(res)
        print(f"crewd {res.get('version')} · server {'reachable' if res.get('server_reachable') else 'UNREACHABLE'}")
        for s in res.get("sessions") or []:
            print(
                f"- {s.get('callsign')} ({s.get('adapter')}, {s.get('enforcement')}) {s.get('state') or ''} "
                f"task {s.get('task_ref') or '-'}"
                f" · commit gate {s.get('githook_state')}"
            )
        return 0
    snap = _snapshot(layout, str(who["crew_id"]))
    from datetime import UTC, datetime

    now = datetime.fromtimestamp(server_now(snap, time.time()), UTC)
    lines = [G.render_you_line(snap, str(who["session_id"]), now)] if snap else [f"YOU: {who.get('callsign')} (no snapshot yet)"]
    dnt = G.render_do_not_touch(snap, str(who["session_id"])) if snap else ""
    if dnt:
        lines.append(dnt)
    live = [s for s in snap.get("sessions") or () if s.get("state") not in ("ended", "lost")]
    lines.append("CREW: " + " · ".join(f"{s.get('callsign')} {s.get('state')}" for s in live) if live else "CREW: only you")
    titles = [
        f"T-{t.get('number')} title: {agent_text(t.get('title'))}"
        for t in snap.get("tasks") or ()
        if t.get("owner_session_id") == who["session_id"]
    ]
    block = data_block(titles)
    if args.json:
        _print_json({"who": who, "lines": lines})
        return 0
    print("\n".join(lines + ([block] if block else [])))
    return 0


def cmd_whereami(args: argparse.Namespace, layout: Layout) -> int:
    from remembra.crew import gatecore as G

    who = _need_session(layout)
    snap = _snapshot(layout, str(who["crew_id"]))
    top = str(who.get("toplevel") or "")
    cwd = os.path.realpath(os.getcwd())
    rel = os.path.relpath(cwd, top) if top and (cwd == top or cwd.startswith(top + "/")) else None
    idx = G.SnapshotIndex(snap) if snap else None
    zones: list[str] = []
    if idx is not None and rel is not None:
        probe = "x" if rel == "." else f"{rel}/x"
        zones = [str(z.get("slug")) for z in idx.zones.match(probe, False)]
    mine = [c for c in snap.get("claims") or () if c.get("holder_session_id") == who["session_id"]]
    out: dict[str, Any] = {
        "session": who.get("callsign"),
        "crew": who.get("crew_id"),
        "project": who.get("project_id"),
        "checkout": top,
        "cwd_rel": rel,
        "zones_here": zones,
        "task": who.get("task_ref"),
        "claims": [
            {
                "zone": next((z.get("slug") for z in snap.get("zones") or () if z.get("id") == c.get("zone_id")), None),
                "state": c.get("state"),
                "epoch": c.get("epoch"),
                "lease_expires_at": c.get("lease_expires_at"),
            }
            for c in mine
        ],
    }
    if args.json:
        _print_json(out)
        return 0
    print(f"You are {out['session']} in project {out['project']} ({out['crew']}); task {out['task'] or '-'}")
    print(f"Checkout {top} · here: {rel or '(outside the checkout)'} · zones here: {', '.join(zones) or 'none'}")
    for c in out["claims"]:
        print(f"  holding zone {c['zone']} ({c['state']}, epoch {c['epoch']}, lease until {c['lease_expires_at'] or '-'})")
    return 0


def cmd_doctor(args: argparse.Namespace, layout: Layout) -> int:
    from remembra.relay.crew.gate import crewd_status, verify_gate

    gate = verify_gate(layout)
    status = crewd_status(layout)
    res = rpc(layout, "doctor", timeout=3.0)
    report = {
        "home": str(layout.root),
        "gate_installed": gate.installed,
        "gate_ok": gate.ok,
        "crewd_running": bool(status.get("running")),
        "socket": str(layout.socket),
        "crewd": res,
    }
    if args.json:
        _print_json(report)
    else:
        print(f"gate: {'ok' if gate.ok else ('MODIFIED' if gate.installed else 'not installed')}")
        print(f"crewd: {'running' if status.get('running') else 'NOT running'} (socket {layout.socket})")
        if res:
            print(
                f"server: {'reachable' if res.get('server_reachable') else 'UNREACHABLE'}"
                f"{' (outage)' if res.get('server_outage') else ''}"
            )
            print(f"outbox: {res.get('outbox', {}).get('pending', 0)} pending")
            for s in res.get("sessions") or []:
                print(
                    f"- {s.get('callsign')}: commit gate {s.get('githook_state')}, "
                    f"agent {'alive' if s.get('agent_alive') else 'gone'},"
                    f" fenced paths {s.get('fenced')}"
                )
    healthy = gate.ok and bool(status.get("running"))
    return 0 if healthy else 1


def cmd_renew(args: argparse.Namespace, layout: Layout) -> int:
    who = _need_session(layout)
    res = _call(layout, "renew", {"key": who["key"]}, timeout=15.0)
    if not res.get("ok"):
        return _fail(res)
    print(f"Lease renewed: expires {res.get('lease_expires_at') or '(no held claims)'}")
    return 0


# ===========================================================================
# claims, adopt, tasks
# ===========================================================================


def _claim_target(snap: Mapping[str, Any], target: str) -> dict[str, Any]:
    if ":" in target and target.split(":", 1)[0] in ("schema", "deploy", "service", "supabase", "mcp"):
        return {"resource": target}
    zone = _zone_by_slug(snap, target)
    if zone is not None:
        return {"zone_id": zone["id"]}
    return {"path_glob": target.strip("/")}


def cmd_claim(args: argparse.Namespace, layout: Layout) -> int:
    who = _need_session(layout)
    snap = _snapshot(layout, str(who["crew_id"]))
    body: dict[str, Any] = {**_claim_target(snap, args.target), "mode": args.mode, "wait": args.wait > 0, "source": "mcp"}
    if args.wait:
        body["wait_s"] = min(S.CLAIM_WAIT_MAX_S, args.wait)
    if args.task:
        task = _task_by_ref(layout, str(who["crew_id"]), args.task)
        if task is None:
            return _fail({"message": f"no task {args.task}"})
        body["task_id"] = task["id"]
    if args.reason:
        body["reason"] = args.reason[:280]
    res = _api(layout, "POST", f"/crews/{who['crew_id']}/claims", json_body=body, timeout=float(args.wait or 0) + 15.0)
    data = res.get("body") or {}
    detail = data.get("detail", data) if isinstance(data, dict) else {}
    if res.get("status") in (200, 201):
        claim = data.get("claim") or {}
        print(f"GRANTED {args.target} ({claim.get('mode')}, epoch {claim.get('epoch')})")
        return 0
    if res.get("status") == 202:
        print(f"QUEUED {args.target}" + (f" (waited {args.wait}s)" if args.wait else ""))
        return 0
    blockers = detail.get("blockers") if isinstance(detail, dict) else None
    if blockers:
        b = blockers[0]
        print(
            f"REFUSED: {args.target} held {str(b.get('reason') or '').upper()} by {b.get('holder_callsign') or 'a human'}. "
            f'Work elsewhere or ask with: remembra-crew say --to @{b.get("holder_callsign") or "mani"} --kind request_release "…"'
        )
        return 1
    return _fail(res)


def cmd_release(args: argparse.Namespace, layout: Layout) -> int:
    who = _need_session(layout)
    snap = _snapshot(layout, str(who["crew_id"]))
    zone = _zone_by_slug(snap, args.target)
    claim = next(
        (
            c
            for c in snap.get("claims") or ()
            if c.get("holder_session_id") == who["session_id"]
            and (c.get("id") == args.target or (zone and c.get("zone_id") == zone["id"]) or c.get("path_glob") == args.target)
        ),
        None,
    )
    if claim is None:
        return _fail({"message": f"you hold no claim on {args.target}"})
    res = _api(layout, "POST", f"/claims/{claim['id']}/release", json_body={"baton": bool(args.baton)})
    if not res.get("ok"):
        return _fail(res)
    print(f"RELEASED {args.target}" + (" (held as a baton for the next pickup)" if args.baton else ""))
    return 0


def cmd_adopt(args: argparse.Namespace, layout: Layout) -> int:
    who = _need_session(layout)
    res = _call(layout, "adopt", {"key": who["key"], "task": args.task, "restore_only": args.restore_only}, timeout=60.0)
    if not res.get("ok"):
        return _fail(res)
    print(f"ADOPTED {res.get('task_ref')}" + (f" · claims {', '.join(res.get('claims') or [])}" if res.get("claims") else ""))
    restore = res.get("restore")
    if restore:
        if restore.get("restored"):
            print(
                f"Work restored from {restore.get('ref')}: {len(restore.get('files') or [])} files"
                f"{', ' + str(len(restore.get('deleted') or [])) + ' deleted' if restore.get('deleted') else ''}"
                f" on branch {restore.get('branch')}"
            )
        elif restore.get("reason") == "dirty_tree":
            print(
                "Saved work NOT restored: this checkout has uncommitted changes. "
                f"Commit or move them, then run: {restore.get('next_command')}"
            )
        elif restore.get("reason") == "restore_failed":
            state = (
                "Your checkout was put back as it was."
                if restore.get("rolled_back")
                else "Check `git status`: the checkout could not be fully put back."
            )
            print(
                f"Saved work NOT restored: {S.clip_item(str(restore.get('error') or 'git failed'), 200)}. {state}"
                f" Fix the cause, then run: {restore.get('next_command')}"
            )
        else:
            print(f"Saved work not restored ({restore.get('reason')}).")
    elif res.get("baton_ref") is None:
        print("No saved uncommitted work came with this baton.")
    return 0


def _print_tasks(tasks: Sequence[Mapping[str, Any]]) -> None:
    for t in tasks:
        owner = t.get("owner_session_id") or "-"
        print(f"T-{t.get('number')} {t.get('status')} owner {owner} zones {len(t.get('zone_ids') or [])}")
    block = data_block([f"T-{t.get('number')} title: {agent_text(t.get('title'))}" for t in tasks])
    if block:
        print(block)


def cmd_task(args: argparse.Namespace, layout: Layout) -> int:
    who = _need_session(layout)
    crew = str(who["crew_id"])
    action = args.action
    if action == "list":
        res = _api(layout, "GET", f"/crews/{crew}/tasks")
        if not res.get("ok"):
            return _fail(res)
        _print_tasks((res.get("body") or {}).get("tasks") or [])
        return 0
    if action == "create":
        if not args.title:
            return _fail({"message": "--title is required"})
        snap = _snapshot(layout, crew)
        zone_ids = []
        for slug in args.zone or []:
            z = _zone_by_slug(snap, slug)
            if z is None:
                return _fail({"message": f"no zone {slug}"})
            zone_ids.append(z["id"])
        acceptance = []
        for i, spec in enumerate(args.accept or []):
            kind, _, rest = spec.partition(":")
            text, _, match = rest.partition("|")
            item: dict[str, Any] = {"id": f"c{i + 1}", "kind": kind or "manual", "text": text or spec, "required": True}
            if match:
                item["url" if kind == "deploy" else "match"] = match
            acceptance.append(item)
        body = {"title": args.title, "zone_ids": zone_ids, "acceptance": acceptance, "depends_on": []}
        if args.body:
            body["body"] = args.body
        res = _api(layout, "POST", f"/crews/{crew}/tasks", json_body=body, idem=f"task{uuid.uuid4().hex[:16]}")
        if not res.get("ok"):
            return _fail(res)
        task = (res.get("body") or {}).get("task") or {}
        print(f"CREATED T-{task.get('number')} ({task.get('status')})")
        return 0
    if not args.task:
        return _fail({"message": "which task? (T-n)"})
    task = _task_by_ref(layout, crew, args.task)
    if task is None:
        return _fail({"message": f"no task {args.task}"})
    tid = task["id"]
    if action == "start":
        head = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True, check=False).stdout.strip() or None  # noqa: S603,S607
        res = _api(layout, "POST", f"/tasks/{tid}/start", json_body={"head": head} if head else {})
    elif action == "update":
        patch: dict[str, Any] = {}
        if args.status:
            patch["status"] = args.status
        if args.phase:
            patch["phase"] = args.phase
        if not patch:
            return _fail({"message": "nothing to update (--status or --phase)"})
        res = _api(layout, "PATCH", f"/tasks/{tid}", json_body=patch, if_match=str(task.get("version") or 1))
        if not res.get("ok") and "report_required" in json.dumps(res.get("body") or {}):
            print(f"REFUSED: T-{task.get('number')} needs a completion report: run remembra-crew report T-{task.get('number')}")
            return 1
    elif action == "block":
        res = _api(layout, "POST", f"/tasks/{tid}/block", json_body={"reason": (args.reason or "blocked")[:280]})
    elif action == "unblock":
        res = _api(layout, "POST", f"/tasks/{tid}/unblock", json_body={})
    elif action == "release":
        res = _api(layout, "POST", f"/tasks/{tid}/release", json_body={"baton": bool(args.baton)})
    else:
        return _fail({"message": f"unknown task action {action}"})
    if not res.get("ok"):
        return _fail(res)
    new = (res.get("body") or {}).get("task") or {}
    print(f"T-{new.get('number') or task.get('number')} {new.get('status') or 'ok'}")
    return 0


# ===========================================================================
# checkpoint, report, say, watch, zones, bypass
# ===========================================================================


def cmd_checkpoint(args: argparse.Namespace, layout: Layout) -> int:
    who = _need_session(layout)
    extra = {k: v for k, v in (("summary", args.summary), ("next_step", args.next)) if v}
    res = _call(layout, "checkpoint", {"key": who["key"], "trigger": "turn", "force": True, "extra": extra}, timeout=20.0)
    if not res.get("ok"):
        return _fail(res)
    body = res.get("body") or {}
    ck = body.get("checkpoint") if isinstance(body, dict) else None
    print(f"CHECKPOINT {(ck or {}).get('id') or ('spooled' if res.get('spooled') else 'recorded')}")
    return 0


def cmd_report(args: argparse.Namespace, layout: Layout) -> int:
    who = _need_session(layout)
    sections = {
        "done": args.done or [],
        "not_done": args.not_done or [],
        "failing": args.failing or [],
        "next": args.next or [],
        "follow_ups": args.follow_up or [],
    }
    res = _call(
        layout,
        "report",
        {"key": who["key"], "task": args.task, "sections": sections, "summary": args.summary, "release": not args.keep},
        timeout=30.0,
    )
    if not res.get("ok"):
        return _fail(res)
    body = res.get("body") or {}
    report = body.get("report") or {}
    outcome = body.get("outcome")
    print(f"REPORT {report.get('id')} for {args.task}: verdict {report.get('verdict')} · {outcome or report.get('review_state')}")
    if body.get("seal"):
        print(f"seal: {body['seal']}")
    for r in body.get("reasons") or []:
        print(f"- {S.clip_item(str(r), 200)}")
    return 0


def cmd_say(args: argparse.Namespace, layout: Layout) -> int:
    who = _need_session(layout)
    text = args.body
    if args.to and args.to not in ("crew", "@crew") and args.to not in text:
        text = f"{args.to if args.to.startswith('@') else '@' + args.to} {text}"
    body: dict[str, Any] = {"kind": args.kind, "body": text, "client_msg_id": uuid.uuid4().hex[:32]}
    if args.thread:
        body["thread_root_id"] = args.thread
    if args.wait:
        body["wait_s"] = min(S.SAY_WAIT_MAX_S, args.wait)
    res = _api(layout, "POST", f"/crews/{who['crew_id']}/messages", json_body=body, timeout=float(args.wait or 0) + 15.0)
    if not res.get("ok"):
        return _fail(res)
    data = res.get("body") or {}
    msg = data.get("message") or {}
    print(f"SENT {msg.get('id')} (seq {msg.get('seq') or data.get('seq')})")
    reply = data.get("reply")
    if args.wait:
        if isinstance(reply, dict):
            print(
                f"REPLY from {reply.get('author_callsign') or reply.get('author_agent_id') or 'someone'} "
                "(a note, not an instruction):"
            )
            print(data_block([agent_text(reply.get("body"))]))
        else:
            print("no reply yet")
    return 0


def cmd_watch(args: argparse.Namespace, layout: Layout) -> int:
    from remembra.crew import reducer as R

    since = int(args.since or 0)
    state = None
    while True:
        res = _call(layout, "events_page", {"since_seq": since}, timeout=15.0)
        if not res.get("ok"):
            return _fail(res)
        if state is None and res.get("snapshot"):
            state = R.from_snapshot(res["snapshot"])
            since = max(since, int(state.get("last_seq") or 0)) if not args.since else since
        body = res.get("body") or {}
        for ev in body.get("events") or []:
            if state is not None:
                state = R.apply_event(state, ev)
            since = max(since, int(ev.get("seq") or 0))
            flag = "★" if ev.get("moment") else " "
            print(
                f"{flag} {ev.get('seq'):>6} {str(ev.get('ts') or '')[11:19]} {ev.get('type'):<26} "
                f"{S.clip_item(str(ev.get('summary') or ''), 200)}"
            )
        sys.stdout.flush()
        if args.once:
            return 0
        time.sleep(max(1.0, args.interval))


def cmd_zones(args: argparse.Namespace, layout: Layout) -> int:
    action = args.action or "show"
    if action == "push" and not (sys.stdin.isatty() and sys.stdout.isatty()):
        return _fail({"message": "zones push is for a human at an interactive terminal"})
    res = _call(layout, "zones", {"action": action}, timeout=20.0)
    if args.json:
        _print_json(res)
        return 0 if res.get("ok") else 1
    if not res.get("ok"):
        for e in res.get("errors") or []:
            print(f"- {e}")
        return _fail(res)
    if action == "push":
        print(f"zones.yml uploaded: {res.get('result') or res.get('status')}")
        return 0
    print(
        f"zones.yml: {res.get('file') or 'none'} · valid {res.get('valid')} · pending upload {res.get('pending_upload')}"
        + (" · TEMPORARY ZONES (no zones.yml)" if res.get("bootstrap") else "")
    )
    for e in res.get("errors") or []:
        print(f"  error: {e}")
    for z in res.get("zones") or []:
        print(f"- {z.get('slug')} ({z.get('mode')}, {z.get('source')}): {', '.join(z.get('include') or [])}")
    return 0


def cmd_bypass(args: argparse.Namespace, layout: Layout) -> int:
    """D34: humans only, at an interactive terminal; the mechanism is never shown to agents."""
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        return _fail({"message": "run this at an interactive terminal"})
    answer = input(f"Allow one crew bypass for session {args.session}? Type the session name again to confirm: ").strip()
    if answer != args.session:
        return _fail({"message": "not confirmed"})
    res = _call(layout, "bypass", {"session": args.session, "code": args.code, "minutes": args.minutes}, timeout=15.0)
    if not res.get("ok"):
        return _fail(res)
    print(
        f"Bypass granted to {res.get('callsign')} for {int(res.get('expires_in_s') or 0) // 60} min,"
        f" scope {res.get('scope') or 'all'} (single use, recorded)."
    )
    return 0


# ===========================================================================
# argparse
# ===========================================================================


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="remembra-crew", description="Remembra Crew mode: coordination of several agents on one project."
    )
    sub = p.add_subparsers(dest="command", required=True)

    def hook_opts(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("--hook", help="read the hook payload from stdin using this adapter (claude-code, codex, …)")
        sp.add_argument("--agent", help="agent id (default: the adapter name)")
        sp.add_argument("--session-id", dest="session_id")
        sp.add_argument("--cwd")

    sp = sub.add_parser("start", help="SessionStart hook: join the crew and print the brief")
    hook_opts(sp)
    sp.add_argument("--source")
    sp.add_argument("--project")
    sp.add_argument("--crew", action="store_true", help="join the crew even without .remembra/ in the repo")
    sp.add_argument("--agent-pid", dest="agent_pid", type=int, help=argparse.SUPPRESS)
    sp = sub.add_parser("end", help="SessionEnd hook")
    hook_opts(sp)
    sp.add_argument("--reason")
    sp = sub.add_parser("stall", help="StopFailure hook")
    hook_opts(sp)
    sp.add_argument("--error")

    for name, helptext in (
        ("status", "crew status (YOU line, DO NOT TOUCH)"),
        ("whereami", "where this session is"),
        ("doctor", "check the local runtime"),
        ("renew", "renew this session's leases now"),
    ):
        sp = sub.add_parser(name, help=helptext)
        sp.add_argument("--json", action="store_true")

    sp = sub.add_parser("claim", help="claim a zone, path or resource")
    sp.add_argument("target")
    sp.add_argument("--mode", default="exclusive", choices=S.CLAIM_MODES)
    sp.add_argument("--task")
    sp.add_argument("--wait", type=int, default=0, help="seconds to wait for a queued claim (≤300)")
    sp.add_argument("--reason")
    sp = sub.add_parser("release", help="release a claim")
    sp.add_argument("target")
    sp.add_argument("--baton", action="store_true")

    sp = sub.add_parser("adopt", help="take over an offered baton (restores the saved work)")
    sp.add_argument("task")
    sp.add_argument("--restore-only", action="store_true", dest="restore_only")

    sp = sub.add_parser("task", help="tasks")
    sp.add_argument("action", choices=["list", "create", "start", "update", "block", "unblock", "release"])
    sp.add_argument("task", nargs="?")
    sp.add_argument("--title")
    sp.add_argument("--body")
    sp.add_argument("--zone", action="append")
    sp.add_argument("--accept", action="append", help="kind:text|match (e.g. 'test:POS tests pass|npm test -- pos')")
    sp.add_argument("--status")
    sp.add_argument("--phase")
    sp.add_argument("--reason")
    sp.add_argument("--baton", action="store_true")

    sp = sub.add_parser("checkpoint", help="record a checkpoint now")
    sp.add_argument("--summary")
    sp.add_argument("--next")

    sp = sub.add_parser("report", help="submit the report for a task")
    sp.add_argument("task")
    sp.add_argument("--done", action="append")
    sp.add_argument("--not-done", action="append", dest="not_done")
    sp.add_argument("--failing", action="append")
    sp.add_argument("--next", action="append")
    sp.add_argument("--follow-up", action="append", dest="follow_up")
    sp.add_argument("--summary")
    sp.add_argument("--keep", action="store_true", help="keep the task's claims")

    sp = sub.add_parser("say", help="post to the crew channel")
    sp.add_argument("body")
    sp.add_argument(
        "--kind", default="chat", choices=["chat", "question", "answer", "note", "request_release", "decision", "status"]
    )
    sp.add_argument("--to")
    sp.add_argument("--thread")
    sp.add_argument("--wait", type=int, default=0)

    sp = sub.add_parser("watch", help="follow the crew event feed")
    sp.add_argument("--since", type=int, default=0)
    sp.add_argument("--once", action="store_true")
    sp.add_argument("--interval", type=float, default=3.0)

    sp = sub.add_parser("zones", help="show or (human, at a TTY) push .remembra/zones.yml")
    sp.add_argument("action", nargs="?", choices=["show", "push"])
    sp.add_argument("--json", action="store_true")

    # Installers (WP-10): parsed by their own modules; listed here for --help.
    for name, helptext in (
        ("connect", "install crew mode on this machine (dry run unless --apply; asks at a terminal)"),
        ("verify", "round-trip check that an agent's crew hooks fire (switches it to enforce)"),
    ):
        sp = sub.add_parser(name, help=helptext, add_help=False)
        sp.add_argument("rest", nargs=argparse.REMAINDER)

    # The owner's one-time bypass at their own terminal (D34): it works, but the help does not advertise it
    # (a parser without help= is left out of the command list; argparse's SUPPRESS would print "==SUPPRESS==").
    sp = sub.add_parser("bypass")
    sp.add_argument("--session", required=True)
    sp.add_argument("--code")
    sp.add_argument("--minutes", type=int, default=15)
    sub.metavar = "{" + ",".join(name for name in sub.choices if name != "bypass") + "}"
    return p


COMMANDS: Final = {
    "start": cmd_start,
    "end": cmd_end,
    "stall": cmd_stall,
    "status": cmd_status,
    "whereami": cmd_whereami,
    "doctor": cmd_doctor,
    "renew": cmd_renew,
    "claim": cmd_claim,
    "release": cmd_release,
    "adopt": cmd_adopt,
    "task": cmd_task,
    "checkpoint": cmd_checkpoint,
    "report": cmd_report,
    "say": cmd_say,
    "watch": cmd_watch,
    "zones": cmd_zones,
    "bypass": cmd_bypass,
}


def _installer(name: str, argv: list[str]) -> int:
    """``connect`` / ``verify`` run WP-10's own parsers (they own their options and consent rules)."""
    if name == "connect":
        from remembra.relay.crew import install

        return int(install.main(argv))
    from remembra.relay.crew import verify

    return int(verify.main(argv))


INSTALLER_COMMANDS: Final = ("connect", "verify")


def main(argv: Sequence[str] | None = None, *, layout: Layout | None = None) -> int:
    raw = list(sys.argv[1:] if argv is None else argv)
    if raw and raw[0] in INSTALLER_COMMANDS:
        try:
            return _installer(raw[0], raw[1:])
        except SystemExit as e:
            return int(e.code) if isinstance(e.code, int) else 2
    try:
        args = build_parser().parse_args(raw)
    except SystemExit as e:
        # hook commands never fail the agent, even when miswired
        return 0 if raw and raw[0] in HOOK_COMMANDS else (int(e.code) if isinstance(e.code, int) else 2)
    layout = layout or Layout.from_env()
    try:
        return int(COMMANDS[args.command](args, layout))
    except SystemExit as e:
        return int(e.code) if isinstance(e.code, int) else 1
    except Exception as e:
        _err(f"{args.command} failed: {e.__class__.__name__}: {e}")
        return 0 if args.command in HOOK_COMMANDS else 1


def entrypoint() -> None:
    sys.exit(main())


if __name__ == "__main__":
    entrypoint()


__all__ = ["main", "entrypoint", "ensure_crewd", "data_block", "Path"]
