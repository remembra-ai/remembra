"""Crew access control: the human principal, step-up, and crew / entity loading.

Spec anchors: D27 (who is human), §3.1 (permissions), §5.9 (human override),
§6 (access rule for every route), §11.2 (tenancy controls).

Every crew route resolves its crew through exactly one of:

* :func:`load_crew` for ``/crews/{crew_id}/…`` routes, and
* :func:`load_crew_entity` for entity routes (``/claims/{id}``, ``/tasks/{id}``, …).

Both return **404** (never 403) when the caller cannot see the crew: unknown or
malformed id, not a member, or a project-restricted key whose allow-list does not
contain the crew's project. The same generic body is used for every case, so a
guessed id reveals nothing. Once the crew is visible, a missing permission is a
**403** and a stale login on a step-up action is a **401** ``step_up_required``.

Human principal (D27): a request authenticated by a dashboard login
(``api_key_id == "jwt_auth"``); ``dev_key`` (auth disabled) counts only outside
production. API keys are never human, whatever their role. ``crew:override`` and
``crew:admin`` are granted per request, only to a human holding the crew role
``owner`` or ``admin``; no API-key role carries them (``auth.rbac``).

Step-up: the only credential that authenticates a dashboard login is the JWT
issued by ``POST /auth/login`` (``UserManager.authenticate``); there is no refresh
or silent re-issue path, so the token's ``iat_ms`` (``iat`` for older tokens) *is*
the login time. A step-up action needs that login to be at most 15 minutes old;
otherwise the dashboard re-authenticates (logs in again) and retries.

The functions here take the ``crew.db`` connection explicitly (anything with an
aiosqlite-style ``await conn.execute(sql, params)`` returning a cursor), so the
WebSocket layer can call them without a ``Request``. The FastAPI dependencies
read it from ``app.state.crew_db`` (a connection, or an object with ``.conn``).
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Final

import aiosqlite
import jwt
from fastapi import Depends, HTTPException, Request, status
from fastapi.routing import APIRoute

from remembra.auth.middleware import AuthenticatedUser, get_current_user
from remembra.auth.rbac import SYNTHETIC_KEY_IDS, KeyRole, Permission, Role
from remembra.config import get_settings
from remembra.crew.schemas import CREW_ROLES, HUMAN_ONLY_PERMISSIONS, PERMISSIONS, ROUTES, Route, is_id

HUMAN_CREDENTIAL_ID: Final = "jwt_auth"
DEV_CREDENTIAL_ID: Final = "dev_key"

# Step-up window (§5.9, §11.2): login within the last 15 minutes.
STEP_UP_MAX_AGE_S: Final = 15 * 60
# A token "issued" slightly in the future is tolerated (clock skew between workers).
_FUTURE_SKEW_MS: Final = 60_000

# What each crew role allows (intersected with what the credential allows).
CREW_ROLE_PERMISSIONS: Final[Mapping[str, frozenset[str]]] = {
    "owner": frozenset(PERMISSIONS),
    "admin": frozenset(PERMISSIONS),
    "member": frozenset({"crew:read", "crew:write", "crew:claim"}),
    "viewer": frozenset({"crew:read"}),
}
HUMAN_CREW_ROLES: Final = frozenset({"owner", "admin"})

# load_crew_entity kind → crew.db table (spec §3.2). Every table has ``id`` and ``crew_id``.
ENTITY_TABLES: Final[Mapping[str, str]] = {
    "session": "crew_sessions",
    "zone": "crew_zones",
    "zone_change": "crew_zone_changes",
    "claim": "crew_claims",
    "collision": "crew_collisions",
    "task": "crew_tasks",
    "message": "crew_messages",
    "decision": "crew_decisions",
    "inbox_item": "crew_inbox_items",
    "proposal": "crew_proposals",
    "checkpoint": "crew_checkpoints",
    "report": "crew_reports",
    "baton": "crew_batons",
    "offer": "crew_baton_offers",
    "bypass_code": "crew_bypass_codes",
}
# Path parameter carrying the entity id when it is not ``<kind>_id`` (see schemas.ROUTES).
ENTITY_PATH_PARAMS: Final[Mapping[str, str]] = {"zone_change": "change_id", "inbox_item": "item_id"}

NOT_FOUND_BODY: Final[Mapping[str, str]] = {"error": "not_found", "message": "Not found."}

assert set(CREW_ROLE_PERMISSIONS) == set(CREW_ROLES)
assert {p.value for p in Permission if p.value.startswith("crew:")} == set(PERMISSIONS)


# ---------------------------------------------------------------------------
# Errors (CrewError body: {error, message, ...})
# ---------------------------------------------------------------------------


def crew_error(
    status_code: int, error: str, message: str, *, headers: Mapping[str, str] | None = None, **extra: Any
) -> HTTPException:
    """An ``HTTPException`` whose detail is a ``CrewError`` body (schemas.ERROR_BODY)."""
    body: dict[str, Any] = {"error": error, "message": message}
    body.update({k: v for k, v in extra.items() if v is not None})
    return HTTPException(status_code=status_code, detail=body, headers=dict(headers) if headers else None)


def not_found() -> HTTPException:
    """The single 404 used for every "cannot see it" case (no existence oracle)."""
    return crew_error(status.HTTP_404_NOT_FOUND, NOT_FOUND_BODY["error"], NOT_FOUND_BODY["message"])


# ---------------------------------------------------------------------------
# Principal
# ---------------------------------------------------------------------------


def _is_production() -> bool:
    return not bool(get_settings().debug)


def is_human(user: AuthenticatedUser) -> bool:
    """True only for a dashboard login (JWT), or the dev principal outside production (D27)."""
    if user.api_key_id == HUMAN_CREDENTIAL_ID:
        return True
    if user.api_key_id == DEV_CREDENTIAL_ID:
        return not _is_production()
    return False


def key_permissions(user: AuthenticatedUser) -> frozenset[str]:
    """Crew permissions the credential itself allows (before the crew role is applied).

    API keys use their RBAC role and scope whitelist (``AuthenticatedUser.role`` /
    ``.scopes`` come from ``api_key_roles``). JWT and dev sessions are not API keys
    and get the editor set, exactly as ``auth.scopes`` treats them.
    """
    if user.api_key_id in SYNTHETIC_KEY_IDS:
        key_role = KeyRole(api_key_id=user.api_key_id, role=Role.EDITOR)
    else:
        try:
            role = Role(user.role or Role.EDITOR.value)
        except ValueError:
            role = Role.VIEWER  # unknown role string: least privilege
        key_role = KeyRole(api_key_id=user.api_key_id, role=role, scopes=list(user.scopes or []))
    return frozenset(p.value for p in key_role.permissions if p.value.startswith("crew:"))


def effective_permissions(user: AuthenticatedUser, crew_role: str) -> frozenset[str]:
    """Credential permissions ∩ crew-role permissions, plus the human-only ones for a human owner/admin."""
    role_perms = CREW_ROLE_PERMISSIONS.get(crew_role, frozenset())
    perms = set(key_permissions(user) & role_perms)
    if is_human(user) and crew_role in HUMAN_CREW_ROLES:
        perms |= set(HUMAN_ONLY_PERMISSIONS) & role_perms
    return frozenset(perms)


def project_visible(user: AuthenticatedUser, project_id: str) -> bool:
    """A project-restricted key sees only its own projects (§11.2)."""
    return not user.project_ids or project_id in user.project_ids


def jwt_auth_time_ms(request: Request, user: AuthenticatedUser) -> int | None:
    """Login time (epoch ms) of the dashboard JWT that authenticated ``request``, else None.

    The token was already fully validated by ``get_current_user`` (signature, expiry,
    blacklist, ``tokens_valid_after``); this only reads its issue time, and returns
    None for anything that is not the same user's valid access token.
    """
    if user.api_key_id != HUMAN_CREDENTIAL_ID:
        return None
    header = request.headers.get("Authorization") or ""
    if not header.startswith("Bearer "):
        return None
    from remembra.auth.users import JWT_ALGORITHM
    from remembra.security.state import token_issued_at_ms

    try:
        payload = jwt.decode(header[7:].strip(), get_settings().jwt_secret, algorithms=[JWT_ALGORITHM])
    except jwt.InvalidTokenError:
        return None
    if payload.get("type") != "access" or payload.get("sub") != user.user_id:
        return None
    issued = token_issued_at_ms(payload)
    return issued or None


def login_is_fresh(user: AuthenticatedUser, auth_time_ms: int | None, *, now_ms: int | None = None) -> bool:
    """True when the human logged in within :data:`STEP_UP_MAX_AGE_S` (dev principal: always)."""
    if not is_human(user):
        return False
    if user.api_key_id == DEV_CREDENTIAL_ID:
        return True  # auth disabled outside production: there is no login to repeat
    if not auth_time_ms:
        return False
    now = int(time.time() * 1000) if now_ms is None else now_ms
    if auth_time_ms > now + _FUTURE_SKEW_MS:
        return False
    return now - auth_time_ms <= STEP_UP_MAX_AGE_S * 1000


def require_human(
    user: AuthenticatedUser, *, step_up: bool = False, auth_time_ms: int | None = None, now_ms: int | None = None
) -> None:
    """403 unless ``user`` is a human principal; with ``step_up``, 401 unless the login is fresh."""
    if not is_human(user):
        raise crew_error(
            status.HTTP_403_FORBIDDEN,
            "human_only",
            "This action needs a dashboard login; API keys cannot perform it.",
        )
    if step_up and not login_is_fresh(user, auth_time_ms, now_ms=now_ms):
        raise crew_error(
            status.HTTP_401_UNAUTHORIZED,
            "step_up_required",
            f"Sign in again to confirm this action (login within {STEP_UP_MAX_AGE_S // 60} minutes required).",
            headers={"WWW-Authenticate": f'Bearer error="insufficient_user_authentication", max_age="{STEP_UP_MAX_AGE_S}"'},
            max_age_s=STEP_UP_MAX_AGE_S,
        )


# ---------------------------------------------------------------------------
# Crew and entity loading
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Crew:
    id: str
    owner_user_id: str
    project_id: str
    team_id: str | None
    name: str | None
    settings: str
    settings_version: int
    last_seq: int


@dataclass(frozen=True)
class CrewAccess:
    """A resolved, authorised view of one crew for one caller."""

    crew: Crew
    user: AuthenticatedUser
    role: str  # crew role: owner | admin | member | viewer
    human: bool
    permissions: frozenset[str]
    auth_time_ms: int | None = None

    def can(self, perm: str) -> bool:
        return perm in self.permissions

    @property
    def crew_id(self) -> str:
        return self.crew.id

    @property
    def privileged(self) -> bool:
        """The (H) principal of §11.2: a human (D27) **and** crew role owner or admin.

        A dashboard login with crew role member acts as a person (messages, its own claims)
        but gets none of the human shortcuts of the agent-reachable routes.
        """
        return self.human and self.role in HUMAN_CREW_ROLES


@dataclass(frozen=True)
class CrewEntity:
    """An entity row loaded through :func:`load_crew_entity`, with the access it was loaded under."""

    access: CrewAccess
    kind: str
    id: str
    row: Mapping[str, Any] = field(default_factory=dict)


async def _fetchone(conn: Any, sql: str, params: Sequence[Any]) -> dict[str, Any] | None:
    # Keep the entire lookup under one guarded worker call. Separate execute /
    # fetch / close calls each wait behind other crews' write transactions.
    if getattr(conn, "row_factory", None) is aiosqlite.Row:
        rows = await conn.execute_fetchall(sql, tuple(params))
        return dict(rows[0]) if rows else None
    cursor = await conn.execute(sql, tuple(params))
    try:
        row = await cursor.fetchone()
        if row is None:
            return None
        names = [d[0] for d in cursor.description]
        return dict(zip(names, tuple(row), strict=True))
    finally:
        await cursor.close()


def _check_perm_name(perm: str) -> None:
    if perm not in PERMISSIONS:
        raise ValueError(f"unknown crew permission {perm!r}; expected one of {PERMISSIONS}")


async def _visible_crew(conn: Any, crew_id: Any, user: AuthenticatedUser) -> tuple[Crew, str]:
    """The crew and the caller's crew role, or the generic 404."""
    if not is_id("crew", crew_id):
        raise not_found()
    row = await _fetchone(
        conn,
        "SELECT id, owner_user_id, project_id, team_id, name, settings, settings_version, last_seq FROM crews WHERE id = ?",
        (crew_id,),
    )
    if row is None:
        raise not_found()
    crew = Crew(
        id=row["id"],
        owner_user_id=row["owner_user_id"],
        project_id=row["project_id"],
        team_id=row["team_id"],
        name=row["name"],
        settings=row["settings"] or "{}",
        settings_version=int(row["settings_version"] or 1),
        last_seq=int(row["last_seq"] or 0),
    )
    if not project_visible(user, crew.project_id):
        raise not_found()
    if crew.owner_user_id == user.user_id:
        return crew, "owner"
    member = await _fetchone(conn, "SELECT role FROM crew_members WHERE crew_id = ? AND user_id = ?", (crew.id, user.user_id))
    if member is None or member["role"] not in CREW_ROLE_PERMISSIONS:
        raise not_found()
    return crew, str(member["role"])


def _authorize(
    crew: Crew,
    crew_role: str,
    user: AuthenticatedUser,
    perm: str,
    *,
    step_up: bool,
    auth_time_ms: int | None,
    now_ms: int | None,
) -> CrewAccess:
    human = is_human(user)
    perms = effective_permissions(user, crew_role)
    if perm in HUMAN_ONLY_PERMISSIONS:
        require_human(user)
        if perm not in perms:
            raise crew_error(
                status.HTTP_403_FORBIDDEN,
                "crew_role_required",
                "This action needs the crew role owner or admin.",
            )
    elif perm not in perms:
        raise crew_error(status.HTTP_403_FORBIDDEN, "forbidden", f"Insufficient permissions. Required: {perm}")
    if step_up:
        require_human(user, step_up=True, auth_time_ms=auth_time_ms, now_ms=now_ms)
    return CrewAccess(crew=crew, user=user, role=crew_role, human=human, permissions=perms, auth_time_ms=auth_time_ms)


async def load_crew(
    conn: Any,
    crew_id: Any,
    user: AuthenticatedUser,
    perm: str,
    *,
    step_up: bool = False,
    auth_time_ms: int | None = None,
    now_ms: int | None = None,
) -> CrewAccess:
    """Resolve ``crew_id`` for ``user`` and require ``perm`` (§6).

    404 when the crew is not visible to the caller; 403 when it is visible but the
    permission is missing (human-only permissions: API keys always 403); 401
    ``step_up_required`` when ``step_up`` and the login is older than 15 minutes.
    """
    _check_perm_name(perm)
    crew, crew_role = await _visible_crew(conn, crew_id, user)
    return _authorize(crew, crew_role, user, perm, step_up=step_up, auth_time_ms=auth_time_ms, now_ms=now_ms)


async def load_crew_entity(
    conn: Any,
    kind: str,
    entity_id: Any,
    user: AuthenticatedUser,
    perm: str,
    *,
    step_up: bool = False,
    auth_time_ms: int | None = None,
    now_ms: int | None = None,
) -> CrewEntity:
    """Fetch an entity row, load its crew and apply the ACL; the same 404 for every access failure."""
    _check_perm_name(perm)
    table = ENTITY_TABLES.get(kind)
    if table is None:
        raise ValueError(f"unknown crew entity kind {kind!r}")
    if not is_id(kind, entity_id):
        raise not_found()
    row = await _fetchone(conn, f"SELECT * FROM {table} WHERE id = ?", (entity_id,))  # noqa: S608 - table from a fixed map
    if row is None:
        raise not_found()
    crew, crew_role = await _visible_crew(conn, row.get("crew_id"), user)
    access = _authorize(crew, crew_role, user, perm, step_up=step_up, auth_time_ms=auth_time_ms, now_ms=now_ms)
    return CrewEntity(access=access, kind=kind, id=str(entity_id), row=row)


async def require_same_crew(conn: Any, crew_id: str, refs: Iterable[tuple[str, Any]]) -> None:
    """422 unless every ``(kind, id)`` a client supplied belongs to ``crew_id`` (§6).

    Unknown ids and ids of another crew get the same answer, so the check is not an
    existence oracle across tenants.
    """
    for kind, ref_id in refs:
        table = ENTITY_TABLES.get(kind)
        if table is None:
            raise ValueError(f"unknown crew entity kind {kind!r}")
        row = await _fetchone(conn, f"SELECT crew_id FROM {table} WHERE id = ?", (ref_id,)) if is_id(kind, ref_id) else None  # noqa: S608
        if row is None or row["crew_id"] != crew_id:
            raise crew_error(
                422,
                "cross_crew_reference",
                f"The referenced {kind} is not part of this crew.",
            )


# ---------------------------------------------------------------------------
# FastAPI dependencies
# ---------------------------------------------------------------------------


def get_crew_conn(request: Request) -> Any:
    """The ``crew.db`` connection registered at startup (``app.state.crew_db``), else 503."""
    db = getattr(request.app.state, "crew_db", None)
    if db is None:
        raise crew_error(status.HTTP_503_SERVICE_UNAVAILABLE, "crew_unavailable", "Crew mode is not available on this server.")
    return getattr(db, "conn", db)


@dataclass(frozen=True)
class AccessMarker:
    """Attached to every dependency built here so the route-table audit can find it."""

    access: str  # "crew" | "entity" | "human"
    perm: str | None
    entity: str | None
    step_up: bool
    param: str | None


def crew_access(perm: str, *, step_up: bool = False, param: str = "crew_id") -> Callable[..., Any]:
    """Dependency for ``/crews/{crew_id}/…`` routes: ``access: CrewAccess = Depends(crew_access("crew:read"))``."""
    _check_perm_name(perm)
    if step_up and perm not in HUMAN_ONLY_PERMISSIONS:
        raise ValueError("step-up applies only to human-only permissions")

    async def _dep(request: Request, user: AuthenticatedUser = Depends(get_current_user)) -> CrewAccess:
        conn = get_crew_conn(request)
        return await load_crew(
            conn,
            request.path_params.get(param),
            user,
            perm,
            step_up=step_up,
            auth_time_ms=jwt_auth_time_ms(request, user),
        )

    _dep.__crew_access__ = AccessMarker("crew", perm, None, step_up, param)  # type: ignore[attr-defined]
    return _dep


def crew_entity(kind: str, perm: str, *, step_up: bool = False, param: str | None = None) -> Callable[..., Any]:
    """Dependency for entity routes: ``ent: CrewEntity = Depends(crew_entity("claim", "crew:claim"))``."""
    _check_perm_name(perm)
    if kind not in ENTITY_TABLES:
        raise ValueError(f"unknown crew entity kind {kind!r}")
    if step_up and perm not in HUMAN_ONLY_PERMISSIONS:
        raise ValueError("step-up applies only to human-only permissions")
    name = param or ENTITY_PATH_PARAMS.get(kind, f"{kind}_id")

    async def _dep(request: Request, user: AuthenticatedUser = Depends(get_current_user)) -> CrewEntity:
        conn = get_crew_conn(request)
        return await load_crew_entity(
            conn,
            kind,
            request.path_params.get(name),
            user,
            perm,
            step_up=step_up,
            auth_time_ms=jwt_auth_time_ms(request, user),
        )

    _dep.__crew_access__ = AccessMarker("entity", perm, kind, step_up, name)  # type: ignore[attr-defined]
    return _dep


def human_principal(*, step_up: bool = False) -> Callable[..., Any]:
    """Dependency for (H) routes that are not crew-scoped (e.g. ``/notifications/targets``)."""

    async def _dep(request: Request, user: AuthenticatedUser = Depends(get_current_user)) -> AuthenticatedUser:
        require_human(user, step_up=step_up, auth_time_ms=jwt_auth_time_ms(request, user))
        return user

    _dep.__crew_access__ = AccessMarker("human", None, None, step_up, None)  # type: ignore[attr-defined]
    return _dep


# ---------------------------------------------------------------------------
# Route-table audit (§13.1 "Access": every crew route uses load_crew / load_crew_entity)
# ---------------------------------------------------------------------------


def _markers_in(dependencies: Iterable[Any]) -> list[AccessMarker]:
    found: list[AccessMarker] = []
    stack = list(dependencies)
    while stack:
        dep = stack.pop()
        marker = getattr(dep.call, "__crew_access__", None)
        if isinstance(marker, AccessMarker):
            found.append(marker)
        stack.extend(dep.dependencies)
    return found


def _include_markers(depends: Iterable[Any]) -> list[AccessMarker]:
    """Markers on router-level ``dependencies=[Depends(...)]`` of an include."""
    found: list[AccessMarker] = []
    for d in depends:
        marker = getattr(getattr(d, "dependency", None), "__crew_access__", None)
        if isinstance(marker, AccessMarker):
            found.append(marker)
    return found


def _walk_routes(
    routes: Iterable[Any], prefix: str = "", inherited: tuple[AccessMarker, ...] = ()
) -> Iterable[tuple[str, APIRoute, list[AccessMarker]]]:
    """Yield ``(full_path, route, markers)`` for every ``APIRoute``, descending into included routers.

    FastAPI ≥0.13x keeps included routers as lazy ``_IncludedRouter`` entries holding
    ``original_router`` and an ``include_context`` (prefix, dependencies); older
    versions copy the routes with their full path. Both shapes are handled.
    """
    for route in routes:
        if isinstance(route, APIRoute):
            yield prefix + route.path, route, [*inherited, *_markers_in(route.dependant.dependencies)]
            continue
        original = getattr(route, "original_router", None)
        context = getattr(route, "include_context", None)
        if original is not None and context is not None:
            child_prefix = prefix + str(getattr(context, "prefix", "") or "")
            child_markers = (*inherited, *_include_markers(getattr(context, "dependencies", ()) or ()))
            yield from _walk_routes(original.routes, child_prefix, child_markers)


def audit_crew_routes(
    app_routes: Iterable[Any],
    contract: Iterable[Route] = ROUTES,
    *,
    prefix: str = "/api/v1",
    require_all: bool = False,
) -> list[str]:
    """Check registered routes against the contract's access rules. Returns readable violations.

    For every contract route with access ``crew`` or ``entity`` that is registered
    (``prefix`` + path, same method): exactly one access dependency from this module,
    of the right kind, entity kind, permission and step-up. Human-only routes with
    ``user`` access must use :func:`human_principal`. ``require_all`` also reports
    contract L0 routes of those kinds that are not registered yet.
    """
    registered: dict[tuple[str, str], list[AccessMarker]] = {}
    for full_path, r, found in _walk_routes(app_routes):
        for method in r.methods or ():
            registered[(method.upper(), full_path)] = found
    problems: list[str] = []
    for c in contract:
        if c.access not in ("crew", "entity") and not (c.human and c.access == "user"):
            continue
        markers_or_none = registered.get((c.method, prefix + c.path))
        label = f"{c.method} {c.path}"
        if markers_or_none is None:
            if require_all and c.release == "L0":
                problems.append(f"{label}: not registered")
            continue
        markers = markers_or_none
        if c.access == "user":
            if not any(m.access == "human" and m.step_up == c.step_up for m in markers):
                problems.append(f"{label}: (H) route without human_principal(step_up={c.step_up})")
            continue
        scoped = [m for m in markers if m.access in ("crew", "entity")]
        if len(scoped) != 1:
            problems.append(f"{label}: expected exactly one crew_access/crew_entity dependency, found {len(scoped)}")
            continue
        m = scoped[0]
        if m.access != c.access:
            problems.append(f"{label}: uses {m.access} access, contract says {c.access}")
        if c.access == "entity" and m.entity != c.entity:
            problems.append(f"{label}: entity kind {m.entity!r}, contract says {c.entity!r}")
        if m.perm != c.perm:
            problems.append(f"{label}: permission {m.perm!r}, contract says {c.perm!r}")
        if m.step_up != c.step_up:
            problems.append(f"{label}: step_up={m.step_up}, contract says {c.step_up}")
    return problems
