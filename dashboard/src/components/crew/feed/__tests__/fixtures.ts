// Test helpers for the feed: contract envelopes (tests/crew/vectors/events,
// one per L0 event type), an in-memory events endpoint with the server's
// paging rules, and a hand-driven socket tap.

import { eventSamples } from '../../../../lib/crew/__tests__/vectors';
import type { CrewApi, CrewResponse } from '../../../../lib/crew/api';
import { CrewApiError } from '../../../../lib/crew/api';
import type { CrewErrorFrame, CrewEvent, CrewEventsPage, CrewFrame } from '../../../../lib/crew/types';
import type { FeedTap } from '../feedLog';

export const CREW = 'crw_0a1b2c3d4e5f6a7b';

/** Every L0 sample envelope, re-sequenced 1..n on one crew. */
export function samples(): CrewEvent[] {
  return (eventSamples() as unknown as CrewEvent[]).map((e, i) => ({ ...e, seq: i + 1, crew_id: CREW }));
}

export function sample(type: string): CrewEvent {
  const e = samples().find((x) => x.type === type);
  if (!e) throw new Error(`no sample for ${type}`);
  return e;
}

let idSeq = 0;

/** A minimal valid envelope (activity.burst by cc-1). */
export function ev(seq: number, patch: Partial<CrewEvent> = {}): CrewEvent {
  idSeq += 1;
  return {
    seq,
    id: `evt_${seq}_${idSeq}`,
    crew_id: CREW,
    project_id: 'yaadbooks',
    ts: new Date(Date.UTC(2026, 8, 25, 20, 0, seq)).toISOString(),
    type: 'activity.burst',
    v: 1,
    origin: 'server',
    actor: { kind: 'session', id: 'cs_a', callsign: 'cc-1', agent_id: 'claude-code', user_id: 'u_mani', verified: true },
    refs: { session_id: 'cs_a' },
    severity: 'info',
    moment: false,
    summary: `cc-1 event ${seq}`,
    payload: { files_touched: [], command_verbs: [], tests: { pass: 0, fail: 0 } },
    ...patch,
  };
}

/** The server's events endpoint: seq > since_seq, ≤limit (max 200), has_more, last_seq. */
export class FakeEventsApi {
  log: CrewEvent[] = [];
  calls: { since: number; limit: number }[] = [];
  failNext: CrewApiError | null = null;
  private gate: Promise<void> | null = null;
  private release: (() => void) | null = null;

  constructor(count = 0) {
    for (let i = 1; i <= count; i += 1) this.log.push(ev(i));
  }

  append(n = 1): CrewEvent[] {
    const out: CrewEvent[] = [];
    for (let i = 0; i < n; i += 1) {
      const e = ev(this.head + 1);
      this.log.push(e);
      out.push(e);
    }
    return out;
  }

  /** Drop stored events in [from, to] (retention). */
  prune(from: number, to: number): void {
    this.log = this.log.filter((e) => e.seq < from || e.seq > to);
  }

  get head(): number {
    return this.log.length ? this.log[this.log.length - 1].seq : 0;
  }

  /** Hold every request until `open()`. */
  hold(): void {
    this.gate = new Promise((r) => (this.release = r));
  }

  open(): void {
    this.release?.();
    this.gate = null;
  }

  events: CrewApi['events'] = async (crewId, sinceSeq, options = {}): Promise<CrewResponse<CrewEventsPage>> => {
    const limit = Math.max(1, Math.min(200, options.limit ?? 200));
    this.calls.push({ since: sinceSeq, limit });
    if (this.gate) await this.gate;
    if (this.failNext) {
      const err = this.failNext;
      this.failNext = null;
      throw err;
    }
    const head = this.head;
    const events = this.log.filter((e) => e.seq > sinceSeq).slice(0, limit);
    const hasMore = events.length > 0 && events[events.length - 1].seq < head;
    return { status: 200, etag: `"${head}"`, data: { crew_id: crewId, events, last_seq: head, has_more: hasMore } };
  };
}

export class FakeTap {
  handlers: ((frame: CrewFrame | CrewErrorFrame) => void)[] = [];
  unsubscribed = 0;

  tap: FeedTap = (_crewId, handler) => {
    this.handlers.push(handler);
    return () => {
      this.unsubscribed += 1;
      this.handlers = this.handlers.filter((h) => h !== handler);
    };
  };

  send(frame: CrewFrame | CrewErrorFrame): void {
    for (const h of [...this.handlers]) h(frame);
  }

  event(e: CrewEvent): void {
    this.send({ type: 'crew.event', crew_id: e.crew_id, data: e });
  }
}
