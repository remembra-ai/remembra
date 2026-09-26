"""Crew core API (WP-8, spec §6 "Crew", per-agent page, batons).

- ``POST   /api/v1/crews/resolve``                      project id or location -> crew, or 404 (read-only; never creates)
- ``GET    /api/v1/crews``                              the caller's crews with counts (Site Board, §9.2)
- ``GET    /api/v1/crews/{crew_id}``                    crew, role, permissions and settings (ETag = settings version)
- ``PATCH  /api/v1/crews/{crew_id}``                    settings patch (H, step-up, If-Match)
- ``GET    /api/v1/crews/{crew_id}/snapshot``           consistent snapshot (§4.4), 304 on If-None-Match
- ``GET    /api/v1/crews/{crew_id}/events``             polling fallback since_seq (§4.4), 304 on If-None-Match
- ``POST   /api/v1/crews/{crew_id}/events``             crewd's client events (§4.2 whitelist; session token)
- ``GET    /api/v1/crews/{crew_id}/members``            members
- ``POST   /api/v1/crews/{crew_id}/members``            add or change a member (H)
- ``DELETE /api/v1/crews/{crew_id}/members/{user_id}``  remove a member (H)
- ``GET    /api/v1/crews/{crew_id}/agents/{agent_id}``  per-agent page data (§9.10, L0 minimal)
- ``GET    /api/v1/crews/{crew_id}/agents/{agent_id}/timeline``  last 20 sessions in a window (L0)
- ``GET    /api/v1/crews/{crew_id}/batons``             baton passes (?task_id=)
- ``POST   /api/v1/crews/{crew_id}/batons/{id}/restore`` the adopter's crewd reports the baton restore (session token)

Every crew route resolves its crew through ``crew_access`` (``load_crew``): 404
for anything the caller cannot see, 403 for a missing permission, and (H)
routes accept only a dashboard login with crew role owner/admin (D27).
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, Response, status
from pydantic import BaseModel, ConfigDict, Field

from remembra.auth.middleware import AuthenticatedUser, CurrentUser, resolve_project_access
from remembra.client.project import normalize_project_id
from remembra.core.limiter import limiter
from remembra.crew import schemas
from remembra.crew.access import (
    CrewAccess,
    crew_access,
    crew_error,
    effective_permissions,
    key_permissions,
    not_found,
    project_visible,
    require_same_crew,
)
from remembra.crew.core import CrewCore
from remembra.crew.events import (
    Actor,
    CrewEventLog,
    IdempotencyConflict,
    etag_matches,
    events_page,
    idem_lookup,
    idem_store,
)
from remembra.crew.limits import crew_limits_for_owner, enforce_rate_limit
from remembra.crew.settings import SettingsError
from remembra.crew.store import CrewStore, CrewStoreError, NotFound, PreconditionFailed, now_iso
from remembra.relay.identity import ProjectLocator
from remembra.services.relay import BindingNotAllowed, ProjectAccessDenied, RelayService

router = APIRouter(tags=["crew"])

READ_LIMIT = "120/minute"
WRITE_LIMIT = "30/minute"
AGENT_HEADER = "X-Remembra-Agent-Id"
_AGENT_RE = re.compile(schemas.AGENT_ID_PATTERN)
MEMBER_ROLES_BY_ADMIN = frozenset({"member", "viewer"})


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _crew_db(request: Request) -> Any:
    db = getattr(request.app.state, "crew_db", None)
    if db is None:
        raise crew_error(status.HTTP_503_SERVICE_UNAVAILABLE, "crew_unavailable", "Crew mode is not available on this server.")
    return db


def _events(request: Request) -> CrewEventLog:
    events: CrewEventLog | None = getattr(request.app.state, "crew_events", None)
    if events is None:
        # The event log is set by the crew.bus startup hook; without it no mutation may run
        # (every mutation writes its event in the same transaction, §4.3).
        raise crew_error(status.HTTP_503_SERVICE_UNAVAILABLE, "crew_unavailable", "Crew mode is not available on this server.")
    return events


def _require_crew_read(user: AuthenticatedUser) -> None:
    if "crew:read" not in key_permissions(user):
        raise crew_error(status.HTTP_403_FORBIDDEN, "forbidden", "Insufficient permissions. Required: crew:read")


def _session_key(request: Request, access: CrewAccess) -> str | None:
    """Per-session rate-limit key (§11): agents (API keys) per key + agent + crew; dashboard logins are not limited here."""
    if access.human:
        return None
    agent = request.headers.get(AGENT_HEADER) or access.user.agent_id or ""
    return f"{access.user.api_key_id}:{agent}:{access.crew_id}"


def _parse_if_match(value: str | None) -> int:
    if value is None or not value.strip():
        raise crew_error(
            status.HTTP_428_PRECONDITION_REQUIRED,
            "if_match_required",
            "Send If-Match with the crew's current settings_version (the ETag of GET /crews/{id}).",
        )
    raw = value.strip().removeprefix("W/").strip('"')
    if not raw.isdigit():
        raise crew_error(status.HTTP_412_PRECONDITION_FAILED, "version_mismatch", "If-Match must be a settings version.")
    return int(raw)


def _version_etag(version: int) -> str:
    return f'"{int(version)}"'


async def _audit(request: Request, access: CrewAccess, action: str, resource_id: str) -> None:
    """Human-only crew actions go to ``audit_log`` (§11.2; never retention-deleted)."""
    from remembra.security.audit import AuditLogger

    db = getattr(request.app.state, "db", None)
    if db is None:
        return
    await db.log_audit_event(
        audit_id=AuditLogger.generate_audit_id(),
        user_id=access.user.user_id,
        action=action,
        api_key_id=access.user.api_key_id,
        resource_id=resource_id,
        ip_address=request.client.host if request.client else None,
        success=True,
        error_message=None,
    )


IDEMPOTENCY_HEADER = "Idempotency-Key"


def _idem_key(request: Request) -> str | None:
    key = (request.headers.get(IDEMPOTENCY_HEADER) or "").strip()
    if not key:
        return None
    if len(key) > 128:
        raise crew_error(422, "validation_error", "Idempotency-Key is longer than 128 characters.")
    return key


def _principal(access: CrewAccess) -> str:
    return f"user:{access.user.user_id}:{access.user.api_key_id}"


async def _idem_replay(request: Request, access: CrewAccess, route: str, body: Any) -> dict[str, Any] | None:
    """The stored response of a repeated mutation (same ``Idempotency-Key``), or None (§4.3)."""
    key = _idem_key(request)
    if key is None:
        return None
    try:
        return await idem_lookup(_crew_db(request).conn, principal=_principal(access), route=route, key=key, body=body)
    except IdempotencyConflict as e:
        raise crew_error(422, "idempotency_conflict", str(e)) from e


async def _idem_remember(request: Request, access: CrewAccess, route: str, body: Any, response: dict[str, Any]) -> None:
    """Store the response for replay; call inside the crew transaction that made the change."""
    key = _idem_key(request)
    if key is not None:
        await idem_store(_crew_db(request).conn, principal=_principal(access), route=route, key=key, body=body, response=response)


def _access_body(access: CrewAccess) -> dict[str, Any]:
    return {"role": access.role, "permissions": sorted(access.permissions), "human": access.human}


# ---------------------------------------------------------------------------
# Resolve (read-only) and list
# ---------------------------------------------------------------------------


class CrewResolveRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project_id: str | None = Field(default=None, max_length=128)
    git_remote: str | None = Field(default=None, max_length=2000)
    root_commit: str | None = Field(default=None, max_length=64)
    root_path: str | None = Field(default=None, max_length=4096)
    repo_name: str | None = Field(default=None, max_length=200)
    host: str | None = Field(default=None, max_length=255)
    hint_project: str | None = Field(default=None, max_length=128)


@router.post("/crews/resolve", summary="Resolve a crew by project id or location (read-only; never creates)")
@limiter.limit(READ_LIMIT)
async def resolve_crew(request: Request, body: CrewResolveRequest, current_user: CurrentUser) -> dict[str, Any]:
    """The crew for a project (``project_id``) or a location (git remote / root commit / path).

    Read-only (§2): an unseen location is resolved without being recorded, and a
    project with no crew is a 404 (only ``join`` by a key allowed on the project
    creates one). Projects outside a restricted key's list are also a 404.
    """
    _require_crew_read(current_user)
    db = _crew_db(request)
    resolution: dict[str, Any] | None = None
    if body.project_id and body.project_id.strip():
        requested: str | None = normalize_project_id(body.project_id)
    else:
        locator = ProjectLocator(
            git_remote=body.git_remote,
            root_commit=body.root_commit,
            root_path=body.root_path,
            repo_name=body.repo_name,
            host=body.host,
        )
        if locator.is_empty():
            raise crew_error(422, "validation_error", "Send project_id or a location (git_remote, root_commit or root_path).")
        try:
            resolution = await RelayService(db=request.app.state.db).registry.resolve(
                user_id=current_user.user_id,
                locator=locator,
                hint_project=body.hint_project,
                bind=False,
                create=False,
                allowed_projects=current_user.project_ids,
            )
        except (ProjectAccessDenied, BindingNotAllowed):
            raise not_found() from None
        except ValueError as e:
            raise crew_error(422, "validation_error", str(e)) from e
        requested = resolution["project_id"]
    try:
        project = resolve_project_access(current_user, requested)
    except HTTPException as e:
        if e.status_code == status.HTTP_403_FORBIDDEN:
            raise not_found() from None  # no oracle for projects outside the key's list
        raise
    if not project or not project_visible(current_user, project):
        raise not_found()
    core = CrewCore(db)
    crews = await core.crews_for_project(current_user.user_id, project)
    if not crews:
        raise not_found()
    crew = crews[0]
    role = crew.get("role") or "owner"
    perms = effective_permissions(current_user, role)
    if "crew:read" not in perms:
        raise not_found()
    return {
        "crew": await core.crew_view(crew),
        "project_id": project,
        "role": role,
        "permissions": sorted(perms),
        "resolution": resolution,
    }


@router.get("/crews", summary="The caller's crews with live counts and task progress")
@limiter.limit(READ_LIMIT)
async def list_crews(request: Request, current_user: CurrentUser) -> dict[str, Any]:
    _require_crew_read(current_user)
    db = _crew_db(request)
    store = CrewStore(db)
    rows = await store.list_crews_for_user(current_user.user_id, current_user.project_ids or None)
    rows = [r for r in rows if "crew:read" in effective_permissions(current_user, r.get("role") or "viewer")]
    items = await CrewCore(db).list_summaries(rows)
    # Needs-you first, then live, then idle (§9.2).
    items.sort(key=lambda c: (-int(c["needs_you"] > 0), -int(c["live"] > 0), c["crew"]["project_id"]))
    return {"crews": items, "count": len(items)}


# ---------------------------------------------------------------------------
# One crew, settings
# ---------------------------------------------------------------------------


@router.get("/crews/{crew_id}", summary="A crew: view, role, permissions and settings")
@limiter.limit(READ_LIMIT)
async def get_crew(
    request: Request, response: Response, access: CrewAccess = Depends(crew_access("crew:read"))
) -> dict[str, Any]:
    db = _crew_db(request)
    core = CrewCore(db)
    row = await core.crew_row(access.crew_id)
    if row is None:
        raise not_found()
    settings, version = await CrewStore(db).get_settings(access.crew_id)
    members = await CrewStore(db).list_members(access.crew_id)
    response.headers["ETag"] = _version_etag(version)
    return {
        "crew": await core.crew_view(row),
        **_access_body(access),
        "settings": settings,
        "settings_version": version,
        "members": len(members),
        "created_at": row["created_at"],
    }


class CrewPatchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    settings: dict[str, Any] = Field(..., description="Partial settings; nested objects merge key by key")


@router.patch("/crews/{crew_id}", summary="Patch crew settings (human only, step-up, If-Match)")
@limiter.limit(WRITE_LIMIT)
async def patch_crew(
    request: Request,
    response: Response,
    body: CrewPatchRequest,
    access: CrewAccess = Depends(crew_access("crew:admin", step_up=True)),
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
) -> dict[str, Any]:
    """Human principal only (D27) with a login in the last 15 minutes. ``If-Match`` is the
    ``settings_version`` (412 on mismatch). Emits ``crew.settings_changed`` (a moment) and an
    audit row. ``require_report_for_done`` cannot be turned off (422)."""
    expected = _parse_if_match(if_match)
    events = _events(request)
    store = CrewStore(_crew_db(request))
    core = CrewCore(store.db)
    route = f"PATCH /crews/{access.crew_id}"
    request_body = {"if_match": expected, "settings": body.settings}
    replay = await _idem_replay(request, access, route, request_body)
    if replay is not None:
        response.headers["ETag"] = _version_etag(int(replay["settings_version"]))
        return replay
    seq: int | None = None
    try:
        async with events.transaction() as tx:
            settings, version, changed = await store.patch_settings(access.crew_id, body.settings, if_match=expected)
            if changed:
                result = await tx.emit(
                    crew_id=access.crew_id,
                    type="crew.settings_changed",
                    actor=Actor.human(access.user.user_id),
                    payload={
                        "settings_version": version,
                        "changed_keys": changed[:40],
                        "enforcement": settings.get("enforcement") if "enforcement" in changed else None,
                    },
                    summary=f"crew settings v{version} changed by a human: {', '.join(changed[:8])}"[:300],
                )
                seq = result.seq
            row = await core.crew_row(access.crew_id)
            if row is None:
                raise NotFound(access.crew_id)
            out = {
                "crew": await core.crew_view(row),
                "settings": settings,
                "settings_version": version,
                "changed_keys": changed,
                "seq": seq,
            }
            await _idem_remember(request, access, route, request_body, out)
    except PreconditionFailed as e:
        raise crew_error(
            status.HTTP_412_PRECONDITION_FAILED,
            "version_mismatch",
            f"The settings changed; current version is {e.current_version}. Reload and retry.",
            current_version=e.current_version,
        ) from e
    except SettingsError as e:
        raise crew_error(422, "invalid_settings", str(e)[:500], errors=e.errors) from e
    except NotFound:
        raise not_found() from None
    if changed:
        await _audit(request, access, "crew.settings_changed", access.crew_id)
    response.headers["ETag"] = _version_etag(version)
    return out


# ---------------------------------------------------------------------------
# Snapshot and events polling (§4.4)
# ---------------------------------------------------------------------------


@router.get("/crews/{crew_id}/snapshot", summary="Crew snapshot (304 on If-None-Match)")
@limiter.limit(READ_LIMIT)
async def crew_snapshot(
    request: Request,
    response: Response,
    access: CrewAccess = Depends(crew_access("crew:read")),
    if_none_match: Annotated[str | None, Header(alias="If-None-Match")] = None,
) -> Any:
    enforce_rate_limit("snapshot", session_token=_session_key(request, access))
    try:
        body = await CrewCore(_crew_db(request)).snapshot(access.crew_id)
    except LookupError:
        raise not_found() from None
    etag = body["etag"]
    if if_none_match and etag in {t.strip().removeprefix("W/") for t in if_none_match.split(",")}:
        return Response(status_code=status.HTTP_304_NOT_MODIFIED, headers={"ETag": etag})
    response.headers["ETag"] = etag
    return body


@router.get("/crews/{crew_id}/events", summary="Events since_seq (polling fallback; 304 on If-None-Match)")
@limiter.limit(READ_LIMIT)
async def crew_events(
    request: Request,
    response: Response,
    access: CrewAccess = Depends(crew_access("crew:read")),
    since_seq: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=200)] = 200,
    if_none_match: Annotated[str | None, Header(alias="If-None-Match")] = None,
) -> Any:
    """Envelopes with ``seq > since_seq`` in seq order (≤200). The ETag is the crew's ``last_seq``:
    ``If-None-Match`` equal to it answers 304. ``has_more`` asks the client to poll again at once."""
    enforce_rate_limit("events_poll", session_token=_session_key(request, access))
    conn = _crew_db(request).conn
    page = await events_page(conn, access.crew_id, since_seq=since_seq, limit=limit, if_none_match=if_none_match)
    if page.not_modified or (if_none_match and etag_matches(if_none_match, page.last_seq)):
        return Response(status_code=status.HTTP_304_NOT_MODIFIED, headers={"ETag": page.etag})
    response.headers["ETag"] = page.etag
    return {"crew_id": access.crew_id, "events": page.events, "last_seq": page.last_seq, "has_more": page.has_more}


SESSION_TOKEN_HEADER = "X-Remembra-Crew-Session"  # the one session-token header of every crew router


@router.post("/crews/{crew_id}/events", summary="Submit client events (whitelist only; crew session token)")
async def submit_crew_events(request: Request, access: CrewAccess = Depends(crew_access("crew:write"))) -> dict[str, Any]:
    """crewd's client events (§4.2 whitelist: ``activity.*``, ``guard.blocked``, ``guard.tamper_blocked``,
    ``gate.*``, ``githook.missing``). The actor is the session proven by the session token, never the body;
    each item gets ``accepted``/``duplicate``/``coalesced``/``rejected`` (its ``id`` is the idempotency key)."""
    from remembra.crew.events import EventValidationError, ingest_client_events
    from remembra.crew.sessions import session_actor
    from remembra.crew.tasks import session_for_token

    token = (request.headers.get(SESSION_TOKEN_HEADER) or "").strip()
    if not token:
        raise crew_error(403, "session_required", f"Send the crew session token in {SESSION_TOKEN_HEADER}.")
    events = _events(request)
    session = await session_for_token(events.db.conn, access.crew_id, token)
    if session is None or session["user_id"] != access.user.user_id or session["state"] == "ended":
        raise crew_error(401, "invalid_session_token", "The crew session token is not valid for this crew.")
    agent = getattr(access.user, "agent_id", None)
    if agent and session["agent_id"] != agent:
        raise crew_error(403, "agent_mismatch", "This agent-scoped key cannot act as another agent's session.")
    enforce_rate_limit("events", user_id=access.user.user_id, session_token=token)
    try:
        body = await request.json()
    except ValueError:
        raise crew_error(422, "invalid_body", "The body is not valid JSON.") from None
    items = body.get("events") if isinstance(body, dict) else None
    if not isinstance(items, list):
        raise crew_error(422, "invalid_events", "$.events: must be a list")
    try:
        results = await ingest_client_events(events, crew_id=access.crew_id, actor=session_actor(session), items=items)
    except EventValidationError as e:
        raise crew_error(422, "invalid_events", "; ".join(e.errors[:5])[:500], errors=list(e.errors[:10])) from e
    seqs = [r.seq for r in results if r.seq is not None]
    return {"results": [r.to_dict() for r in results], "seq": max(seqs) if seqs else None}


async def _token_session(request: Request, access: CrewAccess) -> dict[str, Any]:
    """The crew session proven by the ``X-Remembra-Crew-Session`` token (same rules as client events)."""
    from remembra.crew.tasks import session_for_token

    token = (request.headers.get(SESSION_TOKEN_HEADER) or "").strip()
    if not token:
        raise crew_error(403, "session_required", f"Send the crew session token in {SESSION_TOKEN_HEADER}.")
    session = await session_for_token(_crew_db(request).conn, access.crew_id, token)
    if session is None or session["user_id"] != access.user.user_id or session["state"] == "ended":
        raise crew_error(401, "invalid_session_token", "The crew session token is not valid for this crew.")
    agent = getattr(access.user, "agent_id", None)
    if agent and session["agent_id"] != agent:
        raise crew_error(403, "agent_mismatch", "This agent-scoped key cannot act as another agent's session.")
    enforce_rate_limit("events", user_id=access.user.user_id, session_token=token)
    return session


@router.post("/crews/{crew_id}/batons/{baton_id}/restore", summary="Report the baton restore (adopter's crew session token)")
async def report_baton_restore(
    request: Request, baton_id: str, access: CrewAccess = Depends(crew_access("crew:write"))
) -> dict[str, Any]:
    """crewd of the session the baton passed to reports restoring its ref into the checkout (D30, §13.3
    step 7): sets ``crew_batons.restored`` and emits ``baton.restored``; a failed restore opens a
    Needs-you item. Only the adopting session may report, once (a repeat returns the recorded outcome)."""
    from remembra.crew.tasks import BatonRestoreError, record_baton_restore

    session = await _token_session(request, access)
    try:
        body = await request.json()
    except ValueError:
        raise crew_error(422, "invalid_body", "The body is not valid JSON.") from None
    errors = schemas.validate(body, schemas.REQUEST_SHAPES["BatonRestore"])
    if errors:
        raise crew_error(422, "invalid_body", "; ".join(errors[:5])[:500])
    try:
        return await record_baton_restore(
            _events(request),
            access.crew_id,
            session,
            baton_id,
            restored=bool(body["restored"]),
            status=str(body["status"]),
            files=int(body["files"]),
        )
    except BatonRestoreError as e:
        if e.status == 404:
            raise not_found() from None
        raise crew_error(e.status, e.code, e.message) from None


# ---------------------------------------------------------------------------
# Members
# ---------------------------------------------------------------------------


@router.get("/crews/{crew_id}/members", summary="Crew members")
@limiter.limit(READ_LIMIT)
async def list_members(request: Request, access: CrewAccess = Depends(crew_access("crew:read"))) -> dict[str, Any]:
    rows = await CrewStore(_crew_db(request)).list_members(access.crew_id)
    members = [
        {
            "user_id": r["user_id"],
            "role": r["role"],
            "added_by": r.get("added_by"),
            "added_at": r["added_at"],
            "is_crew_owner": r["user_id"] == access.crew.owner_user_id,
        }
        for r in rows
    ]
    return {"crew_id": access.crew_id, "members": members, "count": len(members)}


class MemberRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    user_id: str = Field(..., min_length=1, max_length=128)
    role: str = Field(..., description="owner | admin | member | viewer")


def _check_role_change(access: CrewAccess, role: str, current: str | None) -> None:
    if role not in schemas.CREW_ROLES:
        raise crew_error(422, "validation_error", f"role must be one of {', '.join(schemas.CREW_ROLES)}")
    if access.role != "owner" and (role not in MEMBER_ROLES_BY_ADMIN or current in ("owner", "admin")):
        raise crew_error(
            status.HTTP_403_FORBIDDEN, "crew_role_required", "Only a crew owner can grant or change owner and admin roles."
        )


async def _shares_team(main_db: Any, owner_user_id: str, user_id: str, team_id: str | None) -> bool:
    """Has ``user_id`` joined a team the crew owner is in (the crew's own team, when it has one)?

    Team membership is only ever created by accepting a team invite (or creating the team),
    so it is the invitee's consent to working with the owner.
    """
    sql = "SELECT 1 FROM team_members a JOIN team_members b ON a.team_id = b.team_id WHERE a.user_id = ? AND b.user_id = ?"
    params: list[Any] = [owner_user_id, user_id]
    if team_id:
        sql += " AND a.team_id = ?"
        params.append(team_id)
    try:
        cursor = await main_db.conn.execute(sql + " LIMIT 1", params)
        return await cursor.fetchone() is not None
    except Exception:  # no teams tables: nobody is a teammate
        return False


async def _check_new_teammate(request: Request, access: CrewAccess, main_db: Any, user_id: str) -> None:
    """A new crew member needs the owner's plan to include teammates and the member's consent (a shared team)."""
    limits = await crew_limits_for_owner(getattr(request.app.state, "usage_meter", None), access.crew.owner_user_id)
    if not limits.teammates:
        raise crew_error(
            status.HTTP_402_PAYMENT_REQUIRED,
            "plan_required",
            "Crew teammates come with the Team plan; this crew's owner is on a plan without them.",
        )
    if not await _shares_team(main_db, access.crew.owner_user_id, user_id, access.crew.team_id):
        raise crew_error(
            status.HTTP_403_FORBIDDEN,
            "not_a_teammate",
            "Add someone who has joined your team: invite them to the team first; they become a crew member after they accept.",
        )


@router.post("/crews/{crew_id}/members", summary="Add a member or change a role (human only)")
@limiter.limit(WRITE_LIMIT)
async def add_member(
    request: Request, body: MemberRequest, access: CrewAccess = Depends(crew_access("crew:admin"))
) -> dict[str, Any]:
    store = CrewStore(_crew_db(request))
    route = f"POST /crews/{access.crew_id}/members"
    request_body = body.model_dump()
    replay = await _idem_replay(request, access, route, request_body)
    if replay is not None:
        return replay
    current = await store.get_member_role(access.crew_id, body.user_id)
    _check_role_change(access, body.role, current)
    main_db = getattr(request.app.state, "db", None)
    if main_db is None or await main_db.get_user_by_id(body.user_id) is None:
        raise crew_error(422, "unknown_user", "No account with that user id.")
    if body.user_id == access.crew.owner_user_id and body.role != "owner":
        raise crew_error(status.HTTP_409_CONFLICT, "crew_owner", "The crew's owner keeps the owner role.")
    if current is None and body.user_id != access.crew.owner_user_id:
        await _check_new_teammate(request, access, main_db, body.user_id)
    try:
        async with store.db.transaction():
            row = await store.set_member(access.crew_id, body.user_id, body.role, access.user.user_id)
            out = {
                "crew_id": access.crew_id,
                "member": {k: row[k] for k in ("user_id", "role", "added_by", "added_at")},
                "previous_role": current,
            }
            await _idem_remember(request, access, route, request_body, out)
    except CrewStoreError as e:
        raise crew_error(status.HTTP_409_CONFLICT, "last_owner", str(e)) from e
    action = "crew.member_added" if current is None else "crew.member_role_changed"
    await _audit(request, access, action, f"{access.crew_id}:{body.user_id}")
    if current is not None and current != body.role:
        await _revoke_ws(body.user_id, access.crew_id, "crew role changed")
    return out


@router.delete("/crews/{crew_id}/members/{user_id}", summary="Remove a member (human only)")
@limiter.limit(WRITE_LIMIT)
async def remove_member(
    request: Request, user_id: str, access: CrewAccess = Depends(crew_access("crew:admin"))
) -> dict[str, Any]:
    store = CrewStore(_crew_db(request))
    route = f"DELETE /crews/{access.crew_id}/members/{user_id}"
    replay = await _idem_replay(request, access, route, {})
    if replay is not None:
        return replay
    current = await store.get_member_role(access.crew_id, user_id)
    if current is None:
        raise not_found()
    if user_id == access.crew.owner_user_id:
        raise crew_error(status.HTTP_409_CONFLICT, "crew_owner", "The crew's owner cannot be removed.")
    if access.role != "owner" and current in ("owner", "admin"):
        raise crew_error(status.HTTP_403_FORBIDDEN, "crew_role_required", "Only a crew owner can remove an owner or admin.")
    out = {"crew_id": access.crew_id, "removed": user_id}
    try:
        async with store.db.transaction():
            removed = await store.remove_member(access.crew_id, user_id)
            if removed:
                await _idem_remember(request, access, route, {}, out)
    except CrewStoreError as e:
        raise crew_error(status.HTTP_409_CONFLICT, "last_owner", str(e)) from e
    if not removed:
        raise not_found()
    await _audit(request, access, "crew.member_removed", f"{access.crew_id}:{user_id}")
    await _revoke_ws(user_id, access.crew_id, "removed from crew")
    return out


async def _revoke_ws(user_id: str, crew_id: str, reason: str) -> int:
    """Membership changes close that user's crew sockets at once (4003, §4.4); they re-subscribe under the new ACL."""
    from remembra.api.v1.websocket import connection_manager

    return await connection_manager.revoke(user_id=user_id, crew_id=crew_id, reason=reason)


# ---------------------------------------------------------------------------
# Per-agent page, timeline, batons
# ---------------------------------------------------------------------------


def _agent(agent_id: str) -> str:
    if not _AGENT_RE.fullmatch(agent_id):
        raise not_found()
    return agent_id


def _window_bound(value: str | None, name: str) -> str | None:
    if value is None or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        raise crew_error(422, "validation_error", f"{name} must be an ISO 8601 time") from None
    return now_iso(parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC))


@router.get("/crews/{crew_id}/agents/{agent_id}", summary="Per-agent page data (L0 minimal)")
@limiter.limit(READ_LIMIT)
async def agent_page(request: Request, agent_id: str, access: CrewAccess = Depends(crew_access("crew:read"))) -> dict[str, Any]:
    """Current sessions, the last 20 sessions (start and end reason), checkpoints, batons in and
    out with the brief each adopter received, current claims and tasks, and enforcement layers."""
    page = await CrewCore(_crew_db(request)).agent_page(access.crew_id, _agent(agent_id))
    if page is None:
        raise not_found()
    return page


@router.get("/crews/{crew_id}/agents/{agent_id}/timeline", summary="Agent timeline (L0: last 20 sessions)")
@limiter.limit(READ_LIMIT)
async def agent_timeline(
    request: Request,
    agent_id: str,
    access: CrewAccess = Depends(crew_access("crew:read")),
    from_: Annotated[str | None, Query(alias="from", max_length=64)] = None,
    to: Annotated[str | None, Query(max_length=64)] = None,
) -> dict[str, Any]:
    timeline = await CrewCore(_crew_db(request)).agent_timeline(
        access.crew_id, _agent(agent_id), since=_window_bound(from_, "from"), until=_window_bound(to, "to")
    )
    if timeline is None:
        raise not_found()
    return timeline


@router.get("/crews/{crew_id}/batons", summary="Baton passes (?task_id=)")
@limiter.limit(READ_LIMIT)
async def list_batons(
    request: Request,
    access: CrewAccess = Depends(crew_access("crew:read")),
    task_id: Annotated[str | None, Query(max_length=80)] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
) -> dict[str, Any]:
    db = _crew_db(request)
    if task_id is not None:
        await require_same_crew(db.conn, access.crew_id, [("task", task_id)])
    batons = await CrewCore(db).batons(access.crew_id, task_id=task_id, limit=limit)
    return {"crew_id": access.crew_id, "task_id": task_id, "batons": batons, "count": len(batons)}
