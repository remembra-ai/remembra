// The Event Feed's own window onto a crew's event log (§4.4, §9.6).
//
// The crew store (WP-12) reduces events into state and keeps only moments;
// the feed needs the events themselves. FeedLog:
//
// 1. loads the newest `window` events with the polling endpoint
//    (`GET /crews/{id}/events?since_seq=head−window`, forward pages of ≤200);
// 2. taps the shared crew WebSocket for live `crew.event` frames (its
//    subscription passes no cursor, so it never changes where the store's
//    replay starts);
// 3. fills any gap in seq (a dropped frame, a resync, a reconnect) from the
//    same endpoint, and follows the store's head while the socket is down
//    (`notifyHead`), so the feed never shows a hole;
// 4. pages older events on demand (`loadOlder`) and keeps at most `cap`.
//
// Events are deduplicated by seq and kept sorted ascending. Framework-free:
// the API, the socket tap and the clock are injected (useFeedLog wires them).

import { CrewApiError, type CrewApi } from '../../../lib/crew/api';
import { realTimers, type Timers } from '../../../lib/crew/socket';
import type { CrewErrorFrame, CrewEvent, CrewFrame } from '../../../lib/crew/types';

export type FeedTap = (crewId: string, handler: (frame: CrewFrame | CrewErrorFrame) => void) => () => void;

export type FeedStatus = 'idle' | 'loading' | 'ready' | 'error' | 'not_found' | 'stopped';

export interface FeedView {
  status: FeedStatus;
  /** Ascending by seq, no duplicates. */
  events: CrewEvent[];
  newestSeq: number;
  /** The lowest seq this window covers (older pages start below it). */
  floorSeq: number;
  hasOlder: boolean;
  loadingOlder: boolean;
  /** The crew's head when the first load finished: events above it arrived live. */
  loadedHead: number;
  /** Events that arrived after the first load (for the live strip and the new-events pill). */
  arrivals: number;
  /** Client time of the newest arrival (ms), 0 if none yet. */
  lastArrivalAt: number;
  error: CrewApiError | null;
}

export interface FeedLogOptions {
  api: Pick<CrewApi, 'events'>;
  tap?: FeedTap | null;
  timers?: Timers;
  /** Events loaded on open. */
  window?: number;
  /** Most events kept in memory; the oldest are dropped beyond it. */
  cap?: number;
  /** How long to wait for a live frame before fetching a head the store already saw. */
  headGraceMs?: number;
  /** Gap fills requested within this window share one request (the events endpoint is rate-limited). */
  fillDebounceMs?: number;
}

/** Retry delay after a failed fetch: the server's Retry-After on 429, else 2 s doubling to 30 s. */
export function retryDelayMs(err: unknown, attempt: number): number {
  if (err instanceof CrewApiError && err.status === 429 && err.retryAfterS) return Math.min(60, Math.max(1, err.retryAfterS)) * 1000;
  return Math.min(30000, 2000 * 2 ** Math.max(0, attempt));
}

/** Errors worth retrying on their own: rate limits, server errors, the network. */
export function retryable(err: unknown): boolean {
  if (!(err instanceof CrewApiError)) return true;
  return err.status === 0 || err.status === 429 || err.status >= 500;
}

const PAGE = 200;

export class FeedLog {
  readonly crewId: string;
  private readonly api: FeedLogOptions['api'];
  private readonly tap: FeedTap | null;
  private readonly timers: Timers;
  private readonly windowSize: number;
  private readonly cap: number;
  private readonly headGraceMs: number;
  private readonly fillDebounceMs: number;
  private fillTimer: unknown = null;
  private retryTimer: unknown = null;
  private fillAttempt = 0;
  private loadAttempt = 0;

  private bySeq = new Map<number, CrewEvent>();
  private view: FeedView;
  private readonly listeners = new Set<() => void>();
  private untap: (() => void) | null = null;
  private loadedHead = -1;
  private started = false;
  private stopped = false;
  private generation = 0;
  private filling: Promise<void> | null = null;
  private refillWanted = false;
  private headTimer: unknown = null;
  private pendingHead = 0;

  constructor(crewId: string, options: FeedLogOptions) {
    this.crewId = crewId;
    this.api = options.api;
    this.tap = options.tap ?? null;
    this.timers = options.timers ?? realTimers;
    this.windowSize = Math.max(1, options.window ?? 300);
    this.cap = Math.max(this.windowSize, options.cap ?? 2000);
    this.headGraceMs = options.headGraceMs ?? 1500;
    this.fillDebounceMs = options.fillDebounceMs ?? 250;
    this.view = {
      status: 'idle',
      events: [],
      newestSeq: 0,
      floorSeq: 0,
      hasOlder: false,
      loadingOlder: false,
      loadedHead: 0,
      arrivals: 0,
      lastArrivalAt: 0,
      error: null,
    };
  }

  getView = (): FeedView => this.view;

  subscribe = (listener: () => void): (() => void) => {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  };

  private update(patch: Partial<FeedView>): void {
    this.view = { ...this.view, ...patch };
    for (const l of [...this.listeners]) l();
  }

  /**
   * Start following the crew. `headHint` is the crew's last_seq when known
   * (the store's snapshot); without it one probe request learns the head.
   */
  start(headHint: number | null = null): void {
    if (this.started || this.stopped) return;
    this.started = true;
    if (this.tap) this.untap = this.tap(this.crewId, (frame) => this.onFrame(frame));
    void this.loadInitial(headHint);
  }

  stop(): void {
    if (this.stopped) return;
    this.stopped = true;
    this.generation += 1;
    this.untap?.();
    this.untap = null;
    for (const name of ['headTimer', 'fillTimer', 'retryTimer'] as const) {
      if (this[name] !== null) this.timers.clearTimeout(this[name]);
      this[name] = null;
    }
    this.update({ status: 'stopped' });
  }

  /** Retry after an error. */
  retry(): void {
    if (this.stopped || this.view.status !== 'error') return;
    if (this.retryTimer !== null) this.timers.clearTimeout(this.retryTimer);
    this.retryTimer = null;
    void this.loadInitial(null);
  }

  // -- loading ---------------------------------------------------------------------------------

  private async page(sinceSeq: number, limit = PAGE) {
    const res = await this.api.events(this.crewId, sinceSeq, { limit });
    if (!res.data) throw new CrewApiError('Empty events page', res.status, 'bad_response');
    return res.data;
  }

  private fail(err: unknown): void {
    const error = err instanceof CrewApiError ? err : new CrewApiError(String(err), 0, 'client_error');
    if (error.status === 404) {
      this.update({ status: 'not_found', error });
      return;
    }
    this.update({ status: this.view.status === 'ready' ? 'ready' : 'error', error });
  }

  private async loadInitial(headHint: number | null): Promise<void> {
    const gen = this.generation;
    this.update({ status: 'loading', error: null });
    try {
      let head = headHint;
      if (head === null) {
        const probe = await this.page(0, 1);
        if (gen !== this.generation) return;
        head = probe.last_seq;
      }
      const floor = Math.max(0, head - this.windowSize);
      let since = floor;
      for (let guard = 0; guard < 50; guard += 1) {
        const page = await this.page(since);
        if (gen !== this.generation) return;
        this.merge(page.events);
        head = Math.max(head, page.last_seq);
        if (!page.has_more || !page.events.length) break;
        since = page.events[page.events.length - 1].seq;
      }
      this.loadedHead = Math.max(head, this.newest());
      this.update({ status: 'ready', loadedHead: this.loadedHead, floorSeq: floor + 1, hasOlder: floor > 0, error: null, events: this.sorted(), newestSeq: this.newest() });
      this.loadAttempt = 0;
      this.trim();
      if (this.refillWanted) this.scheduleFill();
    } catch (err) {
      if (gen !== this.generation) return;
      this.fail(err);
      if (retryable(err) && this.view.status === 'error') {
        const delay = retryDelayMs(err, this.loadAttempt++);
        this.retryTimer = this.timers.setTimeout(() => {
          this.retryTimer = null;
          if (!this.stopped && this.view.status === 'error') void this.loadInitial(headHint);
        }, delay);
      }
    }
  }

  /** Load the page of events just below the window. */
  loadOlder(): Promise<void> {
    if (this.stopped || this.view.status !== 'ready' || !this.view.hasOlder || this.view.loadingOlder) return Promise.resolve();
    const gen = this.generation;
    this.update({ loadingOlder: true });
    return (async () => {
      try {
        let floor = this.view.floorSeq;
        let found = 0;
        // Retention may have pruned a stretch (§4.5): keep stepping down until a page has events or seq 1 is reached.
        for (let guard = 0; guard < 20 && found === 0 && floor > 1; guard += 1) {
          const since = Math.max(0, floor - 1 - PAGE);
          const page = await this.page(since, floor - 1 - since);
          if (gen !== this.generation) return;
          const older = page.events.filter((e) => e.seq < floor);
          this.merge(older);
          found = older.length;
          floor = since + 1;
        }
        this.update({ loadingOlder: false, floorSeq: floor, hasOlder: floor > 1, events: this.sorted(), error: null });
      } catch (err) {
        if (gen !== this.generation) return;
        this.update({ loadingOlder: false });
        this.fail(err);
      }
    })();
  }

  // -- live --------------------------------------------------------------------------------------

  private onFrame(frame: CrewFrame | CrewErrorFrame): void {
    if (this.stopped) return;
    if (frame.type === 'crew.event') {
      if (frame.crew_id && frame.crew_id !== this.crewId) return;
      this.onEvent(frame.data);
      return;
    }
    if (frame.type === 'resync_required' || frame.type === 'crew.subscribed') {
      // The server dropped or (re)started the subscription: events may have been skipped.
      if (frame.crew_id === this.crewId) this.scheduleFill();
    }
  }

  private onEvent(e: CrewEvent): void {
    if (!e || typeof e.seq !== 'number' || e.crew_id !== this.crewId) return;
    if (this.bySeq.has(e.seq)) return;
    const before = this.newest();
    const gap = before > 0 && e.seq > before + 1;
    this.bySeq.set(e.seq, e);
    if (this.view.status !== 'ready') {
      // The first load is still running; it will pick this up and check for gaps.
      if (gap) this.refillWanted = true;
      return;
    }
    const arrived = e.seq > this.loadedHead;
    this.update({
      events: this.sorted(),
      newestSeq: this.newest(),
      arrivals: this.view.arrivals + (arrived ? 1 : 0),
      lastArrivalAt: arrived ? this.timers.now() : this.view.lastArrivalAt,
    });
    this.trim();
    if (gap) this.scheduleFill();
  }

  /**
   * The crew store saw events up to `seq` (e.g. by polling while the socket is
   * down). If no live frame brings them within the grace period, fetch them.
   */
  notifyHead(seq: number): void {
    if (this.stopped || !Number.isFinite(seq) || seq <= this.newest()) return;
    this.pendingHead = Math.max(this.pendingHead, seq);
    if (this.headTimer !== null) return;
    this.headTimer = this.timers.setTimeout(() => {
      this.headTimer = null;
      if (this.pendingHead > this.newest()) void this.fill();
    }, this.headGraceMs);
  }

  /** Coalesce fill requests (a burst of gaps costs one request). */
  private scheduleFill(delay = this.fillDebounceMs): void {
    if (this.stopped) return;
    if (this.view.status !== 'ready') {
      this.refillWanted = true;
      return;
    }
    if (this.fillTimer !== null) return;
    this.fillTimer = this.timers.setTimeout(() => {
      this.fillTimer = null;
      void this.fill();
    }, delay);
  }

  /** Fetch every event after the first hole (or after the newest) until caught up. */
  private fill(): Promise<void> {
    if (this.stopped) return Promise.resolve();
    if (this.view.status !== 'ready') {
      this.refillWanted = true;
      return Promise.resolve();
    }
    if (this.filling) {
      this.refillWanted = true;
      return this.filling;
    }
    this.refillWanted = false;
    const gen = this.generation;
    const run = async () => {
      try {
        let since = this.firstHoleFrom();
        for (let guard = 0; guard < 50; guard += 1) {
          const page = await this.page(since);
          if (gen !== this.generation) return;
          const fresh = page.events.filter((e) => !this.bySeq.has(e.seq));
          for (const e of fresh) this.bySeq.set(e.seq, e);
          const arrived = fresh.filter((e) => e.seq > this.loadedHead).length;
          if (fresh.length) {
            this.update({
              events: this.sorted(),
              newestSeq: this.newest(),
              arrivals: this.view.arrivals + arrived,
              lastArrivalAt: arrived ? this.timers.now() : this.view.lastArrivalAt,
              error: null,
            });
          }
          if (!page.has_more || !page.events.length) break;
          since = page.events[page.events.length - 1].seq;
        }
        this.trim();
        this.fillAttempt = 0;
      } catch (err) {
        if (gen !== this.generation) return;
        this.fail(err);
        if (retryable(err)) {
          // try again later (on 429, when the server says); a new gap before then joins this retry
          if (this.fillTimer !== null) this.timers.clearTimeout(this.fillTimer);
          this.fillTimer = null;
          this.scheduleFill(retryDelayMs(err, this.fillAttempt++));
        }
      }
    };
    this.filling = run().finally(() => {
      this.filling = null;
      if (this.refillWanted && !this.stopped) this.scheduleFill();
    });
    return this.filling;
  }

  /** The seq to fetch after: just below the first missing seq above the window floor, else the newest. */
  private firstHoleFrom(): number {
    const seqs = [...this.bySeq.keys()].sort((a, b) => a - b);
    const floor = Math.max(this.view.floorSeq, seqs[0] ?? 0);
    let prev = floor - 1;
    for (const s of seqs) {
      if (s < floor) continue;
      if (s > prev + 1 && prev >= floor) return prev;
      prev = s;
    }
    return this.newest();
  }

  // -- storage -----------------------------------------------------------------------------------

  private merge(events: CrewEvent[]): void {
    for (const e of events) {
      if (e && typeof e.seq === 'number' && e.crew_id === this.crewId) this.bySeq.set(e.seq, e);
    }
  }

  private newest(): number {
    let max = 0;
    for (const s of this.bySeq.keys()) if (s > max) max = s;
    return max;
  }

  private sorted(): CrewEvent[] {
    return [...this.bySeq.values()].sort((a, b) => a.seq - b.seq);
  }

  private trim(): void {
    if (this.bySeq.size <= this.cap) return;
    const seqs = [...this.bySeq.keys()].sort((a, b) => a - b);
    const drop = seqs.slice(0, seqs.length - this.cap);
    for (const s of drop) this.bySeq.delete(s);
    const floor = drop[drop.length - 1] + 1;
    this.update({ events: this.sorted(), floorSeq: floor, hasOlder: true });
  }
}
