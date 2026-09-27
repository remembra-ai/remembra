// Every crew the signed-in principal can read (Site Board, §9.2): `GET /crews`
// for the build tree data, overlaid with the WebSocket `crew.summary` counts
// (live sessions, Needs-you, moments in the last 24 h) as they change. A
// summary change also schedules a throttled list refetch so phases and live
// lanes catch up; the list is polled slowly as a fallback.

import { CrewApiError, type CrewApi } from './api';
import { realTimers, type CrewSocket, type Timers } from './socket';
import type { CrewListItem, CrewSummaryItem } from './types';

const PERMANENT = [401, 403, 404, 405, 501, 503];

export interface CrewListView {
  status: 'loading' | 'ready' | 'error' | 'stopped';
  /** Needs-you first, then live, then idle (§9.2); summary counts applied. */
  items: CrewListItem[];
  /** Sum of Needs-you items across crews (sidebar badge). */
  needsYou: number;
  error: CrewApiError | null;
}

export interface CrewListStoreOptions {
  api: Pick<CrewApi, 'listCrews'>;
  socket: Pick<CrewSocket, 'subscribeSummary'> | null;
  timers?: Timers;
  pollMs?: number;
  /** Minimum time between list refetches triggered by summary frames. */
  refetchThrottleMs?: number;
  /** Told what each refresh learned about the server: the list answered ('on') or `/crews` is a 404 ('off'). */
  onMode?: (mode: 'on' | 'off') => void;
}

/** Site Board order: Needs-you first, then live, then idle; project id breaks ties. */
export function sortCrews(items: CrewListItem[]): CrewListItem[] {
  return [...items].sort((a, b) => {
    const needs = Number(b.needs_you > 0) - Number(a.needs_you > 0);
    if (needs) return needs;
    const live = Number(b.live > 0) - Number(a.live > 0);
    if (live) return live;
    return a.crew.project_id < b.crew.project_id ? -1 : a.crew.project_id > b.crew.project_id ? 1 : 0;
  });
}

/** Apply `crew.summary` counts to list items (unknown crews are ignored). */
export function applySummary(items: CrewListItem[], summary: Record<string, CrewSummaryItem>): CrewListItem[] {
  return items.map((item) => {
    const s = summary[item.crew.id];
    if (!s) return item;
    if (s.live === item.live && s.needs_you === item.needs_you && s.moments === item.moments_24h && s.mode === item.crew.mode) {
      return item;
    }
    return { ...item, live: s.live, needs_you: s.needs_you, moments_24h: s.moments, crew: { ...item.crew, mode: s.mode } };
  });
}

export class CrewListStore {
  private readonly opts: Required<Omit<CrewListStoreOptions, 'socket' | 'onMode'>> &
    Pick<CrewListStoreOptions, 'socket' | 'onMode'>;
  private view: CrewListView = { status: 'loading', items: [], needsYou: 0, error: null };
  private readonly listeners = new Set<() => void>();
  private base: CrewListItem[] = [];
  private summary: Record<string, CrewSummaryItem> = {};
  /** Local receipt order of each summary entry (a counter, so equal clock readings still order). */
  private summaryTick: Record<string, number> = {};
  private tick = 0;
  private unsubscribeSummary: (() => void) | null = null;
  private pollTimer: unknown = null;
  private refetchTimer: unknown = null;
  private lastFetchAt = 0;
  private inflight: Promise<void> | null = null;
  private started = false;
  private stopped = false;

  constructor(options: CrewListStoreOptions) {
    this.opts = { timers: realTimers, pollMs: 60000, refetchThrottleMs: 10000, ...options };
  }

  getView = (): CrewListView => this.view;

  subscribe = (listener: () => void): (() => void) => {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  };

  start(): void {
    if (this.started || this.stopped) return;
    this.started = true;
    if (this.opts.socket) this.unsubscribeSummary = this.opts.socket.subscribeSummary((crews) => this.onSummary(crews));
    void this.refresh();
  }

  stop(): void {
    if (this.stopped) return;
    this.stopped = true;
    this.unsubscribeSummary?.();
    this.unsubscribeSummary = null;
    for (const name of ['pollTimer', 'refetchTimer'] as const) {
      if (this[name] !== null) this.opts.timers.clearTimeout(this[name]);
      this[name] = null;
    }
    this.publish({ status: 'stopped' });
  }

  refresh(): Promise<void> {
    if (this.stopped) return Promise.resolve();
    if (this.inflight) return this.inflight;
    this.inflight = (async () => {
      const startedTick = this.tick;
      try {
        const res = await this.opts.api.listCrews();
        if (this.stopped) return;
        this.opts.onMode?.('on');
        this.lastFetchAt = this.opts.timers.now();
        this.base = res.crews ?? [];
        // The fresh list is authoritative over summary frames that arrived before the request;
        // frames that arrived while it was in flight are newer and stay applied.
        const summary: Record<string, CrewSummaryItem> = {};
        const ticks: Record<string, number> = {};
        for (const [id, item] of Object.entries(this.summary)) {
          if (this.summaryTick[id] > startedTick) {
            summary[id] = item;
            ticks[id] = this.summaryTick[id];
          }
        }
        this.summary = summary;
        this.summaryTick = ticks;
        this.publish({ status: 'ready', error: null });
      } catch (err) {
        if (this.stopped) return;
        const error = err instanceof CrewApiError ? err : new CrewApiError(String(err), 0, 'client_error');
        if (error.status === 404) this.opts.onMode?.('off');
        this.publish({ status: this.view.items.length ? 'ready' : 'error', error });
      } finally {
        this.inflight = null;
        this.schedulePoll();
      }
    })();
    return this.inflight;
  }

  private schedulePoll(): void {
    if (this.stopped) return;
    // Signed out, no access, or crew mode not running on this server: polling cannot fix it (refresh() retries).
    if (this.view.error && PERMANENT.includes(this.view.error.status)) return;
    if (this.pollTimer !== null) this.opts.timers.clearTimeout(this.pollTimer);
    this.pollTimer = this.opts.timers.setTimeout(() => {
      this.pollTimer = null;
      void this.refresh();
    }, this.opts.pollMs);
  }

  private onSummary(crews: CrewSummaryItem[]): void {
    if (this.stopped) return;
    const next = { ...this.summary };
    let unknown = false;
    for (const item of crews) {
      next[item.crew_id] = item;
      this.tick += 1;
      this.summaryTick[item.crew_id] = this.tick;
      if (!this.base.some((c) => c.crew.id === item.crew_id)) unknown = true;
    }
    this.summary = next;
    this.publish({});
    this.scheduleRefetch(unknown);
  }

  private scheduleRefetch(now: boolean): void {
    if (this.stopped || this.refetchTimer !== null) return;
    const wait = now ? 0 : Math.max(0, this.lastFetchAt + this.opts.refetchThrottleMs - this.opts.timers.now());
    this.refetchTimer = this.opts.timers.setTimeout(() => {
      this.refetchTimer = null;
      void this.refresh();
    }, wait);
  }

  private publish(patch: Partial<CrewListView>): void {
    const items = sortCrews(applySummary(this.base, this.summary));
    const needsYou = items.reduce((sum, item) => sum + Math.max(0, item.needs_you), 0);
    this.view = { ...this.view, ...patch, items, needsYou };
    for (const listener of [...this.listeners]) listener();
  }
}
