"""Crew outbox worker: applies cross-database effects queued in ``crew_outbox`` (spec D35).

Crew state lives in ``crew.db``; memories and relay handoffs live in the main
database. A crew transaction never writes the main database. Instead it queues
the effect with :meth:`remembra.crew.store.CrewStore.enqueue_outbox` in the same
transaction as the state change, and this worker applies it afterwards.

Delivery is at-least-once, so every handler is idempotent:

* ``memory_promotion`` stamps the stored memory with ``metadata.crew_outbox_id``
  and, before storing, looks for a memory carrying that id. A crash between the
  main-DB write and marking the item done therefore never stores it twice.
* ``relay_handoff`` goes through ``RelayService.close_session`` (one current
  handoff per ``(agent_id, session_id)``). Before closing, it looks for a
  handoff for that session created after the item was queued; if one exists the
  effect already happened (or the agent closed the session itself, which is
  newer and better), so it is not applied again.

Workers serialize the entire read/apply/ack batch per local Crew database.
A nonblocking POSIX file lock excludes other API processes and is released by
the OS if a process dies; it has no lease that can expire during a slow handler.
Do not unlink the persistent lock file, or run crew.db on a filesystem without
shared POSIX locks. This does not make external side effects exactly-once:
handlers still need durable replay checks after a crash. The memory replay
check covers a committed main-DB memory, not a partially completed store or meter.

Failures retry with exponential backoff stored on the row (``next_attempt_at``),
so a restart keeps the schedule. After ``max_attempts``, or on a
:class:`OutboxPermanentError` (a malformed payload), the item is marked
``failed`` with ``last_error`` and kept for inspection
(:meth:`CrewStore.requeue_outbox` puts it back).

Promotions are bounded twice (D15, §12, G7). The producer (checkpoint/decision
service) counts each promotion against the owner's ``crew_memory_promotions_per_day``
when it enqueues it and dates the ones over the cap for the next day; when a
promotion comes due, this handler checks that day's cap again (a deferred row is
pushed on, :class:`OutboxDeferred`, never stored over the cap) and passes the store
through the same plan gate as ``POST /memories`` (memory cap, relay/unenriched
limits, usage metering) when the app has a usage meter.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Final

import structlog

from remembra.crew.store import CrewStore, now_iso

log = structlog.get_logger(__name__)

KIND_MEMORY_PROMOTION: Final = "memory_promotion"
KIND_RELAY_HANDOFF: Final = "relay_handoff"
OUTBOX_KINDS: Final = (KIND_MEMORY_PROMOTION, KIND_RELAY_HANDOFF)

# Memory types a crew promotion may create (§3.1, D15): checkpoints and human-confirmed decisions.
PROMOTION_MEMORY_TYPES: Final = ("checkpoint", "decision")
MAX_PROMOTION_CHARS: Final = 16 * 1024


class OutboxPermanentError(Exception):
    """The item can never succeed (malformed payload): mark it failed without retrying."""


class OutboxDeferred(Exception):
    """Not now (a plan cap): run the item again at ``until`` without counting an attempt."""

    def __init__(self, reason: str, until: datetime) -> None:
        super().__init__(reason)
        self.until = until


@dataclass(frozen=True)
class OutboxItem:
    id: str
    crew_id: str
    kind: str
    payload: dict[str, Any]
    attempts: int
    created_at: str


Handler = Callable[[OutboxItem], Awaitable[str | None]]


class CrewOutboxWorker:
    """Polls ``crew_outbox`` and applies due items with the handler registered for their kind."""

    def __init__(
        self,
        store: CrewStore,
        handlers: Mapping[str, Handler],
        *,
        poll_interval_s: float = 2.0,
        batch_size: int = 20,
        max_attempts: int = 8,
        base_backoff_s: float = 5.0,
        max_backoff_s: float = 600.0,
    ) -> None:
        if poll_interval_s <= 0 or batch_size < 1 or max_attempts < 1 or base_backoff_s < 0 or max_backoff_s < 0:
            raise ValueError("invalid outbox worker configuration")
        self.store = store
        self.handlers: dict[str, Handler] = dict(handlers)
        self.poll_interval_s = poll_interval_s
        self.batch_size = batch_size
        self.max_attempts = max_attempts
        self.base_backoff_s = base_backoff_s
        self.max_backoff_s = max_backoff_s
        self._task: asyncio.Task[None] | None = None
        self._wake = asyncio.Event()
        self._stopping = False

    # -- lifecycle ---------------------------------------------------------------

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def start(self) -> None:
        """Start the polling loop on the running event loop (idempotent)."""
        if self.running:
            return
        self._stopping = False
        self._wake = asyncio.Event()
        self._task = asyncio.create_task(self._loop(), name="crew-outbox-worker")

    async def stop(self) -> None:
        """Stop the loop; an item being applied finishes or is cancelled mid-handler (it stays pending)."""
        self._stopping = True
        self._wake.set()
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    def wake(self) -> None:
        """Process due items now instead of at the next poll (call after enqueueing)."""
        self._wake.set()

    async def _loop(self) -> None:
        while not self._stopping:
            try:
                await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception as e:  # a store failure must not kill the worker
                log.error("crew_outbox_loop_error", error=str(e), error_type=type(e).__name__)
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._wake.wait(), timeout=self.poll_interval_s)
            self._wake.clear()

    # -- processing ------------------------------------------------------------------

    def backoff(self, attempts: int) -> float:
        """Seconds to wait after the ``attempts``-th failure."""
        return float(min(self.max_backoff_s, self.base_backoff_s * (2 ** max(attempts - 1, 0))))

    @contextlib.asynccontextmanager
    async def _exclusive_batch(self) -> AsyncIterator[bool]:
        """Keep competing workers out through the handler and acknowledgement.

        Never hold a Crew SQLite transaction across a handler: handlers may read
        Crew limits or write the main database. If locking fails, propagate the
        error and leave the pending effects untouched, rather than running unlocked.
        """
        db = self.store.db
        if db.outbox_run_lock.locked():
            yield False
            return
        async with db.outbox_run_lock:
            if db.db_path == ":memory:":
                yield True
                return

            # Import here so the SDK still imports on Windows. Crew requires
            # macOS/Linux; unsupported platforms must fail closed when processing.
            import fcntl

            database = Path(db.db_path).resolve()
            lock_path = database.with_name(database.name + ".outbox.lock")
            flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
            fd = os.open(lock_path, flags, 0o600)
            acquired = False
            try:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    acquired = True
                except BlockingIOError:
                    yield False
                    return
                yield True
            finally:
                try:
                    if acquired:
                        fcntl.flock(fd, fcntl.LOCK_UN)
                finally:
                    os.close(fd)

    async def run_once(self) -> dict[str, int]:
        """Apply every due item once; return counts by outcome (``done``, ``retry``, ``failed``)."""
        counts = {"done": 0, "retry": 0, "failed": 0}
        async with self._exclusive_batch() as acquired:
            if not acquired:
                return counts
            for row in await self.store.due_outbox(limit=self.batch_size):
                outcome = await self._process(row)
                counts[outcome] += 1
        return counts

    async def _process(self, row: dict[str, Any]) -> str:
        attempts = int(row["attempts"]) + 1
        try:
            payload = json.loads(row["payload"])
            if not isinstance(payload, dict):
                raise OutboxPermanentError("payload is not an object")
            handler = self.handlers.get(row["kind"])
            if handler is None:
                raise OutboxPermanentError(f"no handler for outbox kind {row['kind']!r}")
            item = OutboxItem(
                id=row["id"],
                crew_id=row["crew_id"],
                kind=row["kind"],
                payload=payload,
                attempts=int(row["attempts"]),
                created_at=row["created_at"],
            )
            result_id = await handler(item)
        except asyncio.CancelledError:
            raise
        except OutboxDeferred as e:
            await self.store.defer_outbox(row["id"], f"deferred: {e}", now_iso(e.until))
            log.info("crew_outbox_deferred", outbox_id=row["id"], kind=row["kind"], reason=str(e), until=now_iso(e.until))
            return "retry"
        except (OutboxPermanentError, json.JSONDecodeError) as e:
            await self.store.mark_outbox_failed(row["id"], f"permanent: {e}")
            log.error("crew_outbox_failed", outbox_id=row["id"], kind=row["kind"], error=str(e), permanent=True)
            return "failed"
        except Exception as e:
            error = f"{type(e).__name__}: {e}"
            if attempts >= self.max_attempts:
                await self.store.mark_outbox_failed(row["id"], error)
                log.error("crew_outbox_failed", outbox_id=row["id"], kind=row["kind"], attempts=attempts, error=error)
                return "failed"
            next_at = now_iso(datetime.now(UTC) + timedelta(seconds=self.backoff(attempts)))
            await self.store.mark_outbox_retry(row["id"], error, next_at)
            log.warning("crew_outbox_retry", outbox_id=row["id"], kind=row["kind"], attempts=attempts, error=error)
            return "retry"
        await self.store.mark_outbox_done(row["id"], result_id)
        log.info("crew_outbox_applied", outbox_id=row["id"], kind=row["kind"], result_id=result_id)
        return "done"


# ---------------------------------------------------------------------------
# Built-in handlers
# ---------------------------------------------------------------------------


def _require_str(payload: Mapping[str, Any], key: str, *, max_len: int = 256) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise OutboxPermanentError(f"payload.{key} must be a non-empty string")
    if len(value) > max_len:
        raise OutboxPermanentError(f"payload.{key} is longer than {max_len} characters")
    return value


def _next_utc_day(now: datetime) -> datetime:
    return datetime.combine(now.date() + timedelta(days=1), datetime.min.time(), tzinfo=UTC)


def memory_promotion_handler(
    main_db: Any,
    memory_service: Any,
    *,
    checkpoint_default_ttl: str | None = None,
    crew_store: CrewStore | None = None,
    limits_for: Callable[[str], Awaitable[Any]] | None = None,
    app: Any = None,
    clock: Callable[[], datetime] | None = None,
) -> Handler:
    """Handler that stores a crew checkpoint or confirmed decision as ONE memory (D15, §3.1).

    Payload: ``{user_id, project_id, memory_type: checkpoint|decision, content,
    metadata?: {...}, ttl?: str}``. The memory is stored atomically (never
    fact-split), with ``metadata.source = "crew"``, ``crew_id`` and
    ``crew_outbox_id``. Returns the memory id.

    With ``crew_store`` and ``limits_for`` (the owner's :class:`CrewLimits`), a
    checkpoint promotion that is not quota/lost is stored only while the owner's
    promotions stored today are under ``memory_promotions_per_day``; otherwise it is
    deferred to the next UTC day. With ``app`` (its ``usage_meter``), the store goes
    through ``gate_write`` like ``POST /memories`` (memory cap and relay/unenriched
    limits: 429 defers an hour, other refusals fail the item) and is metered.
    """
    from remembra.crew.limits import ALWAYS_PROMOTE_TRIGGERS
    from remembra.models.memory import RELAY_MEMORY_TYPES, StoreRequest
    from remembra.services.agent_session import apply_memory_type_policy

    now_fn = clock or (lambda: datetime.now(UTC))

    async def _cap_check(item: OutboxItem, user_id: str, memory_type: str, trigger: Any) -> None:
        if crew_store is None or limits_for is None or memory_type != "checkpoint" or trigger in ALWAYS_PROMOTE_TRIGGERS:
            return
        now = now_fn()
        limits = await limits_for(user_id)
        start = datetime.combine(now.date(), datetime.min.time(), tzinfo=UTC)
        row = await crew_store.db.fetchone(
            "SELECT COUNT(*) AS n FROM crew_outbox WHERE kind = 'memory_promotion' AND state = 'done'"
            " AND json_extract(payload, '$.user_id') = ? AND updated_at >= ? AND id != ?",
            (user_id, now_iso(start), item.id),
        )
        if int(row["n"] if row else 0) >= int(limits.memory_promotions_per_day):
            raise OutboxDeferred("memory_promotions_per_day reached", _next_utc_day(now))

    async def _gate(user_id: str, project_id: str, content: str, relay: bool) -> Any:
        if app is None or getattr(app.state, "usage_meter", None) is None:
            return None
        from types import SimpleNamespace

        from fastapi import HTTPException

        from remembra.cloud.limits import gate_write

        request = SimpleNamespace(app=app, state=SimpleNamespace(), url=SimpleNamespace(path="/crew/outbox/memory_promotion"))
        try:
            return await gate_write(
                request,  # type: ignore[arg-type]
                None,
                user_id,
                [content],
                atomic=[True],
                project_ids=[project_id],
                relay=relay,
                enforce_batch_limit=False,
            )
        except HTTPException as e:
            if e.status_code == 429:
                raise OutboxDeferred(f"plan limit: {e.detail}", now_fn() + timedelta(hours=1)) from None
            raise OutboxPermanentError(f"plan refused the promotion: {e.status_code} {e.detail}") from None

    async def _meter(user_id: str, relay: bool) -> None:
        meter = getattr(getattr(app, "state", None), "usage_meter", None) if app is not None else None
        if meter is None:
            return
        if relay:
            await meter.record_relay_event(user_id, 1)
        else:
            await meter.record_store(user_id, 1)

    async def handle(item: OutboxItem) -> str | None:
        payload = item.payload
        user_id = _require_str(payload, "user_id")
        project_id = _require_str(payload, "project_id")
        memory_type = payload.get("memory_type")
        if memory_type not in PROMOTION_MEMORY_TYPES:
            raise OutboxPermanentError(f"payload.memory_type must be one of {', '.join(PROMOTION_MEMORY_TYPES)}")
        content = _require_str(payload, "content", max_len=MAX_PROMOTION_CHARS)
        extra = payload.get("metadata") or {}
        if not isinstance(extra, dict):
            raise OutboxPermanentError("payload.metadata must be an object")
        ttl = payload.get("ttl")
        if ttl is not None and not isinstance(ttl, str):
            raise OutboxPermanentError("payload.ttl must be a string")

        cursor = await main_db.conn.execute(
            """
            SELECT id FROM memories
             WHERE user_id = ? AND project_id = ? AND memory_type = ?
               AND json_valid(metadata) AND json_extract(metadata, '$.crew_outbox_id') = ?
             LIMIT 1
            """,
            (user_id, project_id, memory_type, item.id),
        )
        existing = await cursor.fetchone()
        if existing is not None:
            return str(existing[0])

        await _cap_check(item, user_id, str(memory_type), extra.get("checkpoint_trigger"))
        relay = memory_type in RELAY_MEMORY_TYPES
        grant = await _gate(user_id, project_id, content, relay)
        metadata = {**extra, "source": "crew", "crew_id": item.crew_id, "crew_outbox_id": item.id}
        try:
            request = StoreRequest(
                content=content,
                user_id=user_id,
                project_id=project_id,
                memory_type=memory_type,
                metadata=metadata,
                ttl=ttl,
                skip_extraction=True,
            )
        except ValueError as e:  # pydantic ValidationError is a ValueError
            raise OutboxPermanentError(f"invalid memory: {e}") from e
        ttl_default = checkpoint_default_ttl or getattr(getattr(memory_service, "settings", None), "checkpoint_default_ttl", None)
        apply_memory_type_policy(request, ttl_default or "7d")  # "7d" = Settings default
        if grant is not None:
            with grant.activate():
                stored = await memory_service.store(request, source="agent_generated", skip_extraction=True)
        else:
            stored = await memory_service.store(request, source="agent_generated", skip_extraction=True)
        if getattr(stored, "status", "stored") == "not_stored" or not getattr(stored, "id", None):
            raise RuntimeError("memory service did not store the promotion")
        await _meter(user_id, relay)
        return str(stored.id)

    return handle


def relay_handoff_handler(
    relay_service: Any,
    *,
    screen: Callable[[str], Any] | None = None,
    scrub: Callable[[str], str] | None = None,
) -> Handler:
    """Handler that writes a relay handoff for a crew session (stall, lost, leave).

    Payload: ``{user_id, project_id, agent_id, session_id, facts: {...},
    summary?, end_reason?, agent_verified?: bool}``. Returns the handoff memory id.
    """
    from remembra.services.relay import relay_key

    async def handle(item: OutboxItem) -> str | None:
        payload = item.payload
        user_id = _require_str(payload, "user_id")
        project_id = _require_str(payload, "project_id")
        agent_id = _require_str(payload, "agent_id", max_len=128)
        session_id = _require_str(payload, "session_id", max_len=256)
        facts = payload.get("facts")
        if not isinstance(facts, dict):
            raise OutboxPermanentError("payload.facts must be an object")
        summary = payload.get("summary")
        end_reason = payload.get("end_reason")
        for key, value in (("summary", summary), ("end_reason", end_reason)):
            if value is not None and not isinstance(value, str):
                raise OutboxPermanentError(f"payload.{key} must be a string")
        agent_verified = payload.get("agent_verified", False)
        if not isinstance(agent_verified, bool):
            raise OutboxPermanentError("payload.agent_verified must be a boolean")

        cursor = await relay_service.db.conn.execute(
            """
            SELECT id FROM memories
             WHERE user_id = ? AND project_id = ? AND memory_type = 'handoff'
               AND json_valid(metadata) AND json_extract(metadata, '$.relay_key') = ?
               AND julianday(created_at) >= julianday(?)
             ORDER BY julianday(created_at) DESC, id DESC LIMIT 1
            """,
            (user_id, project_id, relay_key(agent_id, session_id), item.created_at),
        )
        existing = await cursor.fetchone()
        if existing is not None:
            return str(existing[0])

        result = await relay_service.close_session(
            user_id=user_id,
            project_id=project_id,
            agent_id=agent_id,
            session_id=session_id,
            facts=facts,
            summary=summary,
            end_reason=end_reason,
            agent_verified=agent_verified,
            screen=screen,
            scrub=scrub,
            server_facts=True,  # queued by the server itself: keeps facts_source "server-inferred"
        )
        handoff_id = result.get("handoff_id") if isinstance(result, dict) else None
        if not handoff_id:
            raise RuntimeError("relay close_session returned no handoff id")
        return str(handoff_id)

    return handle


def default_handlers(
    *,
    main_db: Any,
    memory_service: Any,
    relay_service: Any,
    checkpoint_default_ttl: str | None = None,
    screen: Callable[[str], Any] | None = None,
    scrub: Callable[[str], str] | None = None,
    crew_store: CrewStore | None = None,
    limits_for: Callable[[str], Awaitable[Any]] | None = None,
    app: Any = None,
) -> dict[str, Handler]:
    """The production handler map for :class:`CrewOutboxWorker`."""
    return {
        KIND_MEMORY_PROMOTION: memory_promotion_handler(
            main_db,
            memory_service,
            checkpoint_default_ttl=checkpoint_default_ttl,
            crew_store=crew_store,
            limits_for=limits_for,
            app=app,
        ),
        KIND_RELAY_HANDOFF: relay_handoff_handler(relay_service, screen=screen, scrub=scrub),
    }
