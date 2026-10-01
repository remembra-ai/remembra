"""Crew mode over MCP (spec §7, WP-11): the seven crew tools, implicit join and the piggyback.

The MCP server is a separate process that talks to the Remembra REST API with the caller's
own API key, so every crew tool is a thin wrapper over the §6 routes. This module holds the
parts that are not MCP plumbing:

* **Seats.** The MCP session (``Mcp-Session-Id`` on streamable HTTP, one id per stdio
  process) becomes a crew session on first use: ``POST /crews/join`` with
  ``adapter='mcp'`` and ``client_kind='mcp'`` (the server makes it ``advisory``). The
  session token is held in memory only, keyed by the API key's fingerprint, the MCP
  session id and the project, so one tenant's token is never used for another.
* **Liveness.** MCP sessions have no crewd heartbeat. Any Remembra MCP call re-joins
  with the current token at most once per :data:`TOUCH_INTERVAL_S`; the server treats a
  re-join as activity and renews the session's leases (§10.1 "MCP-only: last call").
* **Long-poll.** ``crew_claim(wait_s=…)`` and ``crew_say(wait_s=…)`` pass ``wait_s`` to the
  server, which holds the request (≤300 s / ≤120 s); the HTTP timeout is raised to match.
* **Piggyback.** Every Remembra MCP result gets a ``crew_notice`` (≤300 chars) when this
  session's queue changed: at most one per :data:`NOTICE_INTERVAL_S`, except collisions,
  overrides and pause, which are sent at once (§7). Notices carry server-template text only
  (item titles: ids, slugs, callsigns); agent-authored text is never placed in one.

Agent-facing text follows §11: server templates outside the data block, anything agent- or
repo-authored (task titles, message bodies, zone titles, odd paths) inside the
``<remembra-data untrusted="true">`` block with the relay ``DATA_PREAMBLE``, one line per
item, tags neutralised; nothing ever names a destructive command or the bypass mechanism.
"""

from __future__ import annotations

import re
import shlex
import threading
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Final

import httpx

from remembra.client.memory import Memory, _header_agent_id
from remembra.crew import schemas as S
from remembra.relay.handoff import CREW_FOOTER, data_line, police_item, render_crew_block
from remembra.security.error_sanitizer import sanitize_error_message
from remembra.security.untrusted import DATA_PREAMBLE

SESSION_HEADER: Final = "X-Remembra-Crew-Session"
AGENT_HEADER: Final = "X-Remembra-Agent-Id"
TOUCH_INTERVAL_S: Final = 60.0
POLL_INTERVAL_S: Final = 5.0
NOTICE_INTERVAL_S: Final = float(S.MCP_PIGGYBACK_MIN_INTERVAL_S)
LIST_TTL_S: Final = 15.0
HTTP_GRACE_S: Final = 15.0
MAX_SCOPES: Final = 1000
# Session-queue kinds delivered immediately (server-generated collisions, human overrides and pause, §7).
URGENT_KINDS: Final = frozenset({"collision_notice", "override_notice"})
NOTICE_CAP: Final = S.TEXT_CAPS["piggyback"]
MCP_FOOTER: Final = (
    "You are an MCP session (advisory): nothing is claimed for you automatically. Claim before editing "
    '(crew_claim or crew_task action="start"); call crew_checkpoint after commits and test runs.'
)
CORRECTIVE: Final = "Do not modify these files further. Tell @{who} or @mani with crew_say."

_SAFE_TEXT_RE: Final = re.compile(r"[A-Za-z0-9._/@+:=-]{1,200}")
_TASK_NUMBER_RE: Final = re.compile(r"(?:t-?|#)?([1-9][0-9]{0,6})", re.IGNORECASE)
_TARGET_RE: Final = re.compile(r"@?[A-Za-z0-9][A-Za-z0-9._:-]{0,79}")
_CLIENT_ID_RE: Final = re.compile(r"[^A-Za-z0-9._:-]+")
_LIVE_CLAIM_STATES: Final = ("active", "offered", "reserved", "queued")
_OPEN_TASK_STATUSES: Final = ("backlog", "ready", "claimed", "in_progress", "blocked", "review", "stalled")


# ---------------------------------------------------------------------------
# Errors and HTTP
# ---------------------------------------------------------------------------


class CrewApiError(Exception):
    """A crew REST call failed. ``status`` 0 means the server was not reached."""

    def __init__(self, status: int, error: str, message: str, body: Mapping[str, Any] | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.error = error
        self.message = message
        self.body: dict[str, Any] = dict(body or {})


class CrewUsageError(Exception):
    """The tool was called with arguments that cannot work (answered as text, no REST call)."""


def _clean_message(text: Any, limit: int = 300) -> str:
    return S.clip_item(sanitize_error_message(Exception(str(text or ""))), limit)


def _error_from(res: httpx.Response) -> CrewApiError:
    try:
        payload: Any = res.json()
    except ValueError:
        payload = res.text
    detail = payload.get("detail", payload) if isinstance(payload, dict) else payload
    if isinstance(detail, dict):
        return CrewApiError(
            res.status_code,
            str(detail.get("error") or f"http_{res.status_code}"),
            _clean_message(detail.get("message") or detail.get("error") or ""),
            detail,
        )
    if isinstance(detail, list):  # FastAPI request validation
        parts = [
            f"{'.'.join(str(p) for p in (d.get('loc') or [])[1:])}: {d.get('msg')}" for d in detail[:3] if isinstance(d, dict)
        ]
        return CrewApiError(res.status_code, "validation", _clean_message("; ".join(parts) or "invalid request"))
    return CrewApiError(
        res.status_code, "route_missing" if res.status_code == 404 else f"http_{res.status_code}", _clean_message(detail)
    )


class CrewHttp:
    """Crew REST calls with the caller's own client (key, agent header, base URL)."""

    def __init__(self, client: Memory) -> None:
        self.client = client

    def call(
        self,
        method: str,
        path: str,
        *,
        token: str | None = None,
        json: Mapping[str, Any] | None = None,
        params: Mapping[str, Any] | None = None,
        timeout: float | None = None,
        etag: str | None = None,
        if_match: str | None = None,
    ) -> tuple[int, Any, str | None]:
        """``(status, body, etag)``; ``body`` is None for 304. Raises :class:`CrewApiError` on ≥400 or no answer."""
        headers: dict[str, str] = {}
        agent = _header_agent_id(self.client.agent_id)
        if agent:
            headers[AGENT_HEADER] = agent
        if token:
            headers[SESSION_HEADER] = token
        if etag:
            headers["If-None-Match"] = etag
        if if_match:
            headers["If-Match"] = if_match
        try:
            res = self.client._client.request(
                method,
                f"{self.client.base_url}/api/v1{path}",
                json=dict(json) if json is not None else None,
                params=dict(params) if params is not None else None,
                headers=headers,
                timeout=timeout if timeout is not None else self.client.timeout,
            )
        except httpx.TimeoutException:
            raise CrewApiError(0, "timeout", "The Remembra server did not answer in time.") from None
        except httpx.HTTPError as e:
            raise CrewApiError(0, "unreachable", _clean_message(f"The Remembra server is unreachable: {e}")) from None
        if res.status_code == 304:
            return 304, None, res.headers.get("ETag")
        if res.status_code >= 400:
            raise _error_from(res)
        try:
            body = res.json()
        except ValueError:
            body = None
        return res.status_code, body, res.headers.get("ETag")


# ---------------------------------------------------------------------------
# Seats
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CallerInfo:
    """Who calls: the per-caller SDK client, a fingerprint of its key, the MCP session and agent."""

    client: Memory
    fingerprint: str
    client_session_id: str
    agent_id: str | None
    default_project: str | None = None


@dataclass
class Seat:
    """One crew session held by this MCP session for one project."""

    scope: tuple[str, str]
    project_id: str
    agent_id: str
    client_session_id: str
    crew_id: str
    session_id: str
    token: str
    callsign: str
    verified: bool
    observe_only: bool
    status_cursor: int
    notice_cursor: int
    last_touch: float
    left: bool = False
    last_poll: float = 0.0
    last_notice: float = 0.0
    notice_etag: str | None = None
    pending: list[dict[str, Any]] = field(default_factory=list)
    current_task_id: str | None = None
    cache: dict[str, tuple[float, Any]] = field(default_factory=dict)
    lock: threading.RLock = field(default_factory=threading.RLock, repr=False)

    def cached(self, name: str, loader: Callable[[], Any], ttl: float = LIST_TTL_S) -> Any:
        with self.lock:
            hit = self.cache.get(name)
            if hit is not None and time.monotonic() - hit[0] < ttl:
                return hit[1]
        value = loader()
        with self.lock:
            self.cache[name] = (time.monotonic(), value)
        return value

    def forget(self, *names: str) -> None:
        with self.lock:
            for n in names or tuple(self.cache):
                self.cache.pop(n, None)


def _client_session_suffix(base: str) -> str:
    return f"{base[:110]}.r{uuid.uuid4().hex[:8]}"


def clean_client_session_id(value: str) -> str:
    """A join ``session_id`` (≤128 chars, id-safe) for an MCP session id."""
    cleaned = _CLIENT_ID_RE.sub("-", value.strip()).strip("-")
    return (cleaned or uuid.uuid4().hex)[:120]


# ---------------------------------------------------------------------------
# Text helpers
# ---------------------------------------------------------------------------


def _is_resource(value: str) -> bool:
    """``schema:main``, ``deploy:vercel``: a resource claim rather than a zone slug or id."""
    return ":" in value and not value.startswith("zn_")


def _safe(value: Any) -> str | None:
    """``value`` when it is a plain id/slug/path token that may appear outside the data block."""
    text = str(value) if value is not None else ""
    return text if _SAFE_TEXT_RE.fullmatch(text) else None


def agent_text(value: Any) -> str:
    """One agent- or repo-authored value under the brief's trust policy (R-14,
    :func:`remembra.relay.handoff.police_item`): low-trust text becomes the fixed withheld note,
    commands and URLs are flagged, hidden characters and images removed. Server-template
    prefixes (``T-3 title:``, ``zone pos:``) stay outside it, so a withheld item keeps its id."""
    return police_item(str(value or ""))


def data_block(items: Sequence[str]) -> str:
    """Items inside the one data block (relay wrapper, §11): one line each, clipped, with the trust
    policy's notes kept at the end (the agent-authored parts went through :func:`agent_text`)."""
    lines = [data_line(i, S.DATA_ITEM_CLIP) for i in items if str(i or "").strip()]
    if not lines:
        return ""
    return "\n".join([S.DATA_OPEN, DATA_PREAMBLE, *lines, S.DATA_CLOSE])


def join_text(lines: Sequence[str], data: Sequence[str] = ()) -> str:
    out = [ln for ln in lines if ln]
    block = data_block(list(dict.fromkeys(data)))
    if block:
        out.append(block)
    return "\n".join(out)


def task_ref(task: Mapping[str, Any] | None) -> str | None:
    if not task:
        return None
    number = task.get("number")
    return f"T-{number}" if isinstance(number, int) and not isinstance(number, bool) else None


def error_text(e: CrewApiError, what: str) -> str:
    if e.status == 0:
        return f"CREW UNAVAILABLE: {e.message}"
    if e.status == 503 or e.error in ("route_missing", "crew_unavailable"):
        return "CREW UNAVAILABLE: Crew mode is not enabled on this Remembra server."
    if e.status == 429:
        retry = e.body.get("retry_after_s")
        return f"RATE LIMITED ({what}): retry in {retry}s." if retry is not None else f"RATE LIMITED ({what})."
    return f"CREW ERROR ({what}: {e.status} {S.clip_item(e.error, 64)}): {e.message}"


# ---------------------------------------------------------------------------
# The bridge
# ---------------------------------------------------------------------------


class CrewBridge:
    """Seats per MCP session plus the seven crew tools, implemented over the REST API."""

    def __init__(self, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._lock = threading.RLock()
        self._seats: dict[tuple[str, str], dict[str, Seat]] = {}
        self._current: dict[tuple[str, str], str] = {}

    # -- seat registry ------------------------------------------------------------

    def seat_for_scope(self, caller: CallerInfo) -> Seat | None:
        """The crew session this MCP session uses now (None before its first crew call)."""
        scope = (caller.fingerprint, caller.client_session_id)
        with self._lock:
            project = self._current.get(scope)
            return self._seats.get(scope, {}).get(project) if project else None

    def _remember(self, seat: Seat) -> None:
        with self._lock:
            if seat.scope not in self._seats and len(self._seats) >= MAX_SCOPES:
                oldest = next(iter(self._seats))
                self._seats.pop(oldest, None)
                self._current.pop(oldest, None)
            self._seats.setdefault(seat.scope, {})[seat.project_id] = seat
            self._current[seat.scope] = seat.project_id

    def _join_body(
        self, caller: CallerInfo, project_id: str, client_session_id: str, checkout: Mapping[str, Any] | None
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "project_id": project_id,
            "agent_id": caller.agent_id,
            "session_id": client_session_id,
            "adapter": "mcp",
            "client_kind": "mcp",
            "source": "mcp",
        }
        branch = (checkout or {}).get("branch")
        head = (checkout or {}).get("head_commit")
        if isinstance(branch, str) and branch:
            body["branch"] = branch[:256]
        if isinstance(head, str) and re.fullmatch(S.SHA_PATTERN, head):
            body["head"] = head
        return body

    def seat(
        self,
        caller: CallerInfo,
        project_id: str | None = None,
        checkout: Mapping[str, Any] | None = None,
    ) -> tuple[Seat, dict[str, Any] | None]:
        """The seat for ``project_id`` (default: the current seat, else the caller's default project).

        Joins on first use (implicit join, §7) and re-joins a seat that left. Returns the seat and
        the join response when one was made now.
        """
        if not caller.agent_id:
            raise CrewUsageError(
                "Crew mode needs this agent's id: set REMEMBRA_AGENT_ID in the MCP server's environment "
                "(or send the X-Remembra-Agent-Id header), e.g. claude-desktop or codex."
            )
        scope = (caller.fingerprint, caller.client_session_id)
        with self._lock:
            project = project_id or self._current.get(scope) or caller.default_project
            existing = self._seats.get(scope, {}).get(project) if project else None
        if not project:
            raise CrewUsageError(
                "No project yet: call crew_status(project_id=...) or crew_status(git_remote=... / root_path=...) "
                "first, or call session_brief with your location."
            )
        if existing is not None and not existing.left:
            with self._lock:
                self._current[scope] = project
            return existing, None
        http = CrewHttp(caller.client)
        if existing is not None:
            _, body, _ = http.call(
                "POST",
                "/crews/join",
                token=existing.token,
                json=self._join_body(caller, project, existing.client_session_id, checkout),
            )
            existing.left = False
            existing.last_touch = self._clock()
            existing.observe_only = bool(body.get("observe_only"))
            existing.current_task_id = (body.get("session") or {}).get("current_task_id")
            existing.callsign = str(body.get("callsign") or existing.callsign)
            self._remember(existing)
            return existing, body
        client_session_id = caller.client_session_id
        try:
            _, body, _ = http.call("POST", "/crews/join", json=self._join_body(caller, project, client_session_id, checkout))
        except CrewApiError as e:
            if e.status != 409 or e.error != "session_exists":
                raise
            # This MCP session id joined before (the process restarted with a fixed REMEMBRA_SESSION_ID)
            # and its token is gone: join as a new seat instead of guessing the old token.
            client_session_id = _client_session_suffix(client_session_id)
            _, body, _ = http.call("POST", "/crews/join", json=self._join_body(caller, project, client_session_id, checkout))
        token = body.get("session_token")
        if not isinstance(token, str) or not token:
            raise CrewApiError(500, "no_session_token", "The server did not return a session token for the new crew session.")
        session = body.get("session") or {}
        seq = int(body.get("seq") or 0)
        seat = Seat(
            scope=scope,
            project_id=project,
            agent_id=str(caller.agent_id),
            client_session_id=client_session_id,
            crew_id=str(body["crew_id"]),
            session_id=str(body["session_id"]),
            token=token,
            callsign=str(body.get("callsign") or ""),
            verified=bool(session.get("agent_verified")),
            observe_only=bool(body.get("observe_only")),
            status_cursor=max(0, seq - 20),
            notice_cursor=seq,
            last_touch=self._clock(),
            current_task_id=session.get("current_task_id"),
        )
        self._remember(seat)
        return seat, body

    def touch(self, caller: CallerInfo, seat: Seat, *, force: bool = False) -> dict[str, Any] | None:
        """Re-join with the current token (liveness + lease renewal) at most once per minute."""
        now = self._clock()
        with seat.lock:
            if seat.left or (not force and now - seat.last_touch < TOUCH_INTERVAL_S):
                return None
            seat.last_touch = now
        _, body, _ = CrewHttp(caller.client).call(
            "POST",
            "/crews/join",
            token=seat.token,
            json=self._join_body(caller, seat.project_id, seat.client_session_id, None),
        )
        session = body.get("session") or {}
        with seat.lock:
            seat.observe_only = bool(body.get("observe_only"))
            seat.current_task_id = session.get("current_task_id")
            if body.get("callsign"):
                seat.callsign = str(body["callsign"])
        return dict(body)

    def mark_left(self, caller: CallerInfo) -> None:
        seat = self.seat_for_scope(caller)
        if seat is not None:
            with seat.lock:
                seat.left = True
                seat.pending.clear()

    # -- lookups (cached per seat) --------------------------------------------------

    def _zones(self, http: CrewHttp, seat: Seat) -> list[dict[str, Any]]:
        def load() -> list[dict[str, Any]]:
            _, body, _ = http.call("GET", f"/crews/{seat.crew_id}/zones", token=seat.token)
            return list(body.get("zones") or []) if isinstance(body, dict) else []

        return list(seat.cached("zones", load))

    def _tasks(self, http: CrewHttp, seat: Seat) -> list[dict[str, Any]]:
        def load() -> list[dict[str, Any]]:
            _, body, _ = http.call("GET", f"/crews/{seat.crew_id}/tasks", token=seat.token)
            return list(body.get("tasks") or []) if isinstance(body, dict) else []

        return list(seat.cached("tasks", load))

    def _sessions(self, http: CrewHttp, seat: Seat) -> list[dict[str, Any]]:
        def load() -> list[dict[str, Any]]:
            _, body, _ = http.call("GET", f"/crews/{seat.crew_id}/sessions", token=seat.token)
            return list(body.get("sessions") or []) if isinstance(body, dict) else []

        return list(seat.cached("sessions", load))

    def _claims(self, http: CrewHttp, seat: Seat) -> list[dict[str, Any]]:
        _, body, _ = http.call("GET", f"/crews/{seat.crew_id}/claims", token=seat.token)
        claims = list(body.get("claims") or []) if isinstance(body, dict) else []
        return [c for c in claims if c.get("state") in _LIVE_CLAIM_STATES]

    def _zone(self, http: CrewHttp, seat: Seat, ref: str) -> dict[str, Any]:
        wanted = ref.strip()
        for z in self._zones(http, seat):
            if z.get("id") == wanted or str(z.get("slug") or "").lower() == wanted.lower():
                return z
        raise CrewUsageError(f"Unknown zone {S.clip_item(wanted, 60)!r}. crew_status(verbose=True) lists the zones.")

    def _zone_label(self, http: CrewHttp, seat: Seat, zone_id: Any) -> str | None:
        if not zone_id:
            return None
        for z in self._zones(http, seat):
            if z.get("id") == zone_id:
                return _safe(z.get("slug")) or str(zone_id)
        return str(zone_id)

    def _task(self, http: CrewHttp, seat: Seat, ref: str | None, *, fresh: bool = False) -> dict[str, Any]:
        if fresh:
            seat.forget("tasks")
        if not ref:
            if seat.current_task_id:
                ref = seat.current_task_id
            else:
                raise CrewUsageError('Name the task (task="T-14").')
        wanted = ref.strip()
        tasks = self._tasks(http, seat)
        m = _TASK_NUMBER_RE.fullmatch(wanted)
        for t in tasks:
            if t.get("id") == wanted or (m and t.get("number") == int(m.group(1))):
                return t
        if not fresh:
            return self._task(http, seat, ref, fresh=True)
        raise CrewUsageError(f'Unknown task {S.clip_item(wanted, 40)!r}. crew_task(action="list") lists the tasks.')

    def _task_by_id(self, http: CrewHttp, seat: Seat, task_id: Any) -> dict[str, Any] | None:
        if not task_id:
            return None
        for t in self._tasks(http, seat):
            if t.get("id") == task_id:
                return t
        seat.forget("tasks")
        for t in self._tasks(http, seat):
            if t.get("id") == task_id:
                return t
        return None

    def _session_id_for(self, http: CrewHttp, seat: Seat, to: str) -> str:
        wanted = to.strip().lstrip("@")
        if S.is_id("session", wanted):
            return wanted
        seat.forget("sessions")
        for s in self._sessions(http, seat):
            if str(s.get("callsign") or "").lower() == wanted.lower() and s.get("state") != "ended":
                return str(s["id"])
        raise CrewUsageError(f"No live session called {S.clip_item(wanted, 40)!r} in this crew.")

    def _callsign(self, http: CrewHttp, seat: Seat, session_id: Any) -> str | None:
        if not session_id:
            return None
        if session_id == seat.session_id:
            return seat.callsign
        for s in self._sessions(http, seat):
            if s.get("id") == session_id:
                return _safe(s.get("callsign"))
        seat.forget("sessions")
        for s in self._sessions(http, seat):
            if s.get("id") == session_id:
                return _safe(s.get("callsign"))
        return None

    # -- crew_status ------------------------------------------------------------------

    def status(
        self,
        caller: CallerInfo,
        *,
        project_id: str | None,
        checkout: Mapping[str, Any] | None,
        verbose: bool,
    ) -> str:
        seat, joined = self.seat(caller, project_id, checkout)
        http = CrewHttp(caller.client)
        rejoin = joined or self.touch(caller, seat, force=True) or {}
        lines: list[str] = []
        data: list[str] = []
        brief = self._crew_block(http, caller, seat)
        if brief:
            lines.append(brief)
        lines.append(self._you_line(http, seat, rejoin))
        items = self._queue_items(http, seat)
        if items:
            lines.append("FOR YOU (session queue):")
            lines.extend(f"- {S.clip_item(str(i.get('title') or ''), 200)}" for i in items[:8])
        deltas = self._deltas(http, seat)
        lines.extend(deltas)
        if verbose:
            v_lines, v_data = self._verbose(http, seat)
            lines.extend(v_lines)
            data.extend(v_data)
        lines.append(MCP_FOOTER)
        self._resolve_items(http, seat, items)
        with seat.lock:
            seat.pending.clear()
            seat.notice_cursor = max(seat.notice_cursor, seat.status_cursor)
            seat.last_notice = self._clock()
        return join_text(lines, data)

    def _crew_block(self, http: CrewHttp, caller: CallerInfo, seat: Seat) -> str:
        params = {
            "project_id": seat.project_id,
            "agent_id": seat.agent_id,
            "session_id": seat.client_session_id,
            "recent_n": 0,
            "inbox_limit": 0,
        }
        try:
            _, brief, _ = http.call("GET", "/session/brief", params=params)
        except CrewApiError as e:
            return f"CREW {seat.project_id}: crew block unavailable ({e.status} {S.clip_item(e.error, 40)})"
        crew = brief.get("crew") if isinstance(brief, dict) else None
        if not isinstance(crew, dict):
            return ""
        text = render_crew_block(crew, allowed_urls=tuple(brief.get("repo_url_prefixes") or ()))
        lines = text.split("\n")
        return "\n".join(ln for ln in lines if ln != CREW_FOOTER)

    def _you_line(self, http: CrewHttp, seat: Seat, rejoin: Mapping[str, Any]) -> str:
        session = rejoin.get("session") or {}
        state = _safe(session.get("state")) or "active"
        verified = "key-verified" if seat.verified else "self-declared"
        parts = [f"YOU: {seat.callsign} ({seat.session_id}) · mcp, advisory · {verified} · {state}"]
        if seat.observe_only:
            parts.append("OBSERVE-ONLY (plan limit: you cannot claim)")
        my_tasks = [t for t in rejoin.get("my_tasks") or [] if isinstance(t, dict)]
        if my_tasks:
            parts.append("tasks " + ", ".join(f"{task_ref(t)} {t.get('status')}" for t in my_tasks[:3] if task_ref(t)))
        try:
            mine = [c for c in self._claims(http, seat) if c.get("holder_session_id") == seat.session_id]
        except CrewApiError:
            mine = []
        if mine:
            parts.append("holding " + ", ".join(self._claim_label(http, seat, c) for c in mine[:6]))
        else:
            parts.append("holding nothing")
        return " · ".join(parts)

    def _claim_label(self, http: CrewHttp, seat: Seat, claim: Mapping[str, Any]) -> str:
        zone = self._zone_label(http, seat, claim.get("zone_id"))
        if zone:
            target = f"zone {zone}"
        elif claim.get("resource"):
            target = _safe(claim.get("resource")) or "a resource"
        else:
            target = f"path {_safe(claim.get('path_glob')) or '(see verbose)'}"
        task = task_ref(self._task_by_id(http, seat, claim.get("task_id"))) if claim.get("task_id") else None
        bits = [str(claim.get("mode") or "exclusive"), str(claim.get("state") or "")]
        if task:
            bits.append(task)
        return f"{target} ({', '.join(b for b in bits if b)})"

    def _queue_items(self, http: CrewHttp, seat: Seat) -> list[dict[str, Any]]:
        try:
            _, body, _ = http.call("GET", f"/crews/{seat.crew_id}/inbox", token=seat.token, params={"audience": "me"})
        except CrewApiError:
            return []
        if not isinstance(body, dict) or body.get("audience") != "session":
            return []
        items = [i for i in body.get("items") or [] if isinstance(i, dict) and i.get("recipient") == seat.session_id]
        return sorted(items, key=lambda i: (int(i.get("priority") or 2), str(i.get("id"))))

    def _resolve_items(self, http: CrewHttp, seat: Seat, items: Sequence[Mapping[str, Any]]) -> None:
        """Items shown to the agent are acknowledged (resolved) so FOR YOU only counts new ones."""
        for item in items[:8]:
            try:
                http.call("POST", f"/inbox/items/{item['id']}/resolve", token=seat.token)
            except CrewApiError:
                continue

    def _poll(
        self, http: CrewHttp, seat: Seat, since: int, *, etag: str | None = None
    ) -> tuple[list[dict[str, Any]], int, str | None]:
        events: list[dict[str, Any]] = []
        cursor = since
        new_etag = etag
        for _ in range(5):
            status, body, tag = http.call(
                "GET",
                f"/crews/{seat.crew_id}/events",
                token=seat.token,
                params={"since_seq": cursor, "limit": 200},
                etag=new_etag if not events else None,
            )
            new_etag = tag or new_etag
            if status == 304 or not isinstance(body, dict):
                break
            page = [e for e in body.get("events") or [] if isinstance(e, dict)]
            events.extend(page)
            cursor = max(cursor, int(body.get("last_seq") or cursor))
            if not body.get("has_more") or not page:
                break
        return events, cursor, new_etag

    def _deltas(self, http: CrewHttp, seat: Seat) -> list[str]:
        try:
            events, cursor, _ = self._poll(http, seat, seat.status_cursor)
        except CrewApiError as e:
            return [f"CHANGES: unavailable ({e.status} {S.clip_item(e.error, 40)})"]
        with seat.lock:
            seat.status_cursor = max(seat.status_cursor, cursor)
        shown = [e for e in events if e.get("type") not in ("activity.burst",)]
        if not shown:
            return ["CHANGES since your last crew_status: none"]
        ranked = sorted(shown, key=lambda x: (0 if self._concerns_me(x, seat) or x.get("moment") else 1, -int(x.get("seq") or 0)))
        lines = [f"CHANGES since your last crew_status ({len(shown)}, most relevant first):"]
        for ev in sorted(ranked[:8], key=lambda x: int(x.get("seq") or 0)):
            summary = police_item(str(ev.get("summary") or ""), clip_body=lambda t: S.clip_item(t, 200))
            lines.append(f"- #{ev.get('seq')} {summary}")
        if len(shown) > 8:
            lines.append(f"- … {len(shown) - 8} more")
        return lines

    @staticmethod
    def _concerns_me(event: Mapping[str, Any], seat: Seat) -> bool:
        refs = event.get("refs") or {}
        if refs.get("session_id") == seat.session_id:
            return True
        payload = event.get("payload") or {}
        for key in ("claim", "collision", "task", "item"):
            obj = payload.get(key)
            if isinstance(obj, dict) and seat.session_id in (
                obj.get("holder_session_id"),
                obj.get("offered_to"),
                obj.get("session_a"),
                obj.get("session_b"),
                obj.get("owner_session_id"),
                obj.get("recipient"),
            ):
                return True
        return payload.get("to_session") == seat.session_id

    def _verbose(self, http: CrewHttp, seat: Seat) -> tuple[list[str], list[str]]:
        lines: list[str] = []
        data: list[str] = []
        zones = [z for z in self._zones(http, seat) if not z.get("builtin")]
        if zones:
            lines.append(
                "ZONES: "
                + " · ".join(
                    f"{_safe(z.get('slug')) or z.get('id')} ({z.get('mode')}{', leaf' if z.get('is_leaf') else ''})"
                    for z in zones[:20]
                )
            )
            for z in zones[:20]:
                slug = _safe(z.get("slug")) or str(z.get("id"))
                globs = ", ".join(str(g) for g in (z.get("include_globs") or [])[:4])
                data.append(f"zone {slug}: {agent_text(z.get('title'))} · paths {agent_text(globs)}")
        tasks = [t for t in self._tasks(http, seat) if t.get("status") in _OPEN_TASK_STATUSES]
        if tasks:
            lines.append("OPEN TASKS: " + " · ".join(self._task_line(http, seat, t) for t in tasks[:12]))
            data.extend(f"{task_ref(t)} title: {agent_text(t.get('title'))}" for t in tasks[:12])
        try:
            _, body, _ = http.call("GET", f"/crews/{seat.crew_id}/messages", token=seat.token, params={"limit": 5})
            msgs = [m for m in (body or {}).get("items") or [] if isinstance(m, dict)]
        except CrewApiError:
            msgs = []
        for m in msgs[-5:]:
            who = m.get("author_callsign") or m.get("author_label") or "someone"
            data.append(f"message {m.get('id')} ({m.get('kind')}) from {who}: {agent_text(m.get('body'))}")
        return lines, data

    def _task_line(self, http: CrewHttp, seat: Seat, task: Mapping[str, Any]) -> str:
        owner = self._callsign(http, seat, task.get("owner_session_id")) if task.get("owner_session_id") else None
        zones = [self._zone_label(http, seat, z) for z in task.get("zone_ids") or []]
        bits = [str(task.get("status"))]
        if owner:
            bits.append(f"owner {owner}")
        if zones:
            bits.append("zones " + ",".join(z for z in zones if z))
        return f"{task_ref(task)} [{'; '.join(bits)}]"

    # -- piggyback ----------------------------------------------------------------------

    def notice(self, caller: CallerInfo, *, force: bool = False) -> str | None:
        """The ``crew_notice`` for this MCP session's latest call, or None. Never raises."""
        seat = self.seat_for_scope(caller)
        if seat is None or seat.left:
            return None
        try:
            return self._notice(caller, seat, force=force)
        except (CrewApiError, CrewUsageError, httpx.HTTPError, ValueError, KeyError, TypeError):
            return None

    def _notice(self, caller: CallerInfo, seat: Seat, *, force: bool) -> str | None:
        http = CrewHttp(caller.client)
        try:
            self.touch(caller, seat)
        except CrewApiError:
            pass  # liveness is best effort; the notice still goes out
        now = self._clock()
        with seat.lock:
            due = force or now - seat.last_poll >= POLL_INTERVAL_S
            since, etag = seat.notice_cursor, seat.notice_etag
            if due:
                seat.last_poll = now  # set first: a failing poll (429, outage) is not retried on every call
        if due:
            events, cursor, tag = self._poll(http, seat, since, etag=etag)
            with seat.lock:
                seat.notice_etag = tag
                seat.notice_cursor = max(seat.notice_cursor, cursor)
                for e in events:
                    item = self._queue_item(e, seat)
                    if item is not None and all(p.get("id") != item.get("id") for p in seat.pending):
                        seat.pending.append(item)
        with seat.lock:
            if not seat.pending:
                return None
            urgent = any(p.get("kind") in URGENT_KINDS for p in seat.pending)
            if not (force or urgent) and now - seat.last_notice < NOTICE_INTERVAL_S:
                return None
            items = sorted(seat.pending, key=lambda p: (0 if p.get("kind") in URGENT_KINDS else 1, int(p.get("priority") or 2)))
            seat.pending.clear()
            seat.last_notice = now
        return render_notice(seat.callsign, items)

    @staticmethod
    def _queue_item(event: Mapping[str, Any], seat: Seat) -> dict[str, Any] | None:
        if event.get("type") != "inbox.item_created":
            return None
        item = (event.get("payload") or {}).get("item")
        if not isinstance(item, dict) or item.get("audience") != "session" or item.get("recipient") != seat.session_id:
            return None
        return dict(item)

    # -- crew_claim -------------------------------------------------------------------

    def claim(
        self,
        caller: CallerInfo,
        *,
        action: str,
        zone: str | None,
        paths: Sequence[str] | None,
        mode: str,
        task: str | None,
        to: str | None,
        baton: bool | None,
        reason: str | None,
        wait_s: int,
    ) -> str:
        seat, _ = self.seat(caller)
        http = CrewHttp(caller.client)
        self._touch_quietly(caller, seat)
        if action == "claim":
            return self._claim(http, seat, zone=zone, paths=paths, mode=mode, task=task, reason=reason, wait_s=wait_s)
        if action == "release":
            return self._release(http, seat, zone=zone, paths=paths, task=task, baton=baton, reason=reason)
        if action == "adopt":
            return self._adopt(http, seat, zone=zone, task=task)
        if action == "handover":
            return self._handover(http, seat, zone=zone, paths=paths, task=task, to=to, reason=reason)
        if action in ("accept", "decline"):
            return self._answer_handover(http, seat, action, zone=zone, task=task)
        raise CrewUsageError(f"Unknown action {action!r}.")

    def _touch_quietly(self, caller: CallerInfo, seat: Seat) -> None:
        try:
            self.touch(caller, seat)
        except CrewApiError:
            pass

    def _targets(
        self, http: CrewHttp, seat: Seat, zone: str | None, paths: Sequence[str] | None
    ) -> tuple[list[tuple[str, dict[str, Any]]], list[str]]:
        """``[(label, claim body target)]`` for a zone and/or paths; plus notes for paths that need no claim."""
        targets: list[tuple[str, dict[str, Any]]] = []
        notes: list[str] = []
        if zone:
            if _is_resource(zone):
                label = _safe(zone) or "resource"
                targets.append((label, {"resource": zone}))
            else:
                z = self._zone(http, seat, zone)
                targets.append((f"zone {_safe(z.get('slug')) or z['id']}", {"zone_id": z["id"]}))
        if paths:
            _, body, _ = http.call("POST", f"/crews/{seat.crew_id}/match", token=seat.token, json={"paths": list(paths)[:200]})
            seen = {t[1].get("zone_id") for t in targets}
            for hit in (body or {}).get("paths") or []:
                path = str(hit.get("path") or "")
                label = _safe(path) or "a path"
                if hit.get("error"):
                    notes.append(f"{label}: not a repo-relative path (pass paths relative to the repository root)")
                elif hit.get("crew_policy"):
                    notes.append(f"{label}: crew policy file, never claimable by agents; ask a human")
                elif hit.get("ignored"):
                    notes.append(f"{label}: ignored by the crew, no claim needed")
                elif hit.get("leaf_zone"):
                    z = self._zone(http, seat, str(hit["leaf_zone"]))
                    if z["id"] not in seen:
                        seen.add(z["id"])
                        targets.append((f"zone {_safe(z.get('slug')) or z['id']}", {"zone_id": z["id"]}))
                elif hit.get("zones"):
                    notes.append(f'{label}: only in a parent zone; claim it with a task (crew_task action="start")')
                elif hit.get("commons"):
                    notes.append(f"{label}: shared commons file, no claim needed")
                else:
                    targets.append((f"path {label}", {"path_glob": path}))
        return targets, notes

    def _claim(
        self,
        http: CrewHttp,
        seat: Seat,
        *,
        zone: str | None,
        paths: Sequence[str] | None,
        mode: str,
        task: str | None,
        reason: str | None,
        wait_s: int,
    ) -> str:
        task_row = self._task(http, seat, task) if task else None
        if task_row is not None and not zone and not paths:
            _, claimed, _ = http.call("POST", f"/tasks/{task_row['id']}/claim", token=seat.token)
            seat.forget("tasks")
            seat.current_task_id = str(task_row["id"])
            got = [self._claim_label(http, seat, c) for c in (claimed or {}).get("claims") or []]
            return f"GRANTED {task_ref(task_row)}: " + (", ".join(got) if got else "claimed (the task has no zones)")
        targets, notes = self._targets(http, seat, zone, paths)
        if not targets:
            if notes:
                return join_text(["NOTHING TO CLAIM:", *[f"- {n}" for n in notes]])
            raise CrewUsageError('Name what to claim: zone="pos", paths=["src/app/pos/cart.ts"] or task="T-14".')
        lines: list[str] = []
        data: list[str] = []
        for label, target in targets:
            body: dict[str, Any] = {
                **target,
                "mode": mode,
                "wait": wait_s > 0,
                "source": "mcp",
            }
            if wait_s > 0:
                body["wait_s"] = wait_s
            if task_row is not None:
                body["task_id"] = task_row["id"]
            if reason:
                body["reason"] = reason[:280]
            started = time.monotonic()
            try:
                status, res, _ = http.call(
                    "POST",
                    f"/crews/{seat.crew_id}/claims",
                    token=seat.token,
                    json=body,
                    timeout=(wait_s + HTTP_GRACE_S) if wait_s > 0 else None,
                )
            except CrewApiError as e:
                lines.append(self._refused(http, seat, label, e, data))
                continue
            waited = int(round(time.monotonic() - started))
            claim = (res or {}).get("claim") or {}
            state = claim.get("state")
            if state == "active":
                note = f" after waiting {waited}s" if wait_s > 0 and waited >= 1 else ""
                lines.append(f"GRANTED {label} ({claim.get('mode')}, epoch {claim.get('epoch')}, {claim.get('id')}){note}")
            elif state == "queued":
                who = self._blocker_text(http, seat, (res or {}).get("blockers") or [])
                waited_txt = f" (waited {waited}s)" if wait_s > 0 else ""
                lines.append(
                    f"QUEUED {label}{waited_txt}: {who}. You are next in line ({claim.get('id')}); "
                    "call crew_claim again with wait_s to keep waiting, or work elsewhere."
                )
            else:
                lines.append(f"{str(state or status).upper()} {label} ({claim.get('id')})")
        lines.extend(f"- {n}" for n in notes)
        return join_text(lines, data)

    def _blocker_text(self, http: CrewHttp, seat: Seat, blockers: Sequence[Mapping[str, Any]]) -> str:
        if not blockers:
            return "held by another session"
        b = blockers[0]
        holder = _safe(b.get("holder_callsign")) or self._callsign(http, seat, b.get("holder_session_id")) or "another session"
        task = task_ref(self._task_by_id(http, seat, b.get("task_id"))) if b.get("task_id") else None
        why = _safe(b.get("reason")) or "held"
        return f"{why} by {holder}" + (f" for {task}" if task else "")

    def _refused(self, http: CrewHttp, seat: Seat, label: str, e: CrewApiError, data: list[str]) -> str:
        blockers = e.body.get("blockers") or []
        if e.status == 409 and e.error == "claim_cap":
            return f"REFUSED: {label}: claim limit reached. Release a claim you no longer need first."
        if e.status == 409 and blockers:
            b = blockers[0]
            holder = _safe(b.get("holder_callsign")) or self._callsign(http, seat, b.get("holder_session_id"))
            task = self._task_by_id(http, seat, b.get("task_id")) if b.get("task_id") else None
            ref = task_ref(task)
            if task is not None and task.get("title"):
                data.append(f"{ref} title: {agent_text(task.get('title'))}")
            reason = str(b.get("reason") or "")
            if holder:
                how = "RESERVED for the next pickup" if "reserved" in reason else "held EXCLUSIVELY"
                return (
                    f"REFUSED: {label} {how} by {holder}" + (f" for {ref}" if ref else "") + ". "
                    f'Work elsewhere or crew_say(to="@{holder}", kind="request_release").'
                )
            return f"REFUSED: {label} is held by a human or reserved. Work elsewhere or ask @mani with crew_say."
        if e.status == 423:
            return f"REFUSED: {label}: {e.message} Work elsewhere or ask @mani with crew_say."
        return error_text(e, f"claim {label}")

    def _my_claims(
        self,
        http: CrewHttp,
        seat: Seat,
        *,
        zone: str | None,
        paths: Sequence[str] | None,
        task_id: str | None,
        states: Sequence[str],
    ) -> list[dict[str, Any]]:
        mine = [c for c in self._claims(http, seat) if c.get("holder_session_id") == seat.session_id and c.get("state") in states]
        if not (zone or paths or task_id):
            return mine
        zone_ids: set[str] = set()
        globs: set[str] = set()
        resources: set[str] = set()
        if zone or paths:
            targets, _ = self._targets(http, seat, zone, paths)
            for _, t in targets:
                if t.get("zone_id"):
                    zone_ids.add(str(t["zone_id"]))
                if t.get("path_glob"):
                    globs.add(str(t["path_glob"]))
                if t.get("resource"):
                    resources.add(str(t["resource"]))
        return [
            c
            for c in mine
            if (task_id and c.get("task_id") == task_id)
            or c.get("zone_id") in zone_ids
            or c.get("path_glob") in globs
            or c.get("resource") in resources
        ]

    def _release(
        self,
        http: CrewHttp,
        seat: Seat,
        *,
        zone: str | None,
        paths: Sequence[str] | None,
        task: str | None,
        baton: bool | None,
        reason: str | None,
    ) -> str:
        task_row = self._task(http, seat, task) if task else None
        if task_row is not None and not zone and not paths:
            _, released, _ = http.call(
                "POST",
                f"/tasks/{task_row['id']}/release",
                token=seat.token,
                json={"baton": True if baton is None else bool(baton)},
            )
            seat.forget("tasks")
            seat.current_task_id = None
            t = (released or {}).get("task") or {}
            return f"RELEASED {task_ref(task_row)}: task is now {t.get('status')}" + (
                "; its zones stay reserved for the next pickup (baton)." if t.get("status") == "stalled" else "."
            )
        if not (zone or paths):
            raise CrewUsageError('Name what to release: zone="pos", paths=[...] or task="T-14".')
        claims = self._my_claims(http, seat, zone=zone, paths=paths, task_id=None, states=("active", "offered", "queued"))
        if not claims:
            return "NOTHING RELEASED: you hold no live claim on that."
        lines = []
        for c in claims:
            body: dict[str, Any] = {"baton": bool(baton)}
            if reason:
                body["note"] = reason[:280]
            try:
                _, res, _ = http.call("POST", f"/claims/{c['id']}/release", token=seat.token, json=body)
            except CrewApiError as e:
                lines.append(error_text(e, f"release {c['id']}"))
                continue
            state = ((res or {}).get("claim") or {}).get("state") or "released"
            lines.append(f"RELEASED {self._claim_label(http, seat, c)} → {state}")
        return "\n".join(lines)

    def _adopt(self, http: CrewHttp, seat: Seat, *, zone: str | None, task: str | None) -> str:
        if task:
            task_row = self._task(http, seat, task)
            try:
                _, body, _ = http.call("POST", f"/tasks/{task_row['id']}/adopt", token=seat.token)
            except CrewApiError as e:
                if e.status in (403, 409):
                    return f"NOT ADOPTED {task_ref(task_row)}: {e.message}"
                raise
            seat.forget("tasks")
            seat.current_task_id = str(task_row["id"])
            return self._adopted(http, seat, task_ref(task_row) or "the task", body or {})
        if not zone:
            raise CrewUsageError('Name the baton to adopt: task="T-12" (as offered in your brief) or zone="pos".')
        z = self._zone(http, seat, zone)
        reserved = [c for c in self._claims(http, seat) if c.get("zone_id") == z["id"] and c.get("state") == "reserved"]
        if not reserved:
            return f"NOT ADOPTED zone {_safe(z.get('slug'))}: nothing is reserved there."
        try:
            _, body, _ = http.call("POST", f"/claims/{reserved[0]['id']}/adopt", token=seat.token, json={})
        except CrewApiError as e:
            if e.status in (403, 409):
                return f"NOT ADOPTED zone {_safe(z.get('slug'))}: {e.message}"
            raise
        return self._adopted(http, seat, f"zone {_safe(z.get('slug'))}", body or {})

    def _adopted(self, http: CrewHttp, seat: Seat, what: str, body: Mapping[str, Any]) -> str:
        claims = [c for c in body.get("claims") or ([body["claim"]] if body.get("claim") else []) if isinstance(c, dict)]
        held = ", ".join(self._claim_label(http, seat, c) for c in claims) if claims else "no zone claims"
        lines = [f"ADOPTED {what}: you now hold {held}."]
        ref = None
        baton = body.get("baton")
        if isinstance(baton, dict):
            ref = baton.get("baton_ref")
        ref = ref or next((c.get("baton_ref") for c in claims if c.get("baton_ref")), None)
        if isinstance(ref, str) and re.fullmatch(r"refs/remembra/baton/[A-Za-z0-9_/-]{1,120}", ref):
            lines.append(f"The previous session's uncommitted work is saved as {ref} (the remembra-crew CLI restores it).")
        return "\n".join(lines)

    def _handover(
        self,
        http: CrewHttp,
        seat: Seat,
        *,
        zone: str | None,
        paths: Sequence[str] | None,
        task: str | None,
        to: str | None,
        reason: str | None,
    ) -> str:
        if not to:
            raise CrewUsageError('Name who gets it: to="@codex-1".')
        target = self._session_id_for(http, seat, to)
        task_id = self._task(http, seat, task)["id"] if task else None
        claims = self._my_claims(http, seat, zone=zone, paths=paths, task_id=task_id, states=("active",))
        if not claims:
            return "NOT HANDED OVER: you hold no active claim on that."
        who = self._callsign(http, seat, target) or target
        lines = []
        for c in claims:
            body: dict[str, Any] = {"to": target}
            if reason:
                body["note"] = reason[:280]
            try:
                http.call("POST", f"/claims/{c['id']}/handover", token=seat.token, json=body)
            except CrewApiError as e:
                lines.append(error_text(e, f"handover {c['id']}"))
                continue
            lines.append(f"OFFERED {self._claim_label(http, seat, c)} to {who} (they accept or decline within 10 min).")
        return "\n".join(lines)

    def _answer_handover(self, http: CrewHttp, seat: Seat, action: str, *, zone: str | None, task: str | None) -> str:
        claims = [c for c in self._claims(http, seat) if c.get("state") == "offered" and c.get("offered_to") == seat.session_id]
        if zone and _is_resource(zone):
            claims = [c for c in claims if c.get("resource") == zone]
        elif zone:
            z = self._zone(http, seat, zone)
            claims = [c for c in claims if c.get("zone_id") == z["id"]]
        if task:
            tid = self._task(http, seat, task)["id"]
            claims = [c for c in claims if c.get("task_id") == tid]
        if not claims:
            return f"NOTHING TO {action.upper()}: no handover is offered to you."
        lines = []
        for c in claims:
            label = self._claim_label(http, seat, c)
            try:
                http.call("POST", f"/claims/{c['id']}/{action}", token=seat.token)
            except CrewApiError as e:
                lines.append(error_text(e, f"{action} {c['id']}"))
                continue
            lines.append(f"{'ACCEPTED' if action == 'accept' else 'DECLINED'} {label}")
        return "\n".join(lines)

    # -- crew_guard ---------------------------------------------------------------------

    def guard(self, caller: CallerInfo, *, paths: Sequence[str], command: str | None, mcp_tool: str | None) -> str:
        seat, _ = self.seat(caller)
        http = CrewHttp(caller.client)
        self._touch_quietly(caller, seat)
        body: dict[str, Any] = {"session_id": seat.session_id, "paths": [str(p) for p in paths][:200]}
        if mcp_tool:
            body["op"] = "mcp"
            body["mcp_tool"] = mcp_tool[:128]
        elif command:
            body["op"] = "command"
            try:
                tokens = shlex.split(command)
            except ValueError:
                tokens = command.split()
            body["command_tokens"] = [t[:256] for t in tokens[:64]]
        else:
            body["op"] = "write"
        _, res, _ = http.call("POST", f"/crews/{seat.crew_id}/guard", token=seat.token, json=body)
        res = res or {}
        decision = str(res.get("decision") or "allow")
        reasons = [str(r) for r in res.get("reasons") or [] if r]
        claimed = [self._claim_label(http, seat, c) for c in res.get("auto_claimed") or [] if isinstance(c, dict)]
        if decision == "deny":
            return "DENY " + ("\n".join(reasons) if reasons else f"(rule {res.get('rule')})")
        if decision == "ask":
            return "ASK THE USER FIRST " + ("\n".join(reasons) if reasons else f"(rule {res.get('rule')})")
        lines = ["ALLOW" if decision == "allow" else "ALLOW WITH WARNING"]
        if claimed:
            lines.append("Auto-claimed for you: " + ", ".join(claimed))
        if decision != "allow":
            lines.extend(reasons)
        return "\n".join(lines)

    # -- crew_task ------------------------------------------------------------------------

    def task(
        self,
        caller: CallerInfo,
        *,
        action: str,
        task: str | None,
        title: str | None,
        status: str | None,
        zones: Sequence[str] | None,
        acceptance: Sequence[Mapping[str, Any]] | None,
        phase: str | None,
        note: str | None,
    ) -> str:
        seat, _ = self.seat(caller)
        http = CrewHttp(caller.client)
        self._touch_quietly(caller, seat)
        if action == "list":
            seat.forget("tasks")
            tasks = [t for t in self._tasks(http, seat) if t.get("status") in _OPEN_TASK_STATUSES]
            if not tasks:
                return 'NO OPEN TASKS. Create one with crew_task(action="create", title=..., zones=[...]).'
            lines = ["OPEN TASKS:", *(f"- {self._task_line(http, seat, t)}" for t in tasks[:30])]
            return join_text(lines, [f"{task_ref(t)} title: {agent_text(t.get('title'))}" for t in tasks[:30]])
        if action == "create":
            if not title:
                raise CrewUsageError('crew_task(action="create") needs a title.')
            body: dict[str, Any] = {
                "title": title[:200],
                "zone_ids": [self._zone(http, seat, z)["id"] for z in zones or []],
                "acceptance": [dict(c) for c in acceptance or []],
                "depends_on": [],
            }
            if phase:
                body["phase"] = phase[:64]
            if note:
                body["body"] = note[:8000]
            _, res, _ = http.call("POST", f"/crews/{seat.crew_id}/tasks", token=seat.token, json=body)
            seat.forget("tasks")
            t = (res or {}).get("task") or {}
            ref = task_ref(t)
            return f'CREATED {ref} ({t.get("id")}, {t.get("status")}). Start it with crew_task(action="start", task="{ref}").'
        row = self._task(http, seat, task)
        ref = task_ref(row) or str(row.get("id"))
        if action == "start":
            _, res, _ = http.call("POST", f"/tasks/{row['id']}/start", token=seat.token, json={})
            seat.forget("tasks")
            seat.current_task_id = str(row["id"])
            got = [self._claim_label(http, seat, c) for c in (res or {}).get("claims") or []]
            status_now = ((res or {}).get("task") or {}).get("status")
            return f"STARTED {ref} ({status_now})" + (": claimed " + ", ".join(got) if got else "") + "."
        if action == "block":
            if not note:
                raise CrewUsageError('crew_task(action="block") needs a note saying what blocks it.')
            http.call("POST", f"/tasks/{row['id']}/block", token=seat.token, json={"reason": note[:280]})
            seat.forget("tasks")
            return f'BLOCKED {ref}. The crew inbox shows it; unblock with crew_task(action="update", status="in_progress").'
        if action == "release":
            _, res, _ = http.call("POST", f"/tasks/{row['id']}/release", token=seat.token, json={"baton": True})
            seat.forget("tasks")
            seat.current_task_id = None
            t = (res or {}).get("task") or {}
            return f"RELEASED {ref}: now {t.get('status')}."
        if action == "update":
            return self._update_task(
                http, seat, row, status=status, title=title, zones=zones, acceptance=acceptance, phase=phase, note=note
            )
        raise CrewUsageError(f"Unknown action {action!r}.")

    def _update_task(
        self,
        http: CrewHttp,
        seat: Seat,
        row: Mapping[str, Any],
        *,
        status: str | None,
        title: str | None,
        zones: Sequence[str] | None,
        acceptance: Sequence[Mapping[str, Any]] | None,
        phase: str | None,
        note: str | None,
    ) -> str:
        ref = task_ref(row) or str(row.get("id"))
        done: list[str] = []
        if status and status != row.get("status"):
            if status == "done":
                return f'NOT UPDATED {ref}: a task is finished by a report. Call crew_report(task="{ref}", sections=...).'
            action = {"blocked": "block", "claimed": "claim", "cancelled": None}.get(status, "")
            if status == "in_progress":
                action = "unblock" if row.get("status") == "blocked" else "start"
            if action == "":
                return f"NOT UPDATED {ref}: status {status} is set by the server, not by an update."
            if action is None:
                http.call("PATCH", f"/tasks/{row['id']}", token=seat.token, json={"status": "cancelled"}, if_match=_version(row))
            elif action == "block":
                if not note:
                    raise CrewUsageError("Blocking a task needs a note saying what blocks it.")
                http.call("POST", f"/tasks/{row['id']}/block", token=seat.token, json={"reason": note[:280]})
                note = None
            else:
                http.call("POST", f"/tasks/{row['id']}/{action}", token=seat.token, json={})
            done.append(f"status → {status}")
            seat.forget("tasks")
            row = self._task(http, seat, str(row["id"]), fresh=True)
        patch: dict[str, Any] = {}
        if title:
            patch["title"] = title[:200]
        if phase:
            patch["phase"] = phase[:64]
        if note:
            patch["body"] = note[:8000]
        if zones is not None:
            patch["zone_ids"] = [self._zone(http, seat, z)["id"] for z in zones]
        if acceptance is not None:
            patch["acceptance"] = [dict(c) for c in acceptance]
        if patch:
            http.call("PATCH", f"/tasks/{row['id']}", token=seat.token, json=patch, if_match=_version(row))
            seat.forget("tasks")
            done.append("changed " + ", ".join(sorted(patch)))
        if not done:
            return f"NOTHING TO UPDATE on {ref}."
        return f"UPDATED {ref}: " + "; ".join(done) + "."

    # -- crew_say -------------------------------------------------------------------------

    def say(self, caller: CallerInfo, *, body: str, kind: str, to: str, thread: str | None, wait_s: int) -> str:
        seat, _ = self.seat(caller)
        http = CrewHttp(caller.client)
        self._touch_quietly(caller, seat)
        text = body
        target = (to or "crew").strip()
        if target and target.lower() != "crew":
            if not _TARGET_RE.fullmatch(target):
                raise CrewUsageError(
                    'to must be "crew", "@crew", a callsign like "@codex-1", an agent like "@codex", or "@mani".'
                )
            mention = "@" + target.lstrip("@")
            if mention.lower() not in text.lower():
                text = f"{mention} {text}"
        payload: dict[str, Any] = {"kind": kind, "body": text, "client_msg_id": f"mcp-{uuid.uuid4().hex[:24]}"}
        if thread:
            payload["thread_root_id"] = thread.strip()
        if wait_s > 0:
            payload["wait_s"] = wait_s
        _, res, _ = http.call(
            "POST",
            f"/crews/{seat.crew_id}/messages",
            token=seat.token,
            json=payload,
            timeout=(wait_s + HTTP_GRACE_S) if wait_s > 0 else None,
        )
        res = res or {}
        msg = res.get("message") or {}
        lines = [
            f"SENT {msg.get('id')} (seq {res.get('seq')}, {kind})" + (f" to {mention_list(msg)}" if msg.get("mentions") else "")
        ]
        decision = res.get("decision")
        if isinstance(decision, dict):
            lines.append(
                f"Decision D-{decision.get('number')} is PROPOSED: it is not in force until a human confirms it on the dashboard."
            )
        if wait_s > 0:
            reply_text = res.get("reply_text")
            if res.get("reply") and isinstance(reply_text, str):
                lines.append(reply_text)
            else:
                lines.append(f"no reply yet (waited {int(round(float(res.get('waited_s') or wait_s)))}s)")
        return "\n".join(lines)

    # -- crew_checkpoint ------------------------------------------------------------------

    def checkpoint(
        self,
        caller: CallerInfo,
        *,
        files_changed: Sequence[str],
        summary: str | None,
        commits: Sequence[str] | None,
        tests: Sequence[Mapping[str, Any]] | None,
        next_step: str | None,
        task: str | None,
    ) -> str:
        seat, _ = self.seat(caller)
        http = CrewHttp(caller.client)
        self._touch_quietly(caller, seat)
        task_row = self._task(http, seat, task) if task else None
        facts: dict[str, Any] = {"files_changed": [str(p) for p in files_changed][:500]}
        if commits:
            facts["commits"] = [{"sha": str(c)} for c in commits][:200]
        if tests:
            facts["tests"] = [
                {"command": str(t.get("command") or ""), "passed": int(t.get("passed") or 0), "failed": int(t.get("failed") or 0)}
                for t in tests
            ][:50]
        if summary:
            facts["notes"] = summary[:2000]
        if next_step:
            facts["next_step"] = next_step[:500]
        trigger = "commit" if commits else ("test" if tests else "turn")
        body: dict[str, Any] = {"session_id": seat.session_id, "trigger": trigger, "facts": facts}
        if task_row is not None:
            body["task_id"] = task_row["id"]
        _, res, _ = http.call("POST", f"/crews/{seat.crew_id}/checkpoints", token=seat.token, json=body)
        res = res or {}
        ckp = res.get("checkpoint") or {}
        created = res.get("created", True)
        lines = [
            f"CHECKPOINT {ckp.get('id')} "
            + ("recorded" if created else "unchanged (same facts as your last checkpoint)")
            + (f" (seq {res.get('seq')})" if res.get("seq") else "")
        ]
        data: list[str] = []
        lines.extend(self._collision_lines(http, seat, data))
        notice = self.notice(CallerInfo(caller.client, caller.fingerprint, caller.client_session_id, caller.agent_id), force=True)
        if notice:
            lines.append(notice)
        return join_text(lines, data)

    def _collision_lines(self, http: CrewHttp, seat: Seat, data: list[str]) -> list[str]:
        try:
            _, body, _ = http.call("GET", f"/crews/{seat.crew_id}/collisions", token=seat.token, params={"state": "open"})
        except CrewApiError as e:
            return [f"COLLISION CHECK unavailable ({e.status})."]
        mine = [
            c
            for c in (body or {}).get("collisions") or []
            if isinstance(c, dict) and seat.session_id in (c.get("session_a"), c.get("session_b"))
        ]
        if not mine:
            return ["COLLISION CHECK: no overlap with other sessions."]
        lines = [
            f"COLLISIONS ({len(mine)}):",
            'Inspect with crew_collision(action="list"); resolve only after checking that the overlap is reconciled.',
        ]
        for c in mine[:6]:
            other_id = c.get("session_b") if c.get("session_a") == seat.session_id else c.get("session_a")
            other = self._callsign(http, seat, other_id) or ("a human" if not other_id else "another session")
            zone = self._zone_label(http, seat, c.get("zone_id"))
            subject = _safe(c.get("subject"))
            if subject is None:
                data.append(f"collision {c.get('id')} path: {agent_text(c.get('subject'))}")
                subject = f"a file (see {c.get('id')} in the data below)"
            where = f" (zone {zone})" if zone else ""
            line = f"- {c.get('kind')} [{c.get('severity')}] on {subject}{where} with {other} ({c.get('id')})."
            if c.get("session_a") == seat.session_id and c.get("attribution") != "probable":
                line += " " + CORRECTIVE.format(who=other if _safe(other) and " " not in other else "mani")
            lines.append(line)
        return lines

    # -- crew_collision -------------------------------------------------------------------

    def collision(self, caller: CallerInfo, *, action: str, collision: str | None, resolution: str | None) -> str:
        """Use the current MCP seat's token; never accept a caller-supplied identity."""
        if action not in {"list", "ack", "resolve"}:
            raise CrewUsageError("Collision action must be list, ack or resolve.")
        if action == "list" and (collision is not None or resolution is not None):
            raise CrewUsageError("list takes no collision id or resolution.")
        if action != "list" and (not collision or not re.fullmatch(r"col_[A-Za-z0-9_-]+", collision)):
            raise CrewUsageError("Give a collision id (col_...).")
        if action == "resolve" and (not resolution or not resolution.strip() or len(resolution) > 64):
            raise CrewUsageError("Resolve requires an explanation of 1-64 characters.")
        if action == "ack" and resolution is not None:
            raise CrewUsageError("ack takes no resolution; acknowledgement does not resolve the overlap.")
        seat, _ = self.seat(caller)
        http = CrewHttp(caller.client)
        self._touch_quietly(caller, seat)
        _, body, _ = http.call("GET", f"/crews/{seat.crew_id}/collisions", token=seat.token)
        mine = [
            row
            for row in (body or {}).get("collisions") or []
            if isinstance(row, dict) and seat.session_id in (row.get("session_a"), row.get("session_b"))
        ]
        if action == "list":
            unresolved = [row for row in mine if row.get("state") in {"open", "acknowledged"}]
            lines = [f"YOUR UNRESOLVED COLLISIONS ({len(unresolved)}):"]
            data = []
            for row in unresolved[:50]:
                lines.append(f"- {row['id']} {row.get('kind')} [{row.get('severity')}] {row.get('state')}")
                data.append(f"{row['id']} subject: {agent_text(row.get('subject'))}")
            if len(unresolved) > 50:
                lines.append(f"{len(unresolved) - 50} additional notices omitted; view the crew collision list.")
            return join_text(lines, data)
        target = next((row for row in mine if row.get("id") == collision), None)
        if target is None:
            raise CrewUsageError("Collision is not in your current crew or you are not a party to it.")
        # The REST endpoint rechecks party membership and state under the writer
        # transaction; this inventory check is not an authorization substitute.
        payload = {"resolution": resolution} if action == "resolve" else None
        _, fresh, _ = http.call("POST", f"/collisions/{collision}/{action}", token=seat.token, json=payload)
        state = (fresh or {}).get("state")
        lines = [f"COLLISION {collision}: {state}"]
        if action == "ack":
            lines.append("Acknowledged only; the overlap remains unresolved.")
        return join_text(lines, [f"resolution: {agent_text(resolution)}"] if resolution else [])

    # -- crew_report ----------------------------------------------------------------------

    def report(
        self,
        caller: CallerInfo,
        *,
        task: str,
        sections: Mapping[str, Any],
        criteria_evidence: Sequence[Mapping[str, Any]] | None,
        commits: Sequence[str] | None,
        tests: Sequence[Mapping[str, Any]] | None,
        summary: str | None,
        release: bool,
    ) -> str:
        seat, _ = self.seat(caller)
        http = CrewHttp(caller.client)
        self._touch_quietly(caller, seat)
        row = self._task(http, seat, task)
        ref = task_ref(row) or str(row.get("id"))
        clean_sections: dict[str, list[str]] = {}
        for key in S.MCP_REPORT_SECTIONS:
            value = sections.get(key)
            if value is None:
                continue
            items = [value] if isinstance(value, str) else [str(v) for v in value] if isinstance(value, list) else [str(value)]
            clean_sections[key] = [i[:500] for i in items if i.strip()][:50]
        body: dict[str, Any] = {
            "session_id": seat.session_id,
            "sections": clean_sections,
            "criteria_evidence": [dict(c) for c in criteria_evidence or []][:20],
            "commits": [str(c) for c in commits or []][:200],
            "tests": [
                {"command": str(t.get("command") or ""), "passed": int(t.get("passed") or 0), "failed": int(t.get("failed") or 0)}
                for t in tests or []
            ][:50],
            "release": bool(release),
        }
        if summary:
            body["summary"] = summary[:2000]
        try:
            _, res, _ = http.call("POST", f"/tasks/{row['id']}/reports", token=seat.token, json=body)
        except CrewApiError as e:
            if e.status in (403, 409):
                return f"REPORT NOT ACCEPTED for {ref}: {e.message}"
            raise
        seat.forget("tasks")
        res = res or {}
        report = res.get("report") or {}
        outcome = res.get("outcome")
        t = res.get("task") or {}
        if t.get("status") in ("done", "review", "stalled", "cancelled"):
            seat.current_task_id = None
        lines = [
            f"REPORT {report.get('id')} for {ref}: verdict {report.get('verdict')} · "
            + ("ACCEPTED, task done" if outcome == "accepted" else f"sent to REVIEW (task {t.get('status')})")
        ]
        unmet = [c for c in report.get("criteria") or [] if isinstance(c, dict) and c.get("status") not in ("met", "waived")]
        if unmet:
            lines.append("Unmet criteria: " + ", ".join(f"{c.get('id')} ({c.get('status')})" for c in unmet[:20]))
        if outcome != "accepted":
            lines.append("Evidence given through MCP is agent-declared (self-reported); under strict reports a human reviews it.")
        seal = res.get("seal")
        if isinstance(seal, str) and seal:
            lines.append(f"Receipt: {S.clip_item(seal, 200)}")
        return "\n".join(lines)


def _version(row: Mapping[str, Any]) -> str:
    """PATCH needs If-Match with the task version (§4.3 optimistic concurrency)."""
    return str(row.get("version") or 1)


def mention_list(message: Mapping[str, Any]) -> str:
    return ", ".join("@" + m for m in (message.get("mentions") or []) if _safe(m))


def render_notice(callsign: str, items: Sequence[Mapping[str, Any]]) -> str:
    """One ``crew_notice`` line (≤300 chars): server-set item titles only, most urgent first."""
    head = f"CREW ({callsign}): "
    tail = " · crew_status for details"
    parts: list[str] = []
    for item in items:
        title = S.clip_item(str(item.get("title") or item.get("kind") or ""), 140)
        candidate = head + " · ".join([*parts, title]) + tail
        if len(candidate) > NOTICE_CAP:
            break
        parts.append(title)
    if not parts:
        text = head + f"{len(items)} new item(s) for you" + tail
    else:
        more = len(items) - len(parts)
        text = head + " · ".join(parts) + (f" (+{more} more)" if more > 0 else "") + tail
        if len(text) > NOTICE_CAP:
            text = head + " · ".join(parts) + tail
    if S.check_agent_text(text, "piggyback"):
        text = head + f"{len(items)} new item(s) for you" + tail
    return text[:NOTICE_CAP]
