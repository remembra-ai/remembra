"""Crew tasks, reports and checkpoints REST routes (WP-6, spec §6 "Tasks" and "Checkpoints").

Access (§6, D27): crew routes resolve through :func:`remembra.crew.access.crew_access`,
task routes through :func:`remembra.crew.access.crew_entity` (404 on any access
failure). Human-only routes (``assign``, ``review``, ``waive``) need a dashboard
login with crew role owner/admin; ``waive`` also needs step-up. Changing locked
acceptance criteria through ``PATCH`` needs a human principal too.

Who is acting: a mutation is performed either by a **human** (dashboard JWT) or by a
**crew session**, proven by its session token in the :data:`SESSION_TOKEN_HEADER`
header. The token must belong to a session of this crew owned by the
authenticated user (and, for an agent-scoped key, of that agent). A bare API key
without a session token can read but not mutate. Session tokens are compared by
hash only and never appear in responses or in the idempotency table.

Mutations accept ``Idempotency-Key``; responses that wrote an event carry ``seq``.
Routes are registered by WP-8 in ``main.py`` (``app.include_router(router, prefix="/api/v1")``).
"""

from __future__ import annotations

import json
import secrets
from collections.abc import Awaitable, Callable
from types import SimpleNamespace
from typing import Any, Final, cast

import structlog
from fastapi import APIRouter, Depends, Request, Response, status

from remembra.auth.middleware import get_client_ip
from remembra.crew.access import CrewAccess, CrewEntity, crew_access, crew_entity, crew_error
from remembra.crew.checkpoints import CheckpointService
from remembra.crew.events import CrewEventLog, IdempotencyConflict, idem_lookup, idem_store
from remembra.crew.limits import crew_limits_for_owner, enforce_rate_limit
from remembra.crew.reports import ReportService
from remembra.crew.tasks import Caller, CrewServiceError, TaskService, session_for_token

log = structlog.get_logger(__name__)

router = APIRouter(tags=["crew-tasks"])

SESSION_TOKEN_HEADER: Final = "X-Remembra-Crew-Session"  # same header as sessions/claims/channel
IDEMPOTENCY_HEADER: Final = "Idempotency-Key"
PR, PW, PC, PO = "crew:read", "crew:write", "crew:claim", "crew:override"


# ---------------------------------------------------------------------------
# Wiring
# ---------------------------------------------------------------------------


class CrewTaskServices:
    """The WP-6 services bound to one app (built once, kept on ``app.state.crew_task_services``)."""

    def __init__(self, app: Any) -> None:
        events: CrewEventLog | None = getattr(app.state, "crew_events", None)
        if events is None:
            db = getattr(app.state, "crew_db", None)
            if db is None or not hasattr(db, "transaction"):
                raise crew_error(503, "crew_unavailable", "Crew mode is not available on this server.")
            events = CrewEventLog(db, getattr(app.state, "crew_bus", None))
        self.events = events
        pii = self._pii(app)

        async def limits(owner: str) -> Any:
            return await crew_limits_for_owner(getattr(app.state, "usage_meter", None), owner)

        def wake() -> None:
            worker = getattr(app.state, "crew_outbox", None)
            if worker is not None:
                worker.wake()

        self.checkpoints = CheckpointService(events, limits_resolver=limits, pii=pii, outbox_wake=wake)
        self.tasks = TaskService(events, on_transition=self.checkpoints.on_task_transition, limits_for=limits)
        self.reports = ReportService(events, self.tasks, pii=pii)

    @staticmethod
    def _pii(app: Any) -> Callable[[str], str] | None:
        from remembra.api.v1.relay import pii_scrubber

        return pii_scrubber(cast(Request, SimpleNamespace(app=app)))


def services(request: Request) -> CrewTaskServices:
    app = request.app
    current: CrewTaskServices | None = getattr(app.state, "crew_task_services", None)
    events = getattr(app.state, "crew_events", None)
    if current is None or (events is not None and current.events is not events):
        current = CrewTaskServices(app)
        app.state.crew_task_services = current
    return current


async def _json_body(request: Request, *, required: bool = False) -> dict[str, Any]:
    raw = await request.body()
    if not raw.strip():
        if required:
            raise crew_error(422, "invalid_body", "A JSON object body is required.")
        return {}
    try:
        body = json.loads(raw)
    except ValueError:
        raise crew_error(422, "invalid_body", "The body is not valid JSON.")
    if not isinstance(body, dict):
        raise crew_error(422, "invalid_body", "The body must be a JSON object.")
    return body


async def resolve_caller(request: Request, access: CrewAccess, *, mutation: bool) -> Caller:
    """The acting principal: a human (JWT), or the crew session proven by :data:`SESSION_TOKEN_HEADER`."""
    token = request.headers.get(SESSION_TOKEN_HEADER)
    if token:
        conn = services(request).events.db.conn
        session = await session_for_token(conn, access.crew_id, token.strip())
        user = access.user
        if session is None or session["user_id"] != user.user_id or session["state"] == "ended":
            # an ended session's token is dead on every crew router (same rule as claims/channel)
            raise crew_error(401, "invalid_session_token", "The crew session token is not valid for this crew.")
        agent = getattr(user, "agent_id", None)
        if agent and session["agent_id"] != agent:
            raise crew_error(403, "agent_mismatch", "This agent-scoped key cannot act as another agent's session.")
        return Caller.for_session(session)
    if access.human:
        return Caller.for_human(access.user.user_id, privileged=access.privileged)
    if mutation:
        raise crew_error(
            403,
            "session_required",
            f"Send the crew session token in {SESSION_TOKEN_HEADER}, or act from the dashboard.",
        )
    return Caller(user_id=access.user.user_id)


def _rate(request: Request, bucket: str, access: CrewAccess) -> None:
    enforce_rate_limit(bucket, user_id=access.user.user_id, session_token=request.headers.get(SESSION_TOKEN_HEADER) or None)


async def _audit(request: Request, access: CrewAccess, action: str, resource_id: str, details: dict[str, Any]) -> None:
    """Human-only crew actions go to ``audit_log`` (never retention-deleted)."""
    db = getattr(request.app.state, "db", None)
    if db is None or not hasattr(db, "log_audit_event"):
        log.warning("crew_audit_unavailable", action=action, resource_id=resource_id)
        return
    await db.log_audit_event(
        audit_id=f"audit_{secrets.token_urlsafe(16)}",
        user_id=access.user.user_id,
        action=action,
        api_key_id=access.user.api_key_id,
        resource_id=resource_id,
        ip_address=get_client_ip(request),
        success=True,
        error_message=json.dumps({"crew_id": access.crew_id, **details}, sort_keys=True)[:2000],
    )


async def _mutate(
    request: Request,
    access: CrewAccess,
    caller: Caller,
    route: str,
    body: Any,
    run: Callable[[], Awaitable[dict[str, Any]]],
) -> dict[str, Any]:
    """Run a mutation with ``Idempotency-Key`` replay and CrewServiceError → CrewError mapping."""
    key = request.headers.get(IDEMPOTENCY_HEADER)
    conn = services(request).events.db.conn
    principal = f"{access.user.user_id}:{caller.session_id or ('human' if caller.human else 'key')}"
    if key:
        try:
            stored = await idem_lookup(conn, principal=principal, route=route, key=key, body=body)
        except IdempotencyConflict as e:
            raise crew_error(422, "idempotency_conflict", str(e))
        if stored is not None:
            return {**stored, "replayed": True}
    try:
        result = await run()
    except CrewServiceError as e:
        raise crew_error(e.status, e.error, e.message, **e.extra)
    if key:
        db = services(request).events.db
        async with db.transaction():
            await idem_store(db.conn, principal=principal, route=route, key=key, body=body, response=result)
    return result


def _task_response(result: Any) -> dict[str, Any]:
    out = {"task": result.task, "seq": result.seq}
    for k, v in result.extra.items():
        if k in ("claims", "baton", "cross_checkout", "changed"):
            out[k] = v
    return out


# ---------------------------------------------------------------------------
# Tasks
# ---------------------------------------------------------------------------


@router.get("/crews/{crew_id}/tasks")
async def list_tasks(request: Request, access: CrewAccess = Depends(crew_access(PR))) -> dict[str, Any]:
    status_value = request.query_params.get("status") or None
    try:
        tasks = await services(request).tasks.list_tasks(access.crew_id, status=status_value)
    except CrewServiceError as e:
        raise crew_error(e.status, e.error, e.message)
    return {"tasks": tasks, "count": len(tasks)}


@router.post("/crews/{crew_id}/tasks", status_code=status.HTTP_201_CREATED)
async def create_task(request: Request, access: CrewAccess = Depends(crew_access(PW))) -> dict[str, Any]:
    _rate(request, "tasks", access)
    body = await _json_body(request, required=True)
    caller = await resolve_caller(request, access, mutation=True)

    async def run() -> dict[str, Any]:
        return _task_response(await services(request).tasks.create(access.crew_id, caller, body))

    return await _mutate(request, access, caller, "POST /crews/{crew_id}/tasks", body, run)


@router.get("/tasks/{task_id}")
async def get_task(request: Request, ent: CrewEntity = Depends(crew_entity("task", PR))) -> dict[str, Any]:
    try:
        return {"task": await services(request).tasks.get(ent.access.crew_id, ent.id)}
    except CrewServiceError as e:
        raise crew_error(e.status, e.error, e.message)


@router.patch("/tasks/{task_id}")
async def patch_task(request: Request, ent: CrewEntity = Depends(crew_entity("task", PW))) -> dict[str, Any]:
    access = ent.access
    _rate(request, "tasks", access)
    raw_match = (request.headers.get("If-Match") or "").strip().strip('"').removeprefix("W/").strip('"')
    if not raw_match:
        raise crew_error(428, "if_match_required", "PATCH needs If-Match with the task version.")
    if not raw_match.isdigit():
        raise crew_error(412, "version_mismatch", "If-Match must be the task version number.")
    body = await _json_body(request, required=True)
    caller = await resolve_caller(request, access, mutation=True)
    if "acceptance" in body and caller.human and access.role not in ("owner", "admin"):
        raise crew_error(403, "crew_role_required", "Changing acceptance criteria after the lock needs crew role owner or admin.")

    async def run() -> dict[str, Any]:
        result = await services(request).tasks.patch(access.crew_id, ent.id, caller, body, if_match=int(raw_match))
        if result.extra.get("acceptance_changed_after_lock"):
            await _audit(request, access, "crew_task_acceptance_changed", ent.id, {"criteria": len(result.task["acceptance"])})
        return _task_response(result)

    return await _mutate(request, access, caller, "PATCH /tasks/{task_id}", {**body, "_if_match": raw_match}, run)


def _action(
    path: str,
    perm: str,
    bucket: str | None,
    handler: Callable[[CrewTaskServices, str, str, Caller, dict[str, Any]], Awaitable[Any]],
    *,
    human: bool = False,
    step_up: bool = False,
    audit: str | None = None,
    body_required: bool = False,
) -> None:
    """Register ``POST /tasks/{task_id}/<action>``."""

    async def endpoint(request: Request, ent: CrewEntity = Depends(crew_entity("task", perm, step_up=step_up))) -> dict[str, Any]:
        access = ent.access
        if bucket:
            _rate(request, bucket, access)
        body = await _json_body(request, required=body_required)
        caller = Caller.for_human(access.user.user_id) if human else await resolve_caller(request, access, mutation=True)

        async def run() -> dict[str, Any]:
            result = await handler(services(request), access.crew_id, ent.id, caller, body)
            if audit:
                await _audit(
                    request, access, audit, ent.id, {k: v for k, v in body.items() if k in ("to", "decision", "criterion_id")}
                )
            if hasattr(result, "report"):
                return {
                    "report": result.report,
                    "task": result.task,
                    "seq": result.seq,
                    "outcome": result.outcome,
                    **({"seal": result.gate.seal, "reasons": result.gate.reasons} if result.gate else {}),
                }
            return _task_response(result)

        return await _mutate(request, access, caller, f"POST /tasks/{{task_id}}/{path}", body, run)

    endpoint.__name__ = f"task_{path.replace('-', '_')}"
    router.add_api_route(f"/tasks/{{task_id}}/{path}", endpoint, methods=["POST"], name=endpoint.__name__)


async def _claim(s: CrewTaskServices, crew_id: str, task_id: str, caller: Caller, body: dict[str, Any]) -> Any:
    return await s.tasks.claim(crew_id, task_id, caller)


async def _start(s: CrewTaskServices, crew_id: str, task_id: str, caller: Caller, body: dict[str, Any]) -> Any:
    return await s.tasks.start(crew_id, task_id, caller, head=body.get("head"))


async def _block(s: CrewTaskServices, crew_id: str, task_id: str, caller: Caller, body: dict[str, Any]) -> Any:
    return await s.tasks.block(crew_id, task_id, caller, body.get("reason"))


async def _unblock(s: CrewTaskServices, crew_id: str, task_id: str, caller: Caller, body: dict[str, Any]) -> Any:
    return await s.tasks.unblock(crew_id, task_id, caller)


async def _release(s: CrewTaskServices, crew_id: str, task_id: str, caller: Caller, body: dict[str, Any]) -> Any:
    baton = body.get("baton", True)
    if not isinstance(baton, bool):
        raise CrewServiceError(422, "invalid_body", "baton must be true or false")
    return await s.tasks.release(crew_id, task_id, caller, baton=baton)


async def _adopt(s: CrewTaskServices, crew_id: str, task_id: str, caller: Caller, body: dict[str, Any]) -> Any:
    return await s.tasks.adopt(crew_id, task_id, caller)


async def _assign(s: CrewTaskServices, crew_id: str, task_id: str, caller: Caller, body: dict[str, Any]) -> Any:
    return await s.tasks.assign(crew_id, task_id, caller, body.get("to"))


async def _review(s: CrewTaskServices, crew_id: str, task_id: str, caller: Caller, body: dict[str, Any]) -> Any:
    return await s.reports.review(crew_id, task_id, caller, str(body.get("decision")), body.get("note"))


async def _reopen(s: CrewTaskServices, crew_id: str, task_id: str, caller: Caller, body: dict[str, Any]) -> Any:
    return await s.tasks.reopen(crew_id, task_id, caller)


async def _waive(s: CrewTaskServices, crew_id: str, task_id: str, caller: Caller, body: dict[str, Any]) -> Any:
    return await s.reports.waive(crew_id, task_id, caller, body.get("criterion_id"), body.get("reason"))


async def _add_dep(s: CrewTaskServices, crew_id: str, task_id: str, caller: Caller, body: dict[str, Any]) -> Any:
    return await s.tasks.add_dependency(crew_id, task_id, caller, body.get("depends_on_id"))


async def _report(s: CrewTaskServices, crew_id: str, task_id: str, caller: Caller, body: dict[str, Any]) -> Any:
    return await s.reports.submit(crew_id, task_id, caller, body)


_action("claim", PC, "tasks", _claim)
_action("start", PC, "tasks", _start)
_action("block", PW, "tasks", _block, body_required=True)
_action("unblock", PW, "tasks", _unblock)
_action("release", PC, "tasks", _release)
_action("adopt", PC, "adopt", _adopt)
_action("assign", PO, None, _assign, human=True, audit="crew_task_assign", body_required=True)
_action("review", PO, None, _review, human=True, audit="crew_report_review", body_required=True)
_action("reopen", PW, None, _reopen)
_action("waive", PO, None, _waive, human=True, step_up=True, audit="crew_report_waive", body_required=True)
_action("deps", PW, None, _add_dep, body_required=True)
_action("reports", PW, None, _report, body_required=True)


@router.delete("/tasks/{task_id}/deps/{depends_on_id}")
async def remove_dependency(
    request: Request, depends_on_id: str, ent: CrewEntity = Depends(crew_entity("task", PW))
) -> dict[str, Any]:
    access = ent.access
    caller = await resolve_caller(request, access, mutation=True)

    async def run() -> dict[str, Any]:
        return _task_response(await services(request).tasks.remove_dependency(access.crew_id, ent.id, caller, depends_on_id))

    return await _mutate(request, access, caller, "DELETE /tasks/{task_id}/deps/{depends_on_id}", {"dep": depends_on_id}, run)


@router.get("/tasks/{task_id}/reports")
async def list_reports(request: Request, ent: CrewEntity = Depends(crew_entity("task", PR))) -> dict[str, Any]:
    reports = await services(request).reports.list_reports(ent.access.crew_id, ent.id)
    return {"reports": reports, "count": len(reports)}


# ---------------------------------------------------------------------------
# Checkpoints
# ---------------------------------------------------------------------------


@router.post("/crews/{crew_id}/checkpoints", status_code=status.HTTP_201_CREATED)
async def submit_checkpoint(
    request: Request, response: Response, access: CrewAccess = Depends(crew_access(PW))
) -> dict[str, Any]:
    _rate(request, "checkpoints", access)  # per session and per user (§11 limits; promotions are bounded too)
    body = await _json_body(request, required=True)
    caller = await resolve_caller(request, access, mutation=True)

    async def run() -> dict[str, Any]:
        result = await services(request).checkpoints.ingest(access.crew_id, caller, body)
        return {"checkpoint": result.checkpoint, "created": result.created, "seq": result.seq, "promotion": result.promotion}

    out = await _mutate(request, access, caller, "POST /crews/{crew_id}/checkpoints", body, run)
    if not out.get("created"):
        response.status_code = status.HTTP_200_OK
    return out


@router.get("/crews/{crew_id}/checkpoints")
async def list_checkpoints(request: Request, access: CrewAccess = Depends(crew_access(PR))) -> dict[str, Any]:
    q = request.query_params
    try:
        limit = int(q.get("limit", "100"))
    except ValueError:
        raise crew_error(422, "invalid_filter", "limit must be an integer")
    items = await services(request).checkpoints.list_checkpoints(
        access.crew_id, session_id=q.get("session_id") or None, task_id=q.get("task_id") or None, limit=limit
    )
    return {"checkpoints": items, "count": len(items)}
