"""Crew retention and digests (WP-2, spec §4.5).

A nightly job, per crew:

1. verifies the hash chain (``verify_crew_chain``) **before** anything is pruned;
2. rolls prunable events into ``crew_digests`` and deletes them **in the same
   transaction**, in batches of at most 500 rows, so a crash can never lose an
   event without its digest (and re-running is a no-op). The same transaction
   records each deleted run of seqs in ``crew_pruned_ranges`` with the chain
   links on both sides, so the verifier can tell a pruned gap from a deleted
   event (adjacent runs are merged into one range);
3. applies the per-table rules of the §4.5 table (checkpoint facts, footprints of
   ended sessions, ended session rows, baton brief text) and the 72 h
   ``crew_idempotency`` window.

Never deleted: moment events, human actions, bypasses and policy changes; task
state, reports, decisions, handoffs and baton rows.

``crew_digests.counts`` is ``{event type: number of pruned events}`` for that UTC
day (merged across batches and runs). ``crew_digests.moments`` lists the day's
moment events (``[{seq, type}]``, which are kept in ``crew_events``), captured
the first time the day is digested.
"""

from __future__ import annotations

import asyncio
import json
from collections import defaultdict
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Final

import aiosqlite
import structlog

from remembra.cloud.plans import PlanTier
from remembra.crew.events import CrewDatabase, format_ts, utc_now, verify_crew_chain
from remembra.crew.limits import SELF_HOSTED_CREW_LIMITS, CrewRetention, crew_limits_for_owner, crew_limits_for_tier

log = structlog.get_logger(__name__)

BATCH: Final = 500


@dataclass(frozen=True)
class RetentionPolicy:
    """Days to keep each class of data (None = keep). §4.5 table."""

    raw_event_days: int | None
    burst_days: int | None
    checkpoint_facts_days: int | None
    ended_session_days: int | None
    baton_brief_days: int | None
    footprint_days_after_end: int = 7


def policy_from_retention(retention: CrewRetention) -> RetentionPolicy:
    """The job's view of a plan's §4.5 windows (the numbers live in ``crew.limits`` / ``cloud.plans``, WP-14)."""
    return RetentionPolicy(
        raw_event_days=retention.events_days,
        burst_days=retention.activity_burst_days,
        checkpoint_facts_days=retention.checkpoint_facts_days,
        ended_session_days=retention.ended_sessions_days,
        baton_brief_days=retention.brief_text_days,
        footprint_days_after_end=retention.footprints_after_end_days,
    )


def _tier_policy(tier: PlanTier) -> RetentionPolicy:
    return policy_from_retention(crew_limits_for_tier(tier).retention)


FREE_POLICY: Final = _tier_policy(PlanTier.FREE)
PRO_POLICY: Final = _tier_policy(PlanTier.PRO)
TEAM_POLICY: Final = _tier_policy(PlanTier.TEAM)
ENTERPRISE_POLICY: Final = _tier_policy(PlanTier.ENTERPRISE)
# Self-hosted servers (no metering) use the same defaults WP-14 applies to their limits.
SELF_HOSTED_POLICY: Final = policy_from_retention(SELF_HOSTED_CREW_LIMITS.retention)

IDEMPOTENCY_RETENTION: Final = timedelta(hours=72)

# Kept forever regardless of plan (§4.5 last row): moments, human actions, bypasses, policy changes.
KEEP_TYPES: Final = frozenset(
    {
        "guard.bypass_used",
        "guard.tamper_blocked",
        "gate.tampered",
        "zone.synced",
        "zone.change_pending",
        "zone.change_decided",
        "zone.suggested_applied",
        "crew.settings_changed",
        "human.override",
    }
)

PolicyResolver = Callable[[str], Awaitable[RetentionPolicy]]


def policy_for_tier(tier: str | None) -> RetentionPolicy:
    """Windows for a plan tier; an unknown tier gets the Free windows."""
    try:
        return policy_from_retention(crew_limits_for_tier(str(tier or "free").lower()).retention)
    except (KeyError, ValueError):
        return FREE_POLICY


def usage_meter_resolver(usage_meter: Any | None) -> PolicyResolver:
    """Resolve a crew owner's windows from their plan, exactly as ``crew.limits`` does for caps.

    Uses :func:`remembra.crew.limits.crew_limits_for_owner` (``UsageMeter.get_account``,
    so a team member's pooled plan counts). Without a usage meter (self-hosted, cloud
    disabled) every crew gets :data:`SELF_HOSTED_POLICY`.
    """

    async def resolve(owner_user_id: str) -> RetentionPolicy:
        if usage_meter is None:
            return SELF_HOSTED_POLICY
        limits = await crew_limits_for_owner(usage_meter, owner_user_id)
        return policy_from_retention(limits.retention)

    return resolve


@dataclass
class RetentionReport:
    crews: int = 0
    events_pruned: int = 0
    digests_written: int = 0
    checkpoint_facts_cleared: int = 0
    footprints_pruned: int = 0
    sessions_pruned: int = 0
    baton_briefs_cleared: int = 0
    idempotency_pruned: int = 0
    chain_errors: dict[str, list[str]] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)


def _is_kept(etype: str, moment: int, actor_kind: str | None) -> bool:
    return bool(moment) or actor_kind == "human" or etype in KEEP_TYPES


def _cut(now: datetime, days: int | None) -> str | None:
    return None if days is None else format_ts(now - timedelta(days=days))


async def _merge_digest(conn: aiosqlite.Connection, crew_id: str, day: str, counts: dict[str, int]) -> bool:
    """Add pruned counts into the day's digest row. Returns True if the row was created."""
    async with conn.execute("SELECT counts FROM crew_digests WHERE crew_id = ? AND day = ?", (crew_id, day)) as cur:
        row = await cur.fetchone()
    if row is not None:
        merged: dict[str, int] = json.loads(row[0] or "{}")
        for k, v in counts.items():
            merged[k] = int(merged.get(k, 0)) + v
        await conn.execute(
            "UPDATE crew_digests SET counts = ? WHERE crew_id = ? AND day = ?",
            (json.dumps(merged, sort_keys=True), crew_id, day),
        )
        return False
    start = f"{day}T00:00:00.000Z"
    end = format_ts(datetime.fromisoformat(f"{day}T00:00:00+00:00") + timedelta(days=1))
    async with conn.execute(
        "SELECT seq, type FROM crew_events WHERE crew_id = ? AND moment = 1 AND ts >= ? AND ts < ? ORDER BY seq LIMIT ?",
        (crew_id, start, end, BATCH),
    ) as cur:
        moments = [{"seq": int(r[0]), "type": r[1]} for r in await cur.fetchall()]
    await conn.execute(
        "INSERT INTO crew_digests (crew_id, day, counts, moments) VALUES (?, ?, ?, ?)",
        (crew_id, day, json.dumps(counts, sort_keys=True), json.dumps(moments)),
    )
    return True


async def _record_pruned_runs(conn: aiosqlite.Connection, crew_id: str, runs: list[tuple[int, int, str, str]]) -> None:
    """Record deleted runs ``(first_seq, last_seq, prev_hash, last_hash)``, merging with adjacent recorded ranges."""
    stamp = format_ts(utc_now())
    for first, last, prev_hash, last_hash in runs:
        async with conn.execute(
            "SELECT first_seq, prev_hash FROM crew_pruned_ranges WHERE crew_id = ? AND last_seq = ?", (crew_id, first - 1)
        ) as cur:
            before = await cur.fetchone()
        async with conn.execute(
            "SELECT last_seq, last_hash FROM crew_pruned_ranges WHERE crew_id = ? AND first_seq = ?", (crew_id, last + 1)
        ) as cur:
            after = await cur.fetchone()
        if before is not None:
            first, prev_hash = int(before[0]), str(before[1])
            await conn.execute("DELETE FROM crew_pruned_ranges WHERE crew_id = ? AND first_seq = ?", (crew_id, first))
        if after is not None:
            await conn.execute("DELETE FROM crew_pruned_ranges WHERE crew_id = ? AND first_seq = ?", (crew_id, last + 1))
            last, last_hash = int(after[0]), str(after[1])
        await conn.execute(
            "INSERT INTO crew_pruned_ranges (crew_id, first_seq, last_seq, prev_hash, last_hash, pruned_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (crew_id, first, last, prev_hash, last_hash, stamp),
        )


def _runs(doomed: list[tuple[int, str, str]]) -> list[tuple[int, int, str, str]]:
    """Consecutive seqs among ``(seq, prev_hash, hash)`` (in seq order) → ``(first, last, prev_hash, last_hash)``."""
    out: list[tuple[int, int, str, str]] = []
    for seq, prev_hash, digest in doomed:
        if out and out[-1][1] == seq - 1:
            first, _last, first_prev, _h = out[-1]
            out[-1] = (first, seq, first_prev, digest)
        else:
            out.append((seq, seq, prev_hash, digest))
    return out


async def prune_crew_events(
    db: CrewDatabase,
    crew_id: str,
    policy: RetentionPolicy,
    *,
    now: datetime,
    batch: int = BATCH,
    report: RetentionReport | None = None,
) -> int:
    """Digest-then-delete prunable events of one crew in ≤``batch``-row transactions."""
    report = report if report is not None else RetentionReport()
    raw_cut = _cut(now, policy.raw_event_days)
    burst_cut = _cut(now, policy.burst_days)
    cuts = [c for c in (raw_cut, burst_cut) if c is not None]
    if not cuts:
        return 0
    # Client events may be backdated by up to 24 h, so scan one day past the latest cutoff.
    stop_ts = format_ts(max(datetime.fromisoformat(c.replace("Z", "+00:00")) for c in cuts) + timedelta(days=1))
    cursor = 0
    pruned = 0
    while True:
        async with db.transaction():
            conn = db.conn
            async with conn.execute(
                "SELECT seq, ts, type, moment, actor_kind, prev_hash, hash FROM crew_events "
                "WHERE crew_id = ? AND seq > ? ORDER BY seq LIMIT ?",
                (crew_id, cursor, batch),
            ) as cur:
                rows = list(await cur.fetchall())
            if not rows:
                break
            cursor = int(rows[-1][0])
            doomed: list[tuple[int, str, str]] = []
            per_day: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
            for seq, ts, etype, moment, actor_kind, prev_hash, digest in rows:
                if _is_kept(etype, moment, actor_kind):
                    continue
                cut = burst_cut if etype == "activity.burst" else raw_cut
                if cut is not None and ts < cut:
                    doomed.append((int(seq), str(prev_hash or ""), str(digest or "")))
                    per_day[ts[:10]][etype] += 1
            if doomed:
                for day, counts in sorted(per_day.items()):
                    if await _merge_digest(conn, crew_id, day, dict(counts)):
                        report.digests_written += 1
                seqs = [d[0] for d in doomed]
                marks = ",".join("?" * len(seqs))
                await conn.execute(f"DELETE FROM crew_events WHERE crew_id = ? AND seq IN ({marks})", (crew_id, *seqs))
                await _record_pruned_runs(conn, crew_id, _runs(doomed))
                pruned += len(doomed)
            done = len(rows) < batch or rows[-1][1] >= stop_ts
        if done:
            break
        await asyncio.sleep(0)  # let other crew.db work in between batches
    report.events_pruned += pruned
    return pruned


async def _batched(db: CrewDatabase, sql: str, params: Sequence[Any]) -> int:
    """Run a ``... WHERE rowid IN (SELECT rowid ... LIMIT ?)`` statement until it touches no rows."""
    total = 0
    while True:
        async with db.transaction():
            cur = await db.conn.execute(sql, (*params, BATCH))
            n = cur.rowcount if cur.rowcount is not None else 0
        total += max(0, n)
        if n < BATCH:
            return total
        await asyncio.sleep(0)


async def prune_crew_tables(
    db: CrewDatabase, crew_id: str, policy: RetentionPolicy, *, now: datetime, report: RetentionReport
) -> None:
    footprint_cut = _cut(now, policy.footprint_days_after_end)
    report.footprints_pruned += await _batched(
        db,
        """DELETE FROM crew_footprints WHERE rowid IN (
               SELECT f.rowid FROM crew_footprints f JOIN crew_sessions s ON s.id = f.session_id
               WHERE f.crew_id = ? AND s.state = 'ended' AND s.ended_at < ? LIMIT ?)""",
        (crew_id, footprint_cut),
    )
    if (cut := _cut(now, policy.ended_session_days)) is not None:
        report.sessions_pruned += await _batched(
            db,
            """DELETE FROM crew_sessions WHERE rowid IN (
                   SELECT rowid FROM crew_sessions WHERE crew_id = ? AND state = 'ended' AND ended_at < ? LIMIT ?)""",
            (crew_id, cut),
        )
    if (cut := _cut(now, policy.checkpoint_facts_days)) is not None:
        report.checkpoint_facts_cleared += await _batched(
            db,
            """UPDATE crew_checkpoints SET facts = NULL WHERE rowid IN (
                   SELECT rowid FROM crew_checkpoints WHERE crew_id = ? AND facts IS NOT NULL AND created_at < ? LIMIT ?)""",
            (crew_id, cut),
        )
    if (cut := _cut(now, policy.baton_brief_days)) is not None:
        report.baton_briefs_cleared += await _batched(
            db,
            """UPDATE crew_batons SET brief_text = NULL WHERE rowid IN (
                   SELECT rowid FROM crew_batons WHERE crew_id = ? AND brief_text IS NOT NULL AND created_at < ? LIMIT ?)""",
            (crew_id, cut),
        )


async def run_retention(
    db: CrewDatabase,
    *,
    resolve_policy: PolicyResolver,
    now: datetime | None = None,
    verify_chain: bool = True,
) -> RetentionReport:
    """The nightly job. Each step is isolated: one crew's failure is logged and the rest continue."""
    now = now or utc_now()
    report = RetentionReport()
    async with db.conn.execute("SELECT id, owner_user_id FROM crews ORDER BY id") as cur:
        crews = [(r[0], r[1]) for r in await cur.fetchall()]
    policies: dict[str, RetentionPolicy] = {}
    for crew_id, owner in crews:
        report.crews += 1
        try:
            if owner not in policies:
                policies[owner] = await resolve_policy(owner)
            policy = policies[owner]
            if verify_chain:
                chain = await verify_crew_chain(db.conn, crew_id)
                if not chain.ok:
                    report.chain_errors[crew_id] = chain.errors
                    log.error("crew_hash_chain_broken", crew_id=crew_id, errors=chain.errors[:10])
            await prune_crew_events(db, crew_id, policy, now=now, report=report)
            await prune_crew_tables(db, crew_id, policy, now=now, report=report)
        except Exception as e:
            report.errors.append(f"{crew_id}: {type(e).__name__}: {e}")
            log.error("crew_retention_failed", crew_id=crew_id, error_type=type(e).__name__, error=str(e))
    try:
        report.idempotency_pruned += await _batched(
            db,
            "DELETE FROM crew_idempotency WHERE rowid IN (SELECT rowid FROM crew_idempotency WHERE created_at < ? LIMIT ?)",
            (format_ts(now - IDEMPOTENCY_RETENTION),),
        )
    except Exception as e:
        report.errors.append(f"idempotency: {type(e).__name__}: {e}")
        log.error("crew_idempotency_retention_failed", error_type=type(e).__name__, error=str(e))
    log.info(
        "crew_retention_done",
        crews=report.crews,
        events_pruned=report.events_pruned,
        digests=report.digests_written,
        errors=len(report.errors),
        broken_chains=len(report.chain_errors),
    )
    return report


RUN_AT_UTC: Final = (3, 17)  # 03:17 UTC, off the hour


def seconds_until_next_run(now: datetime, at: tuple[int, int] = RUN_AT_UTC) -> float:
    target = now.replace(hour=at[0], minute=at[1], second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return (target - now).total_seconds()


async def retention_loop(
    db: CrewDatabase,
    resolve_policy: PolicyResolver,
    *,
    clock: Callable[[], datetime] = utc_now,
    sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
) -> None:
    """Run :func:`run_retention` every night at :data:`RUN_AT_UTC` until cancelled."""
    while True:
        await sleep(seconds_until_next_run(clock()))
        try:
            await run_retention(db, resolve_policy=resolve_policy, now=clock())
        except Exception as e:  # keep the schedule alive
            log.error("crew_retention_run_failed", error_type=type(e).__name__, error=str(e))
