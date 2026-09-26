// Live state of one crew (§4.4): snapshot, then events strictly by seq.
//
// 1. Load `GET /crews/{id}/snapshot` and build the reducer state from it.
// 2. Subscribe over the shared socket with `since_seq = last_seq`; the server
//    replays what happened since the snapshot, then streams live events and
//    5-second presence frames.
// 3. A gap, or a `resync_required` frame, sets `needs_resync`: the store
//    refetches the snapshot, applies it as a snapshot frame and re-subscribes
//    from the new `as_of_seq`.
// 4. While the socket is not live, the store polls `GET /crews/{id}/events`
//    (ETag = last_seq, 304 when nothing changed) so the view keeps moving.
//
// Framework-free; `useCrewSocket` wraps it with useSyncExternalStore.

import { CrewApiError, type CrewApi } from './api';
import { applyFrame, fromSnapshot, type FrameLike } from './reducer';
import { realTimers, type ConnectionStatus, type CrewSocket, type Timers } from './socket';
import type { CommonsEntry, CrewErrorFrame, CrewFrame, CrewSnapshot, CrewState, FootprintView } from './types';

export type CrewStreamStatus =
  /** First snapshot not loaded yet. */
  | 'loading'
  /** Subscribed over the WebSocket; events arrive as they happen. */
  | 'live'
  /** Socket unavailable: following the crew by polling the events endpoint. */
  | 'polling'
  /** Refetching the snapshot after a gap or a server resync request. */
  | 'resyncing'
  /** The crew does not exist or is not visible to this login (404). */
  | 'not_found'
  /** Loading failed; retrying with backoff. */
  | 'error'
  /** Stopped. */
  | 'stopped';

/** Snapshot fields the reducer state does not carry (the zone tree and guard views need them). */
export interface CrewSnapshotMeta {
  etag: string;
  server_time: string;
  as_of_seq: number;
  commons: CommonsEntry[];
  ignore: string[];
  footprints: FootprintView[];
  /** server clock − client clock (ms) at the last snapshot; add to Date.now() for server time. */
  clock_offset_ms: number;
  /** Client time the snapshot arrived (ms since epoch). */
  fetched_at: number;
}

export interface CrewStreamView {
  crewId: string;
  status: CrewStreamStatus;
  /** Null until the first snapshot. */
  state: CrewState | null;
  meta: CrewSnapshotMeta | null;
  /** The last load or stream error (cleared by the next success). */
  error: CrewApiError | null;
  /** Client time of the last applied event or presence frame (ms), 0 if none. */
  lastFrameAt: number;
}

export interface CrewStoreOptions {
  api: Pick<CrewApi, 'snapshot' | 'events'>;
  socket: Pick<CrewSocket, 'subscribeCrew' | 'resubscribe' | 'status' | 'onStatus'> | null;
  timers?: Timers;
  pollMs?: number;
  retryInitialMs?: number;
  retryMaxMs?: number;
}

const POLL_MS = 5000;

export class CrewStore {
  readonly crewId: string;
  private readonly opts: Required<Omit<CrewStoreOptions, 'socket'>> & { socket: CrewStoreOptions['socket'] };
  private view: CrewStreamView;
  private readonly listeners = new Set<() => void>();
  private unsubscribeSocket: (() => void) | null = null;
  private unsubscribeStatus: (() => void) | null = null;
  private subscribed = false;
  private pollTimer: unknown = null;
  private retryTimer: unknown = null;
  private retryAttempt = 0;
  private loading: Promise<void> | null = null;
  private polling = false;
  private eventsEtag: string | null = null;
  private started = false;
  private stopped = false;
  private generation = 0;

  constructor(crewId: string, options: CrewStoreOptions) {
    this.crewId = crewId;
    this.opts = {
      timers: realTimers,
      pollMs: POLL_MS,
      retryInitialMs: 1000,
      retryMaxMs: 30000,
      ...options,
    };
    this.view = { crewId, status: 'loading', state: null, meta: null, error: null, lastFrameAt: 0 };
  }

  // -- external store ----------------------------------------------------------------------

  getView = (): CrewStreamView => this.view;

  subscribe = (listener: () => void): (() => void) => {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  };

  private update(patch: Partial<CrewStreamView>): void {
    const next = { ...this.view, ...patch };
    if (
      next.status === this.view.status &&
      next.state === this.view.state &&
      next.meta === this.view.meta &&
      next.error === this.view.error &&
      next.lastFrameAt === this.view.lastFrameAt
    ) {
      return;
    }
    this.view = next;
    for (const listener of [...this.listeners]) listener();
  }

  // -- lifecycle ------------------------------------------------------------------------------

  start(): void {
    if (this.started || this.stopped) return;
    this.started = true;
    const socket = this.opts.socket;
    if (socket) {
      this.unsubscribeStatus = socket.onStatus((status) => this.onSocketStatus(status));
    }
    void this.load('initial');
  }

  stop(): void {
    if (this.stopped) return;
    this.stopped = true;
    this.generation += 1;
    this.unsubscribeSocket?.();
    this.unsubscribeSocket = null;
    this.unsubscribeStatus?.();
    this.unsubscribeStatus = null;
    this.clearTimer('pollTimer');
    this.clearTimer('retryTimer');
    this.update({ status: 'stopped' });
  }

  /** Refetch the snapshot now (manual refresh, or after an action that needs fresh counts). */
  refresh(): Promise<void> {
    return this.load('manual');
  }

  // -- snapshot ------------------------------------------------------------------------------

  private load(reason: 'initial' | 'resync' | 'manual' | 'retry'): Promise<void> {
    if (this.stopped) return Promise.resolve();
    if (this.loading) return this.loading;
    const gen = this.generation;
    if (reason === 'resync') this.update({ status: 'resyncing' });
    const run = async () => {
      try {
        const res = await this.opts.api.snapshot(this.crewId);
        if (gen !== this.generation || this.stopped) return;
        const snap = res.data;
        if (!snap) throw new CrewApiError('Empty snapshot', res.status, 'bad_response');
        this.applySnapshot(snap);
        this.retryAttempt = 0;
        this.clearTimer('retryTimer');
        this.eventsEtag = null;
        this.connectStream();
        this.update({ status: this.liveStatus() });
      } catch (err) {
        if (gen !== this.generation || this.stopped) return;
        const error = err instanceof CrewApiError ? err : new CrewApiError(String(err), 0, 'client_error');
        if (error.status === 404) {
          this.teardownStream();
          this.update({ status: 'not_found', error });
          return;
        }
        this.update({ status: this.view.state ? this.view.status : 'error', error });
        this.scheduleRetry();
      }
    };
    this.loading = run().finally(() => {
      this.loading = null;
    });
    return this.loading;
  }

  private applySnapshot(snap: CrewSnapshot): void {
    const now = this.opts.timers.now();
    const serverMs = Date.parse(snap.server_time);
    const state = applyFrame(this.view.state ?? fromSnapshot(snap), { type: 'snapshot', data: snap });
    this.update({
      state,
      error: null,
      meta: {
        etag: snap.etag,
        server_time: snap.server_time,
        as_of_seq: snap.as_of_seq,
        commons: snap.commons ?? [],
        ignore: snap.ignore ?? [],
        footprints: snap.footprints ?? [],
        clock_offset_ms: Number.isFinite(serverMs) ? serverMs - now : 0,
        fetched_at: now,
      },
    });
  }

  private scheduleRetry(): void {
    if (this.stopped || this.retryTimer !== null) return;
    const delay = Math.min(this.opts.retryMaxMs, this.opts.retryInitialMs * 2 ** this.retryAttempt);
    this.retryAttempt += 1;
    this.retryTimer = this.opts.timers.setTimeout(() => {
      this.retryTimer = null;
      void this.load('retry');
    }, delay);
  }

  // -- stream ---------------------------------------------------------------------------------

  private liveStatus(): CrewStreamStatus {
    const socket = this.opts.socket;
    return socket && socket.status === 'open' && this.subscribed ? 'live' : 'polling';
  }

  private connectStream(): void {
    const socket = this.opts.socket;
    if (socket) {
      if (this.unsubscribeSocket) {
        this.subscribed = false;
        socket.resubscribe(this.crewId); // replaces the server subscription from the new last_seq
      } else {
        this.unsubscribeSocket = socket.subscribeCrew(
          this.crewId,
          (frame) => this.onFrame(frame),
          () => this.view.state?.last_seq ?? null,
        );
      }
    }
    this.syncPolling();
  }

  private teardownStream(): void {
    this.unsubscribeSocket?.();
    this.unsubscribeSocket = null;
    this.subscribed = false;
    this.clearTimer('pollTimer');
  }

  private onSocketStatus(status: ConnectionStatus): void {
    if (status !== 'open') this.subscribed = false;
    if (this.view.state && this.view.status !== 'not_found' && this.view.status !== 'resyncing' && !this.stopped) {
      this.update({ status: this.liveStatus() });
    }
    this.syncPolling();
  }

  private onFrame(frame: CrewFrame | CrewErrorFrame): void {
    if (this.stopped || !this.view.state) return;
    if (frame.type === 'error') {
      const data = frame.data ?? {};
      const code = data.code ?? 'error';
      const error = new CrewApiError(data.message ?? code, code === 'not_found' ? 404 : 0, code, {
        retryAfterS: data.retry_after_s ?? null,
      });
      this.subscribed = false;
      this.update({ error, status: this.liveStatus() });
      if (code === 'rate_limited') {
        const wait = Math.max(1, data.retry_after_s ?? 1) * 1000;
        this.opts.timers.setTimeout(() => {
          if (!this.stopped) this.opts.socket?.resubscribe(this.crewId);
        }, wait);
      }
      this.syncPolling();
      return;
    }
    if (frame.type === 'crew.subscribed') {
      this.subscribed = true;
      this.update({ status: this.view.status === 'resyncing' ? 'resyncing' : this.liveStatus(), error: null });
      this.syncPolling();
      return;
    }
    if (frame.type === 'resync_required') this.subscribed = false; // the server dropped the subscription
    this.applyFrames([frame as FrameLike]);
  }

  private applyFrames(frames: FrameLike[]): void {
    let state = this.view.state;
    if (!state) return;
    for (const frame of frames) state = applyFrame(state, frame);
    if (state === this.view.state) return;
    this.update({ state, lastFrameAt: this.opts.timers.now() });
    if (state.needs_resync) void this.load('resync');
  }

  // -- polling fallback ------------------------------------------------------------------------

  private syncPolling(): void {
    const wanted = !this.stopped && this.view.state !== null && this.view.status !== 'not_found' && !this.isLive();
    if (!wanted) {
      this.clearTimer('pollTimer');
      return;
    }
    if (this.pollTimer === null && !this.polling) {
      this.pollTimer = this.opts.timers.setTimeout(() => {
        this.pollTimer = null;
        void this.poll();
      }, this.opts.pollMs);
    }
  }

  private isLive(): boolean {
    const socket = this.opts.socket;
    return !!socket && socket.status === 'open' && this.subscribed;
  }

  private async poll(): Promise<void> {
    if (this.stopped || this.isLive() || this.loading || !this.view.state) {
      this.syncPolling();
      return;
    }
    const gen = this.generation;
    this.polling = true;
    let again = false;
    try {
      const since = this.view.state.last_seq;
      const res = await this.opts.api.events(this.crewId, since, { etag: this.eventsEtag });
      if (gen !== this.generation || this.stopped) return;
      const page = res.data;
      if (page) {
        if (page.events.length) {
          this.applyFrames(page.events.map((data) => ({ type: 'crew.event', crew_id: this.crewId, data })));
        }
        const caughtUp = (this.view.state?.last_seq ?? 0) >= page.last_seq && !page.has_more;
        // The ETag is the crew's last_seq: only send it back once every event up to it is applied.
        this.eventsEtag = caughtUp ? res.etag : null;
        again = page.has_more && page.events.length > 0 && !this.view.state?.needs_resync;
      }
      if (this.view.error) this.update({ error: null });
    } catch (err) {
      if (gen !== this.generation || this.stopped) return;
      const error = err instanceof CrewApiError ? err : new CrewApiError(String(err), 0, 'client_error');
      if (error.status === 404) {
        this.teardownStream();
        this.update({ status: 'not_found', error });
        return;
      }
      this.update({ error });
    } finally {
      this.polling = false;
    }
    if (again && !this.isLive()) {
      void this.poll();
      return;
    }
    this.syncPolling();
  }

  private clearTimer(name: 'pollTimer' | 'retryTimer'): void {
    const handle = this[name];
    if (handle !== null) this.opts.timers.clearTimeout(handle);
    this[name] = null;
  }
}
