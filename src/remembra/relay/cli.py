"""``remembra-relay`` — leave a trail when an agent stops; pick it up when one starts.

Subcommands::

    remembra-relay brief   [--agent X] [--cwd DIR] [--hook NAME] [--format text|json|hook-json|cursor-json]
    remembra-relay close   [--agent X] [--session-id S] [--cwd DIR] [--transcript PATH] [--reason R] [--hook NAME]
    remembra-relay trail   [--cwd DIR] [--project P] [--limit N]
    remembra-relay resolve [--cwd DIR] [--project P] [--bind]
    remembra-relay connect [--apply] [--agent NAME ...] [--include-unverified] [--agents-md PATH]

``brief``/``close``/``trail`` are hook-safe: they never block (≤10 s total,
git calls and HTTP bounded), never raise, always exit 0 and report problems
on stderr. With ``--hook NAME`` the agent's hook payload is read from stdin
(session id, cwd, transcript path, end reason) using that adapter's mapping.

The project is resolved from the git repository in ``--cwd`` (remote URL,
root commit), so every checkout of the same repo — any machine, drive or
worktree — shares one trail. Outside git the working directory is used. A
configured project (``REMEMBRA_RELAY_PROJECT``, else ``REMEMBRA_PROJECT`` /
the MCP env / credentials, unless it is ``default``) names a location the
server has not seen yet, so existing users keep their one namespace; with
nothing configured each repository gets its own project.

The whole run is bounded by ``TOTAL_BUDGET_SECONDS``: HTTP runs in a worker
thread that is abandoned (the fallback text is printed) when the budget runs
out, so a slow or dripping server cannot hold the agent's session start.

Configuration is discovered from the environment or existing agent config
(see :mod:`remembra.relay.config`); this tool never writes API keys anywhere.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import socket
import sys
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from remembra.client.project import normalize_project_id, parse_project_aliases
from remembra.relay import facts as factlib
from remembra.relay.adapters import REGISTRY, Adapter, agents_md, backup_and_write, get_adapter, relay_command
from remembra.relay.config import RelayConfig, load_config

TOTAL_BUDGET_SECONDS = 9.5
GIT_BUDGET_SECONDS = 4.0
HTTP_TIMEOUT_SECONDS = 8.0
STDIN_WAIT_SECONDS = 1.0
STATE_TTL_SECONDS = 14 * 86400
ADHOC_SESSION_MAX_AGE_SECONDS = 12 * 3600
USER_AGENT = "remembra-relay"


def _err(message: str) -> None:
    try:
        print(f"remembra-relay: {message}", file=sys.stderr)
    except Exception:
        pass


def _version() -> str:
    try:
        from remembra import __version__

        return str(__version__)
    except Exception:
        return "unknown"


# ---------------------------------------------------------------------------
# Hook payload, state
# ---------------------------------------------------------------------------


def read_hook_payload(timeout: float = STDIN_WAIT_SECONDS) -> dict[str, Any]:
    """The hook's stdin JSON, or {} (TTY, empty, not JSON, or nothing within ``timeout``)."""
    try:
        if sys.stdin is None or sys.stdin.isatty():
            return {}
    except (ValueError, OSError):
        return {}
    box: dict[str, str] = {}

    def reader() -> None:
        try:
            box["data"] = sys.stdin.read(4 * 1024 * 1024)
        except Exception:
            box["data"] = ""

    thread = threading.Thread(target=reader, daemon=True)
    thread.start()
    thread.join(timeout)
    raw = box.get("data", "")
    if not raw.strip():
        return {}
    try:
        data = json.loads(raw)
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def _state_dir(home: Path) -> Path:
    return home / ".remembra" / "relay" / "sessions"


def _state_path(home: Path, agent: str, session_id: str) -> Path:
    digest = hashlib.sha256(f"{agent}\x1f{session_id}".encode()).hexdigest()[:24]
    return _state_dir(home) / f"{digest}.json"


def save_session_state(home: Path, agent: str, session_id: str, state: dict[str, Any]) -> None:
    directory = _state_dir(home)
    try:
        directory.mkdir(parents=True, exist_ok=True)
        now = time.time()
        for old in directory.glob("*.json"):
            try:
                if now - old.stat().st_mtime > STATE_TTL_SECONDS:
                    old.unlink()
            except OSError:
                pass
        path = _state_path(home, agent, session_id)
        if not path.exists():  # the first brief of a session records where it started
            path.write_text(json.dumps(state))
            os.chmod(path, 0o600)
    except OSError as e:
        _err(f"could not record session start ({e.__class__.__name__})")


def load_session_state(home: Path, agent: str, session_id: str) -> dict[str, Any]:
    try:
        data = json.loads(_state_path(home, agent, session_id).read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _adhoc_marker_path(home: Path, agent: str, host: str, anchor: str) -> Path:
    digest = hashlib.sha256(f"adhoc\x1f{agent}\x1f{host}\x1f{anchor}".encode()).hexdigest()[:24]
    return _state_dir(home) / f"adhoc-{digest}.json"


def _new_adhoc_session_id() -> str:
    return f"adhoc-{datetime.now(UTC).strftime('%Y%m%dT%H%M%S')}-{os.urandom(4).hex()}"


def start_adhoc_session(home: Path, agent: str, host: str, anchor: str, state: dict[str, Any]) -> str:
    """Record a new session for an agent that has no session id (``brief``
    without a hook). ``close`` in the same place picks up its id, so each
    session gets its own handoff instead of one per day."""
    session_id = _new_adhoc_session_id()
    path = _adhoc_marker_path(home, agent, host, anchor)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({**state, "session_id": session_id}))
        os.chmod(path, 0o600)
    except OSError as e:
        _err(f"could not record session start ({e.__class__.__name__})")
    return session_id


def current_adhoc_session(home: Path, agent: str, host: str, anchor: str) -> dict[str, Any]:
    """The session ``brief`` started here, if recent; {} otherwise."""
    path = _adhoc_marker_path(home, agent, host, anchor)
    try:
        if time.time() - path.stat().st_mtime > ADHOC_SESSION_MAX_AGE_SECONDS:
            return {}
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) and isinstance(data.get("session_id"), str) else {}


# ---------------------------------------------------------------------------
# Context
# ---------------------------------------------------------------------------


class Context:
    """Resolved inputs shared by the hook-safe subcommands."""

    def __init__(self, args: argparse.Namespace, payload: dict[str, Any] | None = None) -> None:
        self.args = args
        self.deadline = factlib.Deadline(TOTAL_BUDGET_SECONDS)
        self.adapter: Adapter | None = get_adapter(getattr(args, "hook", None))
        self.payload = payload if payload is not None else (read_hook_payload() if self.adapter else {})
        mapped = self.adapter.spec.payload.extract(self.payload) if self.adapter else {}
        self.hook_fields = mapped
        prefer = self.adapter.spec.config_source if self.adapter else None
        self.config: RelayConfig = load_config(agent=getattr(args, "agent", None), prefer=prefer)
        agent = self.config.agent_id or (self.adapter.spec.name if self.adapter else None)
        self.agent: str | None = agent
        cwd = getattr(args, "cwd", None) or mapped.get("cwd") or os.getcwd()
        self.cwd = Path(cwd).expanduser()
        self.home = Path(os.environ.get("HOME") or Path.home())
        self.host = socket.gethostname()
        git_deadline = factlib.Deadline(min(GIT_BUDGET_SECONDS, TOTAL_BUDGET_SECONDS))
        self.repo = factlib.repo_info(self.cwd, git_deadline)

    def configured_project(self) -> str | None:
        """The project this client is configured for, used to name a location
        the server has not seen yet (``default`` does not count)."""
        aliases = parse_project_aliases(self.config.project_aliases)
        for value in (os.environ.get("REMEMBRA_RELAY_PROJECT"), self.config.project):
            if value and value.strip():
                project = normalize_project_id(value, aliases)
                if project and project != "default":
                    return project
        return None

    def project_params(self) -> dict[str, Any]:
        """Either ``project_id`` or a location to resolve server-side (+ the configured project as hint)."""
        aliases = parse_project_aliases(self.config.project_aliases)
        explicit = getattr(self.args, "project", None)
        if explicit:
            return {"project_id": normalize_project_id(explicit, aliases)}
        locator = self.repo.locator(self.cwd, self.host)
        hint = self.configured_project()
        if hint:
            locator["hint_project"] = hint
        return locator

    def client(self) -> httpx.Client:
        headers = {"User-Agent": f"{USER_AGENT}/{_version()}", "Accept": "application/json"}
        if self.config.api_key:
            headers["X-API-Key"] = self.config.api_key
        if self.agent:
            headers["X-Remembra-Agent-Id"] = self.agent
        timeout = max(0.5, min(HTTP_TIMEOUT_SECONDS, self.deadline.end - time.monotonic()))
        return httpx.Client(base_url=self.config.url, headers=headers, timeout=httpx.Timeout(timeout, connect=min(4.0, timeout)))

    def request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        """One HTTP call bounded by the WHOLE remaining budget.

        httpx timeouts apply per phase / per socket read, so a server that
        drips one byte every few seconds never trips them. The call runs in a
        daemon thread; when the budget runs out it is abandoned and
        ``TimeoutError`` is raised (the process can still exit: the thread is
        a daemon).
        """
        remaining = self.deadline.end - time.monotonic()
        if remaining <= 0.1:
            raise TimeoutError(f"the {TOTAL_BUDGET_SECONDS:g}s budget ran out before the request")
        box: dict[str, Any] = {}

        def run() -> None:
            try:
                with self.client() as http:
                    box["response"] = http.request(method, path, **kwargs)
            except BaseException as e:  # handed to the caller's thread
                box["error"] = e

        worker = threading.Thread(target=run, name="remembra-relay-http", daemon=True)
        worker.start()
        worker.join(remaining)
        if worker.is_alive():
            raise TimeoutError(f"no complete response from {self.config.url} within the {TOTAL_BUDGET_SECONDS:g}s budget")
        if "error" in box:
            raise box["error"]
        response: httpx.Response = box["response"]
        return response


def _http_error(response: httpx.Response) -> str:
    try:
        detail = response.json().get("detail", response.text)
    except Exception:
        detail = response.text
    return f"HTTP {response.status_code}: {str(detail)[:300]}"


# ---------------------------------------------------------------------------
# brief
# ---------------------------------------------------------------------------


def _emit_brief(mode: str, text: str, raw: dict[str, Any] | None) -> None:
    if mode == "json":
        print(json.dumps(raw if raw is not None else {"error": text}, default=str))
    elif mode == "hook-json":
        print(json.dumps({"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": text}}))
    elif mode == "cursor-json":
        print(json.dumps({"additional_context": text}))
    else:
        print(text)


def cmd_brief(args: argparse.Namespace) -> int:
    mode = args.format or "text"
    try:
        ctx = Context(args)
        if not args.format and ctx.adapter:
            mode = ctx.adapter.spec.output
        if not ctx.config.api_key:
            _emit_brief(
                mode, "Remembra brief unavailable: no API key (set REMEMBRA_API_KEY or configure the remembra MCP server).", None
            )
            return 0
        session_id = ctx.hook_fields.get("session_id") or args.session_id
        start = {
            "head": ctx.repo.head_commit,
            "started_at": datetime.now(UTC).isoformat(),
            "started_ts": time.time(),
            "cwd": str(ctx.cwd),
        }
        if session_id and ctx.agent and ctx.repo.is_git:
            save_session_state(ctx.home, ctx.agent, session_id, start)
        elif not session_id and not os.environ.get("REMEMBRA_SESSION_ID"):
            # No session id (AGENTS.md / manual use): start a fresh ad-hoc session here.
            start_adhoc_session(ctx.home, ctx.agent or "unknown-agent", ctx.host, ctx.repo.toplevel or str(ctx.cwd), start)
        params: dict[str, Any] = {"recent_n": args.recent}
        if ctx.agent:
            params["agent_id"] = ctx.agent
        params.update(ctx.project_params())
        if ctx.repo.branch:
            params["branch"] = ctx.repo.branch
        if ctx.repo.head_commit:
            params["head_commit"] = ctx.repo.head_commit
        response = ctx.request("GET", "/api/v1/session/brief", params=params)
        if response.status_code >= 400:
            message = f"Remembra brief unavailable: {_http_error(response)}"
            _err(message)
            _emit_brief(mode, message, None)
            return 0
        brief = response.json()
        _emit_brief(mode, str(brief.get("rendered") or ""), brief)
    except Exception as e:  # never break the agent's session start
        _err(f"brief failed: {e.__class__.__name__}: {e}")
        try:
            _emit_brief(mode, f"Remembra brief unavailable: {e.__class__.__name__}. Call the session_brief tool.", None)
        except Exception:
            pass
    return 0


# ---------------------------------------------------------------------------
# close
# ---------------------------------------------------------------------------


def _adhoc_session(agent: str, ctx: Context) -> tuple[str, dict[str, Any]]:
    """Session id + start state when no id was given: the session ``brief``
    started here (so a repeat close updates it), else a brand-new id (a close
    without a brief never merges into another session's handoff)."""
    marker = current_adhoc_session(ctx.home, agent, ctx.host, ctx.repo.toplevel or str(ctx.cwd))
    if marker:
        return str(marker["session_id"]), marker
    return _new_adhoc_session_id(), {}


def build_close_payload(ctx: Context, args: argparse.Namespace) -> dict[str, Any]:
    """Gather facts deterministically and build the ``/session/close`` body."""
    transcript_path = args.transcript or ctx.hook_fields.get("transcript")
    transcript = None
    parse_ok = ctx.adapter is None or ctx.adapter.spec.transcript_format == "claude-jsonl"
    if transcript_path and parse_ok:
        path = Path(transcript_path).expanduser()
        if path.is_file():
            try:
                transcript = factlib.parse_claude_transcript(path, factlib.Deadline(3.0), root=ctx.repo.toplevel)
            except Exception as e:
                _err(f"transcript not parsed ({e.__class__.__name__}); using git facts only")
        else:
            _err(f"transcript not found: {path}")

    agent = ctx.agent or "unknown-agent"
    session_id = (
        args.session_id
        or ctx.hook_fields.get("session_id")
        or os.environ.get("REMEMBRA_SESSION_ID")
        or (transcript.session_id if transcript else None)
    )
    if session_id:
        state = load_session_state(ctx.home, agent, session_id)
    else:
        session_id, state = _adhoc_session(agent, ctx)
    started_ts = state.get("started_ts")
    git_facts = factlib.git_facts(
        ctx.cwd,
        factlib.Deadline(min(GIT_BUDGET_SECONDS, max(0.5, ctx.deadline.end - time.monotonic() - 2.0))),
        start_head=state.get("head"),
        session_commits=transcript.commit_shas if transcript else None,
        hours=args.hours,
        info=ctx.repo,
        started_at=float(started_ts) if isinstance(started_ts, int | float) else None,
    )
    facts = factlib.merge_facts(git_facts, transcript, ctx.repo.toplevel)
    if ctx.repo.is_git:
        facts["facts_source"] = "relay-cli:git+transcript" if transcript is not None else "relay-cli:git"
    else:
        facts["facts_source"] = "agent-declared"  # no git: only what was typed (--notes/--todo/--next)
    if args.notes:
        facts["notes"] = args.notes
    if args.next:
        facts["next_step"] = args.next
    for todo in args.todo or []:
        facts.setdefault("todos_open", []).append(todo)

    project = ctx.project_params()
    payload: dict[str, Any] = {"agent_id": agent, "session_id": session_id, "facts": facts}
    if "project_id" in project:
        payload["project_id"] = project["project_id"]
    else:
        payload["project"] = project
    reason = args.reason or ctx.hook_fields.get("reason")
    if reason:
        payload["end_reason"] = reason
    if args.summary:
        payload["summary"] = args.summary
    return payload


def cmd_close(args: argparse.Namespace) -> int:
    try:
        ctx = Context(args)
        payload = build_close_payload(ctx, args)
        if args.dry_run:
            print(json.dumps(payload, indent=2))
            return 0
        if not ctx.config.api_key:
            _err("close skipped: no API key (set REMEMBRA_API_KEY or configure the remembra MCP server)")
            return 0
        response = ctx.request("POST", "/api/v1/session/close", json=payload)
        if response.status_code >= 400:
            _err(f"close failed: {_http_error(response)}")
            return 0
        result = response.json()
        if not ctx.adapter:  # interactive use; hooks keep stdout clean (some require JSON-only stdout)
            print(f"Remembra handoff {result.get('handoff_id')} · project {result.get('project_id')} · {result.get('headline')}")
    except Exception as e:  # never break the agent's shutdown
        _err(f"close failed: {e.__class__.__name__}: {e}")
    return 0


# ---------------------------------------------------------------------------
# trail / resolve
# ---------------------------------------------------------------------------


def cmd_trail(args: argparse.Namespace) -> int:
    try:
        ctx = Context(args, payload={})
        if not ctx.config.api_key:
            _err("trail unavailable: no API key")
            return 0
        params: dict[str, Any] = {"limit": args.limit}
        params.update(ctx.project_params())
        response = ctx.request("GET", "/api/v1/trail", params=params)
        if response.status_code >= 400:
            _err(f"trail failed: {_http_error(response)}")
            return 0
        data = response.json()
        if args.format == "json":
            print(json.dumps(data, indent=2, default=str))
            return 0
        print(f"Trail · project {data.get('project_id')} · {data.get('total')} entries")
        for item in data.get("items") or []:
            where = item.get("branch") or ""
            if item.get("head_commit"):
                where += f"@{str(item['head_commit'])[:7]}"
            print(
                f"- {str(item.get('created_at') or '')[:16].replace('T', ' ')}  {item.get('agent_id') or '?':<14} "
                f"{item.get('memory_type'):<10} {where:<24} {item.get('headline')}"
            )
    except Exception as e:
        _err(f"trail failed: {e.__class__.__name__}: {e}")
    return 0


def cmd_resolve(args: argparse.Namespace) -> int:
    try:
        ctx = Context(args, payload={})
        if not ctx.config.api_key:
            _err("resolve failed: no API key")
            return 1
        locator = ctx.repo.locator(ctx.cwd, ctx.host)
        if args.project:
            locator["hint_project"] = normalize_project_id(args.project, parse_project_aliases(ctx.config.project_aliases))
        elif not args.bind and ctx.configured_project():
            locator["hint_project"] = ctx.configured_project()
        locator["bind"] = bool(args.bind)
        response = ctx.request("POST", "/api/v1/projects/resolve", json=locator)
        if response.status_code >= 400:
            _err(f"resolve failed: {_http_error(response)}")
            return 1
        print(json.dumps(response.json(), indent=2))
        return 0
    except Exception as e:
        _err(f"resolve failed: {e.__class__.__name__}: {e}")
        return 1


# ---------------------------------------------------------------------------
# connect
# ---------------------------------------------------------------------------


def cmd_connect(args: argparse.Namespace) -> int:
    home = Path(os.environ.get("HOME") or Path.home())
    relay = args.relay_command or relay_command()
    wanted = [a.lower() for a in (args.agent or [])]
    unknown = [a for a in wanted if a not in REGISTRY]
    if unknown:
        _err(f"unknown agent(s): {', '.join(unknown)}; known: {', '.join(REGISTRY)}")
        return 2

    config = load_config()
    print(f"Remembra config: {json.dumps(config.redacted())} (keys are read from there at run time; none are written)")
    print(f"Relay command: {relay}")
    missing_key = not config.api_key
    if missing_key:
        _warn_missing_key()
    exit_code = 0
    skipped_unverified: list[str] = []
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    for name, adapter in REGISTRY.items():
        if wanted and name not in wanted:
            continue
        spec = adapter.spec
        detected = adapter.detect(home)
        label = "verified" if spec.verified else "UNVERIFIED"
        if not detected and name not in wanted:
            print(f"\n[{name}] {spec.display}: not detected, skipped")
            continue
        try:
            change = adapter.plan(home, relay)
        except Exception as e:
            print(f"\n[{name}] {spec.display} ({label}): cannot read {spec.config_path(home)}: {e}")
            exit_code = 1
            continue
        print(f"\n[{name}] {spec.display} ({label}) -> {change.path}")
        if spec.notes:
            print(f"  note: {spec.notes}")
        if not change.changed:
            print("  already connected, no change")
            continue
        for line in change.summary:
            print(f"  - {line}")
        diff = change.diff()
        if diff:
            print("  " + diff.replace("\n", "\n  ").rstrip())
        if not args.apply:
            print("  (dry run: re-run with --apply to write, a backup is kept)")
            continue
        if not spec.verified and not args.include_unverified:
            print("  skipped: unverified adapter (add --include-unverified to write it anyway)")
            skipped_unverified.append(name)
            continue
        backup = backup_and_write(change, stamp)
        print(f"  written{f' (backup: {backup})' if backup else ''}")

    md_path = Path(args.agents_md).expanduser() if args.agents_md else None
    print("\n[agents-md] fallback for agents without hooks (plus MCP session_brief / close_session):")
    if md_path is None:
        print("  pass --agents-md PATH to add this section to an AGENTS.md:")
        print("  " + agents_md.block(relay).replace("\n", "\n  ").rstrip())
    else:
        change = agents_md.plan(md_path, relay)
        if not change.changed:
            print(f"  {md_path}: already present")
        elif args.apply:
            backup = backup_and_write(change, stamp)
            print(f"  {md_path}: written{f' (backup: {backup})' if backup else ''}")
        else:
            print("  " + change.diff().replace("\n", "\n  ").rstrip())
            print("  (dry run: re-run with --apply to write)")

    if skipped_unverified:
        agents_flags = " ".join(f"--agent {name}" for name in skipped_unverified)
        print(f"\nNot written (unverified adapters): {', '.join(skipped_unverified)}. To write them anyway:")
        print(f"  remembra-relay connect --apply --include-unverified {agents_flags}")
    if missing_key:
        _warn_missing_key()  # again at the end, where it is seen
        return 1
    return exit_code


def _warn_missing_key() -> None:
    """Loud notice that the hooks cannot reach the server: they will do nothing."""
    red, reset = ("\033[31;1m", "\033[0m") if sys.stderr.isatty() else ("", "")
    _err(
        f"{red}no Remembra API key found{reset}: the hooks will not load or save handoffs until one is set.\n"
        "  Checked: REMEMBRA_API_KEY, ~/.claude.json and ~/.codex/config.toml (remembra MCP server env),"
        " ~/.remembra/credentials.\n"
        "  Fix: create a key in the Remembra dashboard (Settings > API keys), then run\n"
        "    remembra-install --all --api-key <your key> --url <your server URL>\n"
        "  (or export REMEMBRA_API_KEY and REMEMBRA_URL where your agents start)."
    )


# ---------------------------------------------------------------------------
# argparse
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="remembra-relay", description="Session continuity across AI agents (Remembra Relay).")
    sub = parser.add_subparsers(dest="command", required=True)
    hooks = ", ".join(REGISTRY)

    def common(p: argparse.ArgumentParser) -> None:
        p.add_argument("--agent", help="Agent id (default: REMEMBRA_AGENT_ID, the config's id, or the --hook name)")
        p.add_argument("--cwd", help="Working directory to resolve the project from (default: hook cwd or current dir)")
        p.add_argument("--project", help="Use this project id instead of resolving from git")
        p.add_argument("--hook", help=f"Read the hook payload from stdin using this adapter's mapping ({hooks})")
        p.add_argument("--session-id", dest="session_id", help="Session id (default: from the hook payload)")

    p_brief = sub.add_parser("brief", help="Print the pickup brief for this project")
    common(p_brief)
    p_brief.add_argument("--format", choices=["text", "json", "hook-json", "cursor-json"], help="Output format")
    p_brief.add_argument("--recent", type=int, default=8, help="Recent memories to include (default 8)")
    p_brief.set_defaults(func=cmd_brief)

    p_close = sub.add_parser("close", help="Gather session facts and store the handoff")
    common(p_close)
    p_close.add_argument("--transcript", help="Claude Code JSONL transcript to extract commands/tests/todos from")
    p_close.add_argument("--reason", help="Why the session ended")
    p_close.add_argument("--summary", help="Optional summary (checked against the facts)")
    p_close.add_argument("--notes", help="Free-form notes for the next agent")
    p_close.add_argument("--next", help="The next step for whoever picks up")
    p_close.add_argument("--todo", action="append", help="An unfinished item (repeatable)")
    p_close.add_argument("--hours", type=float, default=12.0, help="Commit window when the session start is unknown")
    p_close.add_argument("--dry-run", action="store_true", help="Print the payload instead of sending it")
    p_close.set_defaults(func=cmd_close)

    p_trail = sub.add_parser("trail", help="Handoffs and checkpoints across agents, newest first")
    common(p_trail)
    p_trail.add_argument("--limit", type=int, default=20)
    p_trail.add_argument("--format", choices=["text", "json"], default="text")
    p_trail.set_defaults(func=cmd_trail)

    p_resolve = sub.add_parser("resolve", help="Show (or bind) the project id for this location")
    common(p_resolve)
    p_resolve.add_argument("--bind", action="store_true", help="Re-bind this location to --project")
    p_resolve.set_defaults(func=cmd_resolve)

    p_connect = sub.add_parser("connect", help="Wire installed agents' session hooks (dry run by default)")
    p_connect.add_argument("--apply", action="store_true", help="Write the changes (backups are kept)")
    p_connect.add_argument("--agent", action="append", help=f"Only these agents ({hooks}); repeatable")
    p_connect.add_argument("--include-unverified", action="store_true", help="Also write unverified adapters")
    p_connect.add_argument("--agents-md", help="Also add the relay section to this AGENTS.md")
    p_connect.add_argument("--relay-command", help=argparse.SUPPRESS)
    p_connect.set_defaults(func=cmd_connect)
    return parser


def main(argv: list[str] | None = None) -> int:
    raw = sys.argv[1:] if argv is None else argv
    try:
        args = build_parser().parse_args(raw)
    except SystemExit as e:
        code = int(e.code) if isinstance(e.code, int) else 2
        # Hook-facing commands never fail the agent, even when miswired.
        return 0 if raw and raw[0] in ("brief", "close", "trail") else code
    try:
        code = int(args.func(args))
    except Exception as e:
        _err(f"{args.command} failed: {e.__class__.__name__}: {e}")
        code = 0 if args.command in ("brief", "close", "trail") else 1
    return code


def entrypoint() -> None:
    sys.exit(main())


if __name__ == "__main__":
    entrypoint()
