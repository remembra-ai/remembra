// One shared /ws connection for every crew stream of a dashboard tab (§4.4).
//
// * Credentials never go in the URL: the first message is `{"type":"auth"}`
//   (crew subscriptions refuse query-string credentials).
// * Subscriptions are sent once the server says `connected`, and re-sent after
//   every reconnect with each subscriber's current `since_seq`, so the server
//   replays exactly the events the client missed (≤500, else resync_required).
// * Frames are routed by `crew_id`; `crew.summary` frames go to the summary
//   subscribers. The server's text `ping` is answered with `pong`.
// * Reconnects use capped exponential backoff with jitter. 4001 (credentials
//   rejected) stops until `reconnect()`; 4003 (access revoked) waits the
//   longest backoff, because access can be granted again.
// * A connection that has been silent for longer than the server's ping
//   interval allows is treated as dead and replaced.
//
// The class is framework-free and takes its socket factory and timers, so it
// runs the same way in the browser, in tests and in the live check.

import type { CrewCredentials } from './api';
import type { CrewErrorFrame, CrewFrame, CrewSummaryItem } from './types';

/** The subset of the browser WebSocket this module uses. */
export interface SocketLike {
  readonly readyState: number;
  send(data: string): void;
  close(code?: number, reason?: string): void;
  onopen: ((ev: Event) => void) | null;
  onclose: ((ev: CloseEvent) => void) | null;
  onmessage: ((ev: MessageEvent) => void) | null;
  onerror: ((ev: Event) => void) | null;
}

export type SocketFactory = (url: string) => SocketLike;

export interface Timers {
  setTimeout: (fn: () => void, ms: number) => unknown;
  clearTimeout: (handle: unknown) => void;
  now: () => number;
}

export const realTimers: Timers = {
  setTimeout: (fn, ms) => globalThis.setTimeout(fn, ms),
  clearTimeout: (handle) => globalThis.clearTimeout(handle as ReturnType<typeof setTimeout>),
  now: () => Date.now(),
};

export type ConnectionStatus =
  /** No subscriber yet: no socket. */
  | 'idle'
  /** Opening the socket or waiting for `connected` after the auth message. */
  | 'connecting'
  /** Authenticated; subscriptions are live. */
  | 'open'
  /** Lost the connection; a reconnect is scheduled. */
  | 'reconnecting'
  /** The server rejected the credentials (4001); waiting for `reconnect()`. */
  | 'unauthorized'
  /** The server closed the socket because access was revoked (4003); retrying slowly. */
  | 'forbidden'
  /** Stopped by the client. */
  | 'closed';

export type CrewFrameHandler = (frame: CrewFrame | CrewErrorFrame) => void;
export type SummaryHandler = (crews: CrewSummaryItem[]) => void;

export const CLOSE_NORMAL = 1000;
export const CLOSE_UNAUTHORIZED = 4001;
export const CLOSE_FORBIDDEN = 4003;
const OPEN = 1;

export interface CrewSocketOptions {
  /** Full ws(s)://…/ws URL (no credentials). */
  url: string;
  credentials: () => CrewCredentials;
  createSocket: SocketFactory;
  timers?: Timers;
  random?: () => number;
  initialBackoffMs?: number;
  maxBackoffMs?: number;
  /** Close and reconnect when nothing (not even a ping) arrived for this long. */
  silenceMs?: number;
  /** How long to wait for `connected` after sending the auth message. */
  authTimeoutMs?: number;
}

interface CrewSubscription {
  handler: CrewFrameHandler;
  sinceSeq: () => number | null;
}

/** `ws(s)://<host>/ws` for an API origin ('' = this page's origin). */
export function crewSocketUrl(apiOrigin: string, location: { protocol: string; host: string } | null = null): string {
  if (apiOrigin) return `${apiOrigin.replace(/^http/, 'ws').replace(/\/+$/, '')}/ws`;
  const loc = location ?? (typeof window !== 'undefined' ? window.location : null);
  if (!loc) throw new Error('crewSocketUrl: no API origin and no window.location');
  return `${loc.protocol === 'https:' ? 'wss:' : 'ws:'}//${loc.host}/ws`;
}

export class CrewSocket {
  private readonly opts: Required<Omit<CrewSocketOptions, 'timers' | 'random'>> & { timers: Timers; random: () => number };
  private socket: SocketLike | null = null;
  private status_: ConnectionStatus = 'idle';
  private readonly statusListeners = new Set<(status: ConnectionStatus) => void>();
  private readonly crews = new Map<string, Set<CrewSubscription>>();
  private readonly summaries = new Set<SummaryHandler>();
  private attempt = 0;
  private reconnectTimer: unknown = null;
  private silenceTimer: unknown = null;
  private authTimer: unknown = null;
  private authenticated = false;
  private stopped = false;
  /** Sockets we closed on purpose: their close events must not trigger a reconnect. */
  private readonly retired = new WeakSet<SocketLike>();

  constructor(options: CrewSocketOptions) {
    this.opts = {
      initialBackoffMs: 1000,
      maxBackoffMs: 30000,
      silenceMs: 150000,
      authTimeoutMs: 15000,
      ...options,
      timers: options.timers ?? realTimers,
      random: options.random ?? Math.random,
    };
  }

  get status(): ConnectionStatus {
    return this.status_;
  }

  /** Listen to connection status changes; returns an unsubscribe function. */
  onStatus(listener: (status: ConnectionStatus) => void): () => void {
    this.statusListeners.add(listener);
    return () => this.statusListeners.delete(listener);
  }

  /**
   * Subscribe to one crew. `sinceSeq()` is read whenever the subscription is
   * (re)sent: the last applied seq, or null to stream live only.
   */
  subscribeCrew(crewId: string, handler: CrewFrameHandler, sinceSeq: () => number | null): () => void {
    const sub: CrewSubscription = { handler, sinceSeq };
    let set = this.crews.get(crewId);
    const first = set === undefined;
    if (!set) {
      set = new Set();
      this.crews.set(crewId, set);
    }
    set.add(sub);
    if (first) this.sendCrewSubscribe(crewId);
    this.ensureConnected();
    return () => {
      const current = this.crews.get(crewId);
      if (!current) return;
      current.delete(sub);
      if (current.size === 0) {
        this.crews.delete(crewId);
        this.send({ type: 'unsubscribe', channel: 'crew', crew_id: crewId });
      }
      this.maybeDisconnect();
    };
  }

  /** Re-send one crew's subscription (after a snapshot refetch) with its current since_seq. */
  resubscribe(crewId: string): void {
    if (this.crews.has(crewId)) this.sendCrewSubscribe(crewId);
  }

  /** Counts-only updates for every crew the principal can read (`crew_id: "*"`). */
  subscribeSummary(handler: SummaryHandler): () => void {
    const first = this.summaries.size === 0;
    this.summaries.add(handler);
    if (first) this.sendSummarySubscribe();
    this.ensureConnected();
    return () => {
      this.summaries.delete(handler);
      if (this.summaries.size === 0) this.send({ type: 'unsubscribe', channel: 'crew', crew_id: '*' });
      this.maybeDisconnect();
    };
  }

  /** Reconnect now (e.g. after signing in again). */
  reconnect(): void {
    this.stopped = false;
    this.attempt = 0;
    this.teardown();
    if (this.hasSubscribers()) this.connect();
    else this.setStatus('idle');
  }

  /** Close the socket and drop every subscription. */
  stop(): void {
    this.stopped = true;
    this.crews.clear();
    this.summaries.clear();
    this.teardown();
    this.setStatus('closed');
  }

  // -- connection ----------------------------------------------------------------------

  private hasSubscribers(): boolean {
    return this.crews.size > 0 || this.summaries.size > 0;
  }

  private ensureConnected(): void {
    if (this.stopped || this.socket || this.reconnectTimer !== null) return;
    if (this.status_ === 'unauthorized') return;
    this.connect();
  }

  private maybeDisconnect(): void {
    if (this.hasSubscribers()) return;
    this.teardown();
    if (!this.stopped) this.setStatus('idle');
  }

  private connect(): void {
    this.clearTimer('reconnectTimer');
    let socket: SocketLike;
    try {
      socket = this.opts.createSocket(this.opts.url);
    } catch {
      this.scheduleReconnect(false);
      return;
    }
    this.socket = socket;
    this.authenticated = false;
    this.setStatus(this.attempt > 0 ? 'reconnecting' : 'connecting');
    socket.onopen = () => {
      if (this.socket !== socket) return;
      const creds = this.opts.credentials();
      const auth: Record<string, unknown> = { type: 'auth' };
      if (creds.jwt) auth.token = creds.jwt;
      else if (creds.apiKey) auth.api_key = creds.apiKey;
      socket.send(JSON.stringify(auth));
      this.clearTimer('authTimer');
      this.authTimer = this.opts.timers.setTimeout(() => {
        this.authTimer = null;
        if (this.socket === socket && !this.authenticated) this.replace(socket);
      }, this.opts.authTimeoutMs);
      this.touch(socket);
    };
    socket.onmessage = (ev) => {
      if (this.socket !== socket) return;
      this.touch(socket);
      this.onMessage(socket, ev.data);
    };
    socket.onerror = () => {
      // the close event follows and decides what to do
    };
    socket.onclose = (ev) => {
      if (this.retired.has(socket) || this.socket !== socket) return;
      this.socket = null;
      this.authenticated = false;
      this.clearTimer('silenceTimer');
      this.clearTimer('authTimer');
      if (this.stopped) return;
      if (ev.code === CLOSE_UNAUTHORIZED) {
        this.setStatus('unauthorized');
        return;
      }
      if (!this.hasSubscribers()) {
        this.setStatus('idle');
        return;
      }
      this.scheduleReconnect(ev.code === CLOSE_FORBIDDEN);
    };
  }

  private scheduleReconnect(slow: boolean): void {
    if (this.stopped) return;
    const { initialBackoffMs, maxBackoffMs } = this.opts;
    const base = slow ? maxBackoffMs : Math.min(maxBackoffMs, initialBackoffMs * 2 ** this.attempt);
    const delay = Math.round(base * (0.5 + this.opts.random() / 2));
    this.attempt += 1;
    this.setStatus(slow ? 'forbidden' : 'reconnecting');
    this.clearTimer('reconnectTimer');
    this.reconnectTimer = this.opts.timers.setTimeout(() => {
      this.reconnectTimer = null;
      if (!this.stopped && this.hasSubscribers()) this.connect();
      else if (!this.stopped) this.setStatus('idle');
    }, delay);
  }

  /** Close a stale socket on purpose and reconnect. */
  private replace(socket: SocketLike): void {
    this.retired.add(socket);
    try {
      socket.close(CLOSE_NORMAL, 'reconnecting');
    } catch {
      // already closed
    }
    if (this.socket === socket) this.socket = null;
    this.authenticated = false;
    this.clearTimer('silenceTimer');
    this.clearTimer('authTimer');
    this.scheduleReconnect(false);
  }

  private teardown(): void {
    this.clearTimer('reconnectTimer');
    this.clearTimer('silenceTimer');
    this.clearTimer('authTimer');
    const socket = this.socket;
    this.socket = null;
    this.authenticated = false;
    if (socket) {
      this.retired.add(socket);
      try {
        socket.close(CLOSE_NORMAL, 'client closed');
      } catch {
        // already closed
      }
    }
  }

  private touch(socket: SocketLike): void {
    this.clearTimer('silenceTimer');
    this.silenceTimer = this.opts.timers.setTimeout(() => {
      this.silenceTimer = null;
      if (this.socket === socket) this.replace(socket);
    }, this.opts.silenceMs);
  }

  private clearTimer(name: 'reconnectTimer' | 'silenceTimer' | 'authTimer'): void {
    const handle = this[name];
    if (handle !== null) this.opts.timers.clearTimeout(handle);
    this[name] = null;
  }

  private setStatus(status: ConnectionStatus): void {
    if (this.status_ === status) return;
    this.status_ = status;
    for (const listener of [...this.statusListeners]) listener(status);
  }

  // -- frames ------------------------------------------------------------------------------

  private send(message: Record<string, unknown>): void {
    const socket = this.socket;
    if (!socket || !this.authenticated || socket.readyState !== OPEN) return; // re-sent on `connected`
    socket.send(JSON.stringify(message));
  }

  private sendCrewSubscribe(crewId: string): void {
    const subs = this.crews.get(crewId);
    if (!subs || subs.size === 0) return;
    // several local subscribers share one server subscription: replay from the lowest cursor
    let since: number | null = null;
    for (const sub of subs) {
      const value = sub.sinceSeq();
      if (value === null || value === undefined) continue;
      since = since === null ? value : Math.min(since, value);
    }
    const message: Record<string, unknown> = { type: 'subscribe', channel: 'crew', crew_id: crewId, topics: ['crew'] };
    if (since !== null) message.since_seq = Math.max(0, Math.trunc(since));
    this.send(message);
  }

  private sendSummarySubscribe(): void {
    if (this.summaries.size === 0) return;
    this.send({ type: 'subscribe', channel: 'crew', crew_id: '*', topics: ['crew.summary'] });
  }

  private onMessage(socket: SocketLike, raw: unknown): void {
    if (raw === 'ping') {
      socket.send('pong');
      return;
    }
    if (typeof raw !== 'string' || raw === 'pong') return;
    let frame: Record<string, unknown>;
    try {
      const parsed: unknown = JSON.parse(raw);
      if (typeof parsed !== 'object' || parsed === null || Array.isArray(parsed)) return;
      frame = parsed as Record<string, unknown>;
    } catch {
      return;
    }
    switch (frame.type) {
      case 'connected':
        this.authenticated = true;
        this.attempt = 0;
        this.clearTimer('authTimer');
        this.setStatus('open');
        for (const crewId of this.crews.keys()) this.sendCrewSubscribe(crewId);
        this.sendSummarySubscribe();
        return;
      case 'crew.summary':
        for (const handler of [...this.summaries]) handler((frame.crews as CrewSummaryItem[]) ?? []);
        return;
      case 'crew.subscribed':
        if (frame.crew_id === '*') return;
        this.dispatch(frame.crew_id, frame as unknown as CrewFrame);
        return;
      case 'crew.event':
      case 'presence':
      case 'resync_required':
        this.dispatch(frame.crew_id, frame as unknown as CrewFrame);
        return;
      case 'error': {
        const data = (frame.data ?? {}) as CrewErrorFrame['data'];
        if (data.channel === 'crew' && typeof data.crew_id === 'string') this.dispatch(data.crew_id, frame as unknown as CrewErrorFrame);
        return;
      }
      default:
        return; // memory events and other channels share the socket
    }
  }

  private dispatch(crewId: unknown, frame: CrewFrame | CrewErrorFrame): void {
    if (typeof crewId !== 'string') return;
    const subs = this.crews.get(crewId);
    if (!subs) return;
    for (const sub of [...subs]) sub.handler(frame);
  }
}
