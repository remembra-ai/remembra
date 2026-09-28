"""``remembra-relay`` — leave a trail when an agent stops; pick it up when one starts.

Subcommands::

    remembra-relay brief   [--agent X] [--cwd DIR] [--hook NAME] [--format text|json|hook-json|cursor-json|
                           additional-context-json] [--once]
    remembra-relay close   [--agent X] [--session-id S] [--cwd DIR] [--transcript PATH] [--reason R] [--hook NAME]
    remembra-relay trail   [--cwd DIR] [--project P] [--limit N]
    remembra-relay doctor  [--agent NAME ...] [--format text|json] [--no-server] [--color auto|always|never]
    remembra-relay resolve [--cwd DIR] [--project P] [--bind]
    remembra-relay connect [--apply] [--agent NAME ...] [--include-unverified] [--force] [--agents-md PATH]
    remembra-relay disconnect [--apply] [--agent NAME ...] [--agents-md PATH]
    remembra-relay status  [--format text|json] [--no-check]
    remembra-relay projects split [--project P] [--repo PATH ...] [--apply] [--format text|json]
    remembra-relay projects undo  [--batch ID] [--apply] [--format text|json]

``brief``/``close``/``trail`` are hook-safe: they never block (≤10 s total,
git calls and HTTP bounded), never raise, always exit 0 and report problems
on stderr. With ``--hook NAME`` the agent's hook payload is read from stdin
(session id, cwd, transcript path, end reason) using that adapter's mapping.
``brief --once`` prints nothing when this session already had its brief (a
per-prompt hook that covers a missed start hook, or a start whose output the
agent drops, such as Gemini CLI's after ``/clear``). Where that prompt hook can
run while the start hook still runs (Gemini CLI does not wait for it), the
first of the two to print claims the brief and the other prints nothing; a
resumed session gets the brief again unless its restored history holds it.
For adapters whose agent does not wait for the end hook, ``close --hook``
re-runs itself in a detached process and returns at once.

A hook another agent runs (Grok Build, Cursor, Devin and Continue load Claude
Code's hooks; see :mod:`remembra.relay.hosts`) is routed: ``brief`` does
nothing, and ``close`` files the session under the agent that ran it (or does
nothing when the relay has no adapter for it). ``close --hook`` drops a repeat
of the same session's end within ``dedupe_close_seconds`` (Gemini CLI fires
SessionEnd two or three times on exit), or within a few seconds when the
payload has no transcript to tell a resumed session's new end from a repeat.

A Codex automation or sub-agent thread gets no brief and leaves no handoff
(:mod:`remembra.relay.background`), and a session that recorded nothing sends
no close. ``close --hook`` checks, in this order: the automation / sub-agent
skip (on the hook's own adapter, then on the agent a routing names), the
routing, the repeat, then the empty session. The empty check needs the git
facts and the transcript, so an agent that detaches runs it in the detached
child. ``brief --hook`` checks the skip, then the routing.

A close that cannot be delivered is queued in ``~/.remembra/relay/outbox``
and sent again by the next ``brief`` or ``close`` (see
:mod:`remembra.relay.outbox`); the brief says when something is queued or the
key was rejected, and ``status`` shows the queue and the last result per agent.

The project is resolved from the git repository in ``--cwd`` (remote URL,
root commit), so every checkout of the same repo — any machine, drive or
worktree — shares one trail, and a repository the server has not seen gets
its own project. Outside git the working directory is used, and a configured
project (``REMEMBRA_PROJECT`` / the MCP env / credentials, unless it is
``default``) names such a folder. ``REMEMBRA_RELAY_PROJECT`` opts into one
project for everything, repositories included (the 0.16.0 behaviour). Before
0.16.1 a configured project named every new repository; ``projects split``
gives each of those its own project again (see :mod:`remembra.relay.projects`).

The whole run is bounded by ``TOTAL_BUDGET_SECONDS``: HTTP runs in a worker
thread that is abandoned (the fallback text is printed) when the budget runs
out, so a slow or dripping server cannot hold the agent's session start.

Configuration is discovered from the environment or existing agent config
(see :mod:`remembra.relay.config`); this tool never writes API keys anywhere.
"""

from __future__ import annotations

import argparse
import contextlib
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
from remembra.relay import background, hints, hosts, outbox, projects
from remembra.relay import facts as factlib
from remembra.relay.adapters import REGISTRY, Adapter, Change, agents_md, backup_and_write, get_adapter, relay_command
from remembra.relay.adapters.base import OUTPUT_MODES, AdapterSpec, RefusedEdit
from remembra.relay.config import RelayConfig, load_config, load_config_from_source
from remembra.relay.handoff import build_sections, sections_have_substance
from remembra.relay.identity import HINT_SCOPE_ALL
from remembra.security.untrusted import neutralize_encoded, strip_controls

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
DOCTOR_POINTER = " Ask your agent to run remembra_doctor, or run `remembra-relay doctor`."


def _err(message: str) -> None:
    try:
        print(f"remembra-relay: {strip_controls(message)}", file=sys.stderr)
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


def brief_record(home: Path, key: str, session_id: str) -> dict[str, Any] | None:
    """This session's brief marker while it is fresh (``{"at", "event"}``: when, and which hook event printed it), else None.

    A marker that cannot be parsed (another hook is writing it this instant, or
    an older release wrote it without the event) is ``{}``.
    """
    path = _brief_marker_path(home, key, session_id)
    try:
        if time.time() - path.stat().st_mtime > STATE_TTL_SECONDS:
            return None
    except OSError:
        return None
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def brief_delivered(home: Path, key: str, session_id: str) -> bool:
    """True when this session already got its brief (see ``brief --once``)."""
    return brief_record(home, key, session_id) is not None


def _brief_marker(event: str | None) -> str:
    return json.dumps({"at": datetime.now(UTC).isoformat(), "event": event})


def mark_brief_delivered(home: Path, key: str, session_id: str, event: str | None = None) -> None:
    """Record that this session's brief was printed (by a hook of ``event``), replacing an earlier record."""
    path = _brief_marker_path(home, key, session_id)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(_brief_marker(event))
        os.chmod(path, 0o600)
    except OSError as e:
        _err(f"could not record the brief ({e.__class__.__name__})")


def claim_brief(home: Path, key: str, session_id: str, event: str | None) -> bool:
    """True when this hook is the first of its session to print the brief; False when another one did.

    For hooks of one session that may print it at the same time (a prompt
    hook's ``--once`` and a start hook the agent does not wait for): the marker
    is created with ``O_EXCL`` just before printing, so exactly one prints.
    Never raises; fails open (a brief that cannot be recorded is printed).
    """
    return _claim_marker(_brief_marker_path(home, key, session_id), STATE_TTL_SECONDS, _brief_marker(event))


def forget_brief(home: Path, key: str, session_id: str) -> None:
    """Drop this session's brief marker: a resumed session whose restored history lost the brief gets it again."""
    with contextlib.suppress(OSError):
        _brief_marker_path(home, key, session_id).unlink()


def _claim_marker(path: Path, fresh_for: float, content: str) -> bool:
    """Create ``path`` with ``O_EXCL``: True when this call made it, False when a marker younger than ``fresh_for``
    seconds is there. An older one is renamed away first, so of two claims racing for it exactly one wins.
    Never raises, and fails open (True) when the marker cannot be written."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        return True
    for _ in range(2):
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            pass
        except OSError:
            return True
        else:
            with os.fdopen(fd, "w") as fh:
                fh.write(content)
            return True
        try:
            age = time.time() - path.stat().st_mtime
        except FileNotFoundError:
            continue  # released in between: try to create it again
        except OSError:
            return True
        if age < fresh_for:
            return False
        stale = path.with_name(f"{path.name}.stale-{os.getpid()}-{os.urandom(3).hex()}")
        try:
            os.rename(path, stale)
        except FileNotFoundError:
            return False  # another claim is taking over the stale marker right now
        except OSError:
            return True
        with contextlib.suppress(OSError):
            stale.unlink()
    return False


# How long a repeat of a close with no transcript to measure is dropped for. Its repeats are copies of
# the hook the agent runs at the same moment (Kimi Code runs one event's hooks together; Cursor runs its
# own and Claude Code's); a longer window would also drop a resumed session that ended again with new work.
UNMEASURED_CLOSE_SECONDS = 5


def _close_claim_path(home: Path, agent: str, session_id: str, event: str, reason: str, transcript: str) -> Path:
    key = f"close\x1f{agent}\x1f{session_id}\x1f{event}\x1f{reason}\x1f{transcript}"
    return _state_dir(home) / f"close-{hashlib.sha256(key.encode()).hexdigest()[:24]}.json"


def claim_close(
    home: Path,
    agent: str,
    session_id: str,
    event: str,
    reason: str,
    window: float,
    transcript_size: int | None = None,
) -> bool:
    """True when this close should run; False when the same close ran within ``window`` seconds.

    "The same close" is the same agent, session, hook event and end reason, so
    Claude Code's StopFailure followed by its SessionEnd still closes twice (the
    later one supersedes), while Gemini CLI's two or three SessionEnds on exit,
    or one session's end run by several agents' copies of a hook, close once.
    With ``transcript_size`` (the session transcript's size) a session resumed
    and ended again after new turns is a new close, even within the window.
    Without it (Kimi Code and cursor-agent send no transcript) nothing tells
    that close from a repeat, so the window is at most
    ``UNMEASURED_CLOSE_SECONDS``: long enough for the copies of one end hook,
    which run together, and shorter than a session resumed, used and ended.
    The claim is a marker file created with ``O_EXCL``; a stale one (older than
    the window) is renamed away first, so of two closes racing for it exactly
    one wins. Never raises, and fails open: a close that cannot record its
    claim runs (a duplicate handoff is superseded; a lost one is not).
    """
    size = "" if transcript_size is None else str(transcript_size)
    if transcript_size is None:
        window = min(window, UNMEASURED_CLOSE_SECONDS)
    path = _close_claim_path(home, agent, session_id, event, reason, size)
    return _claim_marker(path, window, json.dumps({"at": datetime.now(UTC).isoformat()}))


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
        self.http_timeout = HTTP_TIMEOUT_SECONDS  # per request; commands that are not hooks may raise both
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
        """The project this client is configured for (``default`` does not count):
        ``REMEMBRA_RELAY_PROJECT``, else ``REMEMBRA_PROJECT`` / the MCP env / credentials."""
        return self.single_namespace() or self.normalize_project(self.config.project)

    def single_namespace(self) -> str | None:
        """``REMEMBRA_RELAY_PROJECT``, when set: the explicit opt-in to keep every location,
        git repositories included, in that one project (the 0.16.0 behaviour)."""
        return self.normalize_project(os.environ.get(hints.RELAY_PROJECT_ENV))

    def normalize_project(self, value: str | None) -> str | None:
        """``value`` as a project id (aliases applied); None when empty or ``default``."""
        return hints.normalize_project(value, self.config)

    def hint_fields(self) -> dict[str, Any]:
        """How the server may name this location when it has not seen it (see "Which project a repository uses").

        The one rule :mod:`remembra.relay.hints` keeps for the relay and crewd: a git
        repository always gets its own project (the configured project goes with
        ``hint_scope=folders``), ``REMEMBRA_RELAY_PROJECT`` keeps one namespace for
        everything (``hint_scope=all``), and when git did not answer in time neither
        ``git_repo`` nor the configured project is sent.
        """
        return hints.hint_fields(self.config, self.repo.git_repo, os.environ.get(hints.RELAY_PROJECT_ENV))

    def project_params(self) -> dict[str, Any]:
        """Either ``project_id`` or a location to resolve server-side (see :meth:`hint_fields`)."""
        aliases = parse_project_aliases(self.config.project_aliases)
        explicit = getattr(self.args, "project", None)
        if explicit:
            return {"project_id": normalize_project_id(explicit, aliases)}
        locator = self.repo.locator(self.cwd, self.host)
        locator.update(self.hint_fields())
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
        timeout = max(0.5, min(self.http_timeout, remaining, budget if budget is not None else remaining))
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


def _json_body(response: httpx.Response) -> Any:
    try:
        return response.json()
    except Exception:
        return None


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


def replay_config(entry: outbox.Entry) -> tuple[RelayConfig | None, str | None]:
    """The config a queued close may be sent with, or ``(None, why it is held)``.

    An entry queued with a key goes only with the key of the SAME config source
    (``env``, ``claude:<path>``, ...), and only while that source still points
    at the server it was queued for: a key configured elsewhere may belong to
    another account. An entry queued without a key is kept for the server that
    was configured then (``REMEMBRA_URL``, else the local default) and goes
    only once a key for that same server is configured; it is never sent to a
    server set up later. An entry without a recorded server (written before
    this rule) is held.
    """
    recorded = entry.data.get("config_source")
    agent = entry.agent_id or None
    if recorded and recorded != "none":
        config = load_config_from_source(str(recorded), agent=agent)
        if config is None or not config.api_key:
            return None, f"its key source ({recorded}) has no key now"
    else:
        config = load_config(agent=agent)
        if not config.api_key:
            return None, "no API key is configured yet"
    if not entry.url:
        return None, "it was queued without a server; it is only kept, never sent (delete the file to drop it)"
    if outbox.clean_url(config.url) != entry.url:
        return None, (
            f"it is for {entry.url}, and the key now points at {outbox.clean_url(config.url)}; it is only sent to the"
            " server it was queued for (delete the file to drop it)"
        )
    return config, None


def _failure(response: httpx.Response | None, error: BaseException | None) -> str:
    if response is not None:
        return _http_error(response)
    return f"{error.__class__.__name__}: {error}" if error else "unknown error"


def replay_outbox(ctx: Context, skip: tuple[str, str] | None = None, reserve: float = 0.0, drop_skipped: bool = True) -> Replay:
    """Send queued closes, oldest first, within a small budget. Never raises.

    Each entry goes with the key of the config source that queued it, to the
    server it was queued for (see :func:`replay_config`), and as the agent that
    wrote it. Stops at the first network failure (the server is
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
        config, _held = replay_config(entry)
        if config is None:
            report.remaining.append(entry)  # no key for its source, or that source now points at another server
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
        if status is not None and response is not None and not outbox.is_retryable_response(status, _json_body(response)):
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
            " `remembra-relay status` shows what is waiting." + DOCTOR_POINTER
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
            " The trail may be missing the newest session. `remembra-relay status` shows why." + DOCTOR_POINTER
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


def _emit_brief(
    mode: str, text: str, raw: dict[str, Any] | None, notices: list[str] | None = None, event: str = "SessionStart"
) -> None:
    """Print the brief in ``mode``; ``event`` labels the hook-json output (the hook event that asked for it).

    Recorded text that spells the data block's tag with HTML character
    references (``&lt;/remembra-data&gt;``) is neutralized here too, whatever the
    server did: Gemini CLI and Qwen Code HTML-escape the real tag into exactly
    that string, so it would end the block early.
    """
    if mode == "json":
        body: dict[str, Any] = dict(raw) if raw is not None else {"error": text}
        if isinstance(body.get("rendered"), str):
            body["rendered"] = neutralize_encoded(body["rendered"])
        if notices:
            body["notices"] = notices
        print(json.dumps(body, default=str))
        return
    text = neutralize_encoded(text)
    if notices:  # the relay's own lines, above the brief (outside its untrusted-data block)
        text = "\n".join(notices) + ("\n" + text if text else "")
    # Recorded text never drives the terminal (CLI-02), nor reaches a hook's context with escape sequences.
    text = strip_controls(text)
    if mode == "hook-json":
        print(json.dumps({"hookSpecificOutput": {"hookEventName": event, "additionalContext": text}}))
    elif mode == "cursor-json":
        print(json.dumps({"additional_context": text}))
    elif mode == "additional-context-json":
        print(json.dumps({"additionalContext": text}))
    else:
        print(text)


def _once_key(args: argparse.Namespace, adapter: Adapter | None) -> str:
    return (adapter.spec.name if adapter else None) or getattr(args, "agent", None) or "unknown-agent"


def _home() -> Path:
    return Path(os.environ.get("HOME") or Path.home())


def route_hook(
    args: argparse.Namespace, adapter: Adapter, payload: dict[str, Any], *, closing: bool
) -> tuple[Adapter | None, Adapter]:
    """``(adapter that reads this hook's payload, or None to do nothing; adapter of the agent running it)``.

    When another agent runs this hook (:func:`remembra.relay.hosts.detect_host`),
    ``brief`` does nothing: a brief here would record a false pickup, and that
    agent gets its brief from its own hooks or the MCP tools. ``close`` files
    the session under that agent when the relay has an adapter for it: ``--hook``
    and ``--agent`` (unless ``--agent`` names a custom id) become its name, so
    its payload mapping reads the session id and cwd and its transcript is never
    read in the first agent's format. Without an adapter the close does nothing.
    A payload key in the adapter's ``skip_payload_keys`` (a subagent's end) also
    does nothing. The detached child of a close gets the same payload and
    environment, so it routes the same way.
    """
    home = _home()
    host = hosts.detect_host(adapter, payload, os.environ, home)
    reader = adapter
    if host is not None:
        target = get_adapter(host)
        reader = target or adapter
        verb = "close" if closing else "brief"
        if not closing or target is None:
            outbox.log(home, f"{verb} --hook {adapter.spec.name}: run by {host}, nothing to do")
            return None, reader
        outbox.log(home, f"{verb} --hook {adapter.spec.name}: run by {host}, filed as {host}")
        if getattr(args, "agent", None) in (None, adapter.spec.name):
            args.agent = host
        args.hook = host
        adapter = target
    skip = [key for key in adapter.spec.skip_payload_keys if key in payload]
    if skip:
        outbox.log(home, f"--hook {adapter.spec.name}: payload has {skip[0]} (not a session of its own), nothing to do")
        return None, reader
    return adapter, reader


def _hook_ack(adapter: Adapter) -> None:
    """What a hook prints when it has nothing to say: ``{}`` for Cursor, which logs empty stdout as a failed hook."""
    if adapter.spec.output == "cursor-json":
        print("{}", flush=True)


def _brief_needed(args: argparse.Namespace, spec: AdapterSpec, source: Any, home: Path, key: str, session: str | None) -> bool:
    """Whether this hook may print the session's brief at all; False when the session has it already.

    Checked before any git or HTTP work, because a prompt hook runs on every prompt.

    - A prompt hook (``--once``) prints it once per session.
    - A start with source ``resume`` prints it again unless the restored history
      holds it: that depends on the hook event that printed it before
      (``resume_keeps_brief_from``). A marker without an event (an older
      release) counts as the start event's.
    - The start hook of an agent that does not wait for it (``start_awaited``
      False) skips a session whose prompt hook already printed it.
    """
    if not session:
        return not args.once  # a prompt hook without a session id cannot tell its first prompt
    if args.once:
        return not brief_delivered(home, key, session)
    if source == "resume":
        earlier = brief_record(home, key, session)
        if earlier is not None and (earlier.get("event") or spec.start_event) in spec.resume_keeps_brief_from:
            return False
        forget_brief(home, key, session)  # the earlier run's brief is not in this run's context
        return True
    return spec.start_awaited or not spec.prompt_event or not brief_delivered(home, key, session)


def _claim_print(home: Path | None, key: str, session: str | None, event: str, exclusive: bool) -> bool:
    """Record that this hook prints the brief (or why it is unavailable), just before it does.

    Recorded for a failed brief too, so it is not retried on every prompt. With
    ``exclusive`` (hooks of one session that can print it at the same moment: a
    prompt hook, and the start hook of an agent that does not wait for it) the
    record is a claim, and False means another hook printed it first: print nothing.
    """
    if home is None or not session:
        return True
    if exclusive:
        return claim_brief(home, key, session, event)
    mark_brief_delivered(home, key, session, event)
    return True


def cmd_brief(args: argparse.Namespace) -> int:
    mode = args.format or "text"
    ctx: Context | None = None
    event = "SessionStart"
    home: Path | None = None  # set once the session is known: the brief is claimed from then on
    once_key, exclusive = "", False
    hook_session: str | None = None
    try:
        adapter = get_adapter(getattr(args, "hook", None))
        payload = read_hook_payload() if adapter else {}
        if adapter and background.skip_hook_session(adapter, payload, "brief", _home()):
            _hook_ack(adapter)
            return 0  # a Codex automation or sub-agent thread: no brief in its prompt, no pickup recorded
        if adapter:
            routed, reader = route_hook(args, adapter, payload, closing=False)
            if routed is None:
                _hook_ack(reader)
                return 0
        if adapter and not args.once and payload.get("source") in adapter.spec.start_sources_without_context:
            # The agent throws this start's output away (Gemini CLI after /clear): nothing is
            # fetched or marked, so the prompt hook's `brief --once` gives the new session its brief.
            return 0
        fields = adapter.spec.payload.extract(payload) if adapter else {}
        event = fields.get("event") or event
        hook_session = fields.get("session_id") or args.session_id
        once_key = _once_key(args, adapter)
        if adapter:
            if not _brief_needed(args, adapter.spec, payload.get("source"), _home(), once_key, hook_session):
                return 0
        elif args.once and (not hook_session or brief_delivered(_home(), once_key, hook_session)):
            return 0
        exclusive = bool(args.once) or bool(adapter and adapter.spec.prompt_event and not adapter.spec.start_awaited)
        home = _home()
        ctx = Context(args, payload=payload)
        if not args.format and ctx.adapter:
            mode = ctx.adapter.spec.output
        if not ctx.config.api_key:
            if _claim_print(home, once_key, hook_session, event, exclusive):
                _emit_brief(
                    mode,
                    "Remembra brief unavailable: no API key (set REMEMBRA_API_KEY or configure the remembra MCP server).",
                    None,
                    queue_notices(ctx, Replay()),
                    event=event,
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
            if _claim_print(home, once_key, hook_session, event, exclusive):
                _emit_brief(mode, message, None, queue_notices(ctx, replay, response.status_code), event=event)
            return 0
        outbox.record(ctx.home, agent_id=ctx.agent, command="brief", ok=True, config_source=ctx.config.source)
        brief = response.json()
        if _claim_print(home, once_key, hook_session, event, exclusive):
            _emit_brief(mode, str(brief.get("rendered") or ""), brief, queue_notices(ctx, replay), event=event)
    except Exception as e:  # never break the agent's session start
        _err(f"brief failed: {e.__class__.__name__}: {e}")
        try:
            notices = queue_notices(ctx, Replay()) if ctx is not None else []
        except Exception:
            notices = []
        try:
            unavailable = f"Remembra brief unavailable: {e.__class__.__name__}. Call the session_brief tool."
            if _claim_print(home, once_key, hook_session, event, exclusive):
                _emit_brief(mode, unavailable, None, notices, event=event)
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


EMPTY_CLOSE_REASON = (
    "the session recorded no summary, notes, commits, file changes, tests, errors, todos or next step, "
    "and it did not stop on a limit"
)


def nothing_to_hand_off(payload: dict[str, Any]) -> bool:
    """True when a close would store an empty handoff (an idle or automated session).

    The same rule the brief uses to skip empty handoffs
    (:func:`~remembra.relay.handoff.sections_have_substance`): nothing in Done,
    Not done, Failing or Next, no summary or notes, and an end reason that is
    not a stop (a usage or billing limit, a close before context compaction:
    that notice is the handoff). Unknown git state (a probe that timed out)
    counts as something, so such a close is still sent.
    """
    facts = payload.get("facts") or {}
    sections = build_sections(facts, facts.get("next_step"))
    return not sections_have_substance(
        sections, summary=payload.get("summary"), notes=facts.get("notes"), end_reason=payload.get("end_reason")
    )


def _sent_marker_path(home: Path, agent: str, session_id: str) -> Path:
    digest = hashlib.sha256(f"sent\x1f{agent}\x1f{session_id}".encode()).hexdigest()[:24]
    return _state_dir(home) / f"sent-{digest}.json"


def mark_close_sent(home: Path, agent: str, session_id: str) -> None:
    """Record that this session has a handoff on the server (or queued for it)."""
    path = _sent_marker_path(home, agent, session_id)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"at": datetime.now(UTC).isoformat()}))
        os.chmod(path, 0o600)
    except OSError as e:
        _err(f"could not record the close ({e.__class__.__name__})")


def close_sent(home: Path, agent: str, session_id: str) -> bool:
    """True when an earlier close of this session was sent or queued (within the state TTL).

    Such a session's later close is sent even when it records nothing: it
    supersedes the earlier handoff, which would otherwise keep describing work
    that no longer exists (a file since discarded, a "still open" note).
    """
    try:
        return time.time() - _sent_marker_path(home, agent, session_id).stat().st_mtime <= STATE_TTL_SECONDS
    except OSError:
        return False


def _skip_empty_close(ctx: Context, payload: dict[str, Any]) -> None:
    """Send nothing for an empty session: one line in relay.log (and on stderr when run by hand)."""
    agent, session = str(payload.get("agent_id") or ""), str(payload.get("session_id") or "")
    outbox.log(ctx.home, f"close: nothing to hand off for {agent} session {session[:40]} ({EMPTY_CLOSE_REASON}); not sent")
    if not ctx.adapter:
        _err(f"nothing to hand off: {EMPTY_CLOSE_REASON}, so nothing was sent. Add --summary to send a handoff anyway.")


def health_summary(health: Any) -> str | None:
    """``Handoff: Ready with warnings - tests not run; 2 commit(s) not pushed`` (None without a grade)."""
    if not isinstance(health, dict) or not health.get("label"):
        return None
    missing = [str(m) for m in health.get("missing") or [] if isinstance(m, str)]
    return f"Handoff: {health['label']}" + (f" - {'; '.join(missing)}" if missing else "")


def _queue_close(ctx: Context, payload: dict[str, Any], error: str, http_status: int | None = None) -> None:
    """Keep an undelivered close for the next brief/close, and say so on stderr."""
    # Always the server this close was for: without a key that is REMEMBRA_URL (or the local default),
    # and the entry is only ever sent there (replay_config).
    path = outbox.enqueue(
        ctx.home, payload, url=ctx.config.url, config_source=ctx.config.source, error=error, http_status=http_status
    )
    if path is not None:  # the session has a handoff on its way: a later empty close must retire it
        mark_close_sent(ctx.home, str(payload.get("agent_id") or ""), str(payload.get("session_id") or ""))
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


def _first_close(args: argparse.Namespace, adapter: Adapter, hook_payload: dict[str, Any]) -> bool:
    """False when this hook's close is a repeat to drop (see :func:`claim_close`); True otherwise."""
    window = adapter.spec.dedupe_close_seconds
    fields = adapter.spec.payload.extract(hook_payload)
    session_id = args.session_id or fields.get("session_id")
    if window <= 0 or not session_id:
        return True
    agent = getattr(args, "agent", None) or adapter.spec.name
    event = (fields.get("event") or "").lower()
    reason = args.reason or fields.get("reason") or ""
    size: int | None = None
    transcript = args.transcript or fields.get("transcript")
    if transcript:
        with contextlib.suppress(OSError, ValueError):
            size = Path(transcript).expanduser().stat().st_size
    if claim_close(_home(), agent, session_id, event, reason, window, transcript_size=size):
        return True
    seconds = window if size is not None else min(window, UNMEASURED_CLOSE_SECONDS)
    outbox.log(_home(), f"close: dropped a repeat {event or 'close'} of {agent} session {session_id[:40]} within {seconds}s")
    return False


def _skip_background_close(args: argparse.Namespace, adapter: Adapter, hook_payload: dict[str, Any]) -> bool:
    """True when this close is a Codex automation's or sub-agent's (see :mod:`remembra.relay.background`):
    nothing is sent, queued or detached. ``--dry-run`` says so on stderr."""
    skipped = background.skip_hook_session(adapter, hook_payload, "close", _home())
    if skipped and args.dry_run:
        _err(f"close skipped: a {adapter.spec.name} {skipped} session leaves no handoff")
    return skipped is not None


def cmd_close(args: argparse.Namespace) -> int:
    ctx: Context | None = None
    payload: dict[str, Any] | None = None
    try:
        adapter = get_adapter(getattr(args, "hook", None))
        hook_payload: dict[str, Any] | None = None
        if adapter:
            hook_payload = read_hook_payload()
            # The order matters: each step below runs only for a close the ones before it let through.
            # 1. A Codex automation or sub-agent thread leaves no handoff: nothing sent, queued or detached.
            if _skip_background_close(args, adapter, hook_payload):
                if not args.dry_run:
                    _hook_ack(adapter)
                return 0
            # 2. A hook another agent runs is filed under that agent, or does nothing.
            routed, reader = route_hook(args, adapter, hook_payload, closing=True)
            if not args.dry_run:
                _hook_ack(reader)  # before any slow work; the detached child's stdout goes nowhere
            if routed is None:
                return 0
            if routed is not adapter and _skip_background_close(args, routed, hook_payload):
                return 0  # the agent that runs it has such threads too (Codex running Claude Code's hooks)
            adapter = routed
            if not args.foreground and not args.dry_run:  # the detached child was cleared by its parent
                if not hook_payload and adapter.spec.drop_empty_payload_close:
                    outbox.log(_home(), f"close --hook {adapter.spec.name}: empty payload (an orphaned end hook), skipped")
                    return 0
                # 3. A repeat of the same session's end is dropped.
                if not _first_close(args, adapter, hook_payload):
                    return 0
                # 5. Agents that do not wait for the end hook: the rest (4 included) runs detached.
                if adapter.spec.detach_close and spawn_detached_close(args, hook_payload):
                    return 0
        ctx = Context(args, payload=hook_payload)
        payload = build_close_payload(ctx, args)
        own = (str(payload.get("agent_id")), str(payload.get("session_id")))
        # 4. An empty close is skipped only while this session has nothing on the server to retire.
        # It needs the git facts and the transcript, so for a detaching agent it runs in the
        # detached child: Codex kills its end hook after at most 3 s.
        nothing = nothing_to_hand_off(payload)
        empty = nothing and not close_sent(ctx.home, *own)
        if args.dry_run:
            print(json.dumps(payload, indent=2))
            if empty:
                _err(f"close would send nothing: {EMPTY_CLOSE_REASON}")
            elif nothing:
                _err("close would send an empty handoff: it retires the one this session sent earlier")
            return 0
        if empty:
            _skip_empty_close(ctx, payload)
            return 0
        if not ctx.config.api_key:
            _err("close skipped: no API key (set REMEMBRA_API_KEY or configure the remembra MCP server)")
            _queue_close(ctx, payload, "no API key configured")
            return 0
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
            if outbox.is_retryable_response(response.status_code, _json_body(response)):
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
        mark_close_sent(ctx.home, *own)
        outbox.record(ctx.home, agent_id=str(payload.get("agent_id")), command="close", ok=True, config_source=ctx.config.source)
        # This close supersedes a queued copy of the same session; then send what is still waiting.
        replay_outbox(ctx, skip=own)
        if not ctx.adapter:  # interactive use; hooks keep stdout clean (some require JSON-only stdout)
            print(
                strip_controls(
                    f"Remembra handoff {result.get('handoff_id')} · project {result.get('project_id')} · {result.get('headline')}"
                )
            )
            health = health_summary(result.get("health"))
            if health:
                print(strip_controls(health))
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
        print(strip_controls(f"Trail · project {data.get('project_id')} · {data.get('total')} entries"))
        for item in data.get("items") or []:
            where = item.get("branch") or ""
            if item.get("head_commit"):
                where += f"@{str(item['head_commit'])[:7]}"
            # Every field was written by an agent: no escape sequence reaches the terminal (CLI-02).
            line = (
                f"- {str(item.get('created_at') or '')[:16].replace('T', ' ')}  {item.get('agent_id') or '?':<14} "
                f"{item.get('memory_type'):<10} {where:<24} {item.get('headline')}"
            )
            print(strip_controls(line))
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
        if args.project:  # a name given here applies to this location, repository or not
            locator["hint_project"] = normalize_project_id(args.project, parse_project_aliases(ctx.config.project_aliases))
            locator["hint_scope"] = HINT_SCOPE_ALL
            if ctx.repo.git_repo is not None:
                locator["git_repo"] = ctx.repo.git_repo
        elif not args.bind:
            locator.update(ctx.hint_fields())
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


def _server_line(config: RelayConfig) -> str:
    """Where the hooks send handoffs, as printed (never the key)."""
    if config.source == "none" and not (os.environ.get("REMEMBRA_URL") or "").strip():
        return "not configured (run remembra-install)"
    return f"{config.url} ({f'key from {config.source}' if config.api_key else 'no API key yet'})"


def _created_path(home: Path) -> Path:
    return outbox.relay_dir(home) / "created.json"


def missing_dirs(path: Path) -> list[Path]:
    """The directories writing ``path`` creates, outermost first (none when its directory exists)."""
    missing: list[Path] = []
    parent = path.parent
    while not parent.exists() and parent != parent.parent:
        missing.append(parent)
        parent = parent.parent
    return missing[::-1]


def created_dirs(home: Path) -> list[str]:
    """The directories ``connect`` created (``~/.remembra/relay/created.json``)."""
    try:
        data = json.loads(_created_path(home).read_text())
    except (OSError, ValueError):
        return []
    dirs = data.get("dirs") if isinstance(data, dict) else None
    return [d for d in dirs if isinstance(d, str)] if isinstance(dirs, list) else []


def _save_created_dirs(home: Path, dirs: list[str]) -> None:
    path = _created_path(home)
    if dirs:
        outbox.atomic_write_private(path, json.dumps({"dirs": sorted(set(dirs))}, indent=2) + "\n")
    else:
        with contextlib.suppress(FileNotFoundError):
            path.unlink()


def record_created_dirs(home: Path, dirs: list[Path]) -> None:
    """Remember directories a write created, so ``disconnect`` can take them away again."""
    if not dirs:
        return
    try:
        _save_created_dirs(home, [*created_dirs(home), *(str(d) for d in dirs)])
    except OSError as e:
        _err(f"could not record the directories connect created ({e.__class__.__name__}); disconnect will leave them")


def _relay_backup(entry: Path) -> bool:
    return ".bak-relay-" in entry.name and entry.is_file() and not entry.is_symlink()


def remove_created_dirs(home: Path, apply: bool) -> list[Path]:
    """Directories ``connect`` created that hold nothing but the relay's own backups now.

    With ``apply`` they are deleted (backups first), innermost first, so a parent
    that held only such a directory goes too; without it they are only listed.
    A directory holding anything else (an agent installed since, another tool's
    file) is kept, and stays recorded. Returns the directories removed (or that
    would be).
    """
    recorded = created_dirs(home)
    keep: list[str] = []
    removed: list[Path] = []
    for name in sorted(recorded, key=lambda d: len(Path(d).parts), reverse=True):
        directory = Path(name)
        if not directory.exists() and not directory.is_symlink():
            continue  # already gone: forget it
        try:
            entries = [] if directory.is_symlink() else [e for e in directory.iterdir() if e not in removed]
        except OSError:
            entries = None
        if entries is None or directory.is_symlink() or not all(_relay_backup(e) for e in entries):
            keep.append(name)
            continue
        if apply:
            try:
                for entry in entries:
                    entry.unlink()
                directory.rmdir()
            except OSError as e:
                _err(f"could not remove {directory} ({e.__class__.__name__})")
                keep.append(name)
                continue
        removed.append(directory)
    if apply and sorted(keep) != sorted(recorded):
        try:
            _save_created_dirs(home, keep)
        except OSError as e:
            _err(f"could not update {_created_path(home)} ({e.__class__.__name__})")
    return removed


def _print_copied_hooks(home: Path) -> None:
    """Point out relay hooks an import copied into another agent's config (they are routed; they can go)."""
    try:
        copied = hosts.copied_relay_hooks(home, REGISTRY)
    except Exception as e:  # a report only: never fail connect over it
        _err(f"could not check other agents' configs for copied relay hooks ({e.__class__.__name__})")
        return
    for item in copied:
        names = ", ".join(f"{n} {name}" for name, n in sorted(item.hooks.items()))
        how = f", copied by {hosts.COPIED_BY[item.host]}" if item.host in hosts.COPIED_BY else ""
        routed = f"files the session under {item.host}" if get_adapter(item.host) else "does nothing"
        print(
            f"\n[copied hooks] {item.path} holds {item.total} relay hook(s) written for another agent ({names}{how}).\n"
            f"  {item.host} runs them there: the relay sees that it is {item.host} and {routed}, never"
            " under the agent they were written for. They can be deleted from that file."
        )


def _is_connected(adapter: Adapter, home: Path) -> bool:
    try:
        return adapter.connected(home)
    except Exception:  # an unreadable config: plan() reports it
        return False


def _plan_error(path: Path, error: Exception) -> str:
    """How ``connect`` / ``disconnect`` report a file they could not plan a change for."""
    if isinstance(error, RefusedEdit):
        return f"not written: {path}: {error}"
    return f"cannot read {path}: {error}"


def _other_changes(adapter: Adapter, home: Path, relay: str) -> tuple[list[tuple[Change, str]], list[str]]:
    """What ``connect`` changes besides the agent's config file, and notes on files it could not read.

    Relay hooks an earlier connect left in :meth:`Adapter.earlier_files` are kept
    current (the agent still reads that file in a session started without its
    home variable); those in :meth:`Adapter.retired_files` are removed (the agent
    no longer reads that file). A file that cannot be read is left as it is.
    """
    changes: list[tuple[Change, str]] = []
    notes: list[str] = []
    home_env = adapter.spec.home_env or "its home variable"
    for path in adapter.earlier_files(home):
        try:
            if not adapter.plan_file_removal(path).changed:
                continue
            change = adapter.plan_file(path, relay)
        except Exception as e:
            notes.append(f"{_plan_error(path, e)} (relay hooks in it, if any, are left as they are)")
            continue
        if change.changed:
            why = f"relay hooks an earlier connect wrote here, kept current: the agent reads it when {home_env} is not set"
            changes.append((change, why))
    for path in adapter.retired_files(home):
        try:
            change = adapter.plan_file_removal(path)
        except Exception as e:
            notes.append(f"{_plan_error(path, e)} (relay hooks in it, if any, are left as they are)")
            continue
        if change.changed:
            changes.append((change, "relay hooks an earlier release wrote here, removed: the agent no longer reads this file"))
    return changes, notes


def _print_change(change: Change, indent: str = "  ") -> None:
    for line in change.summary:
        print(f"{indent}- {line}")
    diff = change.diff()
    if diff:
        print(indent + diff.replace("\n", "\n" + indent).rstrip())


def cmd_connect(args: argparse.Namespace) -> int:
    """Write the relay hooks for detected agents (dry run unless --apply; backups kept).

    Exit 0 when every write it was asked for succeeded (a missing key is only
    warned about: the hooks read it at run time), 1 when a config it would write
    could not be read or written, or an agent named with ``--agent`` was not
    written because it is not detected here (``--force`` writes it anyway), 2
    for an unknown agent name. An unverified adapter that connect skips anyway
    does not fail the run when its config cannot be read: that is only noted.
    """
    home = _home()
    relay = args.relay_command or relay_command()
    wanted = [a.lower() for a in (args.agent or [])]
    unknown = [a for a in wanted if a not in REGISTRY]
    if unknown:
        _err(f"unknown agent(s): {', '.join(unknown)}; known: {', '.join(REGISTRY)}")
        return 2

    config = load_config()
    print(f"Remembra server: {_server_line(config)}")
    print("  The hooks read the key there at run time; connect writes no key.")
    print(f"Relay command: {relay}")
    exit_code = 0
    skipped_unverified: list[str] = []
    not_installed: list[str] = []
    stamp = _stamp()
    for name, adapter in REGISTRY.items():
        if wanted and name not in wanted:
            continue
        spec = adapter.spec
        # Relay hooks already in the file count as installed: they are kept current (a new relay path).
        connected = _is_connected(adapter, home)
        detected = adapter.detect(home) or connected
        label = "verified" if spec.verified else "UNVERIFIED"
        if not detected and name not in wanted:
            print(f"\n[{name}] {spec.display}: not detected, skipped")
            continue
        # Skipped whatever its file holds: unverified, not asked for, no relay hooks of its own to keep current.
        skipped = not spec.verified and not args.include_unverified and not connected and name not in wanted
        try:
            change = adapter.plan(home, relay)
        except Exception as e:
            print(f"\n[{name}] {spec.display} ({label}): {_plan_error(spec.config_file(home), e)}")
            if skipped:
                print("  skipped: unverified adapter, so connect would not write it; this does not fail the run")
            else:
                exit_code = 1
            continue
        print(f"\n[{name}] {spec.display} ({label}) -> {change.path}")
        if spec.notes:
            print(f"  note: {spec.notes}")
        if spec.setup_note:
            print(f"  REQUIRED: {spec.setup_note}")
        others, notes = _other_changes(adapter, home, relay)
        for note in notes:
            print(f"  note: {note}")
        if change.changed:
            _print_change(change)
        else:
            print("  already connected, no change")
        for other, why in others:
            print(f"  also {other.path} ({why}):")
            _print_change(other, "    ")
        if not change.changed and not others:
            continue
        # Named with --agent but not detected: writing would create its directory, or a file such as
        # ~/.gemini/settings.json in the ~/.gemini that Antigravity also uses, and from then on it
        # looks like an installed agent to every detector. Only with --force.
        new_dirs = missing_dirs(change.path) if change.changed else []
        creates = f" (it would create {new_dirs[0]})" if new_dirs else ""
        if not args.apply:
            if not detected and not args.force:
                print(f"  not detected on this machine: --apply writes it only with --force{creates}")
            print("  (dry run: re-run with --apply to write, a backup is kept)")
            continue
        if not detected and not args.force:
            missing = f" and {new_dirs[0]} does not exist" if new_dirs else ""
            print(
                f"  not written: {spec.display} is not detected on this machine{missing}."
                " Install it first, or add --force to write it anyway"
            )
            not_installed.append(name)
            exit_code = 1
            continue
        if not spec.verified and not args.include_unverified:
            if not connected:
                print("  skipped: unverified adapter (add --include-unverified to write it anyway)")
                skipped_unverified.append(name)
                continue
            print("  updating the relay hooks already in this file (written earlier with --include-unverified)")
        if change.changed:
            if not _write(change, stamp):
                exit_code = 1
                continue
            record_created_dirs(home, new_dirs)
        for other, _ in others:
            if not _write(other, stamp, label=f"{other.path}: "):
                exit_code = 1

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
            if not _write(change, stamp, indent="  ", label=f"{md_path}: "):
                exit_code = 1
        else:
            print("  " + change.diff().replace("\n", "\n  ").rstrip())
            print("  (dry run: re-run with --apply to write)")

    if skipped_unverified:
        agents_flags = " ".join(f"--agent {name}" for name in skipped_unverified)
        print(f"\nNot written (unverified adapters): {', '.join(skipped_unverified)}. To write them anyway:")
        print(f"  remembra-relay connect --apply --include-unverified {agents_flags}")
    if not_installed:
        print(
            f"\nNot written (not detected on this machine): {', '.join(not_installed)}. If one is installed where this"
            " shell does not look, add --force; disconnect removes the directories connect created."
        )
    _print_copied_hooks(home)
    if not config.api_key:
        _warn_missing_key()  # once, at the end, where it is seen
    return exit_code


def _write(change: Change, stamp: str, *, indent: str = "  ", label: str = "") -> bool:
    """Back up and write ``change``, printing the outcome; False when the write failed."""
    try:
        backup = backup_and_write(change, stamp)
    except OSError as e:
        print(f"{indent}{label}NOT written: {e.__class__.__name__}: {e}")
        return False
    verb = "removed" if change.delete else "written"
    print(f"{indent}{label}{verb}{f' (backup: {backup})' if backup else ''}")
    return True


def _stamp() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S")


def cmd_disconnect(args: argparse.Namespace) -> int:
    """Remove the relay hooks this tool wrote (dry run unless --apply; backups kept).

    With ``--apply`` it also removes directories ``connect`` created that hold
    nothing but the relay's own backups afterwards.
    """
    home = _home()
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
        # The agent's config file, and wherever an earlier connect or release may have left relay
        # hooks: the default path when $home_env moves it, a file the agent no longer reads.
        had_any = False
        for path in adapter.hook_files(home):
            try:
                change = adapter.plan_file_removal(path)
            except Exception as e:
                print(f"\n[{name}] {spec.display}: {_plan_error(path, e)}")
                exit_code = 1
                had_any = True
                continue
            if not change.changed:
                continue
            found += 1
            had_any = True
            print(f"\n[{name}] {spec.display} -> {change.path}")
            _print_change(change)
            if change.delete:
                print("  (nothing else is left in the file: it is removed)")
            if args.apply:
                if not _write(change, stamp):
                    exit_code = 1
            else:
                print("  (dry run: re-run with --apply to write, a backup is kept)")
        if not had_any and name in wanted:
            print(f"\n[{name}] {spec.display}: no relay hooks in {spec.config_file(home)}")
    if args.agents_md:
        md = agents_md.plan_removal(Path(args.agents_md).expanduser())
        if md.changed:
            found += 1
            print(f"\n[agents-md] {md.path}")
            print("  " + md.diff().replace("\n", "\n  ").rstrip())
            if args.apply:
                if not _write(md, stamp):
                    exit_code = 1
            else:
                print("  (dry run: re-run with --apply to write)")
        else:
            print(f"\n[agents-md] {md.path}: no Remembra Relay section")
    if not found:
        print("No relay hooks found: nothing to remove.")
    leftover = remove_created_dirs(home, apply=args.apply)
    if leftover:
        where = ", ".join(str(d) for d in leftover)
        if args.apply:
            print(f"\nRemoved the directories connect had created (only the relay's backups were left in them): {where}")
        else:
            print(f"\nWith --apply, also removes the directories connect created once only the relay's backups are left: {where}")
    _print_copied_hooks(home)
    print(
        "\nTo remove Remembra completely:\n"
        "  1. remembra-relay disconnect --apply     (the session hooks, above)\n"
        "  2. remembra-install --remove --all --apply --delete-backups\n"
        "     (the MCP server entries, and the backups of agent configs, *.bak-remembra-* and *.bak-relay-*,\n"
        "     that still hold the key)\n"
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
    home = _home()
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
            "config_source": e.data.get("config_source"),
            "held": replay_config(e)[1],
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
    print(f"  server: {_server_line(config)}")
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
        if item["held"]:
            print(f"      held: {item['held']}")
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


def _print_connect_todo(args: argparse.Namespace) -> None:
    """End connect with what the user still has to do (nothing is printed when nothing is left)."""
    try:
        from remembra.marshal import todo

        block = todo.format_todo(todo.after_connect(args))
    except Exception as e:  # the hooks are written either way; never fail connect over its to-do list
        _err(f"could not build the to-do list ({e.__class__.__name__})")
        return
    if block:
        print(block)


def cmd_doctor(args: argparse.Namespace) -> int:
    """Marshal's rules over this machine and (unless --no-server) the trail: exit 0, or 1 when something needs you."""
    from remembra.marshal import doctor

    return doctor.main_args(args.agent, args.format, args.no_server, args.color)


def _warn_missing_key() -> None:
    """Loud notice that the hooks cannot reach the server: they will do nothing."""
    red, reset = ("\033[31;1m", "\033[0m") if sys.stderr.isatty() else ("", "")
    from remembra.marshal.todo import key_step_here, own_server_hint  # connect's to-do list and the doctor's step

    _err(
        f"{red}no Remembra API key found{reset}: the hooks will not load or save handoffs until one is set.\n"
        "  Checked: REMEMBRA_API_KEY, ~/.claude.json and ~/.codex/config.toml (remembra MCP server env),"
        " ~/.remembra/credentials.\n"
        "  Fix: create a key in the Remembra dashboard (Settings > API keys), then run\n"
        f"    {key_step_here()}\n"
        "  which asks for the key (it is never put on the command line), or export REMEMBRA_API_KEY\n"
        "  and REMEMBRA_URL where your agents start." + own_server_hint()
    )


# ---------------------------------------------------------------------------
# argparse
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="remembra-relay", description="Session continuity across AI agents (Remembra Relay).")
    parser.add_argument("--version", action="version", version=f"remembra-relay {_version()}")
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
    p_brief.add_argument("--format", choices=list(OUTPUT_MODES), help="Output format")
    p_brief.add_argument(
        "--recent",
        type=int,
        default=8,
        help="Recent handoffs and checkpoints of this project to list (default 8; at most 5 are shown)",
    )
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

    p_doctor = sub.add_parser(
        "doctor", help="Say why handoffs don't arrive, from this machine's files and your trail (reads only)"
    )
    p_doctor.add_argument("--agent", action="append", choices=list(REGISTRY), help=f"Only these agents ({hooks}); repeatable")
    p_doctor.add_argument("--format", choices=["text", "json"], default="text")
    p_doctor.add_argument("--no-server", action="store_true", help="Do not ask the server; read this machine only")
    p_doctor.add_argument("--color", choices=["auto", "always", "never"], default="auto")
    p_doctor.set_defaults(func=cmd_doctor)

    p_resolve = sub.add_parser("resolve", help="Show (or bind) the project id for this location")
    common(p_resolve)
    p_resolve.add_argument("--bind", action="store_true", help="Re-bind this location to --project")
    p_resolve.set_defaults(func=cmd_resolve)

    p_connect = sub.add_parser("connect", help="Wire installed agents' session hooks (dry run by default)")
    p_connect.add_argument("--apply", action="store_true", help="Write the changes (backups are kept)")
    p_connect.add_argument("--agent", action="append", help=f"Only these agents ({hooks}); repeatable")
    p_connect.add_argument("--include-unverified", action="store_true", help="Also write unverified adapters")
    p_connect.add_argument(
        "--force", action="store_true", help="Write an agent named with --agent even when it is not detected on this machine"
    )
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
    projects.add_parser(sub)
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
    if args.command == "connect":
        _print_connect_todo(args)
    return code


def entrypoint() -> None:
    sys.exit(main())


if __name__ == "__main__":
    entrypoint()
