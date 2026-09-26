// The lane activity strip (spec §9.3 item 4): the last 60 minutes in 1-minute
// buckets, with a mark for each thing worth seeing at a glance:
//
//   ◆ checkpoint  ▲ guard block  ✕ failing test  ★ done  ⇢ baton out
//   ⇠ baton in    ⬆ push         ⛔ tamper blocked
//
// Every event of the session also adds to the bucket's density, which the
// strip draws as dithered pixels (busy minutes look denser).

import { parseServerTime } from '../../../lib/time';
import { eventSessionId } from '../../../lib/crew/reducer';
import type { CrewEvent } from '../../../lib/crew/types';

export type MarkKind = 'checkpoint' | 'guard' | 'fail' | 'done' | 'baton_out' | 'baton_in' | 'push' | 'tamper';

export const MARK_GLYPH: Record<MarkKind, string> = {
  checkpoint: '◆',
  guard: '▲',
  fail: '✕',
  done: '★',
  baton_out: '⇢',
  baton_in: '⇠',
  push: '⬆',
  tamper: '⛔',
};

export const MARK_LABEL: Record<MarkKind, string> = {
  checkpoint: 'checkpoint',
  guard: 'guard block',
  fail: 'failing test',
  done: 'task done',
  baton_out: 'baton out',
  baton_in: 'baton in',
  push: 'push',
  tamper: 'tamper blocked',
};

/** Marks that are alarms (drawn in the signal colour). */
export const ALARM_MARKS: ReadonlySet<MarkKind> = new Set(['guard', 'fail', 'tamper']);

export const WINDOW_MIN = 60;

type Payload = Record<string, unknown>;

/** The event fields the strip reads. */
export type LaneEvent = Pick<CrewEvent, 'seq' | 'type' | 'payload' | 'refs' | 'actor'>;

function num(value: unknown): number {
  const n = Number(value);
  return Number.isFinite(n) ? n : 0;
}

function payloadOf(event: Pick<CrewEvent, 'payload'>): Payload {
  return (event.payload ?? {}) as Payload;
}

/** The marks an event puts on one session's strip (usually none or one). */
export function marksFor(event: LaneEvent, sessionId: string): MarkKind[] {
  const p = payloadOf(event);
  const own = eventSessionId(event) === sessionId;
  switch (event.type) {
    case 'checkpoint.created':
      return own ? ['checkpoint'] : [];
    case 'guard.blocked':
      return own ? ['guard'] : [];
    case 'guard.tamper_blocked':
      return own ? ['tamper'] : [];
    case 'activity.push':
      return own ? ['push'] : [];
    case 'activity.test_verdict_changed':
      return own && (p.to === 'fail' || num(p.failed) > 0) ? ['fail'] : [];
    case 'activity.burst': {
      const tests = (p.tests ?? {}) as Payload;
      return own && num(tests.fail) > 0 ? ['fail'] : [];
    }
    case 'task.done': {
      const task = (p.task ?? {}) as Payload;
      return own || task.owner_session_id === sessionId ? ['done'] : [];
    }
    case 'baton.passed': {
      const out: MarkKind[] = [];
      if (p.from_session === sessionId) out.push('baton_out');
      if (p.to_session === sessionId) out.push('baton_in');
      return out;
    }
    default:
      return [];
  }
}

/** Does the event concern this session at all (for bucket density)? */
export function touches(event: LaneEvent, sessionId: string): boolean {
  if (eventSessionId(event) === sessionId) return true;
  const p = payloadOf(event);
  return p.from_session === sessionId || p.to_session === sessionId;
}

export interface Bucket {
  /** Minutes before now: 59 (oldest) … 0 (this minute). */
  minutesAgo: number;
  count: number;
  marks: MarkKind[];
}

export interface Strip {
  buckets: Bucket[];
  /** Newest-first times (ms) of this session's checkpoints in the window. */
  checkpointTimes: number[];
  total: number;
  /** "3 checkpoints, 1 guard block in the last hour" (for screen readers). */
  summary: string;
}

/** Build one session's 60 × 1-minute strip from events (any order, any crew session). */
export function buildStrip(events: readonly CrewEvent[], sessionId: string, nowMs: number, windowMin = WINDOW_MIN): Strip {
  const buckets: Bucket[] = Array.from({ length: windowMin }, (_, i) => ({ minutesAgo: windowMin - 1 - i, count: 0, marks: [] }));
  const checkpointTimes: number[] = [];
  const tally = new Map<MarkKind, number>();
  let total = 0;
  for (const event of events) {
    if (!touches(event, sessionId)) continue;
    const at = parseServerTime(event.ts);
    if (!at) continue;
    const ago = Math.floor((nowMs - at.getTime()) / 60000);
    if (ago < 0 || ago >= windowMin) {
      // clock skew of a few seconds puts a fresh event "in the future": count it as now
      if (!(ago < 0 && nowMs - at.getTime() > -30000)) continue;
    }
    const bucket = buckets[windowMin - 1 - Math.max(0, ago)];
    bucket.count += 1;
    total += 1;
    for (const mark of marksFor(event, sessionId)) {
      if (!bucket.marks.includes(mark)) bucket.marks.push(mark);
      tally.set(mark, (tally.get(mark) ?? 0) + 1);
      if (mark === 'checkpoint') checkpointTimes.push(at.getTime());
    }
  }
  checkpointTimes.sort((a, b) => b - a);
  const parts = [...tally.entries()].map(([kind, n]) => `${n} ${MARK_LABEL[kind]}${n === 1 ? '' : 's'}`);
  const summary = parts.length
    ? `${parts.join(', ')} in the last hour`
    : total
      ? `${total} events in the last hour`
      : 'no activity in the last hour';
  return { buckets, checkpointTimes, total, summary };
}

/** The mark drawn for a bucket when several happened in one minute (alarms first). */
export function primaryMark(marks: MarkKind[]): MarkKind | null {
  const order: MarkKind[] = ['tamper', 'guard', 'fail', 'baton_in', 'baton_out', 'done', 'push', 'checkpoint'];
  for (const kind of order) if (marks.includes(kind)) return kind;
  return null;
}

/**
 * Merge a page of events into a seq-keyed window, dropping anything older than
 * the window. Returns the same array when nothing changed.
 */
export function mergeEvents(current: CrewEvent[], incoming: readonly CrewEvent[], nowMs: number, windowMin = WINDOW_MIN): CrewEvent[] {
  if (!incoming.length) return current;
  const bySeq = new Map<number, CrewEvent>();
  for (const e of current) bySeq.set(e.seq, e);
  let changed = false;
  for (const e of incoming) {
    if (!bySeq.has(e.seq)) {
      bySeq.set(e.seq, e);
      changed = true;
    }
  }
  if (!changed) return current;
  const horizon = nowMs - (windowMin + 1) * 60000;
  return [...bySeq.values()].filter((e) => (parseServerTime(e.ts)?.getTime() ?? 0) >= horizon).sort((a, b) => a.seq - b.seq);
}
