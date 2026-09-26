// A rolling tail of one crew's event log, for views that need raw events the
// reducer does not keep: the zone drawer's last claim events and near-misses,
// and the policy log. One backfill of the last `window` events, then an
// incremental page each time the live `last_seq` moves. Never more than one
// request in flight; a move during a request is fetched when it returns.
// Framework-free (tested with a fake API); `useEventTail` wraps it.

import type { CrewEvent } from '../../../lib/crew/types';

export interface EventTailView {
  /** Up to `window` events in seq order. */
  events: CrewEvent[];
  /** The lowest seq the tail covers (older history is not loaded). */
  fromSeq: number;
  /** The last failed page (cleared by the next success; a later `want` retries). */
  error: unknown;
  /** True until the first page arrived. */
  loading: boolean;
}

export interface EventTailApi {
  eventsPage: (crewId: string, sinceSeq: number, limit?: number) => Promise<{ events: CrewEvent[]; last_seq: number; has_more: boolean }>;
}

const PAGE = 200;

/** Merge a page into a seq-ordered tail, keeping the last `window` events, without duplicates. */
export function mergeTail(tail: readonly CrewEvent[], page: readonly CrewEvent[], window: number): CrewEvent[] {
  const bySeq = new Map<number, CrewEvent>();
  for (const e of tail) bySeq.set(e.seq, e);
  for (const e of page) bySeq.set(e.seq, e);
  const all = [...bySeq.values()].sort((a, b) => a.seq - b.seq);
  return all.length > window ? all.slice(all.length - window) : all;
}

export class EventTailStore {
  private view: EventTailView = { events: [], fromSeq: 0, error: null, loading: true };
  private readonly listeners = new Set<() => void>();
  private wanted = -1;
  /** Highest seq fetched (-1: nothing yet). */
  private upTo = -1;
  private running = false;
  private stopped = false;
  private readonly api: EventTailApi;
  private readonly crewId: string;
  private readonly window: number;

  constructor(api: EventTailApi, crewId: string, window = 400) {
    this.api = api;
    this.crewId = crewId;
    this.window = window;
  }

  getView = (): EventTailView => this.view;

  subscribe = (listener: () => void): (() => void) => {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  };

  private set(patch: Partial<EventTailView>): void {
    this.view = { ...this.view, ...patch };
    for (const l of [...this.listeners]) l();
  }

  /** Make sure the tail reaches `seq` (the live state's last_seq). Returns when this call's work is done. */
  want(seq: number): Promise<void> {
    if (seq > this.wanted) this.wanted = seq;
    return this.pump();
  }

  /** Stop fetching (unmount). A later `start` resumes from where it was. */
  stop(): void {
    this.stopped = true;
  }

  start(): void {
    this.stopped = false;
  }

  private async pump(): Promise<void> {
    if (this.running || this.stopped) return;
    this.running = true;
    try {
      while (!this.stopped && (this.upTo < 0 || this.upTo < this.wanted)) {
        const first = this.upTo < 0;
        const since = first ? Math.max(0, this.wanted - this.window) : this.upTo;
        const page = await this.api.eventsPage(this.crewId, since, PAGE);
        if (this.stopped) return;
        const last = page.events.length ? page.events[page.events.length - 1].seq : since;
        const reached = page.has_more ? last : Math.max(last, page.last_seq);
        const events = mergeTail(this.view.events, page.events, this.window);
        const trimmed = this.view.events.length + page.events.length > events.length && events.length === this.window;
        this.set({
          events,
          fromSeq: first ? since + 1 : trimmed && events.length ? events[0].seq : this.view.fromSeq,
          error: null,
          loading: false,
        });
        if (reached <= this.upTo) break; // no progress: the server has nothing newer yet
        this.upTo = reached;
      }
    } catch (error) {
      if (!this.stopped) this.set({ error, loading: false });
    } finally {
      this.running = false;
    }
  }
}

/** Claim events for one zone (newest first), from the tail. */
export function zoneClaimEvents(events: readonly CrewEvent[], zoneId: string, limit = 20): CrewEvent[] {
  return events
    .filter((e) => e.type.startsWith('claim.') && e.refs?.zone_id === zoneId)
    .slice(-limit)
    .reverse();
}

/** Guard blocks (near-misses) on one zone, newest first. The payload names the zone by slug or id. */
export function zoneNearMisses(events: readonly CrewEvent[], zone: { id: string; slug: string }, limit = 20): CrewEvent[] {
  return events
    .filter((e) => {
      if (e.type !== 'guard.blocked') return false;
      if (e.refs?.zone_id === zone.id) return true;
      const p = e.payload ?? {};
      return p.zone === zone.slug || p.zone === zone.id || p.zone_id === zone.id;
    })
    .slice(-limit)
    .reverse();
}

/** Event types the Policy log shows. */
export const POLICY_EVENT_TYPES: readonly string[] = [
  'zone.synced',
  'zone.change_pending',
  'zone.change_decided',
  'zone.suggested_applied',
  'crew.settings_changed',
  'guard.bypass_used',
  'guard.tamper_blocked',
  'gate.tampered',
  'githook.missing',
];

export function policyEvents(events: readonly CrewEvent[], limit = 30): CrewEvent[] {
  return events
    .filter((e) => POLICY_EVENT_TYPES.includes(e.type))
    .slice(-limit)
    .reverse();
}
