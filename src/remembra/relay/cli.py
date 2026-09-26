"""``remembra-relay`` — leave a trail when an agent stops; pick it up when one starts.

Subcommands::

    remembra-relay brief   [--agent X] [--cwd DIR] [--hook NAME] [--format text|json|hook-json|cursor-json] [--once]
    remembra-relay close   [--agent X] [--session-id S] [--cwd DIR] [--transcript PATH] [--reason R] [--hook NAME]
    remembra-relay trail   [--cwd DIR] [--project P] [--limit N]
    remembra-relay resolve [--cwd DIR] [--project P] [--bind]
    remembra-relay connect [--apply] [--agent NAME ...] [--include-unverified] [--agents-md PATH]
    remembra-relay disconnect [--apply] [--agent NAME ...] [--agents-md PATH]
    remembra-relay status  [--format text|json] [--no-check]

``brief``/``close``/``trail`` are hook-safe: they never block (≤10 s total,
git calls and HTTP bounded), never raise, always exit 0 and report problems
on stderr. With ``--hook NAME`` the agent's hook payload is read from stdin
(session id, cwd, transcript path, end reason) using that adapter's mapping.
``brief --once`` prints nothing when this session already had its brief (a
per-prompt hook that covers a missed start hook). For adapters whose agent
does not wait for the end hook, ``close --hook`` re-runs itself in a detached
process and returns at once.

A close that cannot be delivered is queued in ``~/.remembra/relay/outbox``
and sent again by the next ``brief`` or ``close`` (see
:mod:`remembra.relay.outbox`); the brief says when something is queued or the
key was rejected, and ``status`` shows the queue and the last result per agent.

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
import subprocess
import sys
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from remembra.client.project import normalize_project_id, parse_project_aliases
from remembra.relay import facts as factlib
from remembra.relay import outbox
from remembra.relay.adapters import REGISTRY, Adapter, agents_md, backup_and_write, get_adapter, relay_command
from remembra.relay.config import RelayConfig, load_config

TOTAL_BUDGET_SECONDS = 9.5
GIT_BUDGET_SECONDS = 4.0
HTTP_TIMEOUT_SECONDS = 8.0
STDIN_WAIT_SECONDS = 1.0
STATE_TTL_SECONDS = 14 * 86400
ADHOC_SESSION_MAX_AGE_SECONDS = 12 * 3600
USER_AGENT = "remembra-relay"
# Queued handoffs a brief/close sends before giving up for this run, and the
# budget one resend may use (the brief itself still needs time after it).
REPLAY_MAX_ENTRIES = 5
REPLAY_BUDGET_SECONDS = 3.5
# Time a close keeps for sending its own handoff when it sends queued ones first.
CLOSE_RESERVE_SECONDS = 4.0
USAGE_LIMIT_REASON = "usage_limit"  # end_reason when the transcript shows a usage-limit stop


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


def _brief_marker_path(home: Path, key: str, session_id: str) -> Path:
    digest = hashlib.sha256(f"brief\x1f{key}\x1f{session_id}".encode()).hexdigest()[:24]
    return _state_dir(home) / f"brief-{digest}.json"


def brief_delivered(home: Path, key: str, session_id: str) -> bool:
    """True when this session already got its brief (see ``brief --once``)."""
    try:
        return time.time() - _brief_marker_path(home, key, session_id).stat().st_mtime <= STATE_TTL_SECONDS
    except OSError:
        return False


def mark_brief_delivered(home: Path, key: str, session_id: str) -> None:
    path = _brief_marker_path(home, key, session_id)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"at": datetime.now(UTC).isoformat()}))
        os.chmod(path, 0o600)
    except OSError as e:
        _err(f"could not record the brief ({e.__class__.__name__})")


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

    def client(self, config: RelayConfig | None = None, agent: str | None = None, budget: float | None = None) -> httpx.Client:
        config = config or self.config
        agent = agent or self.agent
        headers = {"User-Agent": f"{USER_AGENT}/{_version()}", "Accept": "application/json"}
        if config.api_key:
            headers["X-API-Key"] = config.api_key
        if agent:
            headers["X-Remembra-Agent-Id"] = agent
        remaining = self.deadline.end - time.monotonic()
        timeout = max(0.5, min(HTTP_TIMEOUT_SECONDS, remaining, budget if budget is not None else remaining))
        return httpx.Client(base_url=config.url, headers=headers, timeout=httpx.Timeout(timeout, connect=min(4.0, timeout)))

    def request(
        self,
        method: str,
        path: str,
        *,
        config: RelayConfig | None = None,
        agent: str | None = None,
        budget: float | None = None,
        **kwargs: Any,
    ) -> httpx.Response:
        """One HTTP call bounded by the WHOLE remaining budget (or ``budget``, if smaller).

        httpx timeouts apply per phase / per socket read, so a server that
        drips one byte every few seconds never trips them. The call runs in a
        daemon thread; when the budget runs out it is abandoned and
        ``TimeoutError`` is raised (the process can still exit: the thread is
        a daemon). ``config``/``agent`` send with another key/agent id (a
        queued close is sent as the agent that wrote it).
        """
        remaining = self.deadline.end - time.monotonic()
        if budget is not None:
            remaining = min(remaining, budget)
        if remaining <= 0.1:
            raise TimeoutError(f"the {TOTAL_BUDGET_SECONDS:g}s budget ran out before the request")
        box: dict[str, Any] = {}
        url = (config or self.config).url

        def run() -> None:
            try:
                with self.client(config, agent, budget) as http:
                    box["response"] = http.request(method, path, **kwargs)
            except BaseException as e:  # handed to the caller's thread
                box["error"] = e

        worker = threading.Thread(target=run, name="remembra-relay-http", daemon=True)
        worker.start()
        worker.join(remaining)
        if worker.is_alive():
            raise TimeoutError(f"no complete response from {url} within the {remaining:.1f}s left of the budget")
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
# Outbox: send what earlier closes could not deliver
# ---------------------------------------------------------------------------


class Replay:
    """What one run did with the outbox (the brief turns it into notices)."""

    def __init__(self) -> None:
        self.sent: list[str] = []
        self.key_rejected: list[str] = []  # config sources whose key got 401
        self.refused: list[str] = []  # HTTP 403 details
        self.remaining: list[outbox.Entry] = []


def _config_prefer(source: str | None) -> str | None:
    """``load_config(prefer=…)`` for an entry's recorded config source ("claude:/path" -> "claude")."""
    kind = (source or "").split(":", 1)[0]
    return kind if kind in ("claude", "codex", "credentials") else None


def _failure(response: httpx.Response | None, error: BaseException | None) -> str:
    if response is not None:
        return _http_error(response)
    return f"{error.__class__.__name__}: {error}" if error else "unknown error"


def replay_outbox(ctx: Context, skip: tuple[str, str] | None = None, reserve: float = 0.0, drop_skipped: bool = True) -> Replay:
    """Send queued closes, oldest first, within a small budget. Never raises.

    Each entry goes with the key of the config source that queued it and as
    the agent that wrote it. Stops at the first network failure (the server is
    still unreachable), 5xx, 429 or rejected key (401); a 4xx that resending
    cannot fix drops the entry (logged). A refusal (403: the key may not write
    as that agent or to that project) holds back only that entry and the
    entries with the same key, agent and project; it stays queued (shown by
    ``status``, dropped after ``MAX_AGE_SECONDS``) and goes after the others
    on later runs, so it never blocks the rest of the queue. ``skip`` is the
    (agent, session) the caller closes itself: its queued copy is never sent, and with ``drop_skipped`` it is
    dropped (the caller's close has been delivered and supersedes it).
    ``reserve`` is time left untouched for the caller's own request.
    """
    report = Replay()
    try:
        entries = outbox.pending(ctx.home)
    except Exception as e:
        outbox.log(ctx.home, f"outbox: could not read the queue ({e.__class__.__name__})")
        return report
    # Entries the server refused before go last (stable sort: oldest first within each group).
    entries.sort(key=lambda e: e.data.get("last_status") == 403)
    attempted = 0
    stop = False
    refused_scopes: set[tuple[str | None, str, str]] = set()
    for entry in entries:
        if skip and (entry.agent_id, entry.session_id) == skip:
            if drop_skipped:
                outbox.discard(ctx.home, entry.agent_id, entry.session_id)
            continue
        left = ctx.deadline.end - time.monotonic()
        if stop or attempted >= REPLAY_MAX_ENTRIES or left < REPLAY_BUDGET_SECONDS + reserve:
            report.remaining.append(entry)
            continue
        config = load_config(agent=entry.agent_id or None, prefer=_config_prefer(entry.data.get("config_source")))
        if not config.api_key or (entry.url and outbox.clean_url(config.url) != entry.url):
            report.remaining.append(entry)  # no key yet, or the key now points at another server
            continue
        scope = (config.source, entry.agent_id, str(entry.payload.get("project_id") or ""))
        if scope in refused_scopes:
            report.remaining.append(entry)  # this key was just refused for the same agent and project
            continue
        claimed = outbox.claim(entry)
        if claimed is None:
            continue  # another hook is sending it right now
        attempted += 1
        response: httpx.Response | None = None
        error: BaseException | None = None
        try:
            response = ctx.request(
                "POST",
                "/api/v1/session/close",
                json=entry.payload,
                config=config,
                agent=entry.agent_id or None,
                budget=REPLAY_BUDGET_SECONDS,
            )
        except Exception as e:
            error = e
        status = response.status_code if response is not None else None
        detail = _failure(response, error)
        if status is not None and status < 400:
            outbox.finish(entry, claimed, sent=True)
            outbox.record(ctx.home, agent_id=entry.agent_id, command="close (queued)", ok=True, config_source=config.source)
            outbox.log(
                ctx.home,
                f"outbox: delivered {entry.agent_id} session {entry.session_id[:40]} (queued {entry.data.get('queued_at')})",
            )
            report.sent.append(entry.agent_id)
            continue
        outbox.record(
            ctx.home,
            agent_id=entry.agent_id,
            command="close (queued)",
            ok=False,
            config_source=config.source,
            error=detail,
            http_status=status,
        )
        if status is not None and not outbox.is_retryable_status(status):
            outbox.finish(entry, claimed, sent=True)  # resending the same body cannot succeed
            outbox.log(ctx.home, f"outbox: dropped {entry.agent_id} session {entry.session_id[:40]}: {detail}")
            continue
        outbox.finish(entry, claimed, sent=False, error=detail, http_status=status)
        report.remaining.append(entry)
        if status == 403:
            # Refused for this agent or project only: the rest of the queue may use it.
            report.refused.append(detail)
            refused_scopes.add(scope)
            outbox.log(ctx.home, f"outbox: refused {entry.agent_id} session {entry.session_id[:40]} (kept): {detail}")
            continue
        if status == 401:
            report.key_rejected.append(config.source)
        stop = True  # unreachable, rate-limited or a rejected key: try again next time
    return report


def queue_notices(ctx: Context, replay: Replay, brief_status: int | None = None) -> list[str]:
    """One-line notices for the top of the brief: queued handoffs, a rejected key."""
    notices: list[str] = []
    if brief_status == 401 or replay.key_rejected:
        source = ctx.config.source if brief_status == 401 else replay.key_rejected[0]
        notices.append(
            f"Remembra: your API key was rejected (HTTP 401; the key came from {source}), so handoffs are not being"
            " saved. Create a new key in the dashboard (API keys) and store it there, or run `remembra-install --all`;"
            " `remembra-relay status` shows what is waiting."
        )
    elif brief_status == 403 or replay.refused:
        notices.append(
            "Remembra: the server refused this key (HTTP 403). Check the key's projects and agent in the dashboard;"
            " `remembra-relay status` has details."
        )
    try:
        waiting = outbox.pending(ctx.home)
    except Exception:
        waiting = []
    here = outbox.clean_url(ctx.config.url)
    elsewhere = [e for e in waiting if e.url and e.url != here]
    waiting = [e for e in waiting if not (e.url and e.url != here)]
    if waiting:
        by_agent: dict[str, int] = {}
        for entry in waiting:
            by_agent[entry.agent_id or "unknown-agent"] = by_agent.get(entry.agent_id or "unknown-agent", 0) + 1
        who = ", ".join(f"{n} from {agent}" for agent, n in sorted(by_agent.items()))
        total = len(waiting)
        notices.append(
            f"Remembra: {total} handoff{'s' if total != 1 else ''} ({who}) could not be sent yet and"
            f" {'are' if total != 1 else 'is'} queued on this machine; the next brief or close retries."
            " The trail may be missing the newest session. `remembra-relay status` shows why."
        )
    if elsewhere:
        servers = ", ".join(sorted({str(e.url) for e in elsewhere}))
        notices.append(
            f"Remembra: {len(elsewhere)} queued handoff(s) are for another server ({servers}) and are not sent to this one."
            " `remembra-relay status` lists them."
        )
    if replay.sent:
        n = len(replay.sent)
        notices.append(f"Remembra: sent {n} queued handoff{'s' if n != 1 else ''} from {', '.join(sorted(set(replay.sent)))}.")
    return notices


# ---------------------------------------------------------------------------
# brief
# ---------------------------------------------------------------------------


def _emit_brief(mode: str, text: str, raw: dict[str, Any] | None, notices: list[str] | None = None) -> None:
    if mode == "json":
        body: dict[str, Any] = dict(raw) if raw is not None else {"error": text}
        if notices:
            body["notices"] = notices
        print(json.dumps(body, default=str))
        return
    if notices:  # the relay's own lines, above the brief (outside its untrusted-data block)
        text = "\n".join(notices) + ("\n" + text if text else "")
    if mode == "hook-json":
        print(json.dumps({"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": text}}))
    elif mode == "cursor-json":
        print(json.dumps({"additional_context": text}))
    else:
        print(text)


def _once_key(args: argparse.Namespace, adapter: Adapter | None) -> str:
    return (adapter.spec.name if adapter else None) or getattr(args, "agent", None) or "unknown-agent"


def cmd_brief(args: argparse.Namespace) -> int:
    mode = args.format or "text"
    ctx: Context | None = None
    try:
        adapter = get_adapter(getattr(args, "hook", None))
        payload = read_hook_payload() if adapter else {}
        fields = adapter.spec.payload.extract(payload) if adapter else {}
        hook_session = fields.get("session_id") or args.session_id
        home = Path(os.environ.get("HOME") or Path.home())
        # --once (a per-prompt hook) and a resumed session whose context already holds the
        # brief: deliver only if this session has not had one. Checked before any git or HTTP
        # work, because the prompt hook runs on every prompt.
        once = bool(args.once) or (payload.get("source") == "resume" and bool(adapter and adapter.spec.prompt_event))
        once_key = _once_key(args, adapter)
        if once:
            if not hook_session or brief_delivered(home, once_key, hook_session):
                return 0
        ctx = Context(args, payload=payload)
        if not args.format and ctx.adapter:
            mode = ctx.adapter.spec.output
        if hook_session:
            # Recorded before the HTTP call: a failed brief is not retried on every prompt.
            mark_brief_delivered(ctx.home, once_key, hook_session)
        if not ctx.config.api_key:
            _emit_brief(
                mode,
                "Remembra brief unavailable: no API key (set REMEMBRA_API_KEY or configure the remembra MCP server).",
                None,
                queue_notices(ctx, Replay()),
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
            session_id = start_adhoc_session(
                ctx.home, ctx.agent or "unknown-agent", ctx.host, ctx.repo.toplevel or str(ctx.cwd), start
            )
        replay = replay_outbox(ctx)  # first, so the brief below already includes what was queued
        params: dict[str, Any] = {"recent_n": args.recent}
        if ctx.agent:
            params["agent_id"] = ctx.agent
        params.update(ctx.project_params())
        if ctx.repo.branch:
            params["branch"] = ctx.repo.branch
        if ctx.repo.head_commit:
            params["head_commit"] = ctx.repo.head_commit
        pickup_session = session_id or os.environ.get("REMEMBRA_SESSION_ID")
        if pickup_session:
            params["session_id"] = pickup_session  # one pickup is recorded per reader session
        try:
            response = ctx.request("GET", "/api/v1/session/brief", params=params)
        except Exception as e:
            outbox.record(ctx.home, agent_id=ctx.agent, command="brief", ok=False, error=f"{e.__class__.__name__}: {e}")
            raise
        if response.status_code >= 400:
            message = f"Remembra brief unavailable: {_http_error(response)}"
            _err(message)
            outbox.record(
                ctx.home,
                agent_id=ctx.agent,
                command="brief",
                ok=False,
                config_source=ctx.config.source,
                error=_http_error(response),
                http_status=response.status_code,
            )
            _emit_brief(mode, message, None, queue_notices(ctx, replay, response.status_code))
            return 0
        outbox.record(ctx.home, agent_id=ctx.agent, command="brief", ok=True, config_source=ctx.config.source)
        brief = response.json()
        _emit_brief(mode, str(brief.get("rendered") or ""), brief, queue_notices(ctx, replay))
    except Exception as e:  # never break the agent's session start
        _err(f"brief failed: {e.__class__.__name__}: {e}")
        try:
            notices = queue_notices(ctx, Replay()) if ctx is not None else []
        except Exception:
            notices = []
        try:
            _emit_brief(mode, f"Remembra brief unavailable: {e.__class__.__name__}. Call the session_brief tool.", None, notices)
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
    if transcript_path:
        path = Path(transcript_path).expanduser()
        if path.is_file():
            # A hook's adapter names its format (None: not parsed); a path given by hand is sniffed.
            fmt = ctx.adapter.spec.transcript_format if ctx.adapter else factlib.detect_transcript_format(path)
            if fmt in factlib.TRANSCRIPT_FORMATS:
                try:
                    transcript = factlib.parse_transcript(path, fmt, factlib.Deadline(3.0), root=ctx.repo.toplevel)
                except Exception as e:
                    _err(f"transcript not parsed ({e.__class__.__name__}); using git facts only")
        elif not ctx.adapter or ctx.adapter.spec.transcript_format:
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
    # When the session ended: a close sent later from the outbox keeps this time,
    # so the server does not rank it above handoffs written after it.
    payload: dict[str, Any] = {
        "agent_id": agent,
        "session_id": session_id,
        "facts": facts,
        "closed_at": datetime.now(UTC).isoformat(),
    }
    if "project_id" in project:
        payload["project_id"] = project["project_id"]
    else:
        payload["project"] = project
    limit = transcript.usage_limit if transcript else None
    if limit:
        # The agent's last turn stopped on its plan's usage limit: say so first, so
        # whoever picks up knows the work stopped mid-way, not because it was done.
        facts["errors"] = [f"Stopped on a usage limit: {limit}", *(facts.get("errors") or [])]
    reason = args.reason or (USAGE_LIMIT_REASON if limit else None) or ctx.hook_fields.get("reason")
    if reason:
        payload["end_reason"] = reason
    if args.summary:
        payload["summary"] = args.summary
    return payload


def health_summary(health: Any) -> str | None:
    """``Handoff: Ready with warnings - tests not run; 2 commit(s) not pushed`` (None without a grade)."""
    if not isinstance(health, dict) or not health.get("label"):
        return None
    missing = [str(m) for m in health.get("missing") or [] if isinstance(m, str)]
    return f"Handoff: {health['label']}" + (f" - {'; '.join(missing)}" if missing else "")


def _queue_close(ctx: Context, payload: dict[str, Any], error: str, http_status: int | None = None) -> None:
    """Keep an undelivered close for the next brief/close, and say so on stderr."""
    # Without a key there is no server either: send it wherever a key is configured later.
    url = ctx.config.url if ctx.config.api_key else None
    path = outbox.enqueue(ctx.home, payload, url=url, config_source=ctx.config.source, error=error, http_status=http_status)
    outbox.record(
        ctx.home,
        agent_id=str(payload.get("agent_id") or ctx.agent or ""),
        command="close",
        ok=False,
        config_source=ctx.config.source,
        error=error,
        http_status=http_status,
    )
    if path is not None:
        _err(f"the handoff is queued in {path.parent} and will be sent by the next brief or close")


def _close_log_path(home: Path) -> Path:
    return home / ".remembra" / "relay" / "last-detached-close.log"


def spawn_detached_close(args: argparse.Namespace, hook_payload: dict[str, Any]) -> bool:
    """Re-run this ``close`` in a new session (its own process group) and return.

    The child gets the hook payload on stdin and ``--foreground``; its stderr
    goes to ``~/.remembra/relay/last-detached-close.log``. The agent can exit,
    kill the hook, or close its terminal, and the handoff is still posted.
    False when the child could not be started (the caller closes inline).
    """
    home = Path(os.environ.get("HOME") or Path.home())
    argv = [sys.executable, "-m", "remembra.relay.cli", *getattr(args, "raw_argv", ["close"]), "--foreground"]
    log_path = _close_log_path(home)
    log: Any = subprocess.DEVNULL
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        log = os.fdopen(fd, "w")
    except OSError:
        log = subprocess.DEVNULL
    kwargs: dict[str, Any] = {}
    if os.name == "nt":  # pragma: no cover - exercised on Windows only
        kwargs["creationflags"] = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    else:
        kwargs["start_new_session"] = True
    try:
        child = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=log, close_fds=True, **kwargs)
        assert child.stdin is not None
        with child.stdin:
            child.stdin.write(json.dumps(hook_payload).encode())
    except (OSError, ValueError) as e:
        _err(f"could not detach the close ({e.__class__.__name__}); closing inline")
        return False
    finally:
        if log is not subprocess.DEVNULL:
            log.close()
    return True


def cmd_close(args: argparse.Namespace) -> int:
    ctx: Context | None = None
    payload: dict[str, Any] | None = None
    try:
        adapter = get_adapter(getattr(args, "hook", None))
        hook_payload: dict[str, Any] | None = None
        if adapter and adapter.spec.detach_close and not args.foreground and not args.dry_run:
            hook_payload = read_hook_payload()
            if spawn_detached_close(args, hook_payload):
                return 0
        ctx = Context(args, payload=hook_payload)
        payload = build_close_payload(ctx, args)
        if args.dry_run:
            print(json.dumps(payload, indent=2))
            return 0
        if not ctx.config.api_key:
            _err("close skipped: no API key (set REMEMBRA_API_KEY or configure the remembra MCP server)")
            _queue_close(ctx, payload, "no API key configured")
            return 0
        own = (str(payload.get("agent_id")), str(payload.get("session_id")))
        # Older queued handoffs go first, while that leaves this close enough time; the rest follow it.
        replay_outbox(ctx, skip=own, reserve=CLOSE_RESERVE_SECONDS, drop_skipped=False)
        try:
            response = ctx.request("POST", "/api/v1/session/close", json=payload)
        except Exception as e:
            _err(f"close failed: {e.__class__.__name__}: {e}")
            _queue_close(ctx, payload, f"{e.__class__.__name__}: {e}")
            return 0
        if response.status_code >= 400:
            detail = _http_error(response)
            _err(f"close failed: {detail}")
            if outbox.is_retryable_status(response.status_code):
                _queue_close(ctx, payload, detail, response.status_code)
            else:
                outbox.record(
                    ctx.home,
                    agent_id=str(payload.get("agent_id")),
                    command="close",
                    ok=False,
                    config_source=ctx.config.source,
                    error=detail,
                    http_status=response.status_code,
                )
                outbox.log(ctx.home, f"close: not queued, the server rejected the body ({detail})")
            return 0
        result = response.json()
        outbox.record(ctx.home, agent_id=str(payload.get("agent_id")), command="close", ok=True, config_source=ctx.config.source)
        # This close supersedes a queued copy of the same session; then send what is still waiting.
        replay_outbox(ctx, skip=own)
        if not ctx.adapter:  # interactive use; hooks keep stdout clean (some require JSON-only stdout)
            print(f"Remembra handoff {result.get('handoff_id')} · project {result.get('project_id')} · {result.get('headline')}")
            health = health_summary(result.get("health"))
            if health:
                print(health)
    except Exception as e:  # never break the agent's shutdown
        _err(f"close failed: {e.__class__.__name__}: {e}")
        if ctx is not None and payload is not None and not getattr(args, "dry_run", False):
            try:
                _queue_close(ctx, payload, f"{e.__class__.__name__}: {e}")
            except Exception:
                pass
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
    stamp = _stamp()
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
        if spec.setup_note:
            print(f"  REQUIRED: {spec.setup_note}")
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


def _stamp() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S")


def cmd_disconnect(args: argparse.Namespace) -> int:
    """Remove the relay hooks this tool wrote (dry run unless --apply; backups kept)."""
    home = Path(os.environ.get("HOME") or Path.home())
    wanted = [a.lower() for a in (args.agent or [])]
    unknown = [a for a in wanted if a not in REGISTRY]
    if unknown:
        _err(f"unknown agent(s): {', '.join(unknown)}; known: {', '.join(REGISTRY)}")
        return 2
    stamp = _stamp()
    exit_code = 0
    found = 0
    for name, adapter in REGISTRY.items():
        if wanted and name not in wanted:
            continue
        spec = adapter.spec
        try:
            change = adapter.plan_removal(home)
        except Exception as e:
            print(f"\n[{name}] {spec.display}: cannot read {spec.config_path(home)}: {e}")
            exit_code = 1
            continue
        if not change.changed:
            if name in wanted:
                print(f"\n[{name}] {spec.display}: no relay hooks in {change.path}")
            continue
        found += 1
        print(f"\n[{name}] {spec.display} -> {change.path}")
        for line in change.summary:
            print(f"  - {line}")
        if change.delete:
            print("  (nothing else is left in the file: it is removed)")
        diff = change.diff()
        if diff:
            print("  " + diff.replace("\n", "\n  ").rstrip())
        if args.apply:
            backup = backup_and_write(change, stamp)
            print(f"  {'removed' if change.delete else 'written'}{f' (backup: {backup})' if backup else ''}")
        else:
            print("  (dry run: re-run with --apply to write, a backup is kept)")
    if args.agents_md:
        md = agents_md.plan_removal(Path(args.agents_md).expanduser())
        if md.changed:
            found += 1
            print(f"\n[agents-md] {md.path}")
            print("  " + md.diff().replace("\n", "\n  ").rstrip())
            if args.apply:
                backup = backup_and_write(md, stamp)
                print(f"  written{f' (backup: {backup})' if backup else ''}")
            else:
                print("  (dry run: re-run with --apply to write)")
        else:
            print(f"\n[agents-md] {md.path}: no Remembra Relay section")
    if not found:
        print("No relay hooks found: nothing to remove.")
    print(
        "\nTo remove Remembra completely:\n"
        "  1. remembra-relay disconnect --apply     (the session hooks, above)\n"
        "  2. remembra-install --remove --all --apply   (the MCP server entries)\n"
        "  3. pipx uninstall remembra\n"
        "  4. revoke the key in the dashboard (API keys) and delete ~/.remembra (saved key, queue, log)"
    )
    return exit_code


def _age(ts: Any, now: float) -> str:
    try:
        seconds = max(0.0, now - float(ts))
    except (TypeError, ValueError):
        return "at an unknown time"
    if seconds < 90:
        return "just now"
    if seconds < 5400:
        return f"{round(seconds / 60)}m ago"
    if seconds < 36 * 3600:
        return f"{round(seconds / 3600)}h ago"
    return f"{round(seconds / 86400)}d ago"


def _check_key(config: RelayConfig) -> dict[str, Any]:
    """Ask the server whether the key is accepted (a read-only call). Bounded by the hook budget."""
    ns = argparse.Namespace(hook=None, agent=config.agent_id, cwd=None, project=None, session_id=None)
    ctx = Context(ns, payload={})
    try:
        response = ctx.request("GET", "/api/v1/trail/summary", params={"days": 1}, config=config)
    except Exception as e:
        return {"state": "unchecked", "error": f"server unreachable ({e.__class__.__name__}: {e})"}
    if response.status_code < 400:
        return {"state": "accepted"}
    if response.status_code == 401:
        return {"state": "rejected", "http_status": 401, "error": _http_error(response)}
    if response.status_code == 403:
        return {"state": "refused", "http_status": 403, "error": _http_error(response)}
    return {"state": "unchecked", "http_status": response.status_code, "error": _http_error(response)}


def cmd_status(args: argparse.Namespace) -> int:
    """Queue depth, last result per agent and whether the key is accepted. Exit 1 when something needs attention."""
    home = Path(os.environ.get("HOME") or Path.home())
    config = load_config(agent=args.agent)
    entries = outbox.pending(home)
    recorded = outbox.read_status(home)
    if not config.api_key:
        key: dict[str, Any] = {"state": "missing"}
    elif args.no_check:
        stored = (recorded.get("keys") or {}).get(config.source) or {}
        key = {"state": stored.get("state") or "unchecked", "http_status": stored.get("http_status"), "at": stored.get("at")}
    else:
        key = _check_key(config)
        if key["state"] in ("accepted", "rejected", "refused"):
            outbox.record(
                home,
                agent_id=config.agent_id,
                command="status",
                ok=key["state"] == "accepted",
                config_source=config.source,
                error=key.get("error"),
                http_status=key.get("http_status"),
            )
            recorded = outbox.read_status(home)
    now = time.time()
    queue = [
        {
            "agent_id": e.agent_id,
            "session_id": e.session_id,
            "queued_at": e.data.get("queued_at"),
            "attempts": e.attempts,
            "last_error": e.data.get("last_error"),
            "url": e.url,
        }
        for e in entries
    ]
    agents = recorded.get("agents") or {}
    attention = bool(entries) or key["state"] in ("missing", "rejected", "refused")
    if args.format == "json":
        print(
            json.dumps(
                {
                    "config": config.redacted(),
                    "key": key,
                    "queue_depth": len(entries),
                    "queue": queue,
                    "agents": agents,
                    "outbox": str(outbox.outbox_dir(home)),
                    "log": str(outbox.log_path(home)),
                },
                indent=2,
                default=str,
            )
        )
        return 1 if attention else 0
    print("Remembra relay status")
    print(f"  server: {config.url} (key from {config.source})")
    state = key["state"]
    key_line = {
        "accepted": "accepted by the server",
        "rejected": "REJECTED by the server (HTTP 401): create a new key in the dashboard and run remembra-install --all",
        "refused": f"refused by the server (HTTP 403): {key.get('error') or ''}".rstrip(": "),
        "missing": "none found: set REMEMBRA_API_KEY or run remembra-install --all",
    }.get(state, f"not checked ({key.get('error') or 'run without --no-check to ask the server'})")
    print(f"  key: {key_line}")
    print(f"  queue: {len(entries)} handoff(s) waiting in {outbox.outbox_dir(home)}")
    for item in queue:
        print(
            f"    - {item['agent_id']} session {str(item['session_id'])[:24]}, queued {item['queued_at']}, "
            f"{item['attempts']} attempt(s), last error: {item['last_error']}"
        )
    if agents:
        print("  agents:")
        for agent, slot in sorted(agents.items()):
            ok, bad = slot.get("last_success"), slot.get("last_failure")
            parts = []
            if ok:
                parts.append(f"last ok: {ok.get('command')} {_age(ok.get('ts'), now)}")
            if bad:
                code = f"HTTP {bad['http_status']}: " if bad.get("http_status") else ""
                error = str(bad.get("error") or "")
                if code and error.startswith(code):
                    code = ""
                parts.append(f"last failure: {bad.get('command')} {_age(bad.get('ts'), now)} ({code}{error[:160]})")
            print(f"    {agent:<14} " + " · ".join(parts))
    else:
        print("  agents: no brief or close recorded on this machine yet")
    print(f"  log: {outbox.log_path(home)}")
    return 1 if attention else 0


def _warn_missing_key() -> None:
    """Loud notice that the hooks cannot reach the server: they will do nothing."""
    red, reset = ("\033[31;1m", "\033[0m") if sys.stderr.isatty() else ("", "")
    _err(
        f"{red}no Remembra API key found{reset}: the hooks will not load or save handoffs until one is set.\n"
        "  Checked: REMEMBRA_API_KEY, ~/.claude.json and ~/.codex/config.toml (remembra MCP server env),"
        " ~/.remembra/credentials.\n"
        "  Fix: create a key in the Remembra dashboard (Settings > API keys), then run\n"
        "    remembra-install --all --url <your server URL>\n"
        "  which asks for the key (it is never put on the command line), or export REMEMBRA_API_KEY\n"
        "  and REMEMBRA_URL where your agents start."
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
    p_brief.add_argument(
        "--once", action="store_true", help="Print nothing if this session already had its brief (for per-prompt hooks)"
    )
    p_brief.set_defaults(func=cmd_brief)

    p_close = sub.add_parser("close", help="Gather session facts and store the handoff")
    common(p_close)
    p_close.add_argument(
        "--transcript", help="Claude Code JSONL or Codex rollout to extract commands/tests/todos from (format detected)"
    )
    p_close.add_argument("--reason", help="Why the session ended")
    p_close.add_argument("--summary", help="Optional summary (checked against the facts)")
    p_close.add_argument("--notes", help="Free-form notes for the next agent")
    p_close.add_argument("--next", help="The next step for whoever picks up")
    p_close.add_argument("--todo", action="append", help="An unfinished item (repeatable)")
    p_close.add_argument("--hours", type=float, default=12.0, help="Commit window when the session start is unknown")
    p_close.add_argument("--dry-run", action="store_true", help="Print the payload instead of sending it")
    p_close.add_argument("--foreground", action="store_true", help=argparse.SUPPRESS)  # set on the detached child
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

    p_disconnect = sub.add_parser("disconnect", help="Remove the relay hooks connect wrote (dry run by default)")
    p_disconnect.add_argument("--apply", action="store_true", help="Write the changes (backups are kept)")
    p_disconnect.add_argument("--agent", action="append", help=f"Only these agents ({hooks}); repeatable")
    p_disconnect.add_argument("--agents-md", help="Also remove the relay section from this AGENTS.md")
    p_disconnect.set_defaults(func=cmd_disconnect)

    p_status = sub.add_parser("status", help="Queued handoffs, last result per agent, and whether the key is accepted")
    p_status.add_argument("--agent", help="Agent id whose config to check (default: REMEMBRA_AGENT_ID)")
    p_status.add_argument("--format", choices=["text", "json"], default="text")
    p_status.add_argument("--no-check", action="store_true", help="Do not ask the server; show the last recorded key state")
    p_status.set_defaults(func=cmd_status)
    return parser


def main(argv: list[str] | None = None) -> int:
    raw = sys.argv[1:] if argv is None else argv
    try:
        args = build_parser().parse_args(raw)
    except SystemExit as e:
        code = int(e.code) if isinstance(e.code, int) else 2
        # Hook-facing commands never fail the agent, even when miswired.
        return 0 if raw and raw[0] in ("brief", "close", "trail") else code
    args.raw_argv = list(raw)
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
