"""Relay endpoints: project identity, links, session close-out, trail, pickup brief.

- ``POST   /api/v1/projects/resolve``  location (git remote / root commit / path) -> stable project id
- ``POST   /api/v1/projects/links``    link two projects (from, to, relation)
- ``GET    /api/v1/projects/links``    links of a project (both directions)
- ``DELETE /api/v1/projects/links``    remove a link
- ``POST   /api/v1/session/close``     session facts -> ONE structured handoff (idempotent per agent+session)
- ``GET    /api/v1/session/brief``     pickup brief (accepts a project id or a location)
- ``GET    /api/v1/trail``             handoffs + checkpoints across agents, newest first
- ``GET    /api/v1/trail/summary``     per-agent / per-project activity (dashboard)

Attribution: when the API key is agent-scoped, the agent id comes from the key
and a different id in the body or the ``X-Remembra-Agent-Id`` header is
rejected. Unscoped keys may name the agent in the body or the header (they
must agree). Writes (close, links) require a well-formed agent id; the brief
is lenient (a malformed id is ignored for attribution and only used, as
before, to look up the inbox). Every stored string passes ``redact_secrets``;
access to each project is checked with the key's project restrictions, and
project-restricted keys never create or move location bindings.

Reads never write: GET brief and trail compute the project for an unseen
location without recording it; only close and ``POST /projects/resolve``
record bindings.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Callable
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Query, Request, Response, status
from pydantic import BaseModel, Field, field_validator, model_validator

from remembra.api.v1.agent_session import screen_text
from remembra.auth.middleware import AuthenticatedUser, CurrentUser, has_permission, resolve_project_access
from remembra.client.project import normalize_project_id
from remembra.cloud.limits import record_relay_usage, relay_guard
from remembra.core.limiter import limiter
from remembra.relay.identity import ProjectLocator
from remembra.services.relay import BindingNotAllowed, ProjectAccessDenied, RelayService

router = APIRouter(tags=["relay"])

AGENT_HEADER = "X-Remembra-Agent-Id"
_AGENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@/+-]{0,127}$")
_SESSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@/+=-]{0,199}$")
_RELATION_RE = re.compile(r"^[a-z][a-z0-9_-]{0,39}$")


def _service(request: Request) -> RelayService:
    return RelayService(db=request.app.state.db, memory_service=getattr(request.app.state, "memory_service", None))


def _require(current_user: Any, permission: str) -> None:
    if not has_permission(current_user, permission):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=f"Permission denied: {permission} required")


def _clean_agent(value: str | None, source: str) -> str | None:
    agent = (value or "").strip()
    if not agent:
        return None
    if not _AGENT_RE.match(agent):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid agent id in {source}: use 1-128 chars of letters, digits and ._:@/+-",
        )
    return agent


def _trail_cursor(before: str | None, before_id: str | None) -> tuple[datetime, str | None] | None:
    """Parse the trail's keyset cursor (``before`` time, optional ``before_id``)."""
    raw = (before or "").strip()
    cursor_id = (before_id or "").strip() or None
    if not raw:
        if cursor_id:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="before_id needs before (the entry's created_at)")
        return None
    try:
        return datetime.fromisoformat(raw), cursor_id
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid before: use an ISO 8601 time, such as an entry's created_at",
        ) from None


def effective_agent(
    request: Request,
    user: AuthenticatedUser,
    body_agent: str | None,
    *,
    strict: bool = True,
    warnings: list[str] | None = None,
) -> tuple[str | None, bool]:
    """Resolve the agent a relay call acts as. Returns ``(agent_id, verified)``.

    ``verified`` is True when the id comes from an agent-scoped key.
    ``strict=False`` (reads such as the brief): a malformed header is ignored
    and a malformed body/query id is passed through unchanged for the inbox
    lookup (the pre-relay behavior), with a note in ``warnings``; agent-scope
    mismatches are still refused.
    """
    if strict:
        header_agent = _clean_agent(request.headers.get(AGENT_HEADER), f"the {AGENT_HEADER} header")
        claimed = _clean_agent(body_agent, "the request")
    else:
        header_agent = _lenient_agent(request.headers.get(AGENT_HEADER), f"the {AGENT_HEADER} header", warnings, keep=False)
        claimed = _lenient_agent(body_agent, "agent_id", warnings, keep=True)
    scoped = getattr(user, "agent_id", None)
    if scoped:
        for other, where in ((claimed, "request"), (header_agent, f"{AGENT_HEADER} header")):
            if other and other != scoped:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail=f"This API key is scoped to agent '{scoped}'; the {where} claims '{other}'.",
                )
        return scoped, True
    if claimed and header_agent and claimed != header_agent:
        if not strict:
            return claimed, False
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"agent_id '{claimed}' does not match the {AGENT_HEADER} header '{header_agent}'.",
        )
    return claimed or header_agent, False


def _lenient_agent(value: str | None, source: str, warnings: list[str] | None, keep: bool) -> str | None:
    agent = (value or "").strip()
    if not agent:
        return None
    if _AGENT_RE.match(agent):
        return agent
    if warnings is not None:
        warnings.append(
            f"The agent id in {source} is not a valid relay agent id (1-128 chars of letters, digits and ._:@/+-); "
            + ("it is used for the inbox only." if keep else "it was ignored.")
        )
    return agent[:128] if keep else None


def pii_scrubber(request: Request) -> Callable[[str], str] | None:
    """Per-value PII scrub for close-out facts: redact, never reject.

    A blocked PII match in one commit subject must not lose the whole
    handoff, so blocked values are replaced with ``[REDACTED:pii]``. The
    rendered handoff is built only from these scrubbed values plus ids that
    passed their own validation, so it is not PII-screened a second time (a
    session UUID such as ``…-202609251234`` must not reject the close).
    """
    detector = getattr(request.app.state, "pii_detector", None)
    if detector is None:
        return None

    def scrub(text: str) -> str:
        result = detector.scan(text, source="user_input")
        if not result.has_pii:
            return text
        if result.blocked:
            return "[REDACTED:pii]"
        return str(result.redacted_content) if result.redacted_content else text  # detect mode: warn only

    return scrub


def _check_project(user: AuthenticatedUser, project_id: str) -> str:
    resolved = resolve_project_access(user, project_id)
    return resolved or project_id


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


def _clip(value: Any, limit: int) -> Any:
    return value[:limit] if isinstance(value, str) else value


class LocatorIn(BaseModel):
    git_remote: str | None = Field(default=None, max_length=2000, description="Remote URL, any form (https/ssh/scp)")
    root_commit: str | None = Field(default=None, max_length=64, description="git rev-list --max-parents=0 HEAD (smallest)")
    root_path: str | None = Field(default=None, max_length=4096, description="Absolute path of the working tree")
    repo_name: str | None = Field(default=None, max_length=200)
    host: str | None = Field(default=None, max_length=255, description="Machine name; qualifies path fingerprints")

    def locator(self) -> ProjectLocator:
        return ProjectLocator(
            git_remote=self.git_remote,
            root_commit=self.root_commit,
            root_path=self.root_path,
            repo_name=self.repo_name,
            host=self.host,
        )


class ResolveRequest(LocatorIn):
    hint_project: str | None = Field(default=None, max_length=128, description="Project id to use for a new location")
    bind: bool = Field(default=False, description="Re-bind an already-known location to hint_project")


class CommitIn(BaseModel):
    sha: str = Field(..., max_length=64)
    subject: str = ""

    @field_validator("subject", mode="before")
    @classmethod
    def _subject(cls, v: Any) -> Any:
        return _clip(v or "", 300)


class CommandIn(BaseModel):
    cmd: str
    exit_code: int | None = None

    @field_validator("cmd", mode="before")
    @classmethod
    def _cmd(cls, v: Any) -> Any:
        return _clip(v, 1000)


class TestRunIn(BaseModel):
    __test__ = False  # not a pytest class

    cmd: str
    passed: bool
    summary: str | None = None

    @field_validator("cmd", mode="before")
    @classmethod
    def _cmd(cls, v: Any) -> Any:
        return _clip(v, 1000)

    @field_validator("summary", mode="before")
    @classmethod
    def _summary(cls, v: Any) -> Any:
        return _clip(v, 300)


# (field, max items, max chars per item): oversized input is truncated, never
# rejected — a close-out must not fail because an agent did a lot of work.
_LIST_CAPS = {
    "commits": 100,
    "files_changed": 500,
    "uncommitted_files": 500,
    "commands": 200,
    "tests": 100,
    "errors": 50,
    "todos_open": 100,
    "incomplete": 10,
}
_STR_CAPS = {
    "branch": 255,
    "head_commit": 64,
    "upstream": 255,
    "diff_stat": 300,
    "notes": 6000,
    "next_step": 1000,
    "facts_source": 40,
    "commit_evidence": 80,
}


class FactsIn(BaseModel):
    branch: str | None = None
    head_commit: str | None = None
    upstream: str | None = None
    unpushed_commits: int | None = Field(default=None, ge=0)
    no_upstream: bool = False
    commits: list[CommitIn] = Field(default_factory=list)
    files_changed: list[str] = Field(default_factory=list)
    uncommitted_files: list[str] = Field(default_factory=list)
    diff_stat: str | None = None
    commands: list[CommandIn] = Field(default_factory=list)
    tests: list[TestRunIn] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)
    todos_open: list[str] = Field(default_factory=list)
    notes: str | None = None
    next_step: str | None = None
    facts_source: str | None = Field(
        default=None, description="relay-cli:git+transcript | relay-cli:git | agent-declared (default)"
    )
    commit_evidence: str | None = Field(default=None, description="How commits were chosen (session-reflog, last-12h, ...)")
    incomplete: list[str] = Field(default_factory=list, description="git probes that did not finish (log, status, ...)")

    @model_validator(mode="before")
    @classmethod
    def _truncate(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        data = dict(data)
        for name, cap in _LIST_CAPS.items():
            value = data.get(name)
            if isinstance(value, list):
                # Keep the most recent entries for event lists, the first ones for file lists.
                value = value[-cap:] if name in ("commands", "tests", "errors") else value[:cap]
                if name in ("files_changed", "uncommitted_files", "errors", "todos_open", "incomplete"):
                    value = [_clip(v, 1000) for v in value if isinstance(v, str)]
                data[name] = value
        for name, cap in _STR_CAPS.items():
            data[name] = _clip(data.get(name), cap)
        return data


class CloseRequest(BaseModel):
    agent_id: str | None = Field(default=None, max_length=128)
    session_id: str | None = Field(
        default=None, max_length=200, description="Stable per agent session; a repeat close updates the same handoff"
    )
    project_id: str | None = Field(default=None, max_length=128)
    project: str | ResolveRequest | None = Field(default=None, description="Project id or a location to resolve")
    facts: FactsIn = Field(default_factory=FactsIn)
    summary: str | None = Field(default=None, description="Optional agent-written summary; grounding-checked, never trusted")
    end_reason: str | None = None
    closed_at: datetime | None = Field(
        default=None,
        description=(
            "When the session ended on the client (a handoff sent late from an offline queue keeps its original time). "
            "Never later than the server's clock and at most 15 days before it; omitted means now. A handoff that "
            "ended before another session's does not replace it as the project's latest."
        ),
    )

    @field_validator("summary", mode="before")
    @classmethod
    def _summary(cls, v: Any) -> Any:
        return _clip(v, 6000)

    @field_validator("end_reason", mode="before")
    @classmethod
    def _reason(cls, v: Any) -> Any:
        return _clip(v, 100)


class LinkRequest(BaseModel):
    from_project: str = Field(..., min_length=1, max_length=128)
    to_project: str = Field(..., min_length=1, max_length=128)
    relation: str = Field(default="related", max_length=40, description="e.g. related, depends_on, frontend_of")


# ---------------------------------------------------------------------------
# Project identity
# ---------------------------------------------------------------------------


async def _resolve(
    request: Request, user: AuthenticatedUser, locator: LocatorIn, hint: str | None, bind: bool, create: bool
) -> dict[str, Any]:
    try:
        return await _service(request).registry.resolve(
            user_id=user.user_id,
            locator=locator.locator(),
            hint_project=hint,
            bind=bind,
            create=create,
            allowed_projects=user.project_ids,
        )
    except (ProjectAccessDenied, BindingNotAllowed) as e:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(e)) from e
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e)) from e


@router.post("/projects/resolve", summary="Resolve a location (git remote / root commit / path) to a project id")
@limiter.limit("120/minute")
async def resolve_project(request: Request, body: ResolveRequest, current_user: CurrentUser) -> dict[str, Any]:
    """Same repo on any machine, drive or worktree -> the same project id.

    A new location is registered (``created``) unless the key is read-only or
    project-restricted, in which case the id is computed but not persisted
    (``persisted: false``). ``bind`` needs an unrestricted key with
    ``memory:store``. A hint that could not be applied is reported in ``warnings``.
    """
    _require(current_user, "memory:recall")
    can_write = has_permission(current_user, "memory:store")
    if body.bind and not can_write:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Permission denied: memory:store required to bind")
    return await _resolve(request, current_user, body, body.hint_project, body.bind, create=can_write)


# ---------------------------------------------------------------------------
# Links
# ---------------------------------------------------------------------------


def _relation(value: str) -> str:
    relation = (value or "related").strip().lower()
    if not _RELATION_RE.match(relation):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="relation must match [a-z][a-z0-9_-]{0,39}")
    return relation


@router.post("/projects/links", summary="Link two projects")
@limiter.limit("60/minute")
async def add_link(request: Request, body: LinkRequest, current_user: CurrentUser) -> dict[str, Any]:
    _require(current_user, "memory:store")
    source = _check_project(current_user, normalize_project_id(body.from_project))
    target = _check_project(current_user, normalize_project_id(body.to_project))
    agent, _ = effective_agent(request, current_user, None)
    try:
        return await _service(request).registry.add_link(current_user.user_id, source, target, _relation(body.relation), agent)
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e)) from e


@router.get("/projects/links", summary="Links of a project (both directions)")
@limiter.limit("60/minute")
async def list_links(
    request: Request,
    current_user: CurrentUser,
    project_id: Annotated[str, Query(min_length=1, max_length=128)],
) -> dict[str, Any]:
    _require(current_user, "memory:recall")
    project = _check_project(current_user, normalize_project_id(project_id))
    items = await _service(request).registry.links_for(current_user.user_id, project)
    if current_user.project_ids:
        items = [i for i in items if i["project_id"] in current_user.project_ids]
    return {"project_id": project, "count": len(items), "items": items}


@router.delete("/projects/links", summary="Remove a project link")
@limiter.limit("60/minute")
async def remove_link(
    request: Request,
    current_user: CurrentUser,
    from_project: Annotated[str, Query(min_length=1, max_length=128)],
    to_project: Annotated[str, Query(min_length=1, max_length=128)],
    relation: Annotated[str, Query(max_length=40)] = "related",
) -> dict[str, Any]:
    _require(current_user, "memory:store")
    source = _check_project(current_user, normalize_project_id(from_project))
    target = _check_project(current_user, normalize_project_id(to_project))
    removed = await _service(request).registry.remove_link(current_user.user_id, source, target, _relation(relation))
    return {"removed": removed}


# ---------------------------------------------------------------------------
# Close-out
# ---------------------------------------------------------------------------


@router.post("/session/close", summary="Close a session: store ONE structured handoff")
@limiter.limit("60/minute")
async def close_session(
    request: Request,
    body: CloseRequest,
    current_user: CurrentUser,
    response: Response,
) -> dict[str, Any]:
    """Build the handoff (Done / Not done / Failing / Next) from the session's
    facts. Idempotent per (agent_id, session_id): closing again updates the same
    handoff (the previous version is superseded, never duplicated).

    A close is a relay event: it never uses smart credits (the handoff is stored
    without LLM enrichment); it counts toward the plan's relay burst limit and
    soft monthly cap."""
    _require(current_user, "memory:store")
    await relay_guard(request, response, current_user.user_id)
    agent, verified = effective_agent(request, current_user, body.agent_id)
    if not agent:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"agent_id is required (in the body or the {AGENT_HEADER} header).",
        )
    session_id = (body.session_id or "").strip() or None
    if session_id is None:
        session_id = f"adhoc-{uuid.uuid4().hex[:16]}"
    elif not _SESSION_RE.match(session_id):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid session_id characters")

    resolution: dict[str, Any] | None = None
    requested: str | None
    if isinstance(body.project, ResolveRequest):
        resolution = await _resolve(
            request, current_user, body.project, body.project.hint_project, body.project.bind, create=True
        )
        requested = resolution["project_id"]
    else:
        raw = body.project if isinstance(body.project, str) else body.project_id
        requested = normalize_project_id(raw) if raw and raw.strip() else None
    project = resolve_project_access(current_user, requested) or "default"

    result = await _service(request).close_session(
        user_id=current_user.user_id,
        project_id=project,
        agent_id=agent,
        session_id=session_id,
        facts=body.facts.model_dump(),
        summary=body.summary,
        end_reason=body.end_reason,
        agent_verified=verified,
        screen=lambda text: screen_text(request, text, apply_pii=False),
        scrub=pii_scrubber(request),
        closed_at=body.closed_at,
    )
    if result["changed"]:
        await record_relay_usage(request, current_user.user_id)
    return {
        "project_id": project,
        "agent_id": agent,
        "agent_verified": verified,
        "session_id": session_id,
        "resolution": resolution,
        **result,
    }


# ---------------------------------------------------------------------------
# Brief (pickup) and trail
# ---------------------------------------------------------------------------


async def _project_from_query(
    request: Request,
    user: AuthenticatedUser,
    project_id: str | None,
    locator: LocatorIn,
    hint_project: str | None,
) -> tuple[str | None, dict[str, Any] | None]:
    """The project a read addresses. Never records a binding (a read has no side effects)."""
    if project_id:
        return resolve_project_access(user, project_id), None
    if not locator.locator().is_empty():
        resolution = await _resolve(request, user, locator, hint_project, bind=False, create=False)
        return resolve_project_access(user, resolution["project_id"]), resolution
    if hint_project:
        return resolve_project_access(user, normalize_project_id(hint_project)), None
    return resolve_project_access(user, None), None


def _locator_from_query(
    git_remote: str | None, root_commit: str | None, root_path: str | None, repo_name: str | None, host: str | None
) -> LocatorIn:
    return LocatorIn(git_remote=git_remote, root_commit=root_commit, root_path=root_path, repo_name=repo_name, host=host)


@router.get("/session/brief", summary="Session-start brief for an agent (pickup)")
@limiter.limit("60/minute")
async def session_brief(
    request: Request,
    current_user: CurrentUser,
    project_id: Annotated[str | None, Query(max_length=128)] = None,
    agent_id: Annotated[str | None, Query(max_length=128)] = None,
    recent_n: Annotated[int, Query(ge=0, le=50)] = 10,
    inbox_limit: Annotated[int, Query(ge=0, le=50)] = 10,
    git_remote: Annotated[str | None, Query(max_length=2000)] = None,
    root_commit: Annotated[str | None, Query(max_length=64)] = None,
    root_path: Annotated[str | None, Query(max_length=4096)] = None,
    repo_name: Annotated[str | None, Query(max_length=200)] = None,
    host: Annotated[str | None, Query(max_length=255)] = None,
    hint_project: Annotated[str | None, Query(max_length=128)] = None,
    branch: Annotated[str | None, Query(max_length=255, description="The reader's current branch")] = None,
    head_commit: Annotated[str | None, Query(max_length=64, description="The reader's current HEAD")] = None,
) -> dict[str, Any]:
    """Latest handoff ("Last session: ..."), unread inbox, status, linked
    projects and recent memories by time, plus ``rendered`` — a compact
    (~1500 token) text version. Pass ``project_id`` or a location
    (``git_remote`` / ``root_commit`` / ``root_path``) to resolve it;
    ``hint_project`` is the client's configured project (used for a location
    seen for the first time, and reported when the location resolves
    elsewhere). ``branch`` / ``head_commit`` mark a handoff recorded on
    another checkout as possibly stale. Read-only: nothing is recorded."""
    _require(current_user, "memory:recall")
    notes: list[str] = []
    agent, _ = effective_agent(request, current_user, agent_id, strict=False, warnings=notes)
    locator = _locator_from_query(git_remote, root_commit, root_path, repo_name, host)
    project, resolution = await _project_from_query(request, current_user, project_id, locator, hint_project)
    configured = normalize_project_id(hint_project) if hint_project and hint_project.strip() else None
    checkout = {"branch": branch, "head_commit": head_commit} if (branch or head_commit) else None
    brief = await _service(request).brief(
        user_id=current_user.user_id,
        project_id=project,
        agent_id=agent,
        recent_n=recent_n,
        inbox_limit=inbox_limit,
        allowed=current_user.project_ids,
        configured_project=configured if resolution is not None else None,
        checkout=checkout,
        extra_warnings=notes,
    )
    brief["resolution"] = resolution
    return brief


@router.get("/trail", summary="Handoffs and checkpoints across agents, newest first")
@limiter.limit("60/minute")
async def trail(
    request: Request,
    current_user: CurrentUser,
    project_id: Annotated[str | None, Query(max_length=128)] = None,
    project: Annotated[str | None, Query(max_length=128, description="Alias of project_id")] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
    agent_id: Annotated[str | None, Query(max_length=128, description="Only this agent's entries")] = None,
    before: Annotated[
        str | None,
        Query(max_length=64, description="Cursor: only entries older than this created_at (the oldest entry already shown)"),
    ] = None,
    before_id: Annotated[
        str | None,
        Query(max_length=128, description="Cursor tie-break: that entry's id (entries at the same time sort by id)"),
    ] = None,
    git_remote: Annotated[str | None, Query(max_length=2000)] = None,
    root_commit: Annotated[str | None, Query(max_length=64)] = None,
    root_path: Annotated[str | None, Query(max_length=4096)] = None,
    repo_name: Annotated[str | None, Query(max_length=200)] = None,
    host: Annotated[str | None, Query(max_length=255)] = None,
    hint_project: Annotated[str | None, Query(max_length=128)] = None,
) -> dict[str, Any]:
    """Read-only: a location the server has not seen resolves (hint first) without being recorded."""
    _require(current_user, "memory:recall")
    agent_filter = _clean_agent(agent_id, "agent_id")
    cursor = _trail_cursor(before, before_id)
    locator = _locator_from_query(git_remote, root_commit, root_path, repo_name, host)
    resolved, resolution = await _project_from_query(request, current_user, project_id or project, locator, hint_project)
    result = await _service(request).trail(
        current_user.user_id, resolved, limit=limit, offset=offset, agent_id=agent_filter, before=cursor
    )
    result["resolution"] = resolution
    return result


@router.get("/trail/summary", summary="Activity per agent and per project (handoffs + checkpoints)")
@limiter.limit("60/minute")
async def trail_summary(
    request: Request,
    current_user: CurrentUser,
    days: Annotated[int, Query(ge=1, le=90, description="Length of the daily series")] = 14,
    tz_offset_minutes: Annotated[
        int, Query(ge=-840, le=840, description="Minutes east of UTC for day boundaries (JS: -getTimezoneOffset())")
    ] = 0,
) -> dict[str, Any]:
    """Every agent and project seen in the trail, with last activity, sessions in
    the last 7 days and a daily series, plus a 7-day recap. Read-only."""
    _require(current_user, "memory:recall")
    return await _service(request).activity_summary(
        current_user.user_id,
        days=days,
        tz_offset_minutes=tz_offset_minutes,
        allowed=current_user.project_ids or None,
    )
