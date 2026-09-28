"""WebSocket endpoint for real-time memory updates and crew streams.

Security model (SEC-1):

* Every connection is authenticated exactly like REST (API key or dashboard
  JWT, including revocation / deactivation checks) and needs ``memory:recall``
  (memory events) or, when Crew mode is on, ``crew:read`` (crew subscriptions).
* The same checks run again while the socket is open (P-348): before every
  memory event and every crew frame it is sent, every
  ``REVALIDATE_INTERVAL_SECONDS`` while it is idle (then crew membership and
  project access are re-read too), and at once when a key is revoked or
  deleted, a session is signed out, a password changes or the account is
  deactivated or deleted (:func:`recheck_user_connections`). A socket that
  fails them gets no further frames and is closed: 4001 when its key or
  session no longer authenticates, 4003 when it lost ``memory:recall``, the
  project it follows, ``crew:read`` or a crew it subscribed to.
  ``ConnectionManager.revoke`` closes (4003) the sockets of a crew member who
  was removed.
* Memory events are routed by the server-derived owner ``user_id`` — never by a
  client-chosen namespace — so a client can only ever receive its own tenant's
  events. Project-restricted keys only receive events for their projects, and
  subscribing to a project outside the key's allow-list is refused.
* Credentials may be sent as headers (``X-API-Key`` / ``Authorization``) or in a
  first ``{"type": "auth", ...}`` message, so browsers never have to put a token
  in the URL. Query-string credentials are still accepted for older memory
  clients, but **refused for crew subscriptions**.

Crew streams (Crew mode §4.4, contract ``docs/crew/snapshot.md``). With Crew
mode off (no crew.db) crew frames are unknown messages, as on a server without
Crew mode:

* ``{"type":"subscribe","channel":"crew","crew_id":"crw_…","since_seq":N,"topics":["crew"]}``
  requires ``crew:read`` and crew membership (unknown and forbidden crews look
  the same). The server replays events after ``since_seq`` (at most 500, else
  ``resync_required``), then streams live events strictly by seq.
* ``{"type":"subscribe","channel":"crew","crew_id":"*","topics":["crew.summary"]}``
  streams counts only for every readable crew (filtered by ``project_ids``).
* ``{"type":"presence","crew_id":…,"lanes":[…]}`` from crewd (``crew:write``)
  is fanned out to that crew's subscribers, at most once per session per 5 s;
  ``state``/``stuck`` are always the server's values. Never stored or replayed.
* Limits: 20 crew subscriptions per connection, 10 crew connections per user,
  30 replay subscribes per minute per user; each subscription queues at most
  1000 frames (overflow → ``resync_required``).
"""

import asyncio
import json
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import structlog
from fastapi import APIRouter, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from starlette.websockets import WebSocketState

from remembra.auth.middleware import (
    AuthenticatedUser,
    CurrentUser,
    authenticate_api_key,
    authenticate_jwt,
    has_permission,
)
from remembra.config import get_settings
from remembra.core.time import utcnow
from remembra.crew import schemas as crew_schemas
from remembra.crew.access import key_permissions, load_crew
from remembra.crew.bus import CrewBus, CrewRef, presence_sessions, readable_crews, summary_items
from remembra.crew.events import CrewDatabase, crew_head, fetch_events

log = structlog.get_logger(__name__)

router = APIRouter(tags=["websocket"])

AUTH_MESSAGE_TIMEOUT_SECONDS = 10.0
# An idle socket re-runs the connect-time checks this often (frames re-check on every send).
REVALIDATE_INTERVAL_SECONDS = 30.0
# The server sends "ping" after this long without a message from the client.
IDLE_PING_SECONDS = 60.0

CLOSE_UNAUTHORIZED = 4001
CLOSE_FORBIDDEN = 4003
CLOSE_INTERNAL_ERROR = 1011
ACCESS_ENDED_REASON = "Access revoked or expired"
RECHECK_FAILED_REASON = "Could not re-check access"
CREW_ACCESS_ENDED_REASON = "crew access revoked"

CREW_READ = "crew:read"
CREW_WRITE = "crew:write"


def _has_crew_permission(user: AuthenticatedUser, perm: str) -> bool:
    """Crew permissions of the credential: its RBAC role and scopes (WP-14, ``crew.access.key_permissions``)."""
    return perm in key_permissions(user)


async def _load_crew_ref(conn: Any, crew_id: str, user: AuthenticatedUser, perm: str) -> CrewRef | None:
    """WP-14's ``load_crew`` (the same ACL as the REST routes); any refusal reads as None (``not_found``)."""
    try:
        access = await load_crew(conn, crew_id, user, perm)
    except HTTPException:
        return None
    return CrewRef(access.crew.id, access.crew.project_id, access.crew.owner_user_id)


CREW_QUEUE_MAX = 1000
MAX_CREW_SUBS_PER_CONNECTION = 20
MAX_CREW_CONNECTIONS_PER_USER = 10
REPLAY_SUBSCRIBES_PER_MINUTE = 30
PRESENCE_MIN_INTERVAL_S = 5.0
SUMMARY_DEBOUNCE_S = 1.0
REPLAY_MAX = crew_schemas.REPLAY_MAX_EVENTS


def _now_iso() -> str:
    return utcnow().isoformat() + "Z"


@dataclass(eq=False)
class _Subscriber:
    """A socket's memory-event subscription (present when the credential holds ``memory:recall``)."""

    websocket: WebSocket
    user_id: str
    allowed_projects: tuple[str, ...] | None  # None = unrestricted key
    project_filter: str | None
    # The socket this subscription belongs to: its credential is re-checked before every event.
    conn: "_Connection" = field(repr=False)

    def wants(self, project_id: str | None) -> bool:
        if self.allowed_projects is not None and project_id not in self.allowed_projects:
            return False
        return not (self.project_filter and project_id and project_id != self.project_filter)

    def may_follow(self, project_id: str | None) -> bool:
        """Whether the key's current project allow-list lets this socket follow ``project_id``."""
        return not (project_id and self.allowed_projects is not None and project_id not in self.allowed_projects)


@dataclass(frozen=True)
class _Credential:
    # The credentials the socket connected with, kept only to re-run the same
    # checks while it is open. repr=False keeps them out of logs and tracebacks.
    api_key: str | None = field(repr=False)
    token: str | None = field(repr=False)
    source: str  # header | message | query | none


class _Connection:
    """One /ws socket: its principal, credential (for re-validation) and subscriptions."""

    def __init__(self, websocket: WebSocket, user: AuthenticatedUser, credential: _Credential) -> None:
        self.websocket = websocket
        self.user = user
        self.credential = credential
        self.send_lock = asyncio.Lock()
        self.subs: dict[str, _CrewSub] = {}
        self.summary: _SummarySub | None = None
        self.memory_subscriber: _Subscriber | None = None
        self.closed = False
        self.close_code: int | None = None
        # Set once the socket's access has ended; the close frame is sent by this task.
        self.closing: asyncio.Task[None] | None = None

    def __repr__(self) -> str:
        return f"<_Connection user_id={self.user.user_id!r} closed={self.closed}>"

    @property
    def crew_sub_count(self) -> int:
        return len(self.subs) + (1 if self.summary is not None else 0)

    async def send_json(self, obj: Any) -> bool:
        return await self.send_text(json.dumps(obj))

    async def send_text(self, text: str) -> bool:
        if self.closed:
            return False
        try:
            async with self.send_lock:
                if self.closed or self.websocket.client_state != WebSocketState.CONNECTED:
                    return False
                await self.websocket.send_text(text)
            return True
        except Exception as e:
            log.warning("websocket_send_failed", error_type=type(e).__name__)
            return False

    async def close_socket(self, code: int, reason: str) -> None:
        """Send the close frame (after any frame already being sent). Never raises."""
        try:
            async with self.send_lock:
                if self.websocket.application_state == WebSocketState.CONNECTED:
                    await self.websocket.close(code=code, reason=reason)
        except Exception as e:
            log.debug("websocket_close_failed", error_type=type(e).__name__)

    async def crew_error(self, crew_id: Any, code: str, message: str, **extra: Any) -> None:
        data: dict[str, Any] = {"channel": "crew", "crew_id": crew_id, "code": code, "message": message, **extra}
        await self.send_json({"type": "error", "data": data, "timestamp": _now_iso()})


_OVERFLOW = object()


class _CrewSub:
    """One crew subscription: a bounded queue drained by a pump task, strictly by seq."""

    def __init__(self, conn: _Connection, crew: CrewRef, db: CrewDatabase, manager: "ConnectionManager") -> None:
        self.conn = conn
        self.crew_id = crew.crew_id
        self.project_id = crew.project_id
        self.db = db
        self.manager = manager
        self.queue: asyncio.Queue[Any] = asyncio.Queue(maxsize=CREW_QUEUE_MAX)
        self.next_seq = 0
        self.pump: asyncio.Task[None] | None = None
        self.overflowed = False
        self.active = True

    def offer(self, item: Any) -> None:
        if not self.active or self.overflowed:
            return
        try:
            self.queue.put_nowait(item)
        except asyncio.QueueFull:
            self.overflowed = True
            while not self.queue.empty():
                self.queue.get_nowait()
            self.queue.put_nowait(_OVERFLOW)

    def start(self) -> None:
        self.pump = asyncio.create_task(self._run(), name=f"crew-ws-pump-{self.crew_id}")

    async def _send_event(self, env: Any) -> bool:
        return await self.conn.send_json({"type": "crew.event", "crew_id": self.crew_id, "data": env})

    async def resync(self, reason: str) -> None:
        head = await crew_head(self.db.conn, self.crew_id)
        await self.conn.send_json(
            {"type": "resync_required", "crew_id": self.crew_id, "reason": reason, "last_seq": head.last_seq if head else 0}
        )
        self.manager.remove_crew_sub(self, cancel=False)

    async def _run(self) -> None:
        try:
            while self.active:
                item = await self.queue.get()
                if item is _OVERFLOW:
                    await self.resync("overflow")
                    return
                # Re-check before every frame: a revoked key or a signed-out session
                # never gets another crew frame, even with no hook run.
                if not await self.manager.recheck_connection(self.conn) or not self.active:
                    return
                kind, frame = item
                if kind == "presence":
                    await self.conn.send_json(frame)
                    continue
                seq = int(frame["seq"])
                if seq < self.next_seq:
                    continue  # already replayed
                if seq > self.next_seq:
                    missing = await fetch_events(
                        self.db.conn, self.crew_id, after_seq=self.next_seq - 1, upto_seq=seq - 1, limit=REPLAY_MAX
                    )
                    if len(missing) != seq - self.next_seq:
                        await self.resync("gap_too_large")
                        return
                    for env in missing:
                        if not await self._send_event(env):
                            return
                if not await self._send_event(frame):
                    return
                self.next_seq = seq + 1
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.error("crew_ws_pump_failed", crew_id=self.crew_id, error_type=type(e).__name__, error=str(e))
            self.manager.remove_crew_sub(self, cancel=False)
            # Tell the client its stream stopped so it refetches the snapshot and resubscribes.
            await self.conn.send_json(
                {"type": "resync_required", "crew_id": self.crew_id, "reason": "overflow", "last_seq": max(0, self.next_seq - 1)}
            )


class _SummarySub:
    """``crew_id="*"`` subscription: counts only, pushed (debounced) when a readable crew changes."""

    def __init__(self, conn: _Connection, db: CrewDatabase, manager: "ConnectionManager") -> None:
        self.conn = conn
        self.db = db
        self.manager = manager
        self.crews: dict[str, CrewRef] = {}
        self.dirty: set[str] = set()
        self.timer: asyncio.TimerHandle | None = None

    async def refresh(self) -> None:
        """Recompute the readable crews and send the full summary."""
        user = self.conn.user
        crews = await readable_crews(self.db.conn, user.user_id, user.project_ids or None)
        self.crews = {c.crew_id: c for c in crews}
        items = await summary_items(self.db.conn, crews)
        await self.conn.send_json({"type": "crew.summary", "crews": items})

    def mark_dirty(self, crew_id: str) -> None:
        self.dirty.add(crew_id)
        if self.timer is None:
            loop = asyncio.get_running_loop()
            self.timer = loop.call_later(SUMMARY_DEBOUNCE_S, self._fire)

    def _fire(self) -> None:
        self.timer = None
        self.manager.spawn(self.flush(), name="crew-summary-flush")

    async def flush(self) -> None:
        ids, self.dirty = self.dirty, set()
        crews = [self.crews[i] for i in sorted(ids) if i in self.crews]
        if not crews or self.conn.closed:
            return
        if not await self.manager.recheck_connection(self.conn):
            return
        items = await summary_items(self.db.conn, crews)
        await self.conn.send_json({"type": "crew.summary", "crews": items})

    def cancel(self) -> None:
        if self.timer is not None:
            self.timer.cancel()
            self.timer = None


class ConnectionManager:
    """Tenant-isolated fan-out of memory events and crew streams to WebSocket subscribers."""

    def __init__(self) -> None:
        self._by_user: dict[str, set[_Subscriber]] = {}
        self._lock = asyncio.Lock()
        self._closing: set[asyncio.Task[None]] = set()
        # Crew mode (§4.4)
        self._connections: set[_Connection] = set()
        self._by_crew: dict[str, set[_CrewSub]] = {}
        self._summary_subs: set[_SummarySub] = set()
        self._crew_conns: dict[str, set[_Connection]] = {}
        self._replays: dict[str, deque[float]] = {}
        self._presence_last: dict[tuple[str, str], float] = {}
        self._presence_pending: dict[str, dict[str, dict[str, Any]]] = {}
        self._presence_timers: dict[str, asyncio.TimerHandle] = {}
        self._bg: set[asyncio.Task[Any]] = set()

    # ---- memory events -------------------------------------------------

    async def register(self, subscriber: _Subscriber) -> None:
        async with self._lock:
            self._by_user.setdefault(subscriber.user_id, set()).add(subscriber)
        log.info("websocket_connected", user_id=subscriber.user_id, total_connections=self._count_all())

    async def unregister(self, subscriber: _Subscriber) -> None:
        async with self._lock:
            subs = self._by_user.get(subscriber.user_id)
            if subs is None or subscriber not in subs:
                return
            subs.discard(subscriber)
            if not subs:
                del self._by_user[subscriber.user_id]
        log.info("websocket_disconnected", user_id=subscriber.user_id, total_connections=self._count_all())

    async def broadcast(
        self,
        event_type: str,
        data: dict[str, Any],
        *,
        user_id: str | None,
        project_id: str | None = None,
    ) -> int:
        """Send an event to the owning user's subscribers only. Returns recipients count.

        Events without an owner are dropped (fail closed) rather than fanned out.
        """
        if not user_id:
            log.warning("websocket_broadcast_without_owner_dropped", event_type=event_type)
            return 0

        message = json.dumps(
            {
                "type": event_type,
                "data": data,
                "timestamp": _now_iso(),
                "namespace": f"{user_id}:{project_id or '*'}",
                "project_id": project_id,
            }
        )

        async with self._lock:
            subscribers = list(self._by_user.get(user_id, ()))

        sent = 0
        dead: list[_Subscriber] = []
        for sub in subscribers:
            if not sub.wants(project_id):
                continue
            # Re-check before every send: a revoked key, a signed-out session or a
            # deactivated account never gets another event, even with no hook run.
            if not await self.recheck(sub) or not sub.wants(project_id):
                continue
            conn = sub.conn
            try:
                async with conn.send_lock:
                    if not conn.closed and sub.websocket.client_state == WebSocketState.CONNECTED:
                        await sub.websocket.send_text(message)
                        sent += 1
            except Exception as e:
                log.warning("websocket_send_failed", error_type=type(e).__name__)
                dead.append(sub)
        for sub in dead:
            await self.unregister(sub)
        return sent

    def _count_all(self) -> int:
        return sum(len(subs) for subs in self._by_user.values())

    async def get_stats(self, user_id: str | None = None) -> dict[str, Any]:
        """Connection counts. With ``user_id`` only that tenant's connections are reported."""
        async with self._lock:
            if user_id is not None:
                return {"connections": len(self._by_user.get(user_id, ()))}
            return {"total_connections": self._count_all(), "tenants": len(self._by_user)}

    # ---- re-validation and revocation ---------------------------------

    async def recheck(self, subscriber: _Subscriber) -> bool:
        """Re-run the connect-time checks for a memory subscriber's socket. True while it may still get events."""
        return await self.recheck_connection(subscriber.conn)

    async def recheck_connection(self, conn: _Connection, *, crews: bool = False) -> bool:
        """Re-run the connect-time checks for an open socket. True while it may still get frames.

        The credential is validated exactly like REST (key active, account
        active, JWT signature/expiry/logout/session cut-off), then
        ``memory:recall`` and the followed project (memory subscription) and
        ``crew:read`` (crew subscriptions) are checked again. With ``crews=True``
        (the periodic check) every subscribed crew is loaded again through the
        REST access check and the summary is recomputed. On failure the socket is
        taken out of every fan-out at once, so no further frame reaches it, and
        closed: 4001 when the key or session no longer authenticates, 4003 when
        it lost a permission, its project or a crew, 1011 when the check itself
        could not run.
        """
        if conn.closed:
            return False
        try:
            user = await _authenticate(conn.websocket, conn.credential.api_key, conn.credential.token, record_use=False)
        except Exception as e:
            log.warning("websocket_recheck_failed", user_id=conn.user.user_id, error_type=type(e).__name__)
            await self._end(conn, CLOSE_INTERNAL_ERROR, RECHECK_FAILED_REASON)
            return False
        if user is None or user.user_id != conn.user.user_id:
            await self._end(conn, CLOSE_UNAUTHORIZED, ACCESS_ENDED_REASON)
            return False
        conn.user = user
        sub = conn.memory_subscriber
        if sub is not None:
            if not has_permission(user, "memory:recall"):
                await self._end(conn, CLOSE_FORBIDDEN, "memory:recall permission required")
                return False
            if not _project_allowed(user, sub.project_filter):
                await self._end(conn, CLOSE_FORBIDDEN, "No access to project")
                return False
            sub.allowed_projects = tuple(user.project_ids) if user.project_ids else None
        elif not _has_crew_permission(user, CREW_READ):
            # A crew-only socket (no memory subscription) that lost crew:read keeps nothing it may read.
            await self._end(conn, CLOSE_FORBIDDEN, CREW_ACCESS_ENDED_REASON)
            return False
        if conn.subs or conn.summary is not None:
            if not _has_crew_permission(user, CREW_READ):
                await self._end(conn, CLOSE_FORBIDDEN, CREW_ACCESS_ENDED_REASON)
                return False
            if crews:
                try:
                    lost = await _crew_access_lost(conn)
                except Exception as e:  # fail closed
                    log.warning("websocket_crew_recheck_failed", user_id=conn.user.user_id, error_type=type(e).__name__)
                    await self._end(conn, CLOSE_INTERNAL_ERROR, RECHECK_FAILED_REASON)
                    return False
                if lost:
                    await self._end(conn, CLOSE_FORBIDDEN, CREW_ACCESS_ENDED_REASON)
                    return False
        return True

    async def recheck_user(self, user_id: str) -> int:
        """Re-check every open socket of ``user_id`` now (memory and crew). Returns how many were closed.

        Only the credential and permission checks run here: crew membership changes close
        sockets through :meth:`revoke`, and this never sends a frame from the caller's request.
        """
        closed = 0
        for conn in [c for c in list(self._connections) if c.user.user_id == user_id]:
            if not await self.recheck_connection(conn):
                closed += 1
        return closed

    async def _end(self, conn: _Connection, code: int, reason: str) -> None:
        """Stop sending to ``conn`` now and close its socket without blocking the caller.

        Closing can wait on a slow client, so it runs in its own task; the
        socket's handler awaits that task before it returns.
        """
        if conn.memory_subscriber is not None:
            await self.unregister(conn.memory_subscriber)
        for sub in list(conn.subs.values()):
            self.remove_crew_sub(sub)
        self.remove_summary_sub(conn)
        if conn.closed:
            return
        conn.closed = True
        conn.close_code = code
        task = asyncio.create_task(conn.close_socket(code, reason))
        conn.closing = task
        self._closing.add(task)
        task.add_done_callback(self._closing.discard)
        log.info("websocket_access_ended", user_id=conn.user.user_id, code=code, reason=reason)

    def spawn(self, coro: Any, *, name: str) -> None:
        task = asyncio.create_task(coro, name=name)
        self._bg.add(task)
        task.add_done_callback(self._bg.discard)

    def add_connection(self, conn: _Connection) -> None:
        self._connections.add(conn)

    def remove_connection(self, conn: _Connection) -> None:
        for sub in list(conn.subs.values()):
            self.remove_crew_sub(sub)
        self.remove_summary_sub(conn)
        self._connections.discard(conn)

    async def revoke(
        self,
        *,
        user_id: str | None = None,
        api_key_id: str | None = None,
        crew_id: str | None = None,
        reason: str = "access revoked",
    ) -> int:
        """Close (4003) every socket matching all given filters. Returns how many were closed.

        Call on crew membership or share changes (``user_id`` + ``crew_id``). A
        revoked key, a signed-out session or a deactivated account needs no call:
        :func:`recheck_user_connections` closes those sockets with 4001.
        """
        if user_id is None and api_key_id is None and crew_id is None:
            return 0
        targets = []
        for conn in list(self._connections):
            if conn.closed:
                continue
            if user_id is not None and conn.user.user_id != user_id:
                continue
            if api_key_id is not None and conn.user.api_key_id != api_key_id:
                continue
            if crew_id is not None and crew_id not in conn.subs and not (conn.summary and crew_id in conn.summary.crews):
                continue
            targets.append(conn)
        for conn in targets:
            await self._end(conn, CLOSE_FORBIDDEN, reason)
        return len(targets)

    # ---- crew subscriptions -------------------------------------------

    def attach_crew_bus(self, bus: CrewBus) -> Callable[[], None]:
        return bus.subscribe(self.dispatch_crew_event)

    def dispatch_crew_event(self, envelope: Any) -> None:
        """CrewBus listener: enqueue a committed event for every subscriber of its crew (non-blocking)."""
        crew_id = envelope["crew_id"]
        for sub in list(self._by_crew.get(crew_id, ())):
            sub.offer(("event", envelope))
        for summary in list(self._summary_subs):
            if crew_id in summary.crews:
                summary.mark_dirty(crew_id)

    def crew_connection_count(self, user_id: str) -> int:
        return len(self._crew_conns.get(user_id, ()))

    def add_crew_sub(self, sub: _CrewSub) -> None:
        sub.conn.subs[sub.crew_id] = sub
        self._by_crew.setdefault(sub.crew_id, set()).add(sub)
        self._crew_conns.setdefault(sub.conn.user.user_id, set()).add(sub.conn)

    def remove_crew_sub(self, sub: _CrewSub, *, cancel: bool = True) -> None:
        sub.active = False
        if sub.conn.subs.get(sub.crew_id) is sub:
            del sub.conn.subs[sub.crew_id]
        subs = self._by_crew.get(sub.crew_id)
        if subs is not None:
            subs.discard(sub)
            if not subs:
                del self._by_crew[sub.crew_id]
        if cancel and sub.pump is not None and sub.pump is not asyncio.current_task():
            sub.pump.cancel()
        self._drop_crew_conn_if_idle(sub.conn)

    def add_summary_sub(self, summary: _SummarySub) -> None:
        summary.conn.summary = summary
        self._summary_subs.add(summary)
        self._crew_conns.setdefault(summary.conn.user.user_id, set()).add(summary.conn)

    def remove_summary_sub(self, conn: _Connection) -> None:
        if conn.summary is not None:
            conn.summary.cancel()
            self._summary_subs.discard(conn.summary)
            conn.summary = None
        self._drop_crew_conn_if_idle(conn)

    def _drop_crew_conn_if_idle(self, conn: _Connection) -> None:
        if conn.crew_sub_count:
            return
        conns = self._crew_conns.get(conn.user.user_id)
        if conns is not None:
            conns.discard(conn)
            if not conns:
                del self._crew_conns[conn.user.user_id]

    def allow_replay(self, user_id: str, now: float | None = None) -> float:
        """0 if a replay subscribe is allowed (and counted), else seconds until the next one is."""
        now = time.monotonic() if now is None else now
        window = self._replays.setdefault(user_id, deque())
        while window and now - window[0] >= 60.0:
            window.popleft()
        if len(window) >= REPLAY_SUBSCRIBES_PER_MINUTE:
            return max(0.001, 60.0 - (now - window[0]))
        window.append(now)
        return 0.0

    # ---- presence ------------------------------------------------------

    def offer_presence(self, crew_id: str, lanes: list[dict[str, Any]], now: float | None = None) -> int:
        """Fan out lanes now, or hold the latest per session until its 5 s window opens. Returns lanes sent now."""
        now = time.monotonic() if now is None else now
        immediate: list[dict[str, Any]] = []
        for lane in lanes:
            sid = lane["session_id"]
            key = (crew_id, sid)
            last = self._presence_last.get(key)
            if last is None or now - last >= PRESENCE_MIN_INTERVAL_S:
                immediate.append(lane)
                self._presence_last[key] = now
                self._presence_pending.get(crew_id, {}).pop(sid, None)
            else:
                self._presence_pending.setdefault(crew_id, {})[sid] = lane
                self._schedule_presence_flush(crew_id, last + PRESENCE_MIN_INTERVAL_S - now)
        if len(self._presence_last) > 10_000:
            stale = [k for k, t in self._presence_last.items() if now - t > 600]
            for k in stale:
                del self._presence_last[k]
        if immediate:
            frame = {"type": "presence", "crew_id": crew_id, "lanes": immediate}
            for sub in list(self._by_crew.get(crew_id, ())):
                sub.offer(("presence", frame))
        return len(immediate)

    def _schedule_presence_flush(self, crew_id: str, delay: float) -> None:
        if crew_id in self._presence_timers:
            return
        loop = asyncio.get_running_loop()
        self._presence_timers[crew_id] = loop.call_later(max(0.0, delay), self._flush_presence, crew_id)

    def _flush_presence(self, crew_id: str) -> None:
        self._presence_timers.pop(crew_id, None)
        pending = self._presence_pending.pop(crew_id, {})
        if pending:
            self.offer_presence(crew_id, list(pending.values()))


# Global connection manager instance
connection_manager = ConnectionManager()


def get_connection_manager() -> ConnectionManager:
    """Get the global connection manager (for dependency injection)."""
    return connection_manager


async def recheck_user_connections(user_id: str) -> None:
    """Re-check every open socket of ``user_id`` now; call right after cutting that account's access.

    Sockets whose key or session no longer passes are closed at once. Never
    raises: cutting access must not fail because of socket bookkeeping.
    """
    try:
        await connection_manager.recheck_user(user_id)
    except Exception as e:
        log.warning("websocket_recheck_user_failed", user_id=user_id, error_type=type(e).__name__)


def _credentials_from_headers(websocket: WebSocket) -> tuple[str | None, str | None]:
    api_key = websocket.headers.get("x-api-key")
    token = None
    auth_header = websocket.headers.get("authorization", "")
    if auth_header.startswith("Bearer "):
        bearer = auth_header[7:].strip()
        if bearer.startswith("rem_"):
            api_key = api_key or bearer
        else:
            token = bearer
    return api_key, token


async def _authenticate(
    websocket: WebSocket, api_key: str | None, token: str | None, *, record_use: bool = True
) -> AuthenticatedUser | None:
    """Validate the socket's credentials like REST. ``record_use=False`` leaves the key's last-used time alone."""
    settings = get_settings()
    if not settings.auth_enabled:
        return AuthenticatedUser(user_id="default_user", api_key_id="dev_key", rate_limit_tier="standard")
    # authenticate_* only use .app/.state, which WebSocket shares with Request.
    conn: Any = websocket
    if token:
        user = await authenticate_jwt(conn, token)
        if user:
            return user
    if api_key:
        return await authenticate_api_key(conn, api_key, record_use=record_use)
    return None


def _project_allowed(user: AuthenticatedUser, project_id: str | None) -> bool:
    return not (project_id and user.project_ids and project_id not in user.project_ids)


def _crew_db(websocket: WebSocket) -> CrewDatabase | None:
    db: CrewDatabase | None = getattr(websocket.app.state, "crew_db", None)
    return db


async def _crew_access_lost(conn: _Connection) -> bool:
    """Whether a subscribed crew is no longer readable (the REST access check); refreshes the summary otherwise."""
    db = _crew_db(conn.websocket)
    if db is None:
        return True  # Crew mode was switched off under an open crew subscription
    for crew_id in list(conn.subs):
        if await _load_crew_ref(db.conn, crew_id, conn.user, CREW_READ) is None:
            return True
    if conn.summary is not None:
        await conn.summary.refresh()
    return False


# ---- crew handlers ----------------------------------------------------------


async def _handle_crew_subscribe(conn: _Connection, msg: dict[str, Any]) -> None:
    crew_id = msg.get("crew_id")
    errors = crew_schemas.validate(msg, crew_schemas.WS_SUBSCRIBE)
    if errors:
        await conn.crew_error(crew_id, "invalid", "invalid crew subscription", errors=errors[:5])
        return
    if conn.credential.source == "query":
        await conn.crew_error(crew_id, "query_credentials", "crew subscriptions require header or first-message credentials")
        return
    if not _has_crew_permission(conn.user, CREW_READ):
        await conn.crew_error(crew_id, "forbidden", "crew:read permission required")
        return
    db = _crew_db(conn.websocket)
    if db is None:
        await conn.crew_error(crew_id, "unavailable", "crew mode is not enabled")
        return
    topics = set(msg["topics"])
    replacing = crew_id in conn.subs or (crew_id == "*" and conn.summary is not None)
    if not replacing and conn.crew_sub_count >= MAX_CREW_SUBS_PER_CONNECTION:
        await conn.crew_error(crew_id, "limit", f"at most {MAX_CREW_SUBS_PER_CONNECTION} crew subscriptions per connection")
        return
    if conn.crew_sub_count == 0 and connection_manager.crew_connection_count(conn.user.user_id) >= MAX_CREW_CONNECTIONS_PER_USER:
        await conn.crew_error(crew_id, "limit", f"at most {MAX_CREW_CONNECTIONS_PER_USER} crew connections per user")
        return
    if crew_id == "*":
        if topics != {"crew.summary"}:
            await conn.crew_error(crew_id, "invalid", 'crew_id "*" supports only the crew.summary topic')
            return
        await _subscribe_summary(conn, db)
        return
    if topics != {"crew"}:
        await conn.crew_error(crew_id, "invalid", "a crew subscription supports only the crew topic")
        return
    await _subscribe_crew(conn, db, str(crew_id), msg.get("since_seq"))


async def _subscribe_summary(conn: _Connection, db: CrewDatabase) -> None:
    connection_manager.remove_summary_sub(conn)
    summary = _SummarySub(conn, db, connection_manager)
    connection_manager.add_summary_sub(summary)
    await conn.send_json({"type": "crew.subscribed", "crew_id": "*", "since_seq": 0, "replayed": 0})
    await summary.refresh()


async def _subscribe_crew(conn: _Connection, db: CrewDatabase, crew_id: str, since_seq: int | None) -> None:
    user = conn.user
    crew = await _load_crew_ref(db.conn, crew_id, user, CREW_READ)
    if crew is None:
        await conn.crew_error(crew_id, "not_found", "crew not found")
        return
    if since_seq is not None:
        wait = connection_manager.allow_replay(user.user_id)
        if wait:
            await conn.crew_error(crew_id, "rate_limited", "too many replay subscribes", retry_after_s=round(wait, 3))
            return
    old = conn.subs.get(crew_id)
    if old is not None:
        connection_manager.remove_crew_sub(old)
    sub = _CrewSub(conn, crew, db, connection_manager)
    connection_manager.add_crew_sub(sub)  # live events queue from here on; replay overlap is skipped by seq
    head = await crew_head(db.conn, crew_id)
    last = head.last_seq if head else 0
    start = last if since_seq is None else int(since_seq)
    if start > last:
        await sub.resync("server_restart")
        return
    if last - start > REPLAY_MAX:
        await sub.resync("gap_too_large")
        return
    events = await fetch_events(db.conn, crew_id, after_seq=start, upto_seq=last, limit=REPLAY_MAX) if last > start else []
    if len(events) != last - start:  # part of the range was pruned by retention
        await sub.resync("gap_too_large")
        return
    await conn.send_json({"type": "crew.subscribed", "crew_id": crew_id, "since_seq": start, "replayed": len(events)})
    for env in events:
        await conn.send_json({"type": "crew.event", "crew_id": crew_id, "data": env})
    sub.next_seq = last + 1
    if sub.active:
        sub.start()


async def _handle_crew_unsubscribe(conn: _Connection, msg: dict[str, Any]) -> None:
    crew_id = msg.get("crew_id")
    if crew_id == "*":
        connection_manager.remove_summary_sub(conn)
    elif isinstance(crew_id, str) and crew_id in conn.subs:
        connection_manager.remove_crew_sub(conn.subs[crew_id])


async def _handle_presence(conn: _Connection, msg: dict[str, Any]) -> None:
    crew_id = msg.get("crew_id")
    errors = crew_schemas.validate(msg, crew_schemas.WS_FRAMES["presence"])
    if errors:
        await conn.crew_error(crew_id, "invalid", "invalid presence frame", errors=errors[:5])
        return
    if conn.credential.source == "query":
        await conn.crew_error(crew_id, "query_credentials", "presence requires header or first-message credentials")
        return
    if not _has_crew_permission(conn.user, CREW_WRITE):
        await conn.crew_error(crew_id, "forbidden", "crew:write permission required")
        return
    db = _crew_db(conn.websocket)
    if db is None:
        await conn.crew_error(crew_id, "unavailable", "crew mode is not enabled")
        return
    user = conn.user
    crew = await _load_crew_ref(db.conn, str(crew_id), user, CREW_WRITE)
    if crew is None:
        await conn.crew_error(crew_id, "not_found", "crew not found")
        return
    lanes_in: list[dict[str, Any]] = msg["lanes"]
    rows = await presence_sessions(db.conn, crew.crew_id, [lane["session_id"] for lane in lanes_in])
    lanes: list[dict[str, Any]] = []
    rejected: list[str] = []
    for lane in lanes_in:
        row = rows.get(lane["session_id"])
        if row is None or row.user_id != user.user_id or row.state in ("ended", "lost"):
            rejected.append(lane["session_id"])
            continue
        # Presence state is server-computed (§10.1): never trust the client's state/stuck.
        lanes.append({**lane, "state": row.state, "stuck": row.stuck})
    if lanes:
        connection_manager.offer_presence(crew.crew_id, lanes)
    if rejected:
        await conn.crew_error(crew_id, "invalid", "presence lanes rejected", session_ids=rejected[:20])


# ---- endpoint ---------------------------------------------------------------


@router.websocket("/ws")
async def websocket_endpoint(
    websocket: WebSocket,
    namespace: str | None = Query(None, description="Deprecated; routing is derived from your credentials"),
    project_id: str | None = Query(None, description="Optional project ID filter"),
    api_key: str | None = Query(None, description="Deprecated: prefer header or auth message"),
    token: str | None = Query(None, description="Deprecated: prefer header or auth message"),
) -> None:
    """Real-time memory events and crew streams for the authenticated principal.

    Memory events: ``memory.created``, ``memory.updated``, ``memory.superseded``,
    ``memory.deleted``. Send ``ping`` for ``pong``; send
    ``{"type": "subscribe", "project_id": "..."}`` to change the project filter.
    Crew frames: see the module docstring.
    """
    await websocket.accept()

    header_key, header_token = _credentials_from_headers(websocket)
    source = "header" if (header_key or header_token) else "none"
    if source == "none" and (api_key or token):
        source = "query"
    api_key = header_key or api_key
    token = header_token or token
    if websocket.query_params.get("api_key") or websocket.query_params.get("token"):
        log.info("websocket_query_credentials_deprecated")

    if not api_key and not token and get_settings().auth_enabled:
        # Browser clients authenticate with a first message so tokens stay out of URLs.
        try:
            raw = await asyncio.wait_for(websocket.receive_text(), timeout=AUTH_MESSAGE_TIMEOUT_SECONDS)
            msg = json.loads(raw)
            if isinstance(msg, dict) and msg.get("type") == "auth":
                api_key = msg.get("api_key") or None
                token = msg.get("token") or None
                project_id = msg.get("project_id", project_id)
                source = "message"
        except (TimeoutError, json.JSONDecodeError, WebSocketDisconnect):
            pass

    user = await _authenticate(websocket, api_key, token)
    if user is None:
        await websocket.close(code=CLOSE_UNAUTHORIZED, reason="Authentication required")
        return
    can_memory = has_permission(user, "memory:recall")
    # crew:read alone opens a socket only when Crew mode is on; otherwise memory:recall is required.
    can_crew = _crew_db(websocket) is not None and _has_crew_permission(user, CREW_READ)
    if not can_memory and not can_crew:
        await websocket.close(code=CLOSE_FORBIDDEN, reason="memory:recall permission required")
        return
    if not _project_allowed(user, project_id):
        await websocket.close(code=CLOSE_FORBIDDEN, reason="No access to project")
        return

    conn = _Connection(websocket, user, _Credential(api_key, token, source))
    connection_manager.add_connection(conn)
    subscriber: _Subscriber | None = None
    if can_memory:
        subscriber = _Subscriber(
            websocket=websocket,
            user_id=user.user_id,
            allowed_projects=tuple(user.project_ids) if user.project_ids else None,
            project_filter=project_id,
            conn=conn,
        )
        conn.memory_subscriber = subscriber
        await connection_manager.register(subscriber)

    def _ns() -> str:
        return f"{user.user_id}:{subscriber.project_filter if subscriber and subscriber.project_filter else '*'}"

    try:
        await conn.send_json(
            {
                "type": "connected",
                "data": {
                    "namespace": _ns(),
                    "project_id": subscriber.project_filter if subscriber else None,
                    "message": "Connected to Remembra real-time updates",
                },
                "timestamp": _now_iso(),
            }
        )

        loop = asyncio.get_running_loop()
        next_recheck = loop.time() + REVALIDATE_INTERVAL_SECONDS
        next_ping = loop.time() + IDLE_PING_SECONDS
        while not conn.closed:
            now = loop.time()
            if now >= next_recheck:
                if not await connection_manager.recheck_connection(conn, crews=True):
                    break
                next_recheck = now + REVALIDATE_INTERVAL_SECONDS
            if now >= next_ping:
                if not await conn.send_text("ping"):
                    break
                next_ping = now + IDLE_PING_SECONDS
            try:
                data = await asyncio.wait_for(
                    websocket.receive_text(), timeout=max(0.0, min(next_recheck, next_ping) - loop.time())
                )
            except TimeoutError:
                continue
            if conn.closed:
                break
            next_ping = loop.time() + IDLE_PING_SECONDS

            if data == "ping":
                await conn.send_text("pong")
                continue

            try:
                msg = json.loads(data)
            except json.JSONDecodeError:
                continue
            if not isinstance(msg, dict):
                continue
            mtype = msg.get("type")
            # With Crew mode off (no crew.db) crew frames are unknown messages here, like on a server
            # without Crew mode: nothing names the feature.
            crew_on = _crew_db(websocket) is not None
            if crew_on and msg.get("channel") == "crew" and mtype == "subscribe":
                await _handle_crew_subscribe(conn, msg)
                continue
            if crew_on and msg.get("channel") == "crew" and mtype == "unsubscribe":
                await _handle_crew_unsubscribe(conn, msg)
                continue
            if crew_on and mtype == "presence":
                await _handle_presence(conn, msg)
                continue
            if mtype != "subscribe":
                continue

            if subscriber is None:
                await conn.send_json(
                    {"type": "error", "data": {"message": "memory:recall permission required"}, "timestamp": _now_iso()}
                )
                continue
            new_project = msg.get("project_id", subscriber.project_filter)
            if not subscriber.may_follow(new_project):
                await conn.send_json(
                    {
                        "type": "error",
                        "data": {"message": "No access to project", "project_id": new_project},
                        "timestamp": _now_iso(),
                    }
                )
                continue
            subscriber.project_filter = new_project
            await conn.send_json(
                {
                    "type": "subscribed",
                    "data": {"namespace": _ns(), "project_id": subscriber.project_filter},
                    "timestamp": _now_iso(),
                }
            )

    except WebSocketDisconnect:
        pass
    except Exception as e:
        log.warning("websocket_error", error_type=type(e).__name__)
    finally:
        connection_manager.remove_connection(conn)
        if subscriber is not None:
            await connection_manager.unregister(subscriber)
        if conn.closing is not None:
            # Send the close frame (and its code) before the handler returns.
            await conn.closing


@router.get("/ws/stats", tags=["websocket"])
async def websocket_stats(request: Request, current_user: CurrentUser) -> dict[str, Any]:
    """Connection statistics: your own connections (platform totals for superadmins)."""
    from remembra.auth.superadmin import is_superadmin

    if await is_superadmin(request, current_user):
        return await connection_manager.get_stats()
    return await connection_manager.get_stats(user_id=current_user.user_id)
