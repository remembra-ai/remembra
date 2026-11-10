"""Crew mode over MCP (spec §7, WP-11): the seven crew tools, implicit join and the piggyback.

The MCP server is a separate process that talks to the Remembra REST API with the caller's
own API key, so every crew tool is a thin wrapper over the §6 routes. This module holds the
parts that are not MCP plumbing:

* **Seats.** The MCP session (``Mcp-Session-Id`` on streamable HTTP, one id per stdio
  process) becomes a crew session on first use: ``POST /crews/join`` with
  ``adapter='mcp'`` and ``client_kind='mcp'`` (the server makes it ``advisory``). The
  session token is held in memory, keyed by the API key's fingerprint, the MCP
  session id and the project, so one tenant's token is never used for another. A stdio
  server also keeps it in a private file (:mod:`remembra.mcp.seats`), so the process that
  replaces it re-joins as the same crew session instead of a new callsign.
* **Liveness.** MCP sessions have no crewd heartbeat. Any Remembra MCP call re-joins
  with the current token at most once per :data:`TOUCH_INTERVAL_S`; the server treats a
  re-join as activity and renews the session's leases (§10.1 "MCP-only: last call").
* **Long-poll.** ``crew_claim(wait_s=…)`` and ``crew_say(wait_s=…)`` pass ``wait_s`` to the
  server, which holds the request; the HTTP timeout is raised to match. One call waits at
  most :data:`MAX_WAIT_S`: MCP clients drop a request after about 60 s and may then replace
  the server process, so a longer wait is cut and the answer says so.
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

import functools
import math
import re
import shlex
import threading
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Final, TypeVar, cast

import httpx

from remembra.client.memory import Memory, _header_agent_id
from remembra.crew import gatecore as G
from remembra.crew import schemas as S
from remembra.mcp.seats import SeatRecord, SeatStore, pool_key
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
# The longest one crew call waits on the server (wait_s). An MCP client gives a tool call about 60 s; a call
# that runs longer is dropped and the client may replace the stdio server, which costs the session its seat.
MAX_WAIT_S: Final = 40
# Join answers that mean a stored seat can never be used again: the server refuses its token (409
# session_exists) or the session has ended (409 session_ended, 410). Anything else is about the request,
# the API key or the server (401, 403, 404, 422, 5xx) and says nothing about the seat, which is kept.
_SEAT_GONE_STATUSES: Final = frozenset({409, 410})
# Joins are serialised per (MCP session, project) over this many locks (see CrewBridge.seat).
_JOIN_STRIPES: Final = 64
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
# A repo path or path glob: one token without whitespace, with the characters framework routes are named with.
_SAFE_PATH_RE: Final = re.compile(r"[A-Za-z0-9._@+$(\[][A-Za-z0-9._/@+:=$()\[\]*-]{0,199}")
_TASK_NUMBER_RE: Final = re.compile(r"(?:t-?|#)?([1-9][0-9]{0,6})", re.IGNORECASE)
_TARGET_RE: Final = re.compile(r"@?[A-Za-z0-9][A-Za-z0-9._:-]{0,79}")
_CLIENT_ID_RE: Final = re.compile(r"[^A-Za-z0-9._:-]+")
_RETRY_TAIL_RE: Final = re.compile(r"\s*Retry in [0-9]+(?:\.[0-9]+)?s\.?\s*$")
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


@dataclass
class LocalCrew:
    """The crew session the agent's crew hooks joined, when this MCP server runs inside that agent's process tree.

    MCP crew tools otherwise hold a separate ``mcp`` seat: its claims are not the ones crewd saves as a baton
    when the agent dies, and its project is the configured default rather than this repository's crew. Claim,
    release and adopt go through crewd as the hook session instead (crewd checks the caller's process ancestry),
    and adopt restores the saved work into the checkout, which a sandboxed agent's own shell cannot do.
    """

    layout: Any
    who: dict[str, Any]

    def http(self, client: Memory) -> LocalCrewHttp:
        return LocalCrewHttp(self.layout, str(self.who["key"]), client)


def local_crew(timeout: float = 1.0) -> LocalCrew | None:
    """The agent's crew-hook session via crewd's ``whoami``, or None (no crewd, no hook session, not local)."""
    try:
        from pathlib import Path

        from remembra.relay.crew.gate import Layout, rpc

        layout = Layout(Path.home())
        if not layout.socket.exists():
            return None
        who = rpc(layout, "whoami", {}, timeout=timeout)
    except Exception:
        return None
    if not who or not who.get("ok") or not who.get("key") or not who.get("crew_id") or not who.get("session_id"):
        return None
    return LocalCrew(layout, dict(who))


# The crew-hook adapter that is the same program as an MCP agent id. Claude Code's hooks join as ``claude-code``
# while the Claude desktop app's MCP server, which also serves the Claude Code sessions that app starts, signs
# as ``claude-desktop``. Any other agent id is its own adapter (codex, cursor, …).
_HOOK_ADAPTER_OF: Final = {"claude": "claude-code", "claude-desktop": "claude-code"}


def hook_adapter_of(agent_id: str | None) -> str:
    """The crew-hook adapter that is the same program as ``agent_id`` (the id itself unless listed above)."""
    agent = str(agent_id or "").strip().lower()
    return _HOOK_ADAPTER_OF.get(agent, agent)


def local_hook_sessions(timeout: float = 1.0) -> list[dict[str, Any]]:
    """The live crew-hook sessions on this machine (crewd's ``status``, open to this user's processes), or []."""
    try:
        from pathlib import Path

        from remembra.relay.crew.gate import Layout, rpc

        layout = Layout(Path.home())
        if not layout.socket.exists():
            return []
        res = rpc(layout, "status", {}, timeout=timeout)
    except Exception:
        return []
    if not res or not res.get("ok"):
        return []
    rows = res.get("sessions")
    return [dict(r) for r in rows if isinstance(r, dict) and r.get("session_id")] if isinstance(rows, list) else []


@dataclass(frozen=True)
class HookShadow:
    """Crew-hook sessions of the caller's own program that this MCP server cannot act as.

    A stdio MCP server started by the agent is a child of it and acts as its hook session (:class:`LocalCrew`).
    One started by something else (the Claude desktop app for the Claude Code sessions it launches) is not, so it
    holds a separate ``mcp`` seat. A claim taken on that seat belongs to a second identity: the edit gate, which
    runs as the hook session, then denies the agent's own edits, and both identities reporting the same files
    open collisions between them. ``here`` are such hook sessions in the seat's crew (claims are not taken while
    there is one); ``elsewhere`` are the projects of the others (a claim here does not cover edits there).
    """

    agent_id: str
    callsign: str
    here: tuple[str, ...]
    elsewhere: tuple[str, ...] = ()

    @property
    def who(self) -> str:
        return ", ".join(self.here)

    def refusal(self, verb: str, cli: str | None) -> str:
        """The answer to a call that would take a claim; ``cli`` is the shell command that does it as the hook session."""
        many = len(self.here) > 1
        one = "one of them" if many else self.who
        own = (
            f"the gate claims an area for it on your first edit, and `{cli}` in your shell acts as it"
            if cli
            else f"ask the sender to hand it over to {'that session' if many else '@' + self.who} instead"
        )
        return (
            f"NOT {verb}: {'crew-hook sessions are' if many else 'a crew-hook session is'} live in this project on this "
            f"machine ({self.who}) and this MCP server runs outside {'their' if many else 'its'} process tree as "
            f"'{self.agent_id}', so it cannot act as {'one of them' if many else 'that session'}. Anything taken here "
            f"would be held by a second identity ({self.callsign}) and the edit gate would block "
            f"{'their' if many else 'its'} edits. If {one} is your own session (`remembra-crew whereami` in your shell "
            f"says), take nothing here: {own}. If you are a different agent, ask "
            f"{'them' if many else '@' + self.who} with crew_say."
        )

    def status_lines(self, held: int) -> list[str]:
        many = len(self.here) > 1
        lines = [
            f"CREW-HOOK SESSION{'S' if many else ''} ON THIS MACHINE: {self.who} (enforced by the edit gate). This MCP "
            f"server runs outside {'their' if many else 'its'} process tree as '{self.agent_id}', so {self.callsign} "
            f"is a separate identity. If {'one of them' if many else self.who} is your own session (`remembra-crew "
            f"whereami` in your shell says), what it holds is yours, also where it is listed under DO NOT TOUCH."
        ]
        if held:
            lines.append(
                f"THIS MCP SEAT HOLDS {held} claim(s) that block {self.who}: release them with "
                'crew_claim(action="release", zone=... or paths=[...]).'
            )
        return lines

    @property
    def footer(self) -> str:
        return (
            "Do not claim through this MCP seat: the edit gate claims for the hook session on its first edit, and "
            "`remembra-crew claim|release|adopt|task|checkpoint` in your shell act as it. crew_say, crew_report "
            "and release still work here."
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


class LocalCrewHttp(CrewHttp):
    """Crew REST calls as the agent's crew-hook session, through crewd's ``api`` op (same shape as :class:`CrewHttp`)."""

    # crewd's ``api`` op serves the crew's own routes; anything else (the session brief) uses the agent's key
    CREWD_PREFIXES = ("/crews/", "/claims/", "/tasks/", "/messages/", "/inbox/items/", "/collisions/")

    def __init__(self, layout: Any, key: str, client: Memory) -> None:
        super().__init__(client)
        self.layout = layout
        self.key = key

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
        from remembra.relay.crew.gate import rpc

        if not path.startswith(self.CREWD_PREFIXES) or path == "/crews/join":
            return super().call(method, path, json=json, params=params, timeout=timeout, etag=etag)
        wait = float(timeout or 15.0)
        res = rpc(
            self.layout,
            "api",
            {
                "key": self.key,
                "method": method,
                "path": path,
                "json": dict(json) if json is not None else None,
                "params": dict(params or {}),
                "if_match": if_match,
                "timeout": wait,
            },
            timeout=wait + 2.0,
        )
        if res is None:
            raise CrewApiError(0, "unreachable", "The local crew daemon did not answer.")
        status = int(res.get("status") or 0)
        body = res.get("body")
        if res.get("ok") and status < 400:
            return status, body, None
        detail = body.get("detail", body) if isinstance(body, dict) else body
        if isinstance(detail, dict):
            return_error = CrewApiError(
                status,
                str(detail.get("error") or res.get("error") or f"http_{status}"),
                _clean_message(detail.get("message") or detail.get("error") or ""),
                detail,
            )
        else:
            return_error = CrewApiError(status, str(res.get("error") or f"http_{status}"), _clean_message(detail or ""))
        raise return_error


@dataclass(frozen=True)
class CallerInfo:
    """Who calls: the per-caller SDK client, a fingerprint of its key, the MCP session and agent."""

    client: Memory
    fingerprint: str
    client_session_id: str
    agent_id: str | None
    default_project: str | None = None
    # the project the agent named itself (crew_status / session_brief), not the configured default
    chosen_project: str | None = None
    # stdio MCP server on the agent's machine: its crew hooks' session may act for it (see LocalCrew)
    local: bool = False
    # the project this one call named (project_id / root_path on the tool): it wins over everything remembered
    call_project: str | None = None
    # REMEMBRA_PROJECT when it is not a default for this caller (a stdio server): named in the "no project" answer
    configured_project: str | None = None


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
    # How this seat was taken over from an earlier server process ("conversation" or "agent"), until the
    # agent has been told: once on the first crew write's answer and once on the next crew_status.
    resumed_write: str | None = None
    resumed_status: str | None = None
    # set when this seat IS the agent's crew-hook session (see LocalCrew): calls go through crewd
    local: LocalCrew | None = None
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


def _safe_path(value: Any) -> str | None:
    """``value`` when it is a repo path (or path glob) that may appear outside the data block.

    Everything :func:`_safe` allows, plus the characters routes and globs are written with: ``[id]``,
    ``[...slug]``, ``(group)``, ``$id`` and ``*``. Still one token with no whitespace, quote or shell
    separator, never a ``..`` segment or a ``$(``; anything else is named only inside the data block.
    """
    plain = _safe(value)
    if plain is not None:
        return plain
    text = str(value) if value is not None else ""
    if not _SAFE_PATH_RE.fullmatch(text) or ".." in text.split("/") or "$(" in text:
        return None
    return text


def _path_claim_covered(
    claim: Mapping[str, Any],
    paths: Sequence[str] | None,
    zone_globs: Sequence[str],
    zone_excludes: Sequence[str] = (),
    *,
    wide: bool = False,
) -> bool:
    """True when ``claim`` is a path claim (no zone) that one of ``paths`` names or the named zone contains.

    A path claim is stored with no zone id, and it stays that way when a zone arrives over its folder
    later (a derived folder zone, a zones.yml upload). Naming the path, a folder above it or that zone
    must still find it, although ``/match`` now files the path under the zone.

    A path names the claim spelled the same and every claim under it as a folder. With ``wide`` it also
    names a wider claim whose glob covers it (to say who holds a file, never to release: one file does
    not end a claim on its whole folder). A zone contains a claim its ``include_globs`` overlap, unless
    its ``exclude_globs`` cover the claim: an excluded part of the zone is not in the zone.
    """
    glob = str(claim.get("path_glob") or "").strip().strip("/")
    if not glob or claim.get("zone_id"):
        return False
    for p in paths or ():
        path = str(p).strip().strip("/")
        if path and (path == glob or glob.startswith(path + "/") or (wide and G.claim_covers_path(path, glob))):
            return True
    if not zone_globs:
        return False
    from remembra.crew import policy as P  # the overlap test the server uses for a path claim under a zone

    try:
        if not P.any_overlap([str(g) for g in zone_globs], [glob]):
            return False
        # the claim's own text is matched as a path: an exclude covers it when it matches every spelling of it
        return not any(str(x) == glob or G.glob_match(str(x), glob, case_insensitive=True) for x in zone_excludes)
    except (ValueError, re.error):
        return False


def _count(value: Any) -> int | None:
    """A small non-negative count from a server body (None when it is not one)."""
    return value if isinstance(value, int) and not isinstance(value, bool) and 0 <= value < 100_000 else None


def _dicts(value: Any) -> list[dict[str, Any]]:
    return [x for x in value if isinstance(x, dict)] if isinstance(value, list) else []


def _safe_target(value: Any) -> str | None:
    """``zone pos`` / ``path src/a.ts`` / a resource from a server body, when it is plain tokens."""
    kind, _, rest = str(value or "").partition(" ")
    if kind in ("zone", "path") and _safe(rest):
        return f"{kind} {rest}"
    return _safe(value)


def _temporary_file_zone(zone: Mapping[str, Any]) -> bool:
    """A TEMPORARY zone (auto-derived from a folder, no zones.yml) where single files are claimed: the
    server's ``claims.temporary_file_zone``. A zone a human declared, protected, froze or reserved is not."""
    return (
        zone.get("source") == "suggested"
        and bool(zone.get("is_leaf", True))
        and not zone.get("builtin")
        and not zone.get("protected")
        and not zone.get("reserve_for")
        and not zone.get("frozen_by")
        and str(zone.get("mode") or "exclusive") == "exclusive"
    )


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


def rate_limit_text(e: CrewApiError) -> str:
    """What a 429 said, with a long wait also in minutes; empty when it carried neither a sentence nor a wait.

    The crew limiter's sentence names the kind of request, whose budget it is and the limit
    (``Too many adopt requests for this session (6/hour). Retry in 3569s.``); it is printed as sent.
    Only a JSON answer's own ``message`` counts as a sentence: the page of a proxy in front of the
    server, or any other body, is not printed. The wait is ``retry_after_s`` (any number, rounded up),
    and it is given once: it replaces a wait the sentence ends with.
    """
    raw = e.body.get("retry_after_s")
    retry: int | None = None
    if isinstance(raw, (int, float)) and not isinstance(raw, bool) and 0 <= raw < 10**9:  # also not NaN or inf
        retry = math.ceil(raw)
    wait = "" if retry is None else f"{retry}s" + (f" (about {round(retry / 60)} min)" if retry >= 120 else "")
    said = (e.message or "").strip() if isinstance(e.body.get("message"), str) else ""
    if said in ("rate_limited", e.error) or "<" in said or ">" in said:  # only the error code came back, or markup
        said = ""
    if not said:
        return f"retry in {wait}." if wait else ""
    if wait:
        said = f"{_RETRY_TAIL_RE.sub('', said)} Retry in {wait}.".lstrip()
    if e.error == "rate_limited":  # the crew limiter: one budget per kind of request
        said += " Other kinds of crew call are not limited by this."
    return said


def error_text(e: CrewApiError, what: str) -> str:
    if e.status == 0:
        return f"CREW UNAVAILABLE: {e.message}"
    if e.status == 503 or e.error in ("route_missing", "crew_unavailable"):
        return "CREW UNAVAILABLE: Crew mode is not enabled on this Remembra server."
    if e.status == 429:
        said = rate_limit_text(e)
        return f"RATE LIMITED ({what}): {said}" if said else f"RATE LIMITED ({what})."
    return f"CREW ERROR ({what}: {e.status} {S.clip_item(e.error, 64)}): {e.message}"


def resumed_text(kind: str, *, on_write: bool = False) -> str:
    """The RESUMED note for a seat taken over from an earlier server process (``kind``: whose seat it was)."""
    if kind == "conversation":
        text = "RESUMED after an MCP server restart (this conversation's seat: it keeps what it held"
        return text + ("; crew_status shows it)" if on_write else ")")
    text = "RESUMED the seat this agent's previous MCP server process held on this machine (it keeps what that seat held: "
    return text + ("call crew_status to see it, and " if on_write else "") + "release what is not yours to continue)"


def crew_tag(seat: Seat) -> str:
    """Which crew a write went to and as whom: the last line of every crew write's answer."""
    return f"(in CREW {_safe(seat.project_id) or 'this project'} as {_safe(seat.callsign) or 'this session'})"


_F = TypeVar("_F", bound=Callable[..., str])


def _names_crew(fn: _F) -> _F:
    """Decorate a crew write: its answer (or usage error) ends with the crew the write used (:func:`crew_tag`).

    One MCP server process can serve several conversations in different projects, so an answer such as
    ``SENT msg_…`` or ``Unknown zone 'api'`` has to say which crew it is about.
    """

    @functools.wraps(fn)
    def wrapper(self: CrewBridge, caller: CallerInfo, **kwargs: Any) -> str:
        self._written.seat = None
        try:
            text = fn(self, caller, **kwargs)
        except CrewUsageError as e:
            seat = getattr(self._written, "seat", None)
            if seat is None:
                raise
            raise CrewUsageError(f"{e} {crew_tag(seat)}") from None
        seat = getattr(self._written, "seat", None)
        if seat is None:
            return text
        with seat.lock:  # a write (not crew_status) took this seat over from an earlier process: say so, once
            resumed, seat.resumed_write = seat.resumed_write, None
        if resumed:
            text = f"{text}\n{resumed_text(resumed, on_write=True)}"
        return f"{text}\n{crew_tag(seat)}"

    return cast(_F, wrapper)


# ---------------------------------------------------------------------------
# The bridge
# ---------------------------------------------------------------------------


class CrewBridge:
    """Seats per MCP session plus the seven crew tools, implemented over the REST API."""

    def __init__(self, *, clock: Callable[[], float] = time.monotonic, max_wait_s: int = MAX_WAIT_S) -> None:
        self._clock = clock
        self.max_wait_s = max(1, int(max_wait_s))
        # set for a stdio server only (remembra.mcp.server.main): seats then survive a process restart
        self.seat_store: SeatStore | None = None
        self._lock = threading.RLock()
        self._join_locks = tuple(threading.Lock() for _ in range(_JOIN_STRIPES))
        self._seats: dict[tuple[str, str], dict[str, Seat]] = {}
        self._current: dict[tuple[str, str], str] = {}
        self._used = threading.local()
        # every project this MCP session chose (session_brief, crew_status, a write that named none), in
        # order; a write that names its project is for that call only and is not counted. With more than one,
        # a crew write that names no project is refused instead of following the latest choice (write_seat)
        self._pointed: dict[tuple[str, str], dict[str, None]] = {}
        self._written = threading.local()  # the seat the running crew write uses (see _names_crew)

    # -- seat registry ------------------------------------------------------------

    def seat_for_scope(self, caller: CallerInfo) -> Seat | None:
        """The crew session this MCP session uses now (None before its first crew call)."""
        scope = (caller.fingerprint, caller.client_session_id)
        with self._lock:
            project = self._current.get(scope)
            return self._seats.get(scope, {}).get(project) if project else None

    def seat_in(self, caller: CallerInfo, project_id: str | None) -> Seat | None:
        """This MCP session's seat in exactly ``project_id`` (None when it holds none there). Never joins."""
        if not project_id:
            return None
        with self._lock:
            return self._seats.get((caller.fingerprint, caller.client_session_id), {}).get(project_id)

    def pointed_projects(self, caller: CallerInfo) -> list[str]:
        """Every project this MCP session chose (a brief, crew_status, a write that named none), oldest first.

        A write that named its project is for that call only: it moves nothing and is not counted here.
        """
        with self._lock:
            return list(self._pointed.get((caller.fingerprint, caller.client_session_id), ()))

    def seat_projects(self, caller: CallerInfo) -> list[str]:
        """The projects this MCP session holds a crew seat in that has not left, oldest first."""
        with self._lock:
            seats = self._seats.get((caller.fingerprint, caller.client_session_id), {})
            return [project for project, seat in seats.items() if not seat.left]

    def single_conversation(self, caller: CallerInfo) -> bool:
        """Whether this MCP session is known to serve one conversation, so "the latest choice wins" is safe.

        True for a stdio server that crewd recognises as part of one agent's process tree (the agent's crew
        hooks joined a session for it). A stdio server started by a desktop app for all its conversations is
        not in any conversation's tree, and a remote transport's session is not local: both answer False.
        """
        if not caller.local:
            return False
        with self._lock:
            seats = list(self._seats.get((caller.fingerprint, caller.client_session_id), {}).values())
        return any(s.local is not None for s in seats) or local_crew() is not None

    def _point(self, scope: tuple[str, str], project_id: str) -> None:
        with self._lock:
            if scope not in self._pointed and len(self._pointed) >= MAX_SCOPES:
                self._pointed.pop(next(iter(self._pointed)), None)
            self._pointed.setdefault(scope, {})[project_id] = None

    def _remember(self, seat: Seat, *, sticky: bool = True) -> None:
        with self._lock:
            if seat.scope not in self._seats and len(self._seats) >= MAX_SCOPES:
                oldest = next(iter(self._seats))
                self._seats.pop(oldest, None)
                self._current.pop(oldest, None)
                self._pointed.pop(oldest, None)
            self._seats.setdefault(seat.scope, {})[seat.project_id] = seat
            if sticky:
                self._current[seat.scope] = seat.project_id
                self._point(seat.scope, seat.project_id)

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

    def _local_seat(
        self, caller: CallerInfo, scope: tuple[str, str], project_id: str | None, *, sticky: bool = True
    ) -> Seat | None:
        """The agent's crew-hook session as this MCP session's seat (one crew session per agent), or None.

        Used when the MCP server runs inside that agent's process tree (stdio, ``caller.local``) and neither this
        call nor an earlier one in this MCP session chose another project (``crew_status(project_id=…)``).
        """
        if not caller.local:
            return None
        local = local_crew()
        if local is None:
            return None
        project = str(local.who.get("project_id") or "")
        with self._lock:
            current = self._current.get(scope)
        # the latest choice (this call, else crew_status / session_brief, see note_project) decides
        chosen = project_id or current or caller.chosen_project
        if not project or (chosen and chosen != project):
            return None
        with self._lock:
            seat = self._seats.get(scope, {}).get(project)
            if seat is None or seat.local is None or seat.session_id != str(local.who["session_id"]):
                seat = Seat(
                    scope=scope,
                    project_id=project,
                    agent_id=str(caller.agent_id or local.who.get("adapter") or ""),
                    client_session_id=str(local.who.get("client_session_id") or ""),
                    crew_id=str(local.who["crew_id"]),
                    session_id=str(local.who["session_id"]),
                    token="",
                    callsign=str(local.who.get("callsign") or ""),
                    verified=True,
                    observe_only=False,
                    status_cursor=0,
                    notice_cursor=0,
                    last_touch=self._clock(),
                    local=local,
                )
                self._seats.setdefault(scope, {})[project] = seat
            else:
                seat.local = local
            if sticky:
                self._current[scope] = project
                self._point(scope, project)
        return seat

    def note_project(self, caller: CallerInfo, project_id: str) -> None:
        """Record a project the agent chose outside the crew tools (session_brief): the latest choice wins."""
        scope = (caller.fingerprint, caller.client_session_id)
        with self._lock:
            self._current[scope] = project_id
            self._point(scope, project_id)

    def _shadow(self, caller: CallerInfo, seat: Seat) -> HookShadow | None:
        """Hook sessions of the caller's own program beside this MCP seat (see :class:`HookShadow`), or None.

        Only for a stdio server on the agent's machine whose seat is not the hook session itself, and only when
        this process is in no hook session's tree at all (a server inside one that chose another project keeps
        its seat as before). Asked on every call: the server can outlive many agent sessions.
        """
        if not caller.local or seat.local is not None:
            return None
        agent = str(caller.agent_id or seat.agent_id or "").strip().lower()
        adapter = hook_adapter_of(agent)
        rows = [
            r
            for r in local_hook_sessions()
            if str(r.get("adapter") or "").strip().lower() == adapter and r.get("session_id") != seat.session_id
        ]
        if not rows or local_crew() is not None:
            return None
        here = [str(_safe(r.get("callsign")) or "a hook session") for r in rows if r.get("crew_id") == seat.crew_id]
        elsewhere = [str(p) for p in (_safe(r.get("project_id")) for r in rows if r.get("crew_id") != seat.crew_id) if p]
        return HookShadow(
            agent_id=_safe(agent) or "this agent id",
            callsign=_safe(seat.callsign) or "this seat",
            here=tuple(dict.fromkeys(here)),
            elsewhere=tuple(dict.fromkeys(elsewhere)),
        )

    def _elsewhere_note(self, seat: Seat, shadow: HookShadow | None) -> str | None:
        """Said with a grant in a crew the agent's hook session is not in: the gate does not read this crew."""
        if shadow is None or shadow.here or not shadow.elsewhere:
            return None
        return (
            f"NOTE: this is the crew of project {_safe(seat.project_id) or '(this project)'}. The crew-hook sessions on "
            f"this machine are in {', '.join(shadow.elsewhere[:4])}: their edit gate does not read claims made here. "
            "If you work in one of those repositories, call crew_status(root_path=...) for it first."
        )

    def _http(self, caller: CallerInfo, seat: Seat) -> CrewHttp:
        """How this seat reaches the server: crewd as the hook session, or the MCP seat's own token."""
        return seat.local.http(caller.client) if seat.local is not None else CrewHttp(caller.client)

    def begin_call(self) -> None:
        """Forget the project of the previous tool call run on this thread (see :meth:`used_project`)."""
        self._used.project = None

    def used_project(self) -> str | None:
        """The project of the seat the tool call running on this thread used, so its result can say where it wrote."""
        project = getattr(self._used, "project", None)
        return str(project) if project else None

    def seat(
        self,
        caller: CallerInfo,
        project_id: str | None = None,
        checkout: Mapping[str, Any] | None = None,
        *,
        sticky: bool = True,
    ) -> tuple[Seat, dict[str, Any] | None]:
        """:meth:`_seat`, remembering the seat's project for :meth:`used_project`.

        A project named by one call (``caller.call_project``, or ``sticky=False``: a write that named its
        project) is used for that call only: the session's current project, which later calls without a
        project follow, is left as it was.
        """
        call_only = bool(caller.call_project) and not project_id
        scope = (caller.fingerprint, caller.client_session_id)
        with self._lock:
            before = self._current.get(scope)
        seat, joined = self._seat(caller, project_id or caller.call_project, checkout, sticky=sticky and not call_only)
        if call_only:
            with self._lock:
                if before is None:
                    self._current.pop(scope, None)
                else:
                    self._current[scope] = before
        self._used.project = seat.project_id
        return seat, joined

    def _seat(
        self,
        caller: CallerInfo,
        project_id: str | None = None,
        checkout: Mapping[str, Any] | None = None,
        *,
        sticky: bool = True,
    ) -> tuple[Seat, dict[str, Any] | None]:
        """The seat for ``project_id`` (default: the current seat, else the caller's default project).

        Joins on first use (implicit join, §7) and re-joins a seat that left. Returns the seat and
        the join response when one was made now. ``sticky=False`` (a write that named its project) uses
        the seat without making its project the current one for later calls.
        """
        if not caller.agent_id:
            raise CrewUsageError(
                "Crew mode needs this agent's id: set REMEMBRA_AGENT_ID in the MCP server's environment "
                "(or send the X-Remembra-Agent-Id header), e.g. claude-desktop or codex."
            )
        scope = (caller.fingerprint, caller.client_session_id)
        local_seat = self._local_seat(caller, scope, project_id, sticky=sticky)
        if local_seat is not None:
            return local_seat, None
        with self._lock:
            project = project_id or self._current.get(scope) or caller.default_project
            existing = self._seats.get(scope, {}).get(project) if project else None
        if not project:
            configured = caller.configured_project
            raise CrewUsageError(
                "No project yet: pass root_path=<your repository checkout> or project_id=... on this call, or call "
                "crew_status(project_id=...) or crew_status(git_remote=... / root_path=...) first, or call "
                "session_brief with your location."
                + (
                    f" The configured project {S.clip_item(configured, 64)!r} is not used on its own: "
                    f"pass project_id={S.clip_item(configured, 64)!r} to work there."
                    if configured
                    else ""
                )
            )
        if existing is not None and not existing.left:
            with self._lock:
                if sticky:
                    self._current[scope] = project
                    self._point(scope, project)
            return existing, None
        # One join at a time per (MCP session, project): tool calls arrive in parallel, and without this each
        # first call after a restart would join (or resume) for itself and leave several callsigns behind.
        with self._join_locks[hash((scope, project)) % _JOIN_STRIPES]:
            with self._lock:
                existing = self._seats.get(scope, {}).get(project)
            if existing is not None and not existing.left:  # a parallel call joined while this one waited
                with self._lock:
                    if sticky:
                        self._current[scope] = project
                    self._point(scope, project)
                return existing, None
            return self._join(caller, scope, project, existing, checkout, sticky=sticky)

    def _join(
        self,
        caller: CallerInfo,
        scope: tuple[str, str],
        project: str,
        existing: Seat | None,
        checkout: Mapping[str, Any] | None,
        *,
        sticky: bool,
    ) -> tuple[Seat, dict[str, Any] | None]:
        """Join ``project``: re-join a seat that left, else resume a stored seat, else a new crew session."""
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
            self._remember(existing, sticky=sticky)
            self._store_seat(caller, existing)
            return existing, body
        resumed = self._resume_stored(caller, scope, project, checkout)
        if resumed is not None:
            self._remember(resumed[0], sticky=sticky)
            return resumed
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
        self._remember(seat, sticky=sticky)
        self._store_seat(caller, seat)
        return seat, body

    # -- seats that outlive the process (stdio) ---------------------------------------

    def _seat_record(self, caller: CallerInfo, seat: Seat) -> SeatRecord:
        return SeatRecord(
            pool=pool_key(caller.client.base_url, caller.fingerprint, seat.agent_id, seat.project_id),
            anchor="",
            project_id=seat.project_id,
            agent_id=seat.agent_id,
            client_session_id=seat.client_session_id,
            crew_id=seat.crew_id,
            session_id=seat.session_id,
            callsign=seat.callsign,
            token=seat.token,
            verified=seat.verified,
        )

    def _store_seat(self, caller: CallerInfo, seat: Seat) -> None:
        """Keep (or refresh) the seat's token on disk for the process that may replace this one."""
        store = self.seat_store
        if store is None or seat.local is not None or seat.left or not seat.token:
            return
        store.save(self._seat_record(caller, seat))

    def _forget_seat(self, caller: CallerInfo, seat: Seat) -> None:
        store = self.seat_store
        if store is not None and seat.local is None and seat.token:
            store.drop(self._seat_record(caller, seat))

    def _resume_stored(
        self, caller: CallerInfo, scope: tuple[str, str], project: str, checkout: Mapping[str, Any] | None
    ) -> tuple[Seat, dict[str, Any]] | None:
        """Re-join as the crew session an earlier process of this agent held here, when its owner is gone.

        The server treats a join with the session's current token as that session coming back: same callsign,
        leases renewed, its reservations taken back. A token the server refuses is dropped and the next stored
        seat (or a fresh join) is used.
        """
        store = self.seat_store
        if store is None:
            return None
        http = CrewHttp(caller.client)
        for record in store.take(pool_key(caller.client.base_url, caller.fingerprint, str(caller.agent_id), project)):
            try:
                _, body, _ = http.call(
                    "POST",
                    "/crews/join",
                    token=record.token,
                    json=self._join_body(caller, project, record.client_session_id, checkout),
                )
            except CrewApiError as e:
                if e.status in _SEAT_GONE_STATUSES:
                    store.drop(record)
                    continue
                store.release(record)  # unreachable or overloaded: the seat is still good for a later call
                raise
            body = dict(body or {})
            if not body.get("crew_id") or not body.get("session_id"):
                store.drop(record)
                continue
            # a join that created a session (the server no longer knew this one) answers with a new token
            issued = body.get("session_token")
            session = body.get("session") or {}
            seq = int(body.get("seq") or 0)
            seat = Seat(
                scope=scope,
                project_id=project,
                agent_id=str(caller.agent_id),
                client_session_id=record.client_session_id,
                crew_id=str(body["crew_id"]),
                session_id=str(body["session_id"]),
                token=issued if isinstance(issued, str) and issued else record.token,
                callsign=str(body.get("callsign") or record.callsign),
                verified=bool(session.get("agent_verified")),
                observe_only=bool(body.get("observe_only")),
                status_cursor=max(0, seq - 20),
                notice_cursor=seq,
                last_touch=self._clock(),
                current_task_id=session.get("current_task_id"),
            )
            if seat.session_id == record.session_id:
                body["resumed_seat"] = "conversation" if store.anchor else "agent"
                seat.resumed_write = seat.resumed_status = body["resumed_seat"]
            self._store_seat(caller, seat)
            return seat, body
        return None

    def write_seat(self, caller: CallerInfo, project_id: str | None, tool: str) -> Seat:
        """The seat a crew write acts as: the project it names, else the only project this session knows.

        A write that names its project (``project_id``, or ``caller.call_project``: the tool's project_id or
        root_path) never moves the current project. One that names none is refused once this MCP session has
        chosen more than one project (:meth:`pointed_projects`), unless it is known to serve a single
        conversation (:meth:`single_conversation`): a desktop app runs one server for all its conversations,
        and "the latest choice" may be another conversation's ``session_brief`` or ``crew_status``.
        """
        named = project_id or caller.call_project
        if named:
            seat, _ = self.seat(caller, named, sticky=False)
        else:
            pointed = self.pointed_projects(caller)
            if len(pointed) > 1 and not self.single_conversation(caller):
                names = ", ".join(n for n in (_safe(p) for p in pointed[-6:]) if n)
                raise CrewUsageError(
                    f"This MCP server has been used for more than one project ({names}) and one server can serve "
                    f"several conversations, so {tool} without project_id could reach another conversation's crew. "
                    f'Nothing was done. Call {tool} again with project_id="<the project you are working in>".'
                )
            seat, _ = self.seat(caller)
        self._written.seat = seat
        return seat

    def touch(self, caller: CallerInfo, seat: Seat, *, force: bool = False) -> dict[str, Any] | None:
        """Re-join with the current token (liveness + lease renewal) at most once per minute."""
        if seat.local is not None:  # crewd's heartbeat keeps the hook session alive
            return None
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
        self._store_seat(caller, seat)
        return dict(body)

    def mark_left(self, caller: CallerInfo, seat: Seat | None = None) -> None:
        """Mark ``seat`` (default: the current seat) as left: no liveness calls or notices until it re-joins."""
        seat = seat or self.seat_for_scope(caller)
        if seat is not None:
            with seat.lock:
                seat.left = True
                seat.pending.clear()
            self._forget_seat(caller, seat)

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
        for fresh in (False, True):
            if fresh:  # the cached list may predate the zone (a folder zone derived a moment ago)
                seat.forget("zones")
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
        join: bool = True,
    ) -> str:
        if not join:
            return self._look(caller, project_id)
        seat, joined = self.seat(caller, project_id, checkout)
        http = self._http(caller, seat)
        rejoin = dict(joined or self.touch(caller, seat, force=True) or {})
        with seat.lock:  # taken over from an earlier process by a crew write: this status still has to say so
            if seat.resumed_status:
                rejoin["resumed_seat"] = seat.resumed_status
        lines: list[str] = []
        data: list[str] = []
        brief = self._crew_block(http, caller, seat)
        if brief:
            lines.append(brief)
        lines.append(self._you_line(http, seat, rejoin, data))
        shadow = self._shadow(caller, seat)
        if shadow is not None and shadow.here:
            try:
                held = sum(1 for c in self._claims(http, seat) if c.get("holder_session_id") == seat.session_id)
            except CrewApiError:
                held = 0
            lines.extend(shadow.status_lines(held))
        note = self._elsewhere_note(seat, shadow)
        if note:
            lines.append(note)
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
        lines.append(shadow.footer if shadow is not None and shadow.here else MCP_FOOTER)
        self._resolve_items(http, seat, items)
        with seat.lock:
            seat.pending.clear()
            seat.notice_cursor = max(seat.notice_cursor, seat.status_cursor)
            seat.last_notice = self._clock()
            seat.resumed_write = seat.resumed_status = None  # the YOU line above said it
        return join_text(lines, data)

    def _look(self, caller: CallerInfo, project_id: str | None) -> str:
        """``crew_status(join=False)``: the crew block, read with the API key alone.

        Nothing is joined, touched, resolved or remembered: no crew session, no presence, no ``session.joined``
        event, and the project does not become the current one. A seat this session already holds there adds
        its YOU line (read with its token, no re-join).
        """
        scope = (caller.fingerprint, caller.client_session_id)
        with self._lock:
            project = project_id or self._current.get(scope) or caller.default_project
        if not project:
            raise CrewUsageError("No project yet: call crew_status(project_id=..., join=False) or pass git_remote / root_path.")
        seat = self.seat_in(caller, project)
        held = seat if seat is not None and not seat.left else None
        params: dict[str, Any] = {"project_id": project, "recent_n": 0, "inbox_limit": 0}
        if caller.agent_id:
            params["agent_id"] = caller.agent_id
        http = self._http(caller, held) if held is not None else CrewHttp(caller.client)
        block = self._crew_block_of(http, project, params)
        lines = [block or f"CREW {_safe(project) or 'this project'}: no crew yet (nobody has joined it)."]
        if held is not None:
            lines.append(self._you_line(http, held, {}))
            lines.append("LOOK ONLY: nothing was joined, renewed or marked as read by this call.")
        else:
            lines.append(
                "NOT JOINED (look only): you hold no seat in this crew, so you can claim nothing and nobody sees "
                "you here. Call crew_status without join=False to join."
            )
        return join_text(lines)

    def _crew_block(self, http: CrewHttp, caller: CallerInfo, seat: Seat) -> str:
        params = {
            "project_id": seat.project_id,
            "agent_id": seat.agent_id,
            "session_id": seat.client_session_id,
            "recent_n": 0,
            "inbox_limit": 0,
        }
        return self._crew_block_of(http, seat.project_id, params)

    def _crew_block_of(self, http: CrewHttp, project_id: str, params: Mapping[str, Any]) -> str:
        try:
            _, brief, _ = http.call("GET", "/session/brief", params=params)
        except CrewApiError as e:
            return f"CREW {project_id}: crew block unavailable ({e.status} {S.clip_item(e.error, 40)})"
        crew = brief.get("crew") if isinstance(brief, dict) else None
        if not isinstance(crew, dict):
            return ""
        text = render_crew_block(crew, allowed_urls=tuple(brief.get("repo_url_prefixes") or ()))
        lines = text.split("\n")
        return "\n".join(ln for ln in lines if ln != CREW_FOOTER)

    def _you_line(self, http: CrewHttp, seat: Seat, rejoin: Mapping[str, Any], data: list[str] | None = None) -> str:
        session = rejoin.get("session") or {}
        state = _safe(session.get("state")) or "active"
        verified = "key-verified" if seat.verified else "self-declared"
        parts = [f"YOU: {seat.callsign} ({seat.session_id}) · mcp, advisory · {verified} · {state}"]
        if rejoin.get("resumed_seat"):
            parts.append(resumed_text(str(rejoin["resumed_seat"])))
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
            more = f", +{len(mine) - 6} more" if len(mine) > 6 else ""
            parts.append("holding " + ", ".join(self._claim_label(http, seat, c, data) for c in mine[:6]) + more)
        else:
            parts.append("holding nothing")
        return " · ".join(parts)

    def _claim_label(
        self,
        http: CrewHttp,
        seat: Seat,
        claim: Mapping[str, Any],
        data: list[str] | None = None,
        *,
        with_id: bool = False,
    ) -> str:
        """``zone pos (exclusive, active)``. A path that is not one plain token is labelled by its claim id,
        and its name goes into ``data`` (the reply's data block) when the caller has one."""
        zone = self._zone_label(http, seat, claim.get("zone_id"))
        if zone:
            target = f"zone {zone}"
        elif claim.get("resource"):
            target = _safe(claim.get("resource")) or "a resource"
        else:
            named = _safe_path(claim.get("path_glob"))
            cid = _safe(claim.get("id"))
            if named is None and cid is not None and data is not None and claim.get("path_glob"):
                data.append(f"{cid} path: {agent_text(claim.get('path_glob'))}")
            target = f"path {named or cid or '(unnamed)'}"
        task = task_ref(self._task_by_id(http, seat, claim.get("task_id"))) if claim.get("task_id") else None
        bits = [str(claim.get("mode") or "exclusive"), str(claim.get("state") or "")]
        if task:
            bits.append(task)
        if with_id and _safe(claim.get("id")):
            bits.append(str(claim["id"]))
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
        return self.seat_notice(caller, seat, force=force) if seat is not None else None

    def seat_notice(self, caller: CallerInfo, seat: Seat, *, force: bool = False) -> str | None:
        """The ``crew_notice`` of one seat (the crew a write just used), or None. Never raises."""
        if seat.left:
            return None
        try:
            return self._notice(caller, seat, force=force)
        except (CrewApiError, CrewUsageError, httpx.HTTPError, ValueError, KeyError, TypeError):
            return None

    def _notice(self, caller: CallerInfo, seat: Seat, *, force: bool) -> str | None:
        http = self._http(caller, seat)
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

    @_names_crew
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
        project_id: str | None = None,
    ) -> str:
        seat = self.write_seat(caller, project_id, "crew_claim")
        http = self._http(caller, seat)
        self._touch_quietly(caller, seat)
        shadow = self._shadow(caller, seat)
        if shadow is not None and shadow.here and action in ("claim", "adopt", "accept"):
            # release, handover and decline stay open: they give up what this seat holds
            refusals: dict[str, tuple[str, str | None]] = {
                "claim": ("CLAIMED", "remembra-crew claim <zone|path>"),
                "adopt": ("ADOPTED", "remembra-crew adopt T-n"),
                "accept": ("ACCEPTED", None),
            }
            return shadow.refusal(*refusals[action])
        if action == "claim":
            text = self._claim(http, seat, zone=zone, paths=paths, mode=mode, task=task, reason=reason, wait_s=wait_s)
            note = self._elsewhere_note(seat, shadow)
            return f"{text}\n{note}" if note else text
        if action == "release":
            return self._release(http, seat, zone=zone, paths=paths, task=task, baton=baton, reason=reason)
        if action == "adopt":
            if seat.local is not None and not (zone and _is_resource(zone)):
                # through crewd, which also restores the saved work into this checkout (a resource has none)
                return self._local_adopt(seat.local, zone=zone, paths=paths, task=task)
            return self._adopt(http, seat, zone=zone, paths=paths, task=task)
        if action == "handover":
            return self._handover(http, seat, zone=zone, paths=paths, task=task, to=to, reason=reason)
        if action in ("accept", "decline"):
            return self._answer_handover(http, seat, action, zone=zone, task=task)
        raise CrewUsageError(f"Unknown action {action!r}.")

    def _local_adopt(self, local: LocalCrew, *, zone: str | None, paths: Sequence[str] | None, task: str | None) -> str:
        """Adopt through crewd, which also restores the saved work into this checkout and reports the restore."""
        from remembra.relay.crew.gate import rpc

        data: list[str] = []
        args: dict[str, Any] = {"key": local.who["key"]}
        if task:
            args["task"] = task
            what = _safe(task) or "the task"
        elif paths:
            args["paths"] = [str(x) for x in list(paths)[:20]]
            what = self._paths_label(paths, data)
        elif zone:
            args["zone"] = zone
            what = f"zone {_safe(zone) or 'named'}"
        else:
            raise CrewUsageError(
                'Name the baton to adopt: task="T-12", zone="pos" or paths=["src/app/cart.ts"] (as offered in your brief).'
            )
        res = rpc(local.layout, "adopt", args, timeout=90.0)
        if res is not None and not res.get("ok"):
            # already adopted (by this session, earlier) and its restore did not finish: restore only
            again = rpc(local.layout, "adopt", {**args, "restore_only": True}, timeout=90.0)
            if again is not None and again.get("ok") and again.get("restore"):
                res = again
        if res is None:
            return join_text([f"NOT ADOPTED {what}: the local crew daemon did not answer. Try again in a moment."], data)
        if not res.get("ok"):
            msg = _clean_message(res.get("message") or res.get("error") or "refused")
            return join_text([f"NOT ADOPTED {what}: {msg}"], data)
        claims = [c for c in res.get("claims") or [] if isinstance(c, str)]
        me = _safe(local.who.get("callsign")) or "your crew session"
        lines = [f"ADOPTED {what}: you now hold {len(claims) or 'its'} claim(s) as {me}."]
        restore = res.get("restore") if isinstance(res.get("restore"), dict) else None
        ref = str((restore or {}).get("ref") or res.get("baton_ref") or "")
        ref = ref if re.fullmatch(r"refs/remembra/baton/[A-Za-z0-9_/-]{1,120}", ref) else ""
        if restore and restore.get("restored"):
            n = len(restore.get("files") or [])
            lines.append(
                f"The previous session's saved work is restored into this checkout: {n} file(s)"
                + (f" from {ref}." if ref else ".")
            )
        elif restore and restore.get("reason") == "dirty_tree":
            lines.append(
                "The saved work is NOT restored yet: this checkout has uncommitted changes. Commit or move them, "
                "then call this same crew_claim adopt again to restore it."
            )
        elif restore:
            lines.append("The saved work could not be restored here" + (f"; it is kept as {ref}." if ref else "."))
        for item in res.get("refused") or []:
            lines.append(f"NOT ADOPTED {_clean_message(item, 160)}")
        return join_text(lines, data)

    @staticmethod
    def _paths_label(paths: Sequence[str], data: list[str]) -> str:
        """The first three of ``paths`` for a headline; one that is not a plain token is ``path #n``, named in ``data``."""
        out: list[str] = []
        for n, p in enumerate(list(paths)[:3], 1):
            named = _safe_path(p)
            if named is None:
                data.append(f"path #{n}: {agent_text(p)}")
            out.append(named or f"path #{n}")
        return ", ".join(out)

    def _touch_quietly(self, caller: CallerInfo, seat: Seat) -> None:
        try:
            self.touch(caller, seat)
        except CrewApiError:
            pass

    def _targets(
        self,
        http: CrewHttp,
        seat: Seat,
        zone: str | None,
        paths: Sequence[str] | None,
        data: list[str] | None = None,
        *,
        claiming: bool = False,
    ) -> tuple[list[tuple[str, dict[str, Any]]], list[str]]:
        """``[(label, claim body target)]`` for a zone and/or paths; plus notes for paths that need no claim.

        A path that is not one plain token is labelled ``#n`` (its place in ``paths``) and named in ``data``.

        A path inside a zone stands for that zone. ``claiming`` (a new claim) narrows that: inside a
        TEMPORARY folder zone the path itself is the target (its ``_zone`` key keeps the zone, for a
        server that only takes whole zones), and a path widened to a declared zone gets a note.
        """
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
            for n, hit in enumerate((body or {}).get("paths") or [], 1):
                path = str(hit.get("path") or "")
                label = _safe_path(path) or f"#{n}"
                if label == f"#{n}" and data is not None and path:
                    data.append(f"path #{n}: {agent_text(path)}")
                if hit.get("error"):
                    notes.append(f"{label}: not a repo-relative path (pass paths relative to the repository root)")
                elif hit.get("crew_policy"):
                    notes.append(f"{label}: crew policy file, never claimable by agents; ask a human")
                elif hit.get("ignored"):
                    notes.append(f"{label}: ignored by the crew, no claim needed")
                elif hit.get("leaf_zone"):
                    z = self._zone(http, seat, str(hit["leaf_zone"]))
                    slug = _safe(z.get("slug")) or z["id"]
                    if claiming and _temporary_file_zone(z) and z["id"] not in seen and _safe(path):
                        # an auto-derived folder zone: claim the file, not the whole folder
                        targets.append((f"path {label}", {"path_glob": path, "_zone": z}))
                        continue
                    if claiming and z["id"] not in seen:
                        kind = "zone" if z.get("source") == "suggested" else "declared zone"
                        notes.append(
                            f"{label}: inside {kind} {slug}: the whole zone is claimed (its files are claimed through the zone)"
                        )
                    if z["id"] not in seen:
                        seen.add(z["id"])
                        targets.append((f"zone {slug}", {"zone_id": z["id"]}))
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
        data: list[str] = []
        targets, notes = self._targets(http, seat, zone, paths, data, claiming=True)
        if not targets:
            if notes:
                return join_text(["NOTHING TO CLAIM:", *[f"- {n}" for n in notes]], data)
            raise CrewUsageError('Name what to claim: zone="pos", paths=["src/app/pos/cart.ts"] or task="T-14".')
        lines: list[str] = []
        granted = capped = 0
        widened: dict[str, list[str]] = {}  # zone id -> the paths a whole-zone-only server widened to it
        asked = wait_s
        budget = min(asked, self.max_wait_s) if asked > 0 else 0  # one budget for the whole call, not per target
        cut = f" Waits are cut to {self.max_wait_s}s per call." if asked > budget else ""
        deadline = time.monotonic() + budget
        queue = list(targets)
        while queue:
            label, target = queue.pop(0)
            fallback = target.get("_zone")
            # a claim that waits is queued; one that does not is refused: keep waiting (≥1 s) once asked to
            wait_s = max(1, int(round(deadline - time.monotonic()))) if asked > 0 else 0
            body: dict[str, Any] = {
                **{k: v for k, v in target.items() if k != "_zone"},
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
                if e.status == 422 and e.error == "zone_path" and isinstance(fallback, dict):
                    # a server before file claims in temporary zones (0.17.5): the zone is claimed, as it always was
                    zid = str(fallback["id"])
                    if zid not in widened:
                        queue.insert(0, (f"zone {_safe(fallback.get('slug')) or zid}", {"zone_id": zid}))
                    widened.setdefault(zid, []).append(label.removeprefix("path "))
                    continue
                capped += 1 if e.status == 409 and e.error == "claim_cap" else 0
                lines.append(self._refused(http, seat, label, e, data))
                continue
            waited = int(round(time.monotonic() - started))
            claim = (res or {}).get("claim") or {}
            state = claim.get("state")
            granted += 1 if state == "active" else 0
            if state == "active":
                note = f" after waiting {waited}s" if asked > 0 and waited >= 1 else ""
                lines.append(f"GRANTED {label} ({claim.get('mode')}, epoch {claim.get('epoch')}, {claim.get('id')}){note}")
            elif state == "queued":
                who = self._blocker_text(http, seat, (res or {}).get("blockers") or [])
                waited_txt = f" (waited {waited}s)" if asked > 0 else ""
                lines.append(
                    f"QUEUED {label}{waited_txt}: {who}. You are next in line ({claim.get('id')}); "
                    f"call crew_claim again with wait_s to keep waiting, or work elsewhere.{cut}"
                )
            else:
                lines.append(f"{str(state or status).upper()} {label} ({claim.get('id')})")
        for zid, wide in widened.items():
            notes.append(
                f"{', '.join(wide[:5])}: this server claims whole zones only: all of zone "
                f"{self._zone_label(http, seat, zid)} is now yours, or refused as shown above"
            )
        if capped and len(targets) > 1:
            lines.append(
                f"PARTIAL: {granted} of {len(targets)} granted; the rest hit a claim limit (see above). "
                "What was granted stays yours: release what you do not need."
            )
        lines.extend(f"- {n}" for n in notes)
        return join_text(lines, data)

    def _blocker_text(self, http: CrewHttp, seat: Seat, blockers: Sequence[Mapping[str, Any]]) -> str:
        if not blockers:
            return "held by another session"
        b = blockers[0]
        holder = _safe(b.get("holder_callsign")) or self._callsign(http, seat, b.get("holder_session_id")) or "another session"
        task = task_ref(self._task_by_id(http, seat, b.get("task_id"))) if b.get("task_id") else None
        why = _safe(b.get("reason")) or "held"
        what = self._blocking_target(http, seat, b.get("claim_id"))
        return f"{why} by {holder}" + (f" ({what})" if what else "") + (f" for {task}" if task else "")

    def _blocking_target(self, http: CrewHttp, seat: Seat, claim_id: Any) -> str | None:
        """``zone api`` / ``path lib/a.ts``: what the blocking claim is on (a blocker row carries no path)."""
        try:
            claim = next((c for c in self._claims(http, seat) if c.get("id") == claim_id), None) if claim_id else None
        except CrewApiError:
            return None
        return self._target_of(http, seat, claim) if claim else None

    def _target_of(self, http: CrewHttp, seat: Seat, claim: Mapping[str, Any]) -> str | None:
        if claim.get("zone_id"):
            return f"zone {self._zone_label(http, seat, claim['zone_id'])}"
        path = _safe(claim.get("path_glob"))
        return f"path {path}" if path else _safe(claim.get("resource"))

    def _refused(self, http: CrewHttp, seat: Seat, label: str, e: CrewApiError, data: list[str]) -> str:
        blockers = e.body.get("blockers") or []
        if e.status == 409 and e.error == "claim_cap":
            return f"REFUSED: {label}: {self._cap_text(http, seat, e)}"
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
                ask = f'Work elsewhere or crew_say(to="@{holder}", kind="request_release").'
                tail = f" for {ref}" if ref else ""
                # say what the blocker really holds: one file inside the zone asked for, or the whole zone
                # around the file asked for, is not "the zone/the path held by" that session
                held = e.body.get("claim") if isinstance(e.body.get("claim"), dict) else None
                what = self._target_of(http, seat, held) if held else self._blocking_target(http, seat, b.get("claim_id"))
                if what and what != label and label.startswith("zone ") and what.startswith("path "):
                    kind = "reserved for the next pickup" if "reserved" in reason else "exclusive"
                    return f"REFUSED: {label}: {holder} holds {what} inside it ({kind}){tail}. {ask}"
                if what and what != label and label.startswith("path ") and what.startswith("zone "):
                    return f"REFUSED: {label}: the whole {what} is {how} by {holder}{tail}. {ask}"
                return f"REFUSED: {label} {how} by {holder}{tail}. {ask}"
            return f"REFUSED: {label} is held by a human or reserved. Work elsewhere or ask @mani with crew_say."
        if e.status == 423:
            return f"REFUSED: {label}: {e.message} Work elsewhere or ask @mani with crew_say."
        return error_text(e, f"claim {label}")

    def _cap_text(self, http: CrewHttp, seat: Seat, e: CrewApiError) -> str:
        """Which claim limit refused, with the numbers and the holders (never "release a claim" to a session
        that holds none). The server's ``cap`` detail when it sends one; a 0.17.5 server sends none, so the
        same facts are worked out from the crew's claims."""
        cap = e.body.get("cap")
        if isinstance(cap, dict) and cap.get("cap") in ("session", "agent") and _count(cap.get("limit")) is not None:
            limit, mine = _count(cap.get("limit")), _count(cap.get("held_by_you")) or 0
            if cap["cap"] == "session":
                held = ", ".join(t for t in (_safe_target(h.get("target")) for h in _dicts(cap.get("holding"))[:6]) if t)
                return (
                    f"Per-session claim limit reached: you hold {mine} of {limit} exclusive areas"
                    + (f" ({held})" if held else "")
                    + '. Release one you no longer need with crew_claim(action="release"), or the owner can raise '
                    "max_exclusive_claims_per_session in the crew settings. Files in one folder count as one area."
                )
            who = ", ".join(
                f"{sign} x{n}"
                for sign, n in ((_safe(h.get("callsign")), _count(h.get("count"))) for h in _dicts(cap.get("holders"))[:8])
                if sign and n
            )
            agent = _safe(cap.get("agent_id")) or "yours"
            return (
                f"Per-agent claim limit reached: sessions with agent id {agent} hold {_count(cap.get('held_by_agent')) or 0} "
                f"of {limit} exclusive areas" + (f" ({who})" if who else "") + f"; you hold {mine}. "
                "Ask a holder to release with crew_say, or the owner can raise max_exclusive_claims_per_agent "
                "in the crew settings."
            )
        try:
            claims = [c for c in self._claims(http, seat) if c.get("mode") == "exclusive" and c.get("source") != "micro_lease"]
        except CrewApiError:
            return "claim limit reached (the per-session limit, or the per-agent limit shared by sessions with your agent id)."
        mine_rows = [
            c
            for c in claims
            if c.get("holder_session_id") == seat.session_id
            and (
                c.get("state") in ("active", "offered")
                or (c.get("state") == "reserved" and c.get("reserved_for") == seat.session_id)
            )
        ]
        counts: dict[str, int] = {}
        for c in claims:
            sid = c.get("holder_session_id")
            if (
                sid
                and sid != seat.session_id
                and c.get("holder_agent_id") == seat.agent_id
                and c.get("state") in ("active", "offered")
            ):
                counts[str(sid)] = counts.get(str(sid), 0) + 1
        who = ", ".join(
            f"{self._callsign(http, seat, sid) or 'another session'} x{n}"
            for sid, n in sorted(counts.items(), key=lambda kv: -kv[1])[:8]
        )
        others = sum(counts.values())
        agent = _safe(seat.agent_id) or "yours"
        if not mine_rows:
            return (
                f"claim limit reached, and you hold no exclusive claim: this is the per-agent limit shared by every session "
                f"with agent id {agent}" + (f" ({who} hold {others})" if who else "") + ". Ask a holder to release with "
                "crew_say, or the owner can raise max_exclusive_claims_per_agent in the crew settings."
            )
        held = ", ".join(self._claim_label(http, seat, c) for c in mine_rows[:6])
        return (
            f"claim limit reached. You hold {len(mine_rows)} exclusive claim(s): {held}"
            + (f"; other sessions with agent id {agent} hold {others} ({who})" if who else "")
            + '. Release one you no longer need with crew_claim(action="release"), or the owner can raise the '
            "claim limits in the crew settings."
        )

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
        named = self._claim_matcher(http, seat, zone, paths) if zone or paths else None
        return [c for c in mine if (task_id and c.get("task_id") == task_id) or (named is not None and named(c))]

    def _claim_matcher(
        self, http: CrewHttp, seat: Seat, zone: str | None, paths: Sequence[str] | None, *, wide: bool = False
    ) -> Callable[[Mapping[str, Any]], bool]:
        """Whether a claim is on the named zone/resource/paths: by the target the server would file them
        under today, and by what a path claim covers (see :func:`_path_claim_covered`; ``wide`` as there)."""
        zone_ids: set[str] = set()
        globs: set[str] = set()
        resources: set[str] = set()
        targets, _ = self._targets(http, seat, zone, paths)
        for _, t in targets:
            if t.get("zone_id"):
                zone_ids.add(str(t["zone_id"]))
            if t.get("path_glob"):
                globs.add(str(t["path_glob"]))
            if t.get("resource"):
                resources.add(str(t["resource"]))
        zone_globs: list[str] = []
        zone_excludes: list[str] = []
        if zone and not _is_resource(zone):
            z = self._zone(http, seat, zone)
            zone_globs = [str(g) for g in z.get("include_globs") or ()]
            zone_excludes = [str(g) for g in z.get("exclude_globs") or ()]

        def named(c: Mapping[str, Any]) -> bool:
            return (
                c.get("zone_id") in zone_ids
                or c.get("path_glob") in globs
                or c.get("resource") in resources
                or _path_claim_covered(c, paths, zone_globs, zone_excludes, wide=wide)
            )

        return named

    def _no_match(self, http: CrewHttp, seat: Seat, base: str, *, zone: str | None, paths: Sequence[str] | None) -> str:
        """The empty answer of release and handover: ``base``, then what this session does hold, who holds
        the named target instead, and how to spell a target that was passed with a ``zone:``/``path:`` prefix."""
        out = [base]
        for p in list(paths or ())[:3]:
            m = re.fullmatch(r"(zone|path|resource):(.+)", str(p).strip())
            if m is None:
                continue
            kind, rest = m.group(1), _safe(m.group(2))
            if kind == "path":
                out.append('Pass a path as it is in the repository, without "path:"' + (f': paths=["{rest}"].' if rest else "."))
            else:
                out.append(f"A {kind} goes in the zone argument, not in paths" + (f': zone="{rest}".' if rest else "."))
        m = re.fullmatch(r"(zone|path):(.+)", str(zone or "").strip())
        if m is not None:
            kind, rest = m.group(1), _safe(m.group(2))
            if kind == "path":
                out.append("A path goes in the paths argument, not in zone" + (f': paths=["{rest}"].' if rest else "."))
            else:
                out.append('Pass the zone slug without "zone:"' + (f': zone="{rest}".' if rest else "."))
        data: list[str] = []
        try:
            live = self._claims(http, seat)
        except CrewApiError:
            return " ".join(out)
        mine = [c for c in live if c.get("holder_session_id") == seat.session_id]
        try:
            named = self._claim_matcher(http, seat, zone, paths, wide=True)  # also who holds a wider claim over it
        except (CrewApiError, CrewUsageError):
            named = None
        theirs = [c for c in live if c not in mine and c.get("state") != "queued" and named is not None and named(c)]
        for c in theirs[:3]:
            who = self._callsign(http, seat, c.get("holder_session_id")) or "a human or another session"
            out.append(f"{self._claim_label(http, seat, c, data)} is held by {who}, not by you.")
        if mine:
            held = ", ".join(self._claim_label(http, seat, c, data, with_id=True) for c in mine[:6])
            out.append(f"You hold: {held}." + (f" (+{len(mine) - 6} more)" if len(mine) > 6 else ""))
        elif len(out) > 1:
            out.append("You hold nothing.")
        return join_text([" ".join(out)], data)

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
        # reserved too: crew_status lists a session's own reservation as held, and the server lets its holder end it
        claims = self._my_claims(http, seat, zone=zone, paths=paths, task_id=None, states=_LIVE_CLAIM_STATES)
        if not claims:
            return self._no_match(http, seat, "NOTHING RELEASED: you hold no live claim on that.", zone=zone, paths=paths)
        lines = []
        data: list[str] = []
        for c in claims:
            label = self._claim_label(http, seat, c, data)
            if c.get("state") == "reserved" and c.get("reserve_reason") == "human_hold":
                lines.append(f"NOT RELEASED {label}: a human put it on hold. Ask @mani with crew_say.")
                continue
            if c.get("state") == "reserved" and baton:
                # the server ends a reserved claim on any release, so a baton that is already kept is left alone
                lines.append(
                    f"ALREADY RESERVED {label}: it is kept as a baton for the next pickup. "
                    "Release it with baton=false to free the area."
                )
                continue
            body: dict[str, Any] = {"baton": bool(baton)}
            if reason:
                body["note"] = reason[:280]
            try:
                _, res, _ = http.call("POST", f"/claims/{c['id']}/release", token=seat.token, json=body)
            except CrewApiError as e:
                lines.append(error_text(e, f"release {c['id']}"))
                continue
            state = ((res or {}).get("claim") or {}).get("state") or "released"
            line = f"RELEASED {label} → {state}"
            ref = str(c.get("baton_ref") or "")
            if c.get("state") == "reserved" and re.fullmatch(r"refs/remembra/baton/[A-Za-z0-9_/-]{1,120}", ref):
                line += f" (the work saved with it is no longer offered; it stays as {ref} in the checkout that saved it)"
            lines.append(line)
        return join_text(lines, data)

    def _adopt(self, http: CrewHttp, seat: Seat, *, zone: str | None, paths: Sequence[str] | None, task: str | None) -> str:
        if task:
            task_row = self._task(http, seat, task)
            try:
                _, body, _ = http.call("POST", f"/tasks/{task_row['id']}/adopt", token=seat.token)
            except CrewApiError as e:
                if e.status in (403, 409):
                    return f"NOT ADOPTED {task_ref(task_row)}: {e.message}"
                if e.status == 429:
                    return f"NOT ADOPTED {task_ref(task_row)}: {rate_limit_text(e) or 'rate limited'}"
                raise
            seat.forget("tasks")
            seat.current_task_id = str(task_row["id"])
            task_data: list[str] = []
            return join_text([self._adopted(http, seat, task_ref(task_row) or "the task", body or {}, task_data)], task_data)
        if not (zone or paths):
            raise CrewUsageError(
                'Name the baton to adopt: task="T-12", zone="pos" or paths=["src/app/cart.ts"] (as offered in your brief).'
            )
        data: list[str] = []
        reserved = [c for c in self._claims(http, seat) if c.get("state") == "reserved"]
        picked: list[dict[str, Any]] = []
        under: list[dict[str, Any]] = []  # reserved path claims inside the named zone (not the zone's own claims)
        labels: list[str] = []
        if zone and _is_resource(zone):
            labels.append(_safe(zone) or "resource")
            picked += [c for c in reserved if c.get("resource") == zone]
        elif zone:
            z = self._zone(http, seat, zone)
            labels.append(f"zone {_safe(z.get('slug'))}")
            zone_globs = [str(g) for g in z.get("include_globs") or ()]
            zone_excludes = [str(g) for g in z.get("exclude_globs") or ()]
            picked += [c for c in reserved if c.get("zone_id") == z["id"]]
            under = [c for c in reserved if _path_claim_covered(c, None, zone_globs, zone_excludes)]
            picked += under
        if paths:
            # a lost session's path claim, or the zone a path lies in
            labels.append(self._paths_label(paths, data))
            targets, _ = self._targets(http, seat, None, paths)
            zone_ids = {t["zone_id"] for _, t in targets if t.get("zone_id")}
            picked += [
                c
                for c in reserved
                if c not in picked
                and (c.get("zone_id") in zone_ids or any(G.claim_covers_path(p, str(c.get("path_glob") or "")) for p in paths))
            ]
        label = " and ".join(labels)
        if not picked:
            return join_text([f"NOT ADOPTED {label}: nothing is reserved there."], data)
        saved = {str(c["baton_ref"]) for c in picked if c.get("baton_ref")}
        later: list[dict[str, Any]] = []
        if len(saved) > 1 and under:
            # the zone slug names the zone's own baton first; a path baton under it with other saved work waits
            first = [c for c in picked if c not in under]
            first_refs = {str(c["baton_ref"]) for c in first if c.get("baton_ref")}
            if first and len(first_refs) <= 1:
                later = [c for c in under if c.get("baton_ref") and str(c["baton_ref"]) not in first_refs]
                picked = [c for c in picked if c not in later]
                saved = {str(c["baton_ref"]) for c in picked if c.get("baton_ref")}
        if len(saved) > 1:  # one adopt takes one lost session's saved work, as remembra-crew adopt does
            names = sorted(
                {
                    f'paths=["{c["path_glob"]}"]'
                    if G.SAFE_CLAIM_PATH_RE.fullmatch(str(c.get("path_glob") or ""))
                    and ".." not in str(c["path_glob"]).split("/")
                    else f"zone {self._zone_label(http, seat, c.get('zone_id'))}"
                    for c in picked
                    if c.get("baton_ref")
                }
            )
            return join_text(
                [f"NOT ADOPTED {label}: {len(saved)} batons with saved work match; adopt one at a time: " + ", ".join(names[:5])],
                data,
            )
        claims: list[dict[str, Any]] = []
        baton: dict[str, Any] | None = None
        refused: list[str] = []
        done: set[str] = set()
        limited = False  # each adopt is charged on its own: after a 429 nothing more is sent, and what was adopted is kept
        own_left = False
        for c in picked:
            if c["id"] in done:  # adopting a task's baton takes its sibling batons too
                continue
            if limited:
                own_left = own_left or c.get("holder_session_id") == seat.session_id
                refused.append(f"NOT ADOPTED {self._claim_label(http, seat, c, data)}: not tried, the adopt limit was reached")
                continue
            try:
                _, body, _ = http.call("POST", f"/claims/{c['id']}/adopt", token=seat.token, json={})
            except CrewApiError as e:
                if e.status in (403, 409, 429):
                    what = label if len(picked) == 1 else self._claim_label(http, seat, c, data)
                    if e.status == 429:
                        limited = True
                        own_left = own_left or c.get("holder_session_id") == seat.session_id
                        refused.append(f"NOT ADOPTED {what}: {rate_limit_text(e) or 'rate limited'}")
                    else:
                        refused.append(f"NOT ADOPTED {what}: {e.message}")
                    continue
                raise
            body = body or {}
            got = [x for x in body.get("claims") or ([body["claim"]] if body.get("claim") else []) if isinstance(x, dict)]
            claims += got
            done |= {str(x.get("id")) for x in got} | {str(c["id"])}
            if baton is None and isinstance(body.get("baton"), dict):
                baton = body["baton"]
        for c in later:
            if str(c["id"]) in done:
                continue
            glob = str(c.get("path_glob") or "")
            quoted = G.SAFE_CLAIM_PATH_RE.fullmatch(glob) and ".." not in glob.split("/")
            how = f'with paths=["{glob}"]' if quoted else "by its path"
            refused.append(f"STILL RESERVED {self._claim_label(http, seat, c, data)}: adopt it on its own {how}.")
        if own_left:
            refused.append(
                "A reserved claim of your own needs no adopt: a plain crew_claim on it re-takes it, "
                'and crew_claim(action="release") frees it.'
            )
        if not claims:
            return join_text(refused or [f"NOT ADOPTED {label}: nothing is reserved there."], data)
        text = self._adopted(http, seat, label, {"claims": claims, **({"baton": baton} if baton else {})}, data)
        return join_text([text, *refused], data)

    def _adopted(self, http: CrewHttp, seat: Seat, what: str, body: Mapping[str, Any], data: list[str] | None = None) -> str:
        claims = [c for c in body.get("claims") or ([body["claim"]] if body.get("claim") else []) if isinstance(c, dict)]
        held = ", ".join(self._claim_label(http, seat, c, data) for c in claims) if claims else "no zone claims"
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
            return self._no_match(http, seat, "NOT HANDED OVER: you hold no active claim on that.", zone=zone, paths=paths)
        who = self._callsign(http, seat, target) or target
        lines = []
        data: list[str] = []
        for c in claims:
            body: dict[str, Any] = {"to": target}
            if reason:
                body["note"] = reason[:280]
            try:
                http.call("POST", f"/claims/{c['id']}/handover", token=seat.token, json=body)
            except CrewApiError as e:
                lines.append(error_text(e, f"handover {c['id']}"))
                continue
            lines.append(f"OFFERED {self._claim_label(http, seat, c, data)} to {who} (they accept or decline within 10 min).")
        return join_text(lines, data)

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

    @_names_crew
    def guard(
        self,
        caller: CallerInfo,
        *,
        paths: Sequence[str],
        command: str | None,
        mcp_tool: str | None,
        project_id: str | None = None,
    ) -> str:
        seat = self.write_seat(caller, project_id, "crew_guard")
        http = self._http(caller, seat)
        self._touch_quietly(caller, seat)
        shadow = self._shadow(caller, seat)
        if shadow is not None and shadow.here:  # the check auto-claims a free zone for the session that asks
            return shadow.refusal("CHECKED", "remembra-crew claim <zone|path>")
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

    @_names_crew
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
        project_id: str | None = None,
    ) -> str:
        seat = self.write_seat(caller, project_id, "crew_task")
        http = self._http(caller, seat)
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
            shadow = self._shadow(caller, seat)
            if shadow is not None and shadow.here:  # starting a task claims its zones for the session that starts it
                return shadow.refusal("STARTED", f"remembra-crew task start {ref}")
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
            # status "claimed" claims the task and "in_progress" starts it (unless it only unblocks a task this seat
            # already owns): both take the task's zones for the session that asks, like action="start"
            takes = status == "claimed" or (status == "in_progress" and row.get("status") != "blocked")
            if takes and status != row.get("status"):
                shadow = self._shadow(caller, seat)
                if shadow is not None and shadow.here:
                    return shadow.refusal("UPDATED", f"remembra-crew task start {ref}")
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

    @_names_crew
    def say(
        self,
        caller: CallerInfo,
        *,
        body: str,
        kind: str,
        to: str,
        thread: str | None,
        wait_s: int,
        project_id: str | None = None,
    ) -> str:
        seat = self.write_seat(caller, project_id, "crew_say")
        http = self._http(caller, seat)
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
        asked = wait_s
        wait_s = min(asked, self.max_wait_s) if asked > 0 else 0
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
                waited = int(round(float(res.get("waited_s") or wait_s)))
                if asked > wait_s:
                    lines.append(
                        f"no reply yet (waited {waited}s; waits are cut to {self.max_wait_s}s per call). The reply "
                        "will reach you as a crew_notice on a later call, or in crew_status."
                    )
                else:
                    lines.append(f"no reply yet (waited {waited}s)")
        return "\n".join(lines)

    # -- crew_checkpoint ------------------------------------------------------------------

    @_names_crew
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
        project_id: str | None = None,
    ) -> str:
        seat = self.write_seat(caller, project_id, "crew_checkpoint")
        http = self._http(caller, seat)
        self._touch_quietly(caller, seat)
        task_row = self._task(http, seat, task) if task else None
        shadow = self._shadow(caller, seat)
        shadowed = shadow is not None and bool(shadow.here)
        # the hook session reports the working tree itself: the same files under this second identity would be
        # recorded as two sessions editing one file (same_file / exclusive_breach between the two)
        facts: dict[str, Any] = {"files_changed": [] if shadowed else [str(p) for p in files_changed][:500]}
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
        if shadow is not None and shadowed and files_changed:
            lines.append(
                f"FILES NOT ATTRIBUTED to this MCP seat ({shadow.callsign}): the crew-hook session on this machine "
                f"({shadow.who}) reports the working tree itself; recorded here they would count as a second session "
                "editing the same files."
            )
        lines.extend(self._collision_lines(http, seat, data))
        notice = self.seat_notice(caller, seat, force=True)  # this crew's queue, not the current project's
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
        lines = [f"COLLISIONS ({len(mine)}):"]
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

    # -- crew_report ----------------------------------------------------------------------

    @_names_crew
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
        project_id: str | None = None,
    ) -> str:
        seat = self.write_seat(caller, project_id, "crew_report")
        http = self._http(caller, seat)
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
