// Pickup slots (spec §9.3 "Pickup slots", §9.4): reserved claims render as a
// dashed empty lane waiting for the next runner.
//
//   Waiting for the next runner: POS section, handed off by cc-2 12m ago
//   (credits ran out). 3 uncommitted files saved. Held until picked up or
//   released.  [Hand baton to…] [Copy pickup command] [Release]
//
// Parked slots (idle for an hour, or its machine offline past the lease) are
// held for the same agent: "cc-1 idle 1h; resumes automatically if it comes
// back". Only a human can hand those on (§10.1).
// One slot per task (a task's zones travel together as one baton), or per
// holder and reason for reservations without a task.

import { parseServerTime } from '../../../lib/time';
import type { ClaimView, CrewEvent, CrewState, ReserveReason, SessionState, TaskView } from '../../../lib/crew/types';
import { pickupCommand, shortAge } from './model';

export interface PickupZone {
  claimId: string;
  slug: string;
  /** Untrusted zone title (plain text). */
  title: string | null;
}

export interface PickupSlotView {
  /** Stable key: `task:<id>` or `hold:<session>:<reason>`. */
  key: string;
  claims: ClaimView[];
  zones: PickupZone[];
  task: TaskView | null;
  taskRef: string | null;
  reason: ReserveReason | null;
  /** "credits ran out", "went silent", … */
  reasonText: string;
  fromSessionId: string | null;
  fromCallsign: string | null;
  /** When the baton was put down (ms), when known. */
  sinceMs: number | null;
  /** Uncommitted files saved in the baton ref, when known. */
  savedFiles: number | null;
  unpushed: number | null;
  batonRef: string | null;
  /** Held for the same agent (idle or its machine offline): it resumes if it comes back. */
  parked: boolean;
  /** Callsigns the baton was offered to (brief, human or reserved_for). */
  offeredTo: string[];
  pickupCommand: string | null;
}

const REASON_TEXT: Record<ReserveReason, string> = {
  quota: 'credits ran out',
  lost: 'went silent',
  ended_dirty: 'ended with unsaved work',
  baton: 'passed the baton',
  human_hold: 'held by a human',
  idle: 'idle',
  offline: 'its machine went offline',
};

export function reasonText(reason: ReserveReason | null | undefined, session: SessionState | null): string {
  if (reason === 'lost' && session?.state_reason === 'process_exited') return 'its process exited';
  if (reason === 'quota' && session?.state_reason === 'rate_limit') return 'hit a rate limit';
  return reason ? REASON_TEXT[reason] : 'released the baton';
}

/** When each claim became reserved, from `claim.reserved` events we have seen (claim id → ms). */
export function reservedTimes(events: readonly CrewEvent[]): Map<string, number> {
  const out = new Map<string, number>();
  for (const event of events) {
    if (event.type !== 'claim.reserved') continue;
    const claim = (event.payload?.claim ?? null) as { id?: string } | null;
    const at = parseServerTime(event.ts);
    if (claim?.id && at) out.set(claim.id, at.getTime());
  }
  return out;
}

function batonFacts(state: CrewState, claims: ClaimView[], task: TaskView | null, events: readonly CrewEvent[]) {
  const byRef = claims.map((c) => c.baton_ref).find((r): r is string => !!r) ?? null;
  // refs created since the snapshot are in the reducer state; older ones come from the event window
  const known: [string, { seq: number; task_id: string | null; dirty_files: number; unpushed: number }][] = Object.entries(
    state.baton_refs,
  );
  for (const e of events) {
    if (e.type !== 'baton.ref_created' || typeof e.payload?.ref !== 'string') continue;
    known.push([
      e.payload.ref,
      {
        seq: e.seq,
        task_id: typeof e.payload.task_id === 'string' ? e.payload.task_id : null,
        dirty_files: Number(e.payload.dirty_files) || 0,
        unpushed: Number(e.payload.unpushed) || 0,
      },
    ]);
  }
  known.sort((a, b) => b[1].seq - a[1].seq);
  let match = byRef ? known.find(([ref]) => ref === byRef) : undefined;
  if (!match && task) match = known.find(([, e]) => e.task_id === task.id);
  return {
    ref: match ? match[0] : byRef,
    savedFiles: match ? match[1].dirty_files : null,
    unpushed: match ? match[1].unpushed : null,
  };
}

export function pickupSlots(state: CrewState, events: readonly CrewEvent[] = []): PickupSlotView[] {
  const reserved = Object.values(state.claims).filter((c) => c.state === 'reserved');
  const groups = new Map<string, ClaimView[]>();
  for (const claim of reserved) {
    const key = claim.task_id
      ? `task:${claim.task_id}`
      : `hold:${claim.holder_session_id ?? claim.holder_user_id ?? '-'}:${claim.reserve_reason ?? '-'}`;
    const list = groups.get(key) ?? [];
    list.push(claim);
    groups.set(key, list);
  }
  const times = reservedTimes(events);
  const slots: PickupSlotView[] = [];
  for (const [key, claims] of groups) {
    claims.sort((a, b) => a.id.localeCompare(b.id));
    const first = claims[0];
    const task = first.task_id ? (state.tasks[first.task_id] ?? null) : null;
    const from = first.holder_session_id ? (state.sessions[first.holder_session_id] ?? null) : null;
    const zones = claims.map((c) => {
      const zone = c.zone_id ? state.zones[c.zone_id] : undefined;
      return { claimId: c.id, slug: zone?.slug ?? c.resource ?? c.path_glob ?? c.id, title: zone?.title ?? null };
    });
    const seen = claims.map((c) => times.get(c.id)).filter((t): t is number => t !== undefined);
    let sinceMs: number | null = seen.length ? Math.min(...seen) : null;
    if (sinceMs === null && from) {
      const fallback = parseServerTime(from.ended_at ?? from.last_activity_at ?? null);
      sinceMs = fallback ? fallback.getTime() : null;
    }
    const facts = batonFacts(state, claims, task, events);
    const claimIds = new Set(claims.map((c) => c.id));
    const offeredTo = [
      ...new Set(
        Object.values(state.offers)
          .filter((o) => claimIds.has(o.claim_id))
          .map((o) => state.sessions[o.to_session]?.callsign ?? o.to_session),
      ),
    ].sort();
    slots.push({
      key,
      claims,
      zones,
      task,
      taskRef: task ? `T-${task.number}` : (first.task_id ?? null),
      reason: first.reserve_reason ?? null,
      reasonText: reasonText(first.reserve_reason, from),
      fromSessionId: first.holder_session_id ?? null,
      fromCallsign: from?.callsign ?? (first.holder_kind === 'human' ? 'a human' : (first.holder_agent_id ?? null)),
      sinceMs,
      savedFiles: facts.savedFiles,
      unpushed: facts.unpushed,
      batonRef: facts.ref,
      parked: first.reserve_reason === 'idle' || first.reserve_reason === 'offline',
      offeredTo,
      // a parked baton is not offered to others: only a human hands it on
      pickupCommand: first.reserve_reason === 'idle' || first.reserve_reason === 'offline' ? null : pickupCommand(task),
    });
  }
  // task batons first (they never expire silently), then by age
  return slots.sort((a, b) => Number(!a.task) - Number(!b.task) || (a.sinceMs ?? 0) - (b.sinceMs ?? 0) || a.key.localeCompare(b.key));
}

function plural(n: number, word: string): string {
  return `${n} ${word}${n === 1 ? '' : 's'}`;
}

/** The zones of a slot in words: titles when known ("POS section"), else slugs. Plain text only. */
export function slotZonesText(slot: PickupSlotView): string {
  return slot.zones.map((z) => z.title || z.slug).join(', ');
}

/** The slot's sentence, rendered as plain text (zone titles are untrusted, never markup). */
export function slotSentence(slot: PickupSlotView, nowMs: number): { lead: string; saved: string; hold: string } {
  const ago = slot.sinceMs !== null ? ` ${shortAge((nowMs - slot.sinceMs) / 1000)} ago` : '';
  if (slot.parked) {
    const who = slot.fromCallsign ?? 'its holder';
    const age = slot.sinceMs !== null ? ` ${shortAge((nowMs - slot.sinceMs) / 1000)}` : '';
    const lead =
      slot.reason === 'offline'
        ? `${who}'s machine offline${age}; it resumes automatically when the machine is back.`
        : `${who} idle${age}; resumes automatically if it comes back.`;
    return { lead, saved: '', hold: 'Held for the same agent. Only you can hand it on.' };
  }
  const who = slot.fromCallsign ? `handed off by ${slot.fromCallsign}${ago}` : `reserved${ago}`;
  const lead = `Waiting for the next runner: ${slotZonesText(slot)}, ${who} (${slot.reasonText}).`;
  let saved = '';
  if (slot.savedFiles !== null && slot.savedFiles > 0) saved = `${plural(slot.savedFiles, 'uncommitted file')} saved.`;
  else if (slot.batonRef) saved = 'Work saved as a baton ref.';
  if (slot.unpushed) saved = `${saved}${saved ? ' ' : ''}${plural(slot.unpushed, 'commit')} not pushed yet.`;
  const hold = slot.task ? 'Held until picked up or released.' : 'Held until picked up, released, or it expires after 24 h.';
  return { lead, saved, hold };
}
