// The crew data layer of one signed-in dashboard tab: one shared /ws
// connection, one CrewStore per crew being viewed (shared by every component
// that shows it) and one crew list. Components lease what they show; a store
// that nobody has leased for `lingerMs` is stopped, so a quick remount (tab
// switch, StrictMode) keeps the live state instead of refetching it.
//
// `dispose()` closes everything (sign-out). A later lease starts again from a
// fresh socket, so a disposed runtime is never left half-dead.

import { CrewApiError, type CrewApi, type CrewCredentials } from './api';
import { CrewListStore } from './crews';
import { CrewSocket, realTimers, type ConnectionStatus, type SocketFactory, type Timers } from './socket';
import { CrewStore } from './store';

export interface CrewRuntimeOptions {
  api: CrewApi;
  socketUrl: string;
  credentials: () => CrewCredentials;
  createSocket: SocketFactory;
  timers?: Timers;
  lingerMs?: number;
  pollMs?: number;
}

/**
 * Whether this server runs Crew mode. The crew routers exist only with REMEMBRA_CREW_MODE on, so
 * `GET /crews` answering 404 means off: the dashboard then shows what it showed before crews
 * (no Crews section, the agent inbox as the Inbox). Unknown until the server has answered.
 */
export type CrewMode = 'unknown' | 'on' | 'off';

interface Entry<T> {
  value: T;
  leases: number;
  started: boolean;
  linger: unknown;
}

export class CrewRuntime {
  readonly api: CrewApi;
  private readonly opts: CrewRuntimeOptions & { timers: Timers; lingerMs: number };
  private socket_: CrewSocket | null = null;
  private readonly stores = new Map<string, Entry<CrewStore>>();
  private list: Entry<CrewListStore> | null = null;
  private readonly statusListeners = new Set<() => void>();
  private unsubscribeStatus: (() => void) | null = null;
  private mode: CrewMode = 'unknown';
  private modeProbe: Promise<void> | null = null;
  private readonly modeListeners = new Set<() => void>();

  constructor(options: CrewRuntimeOptions) {
    this.api = options.api;
    this.opts = { lingerMs: 15000, ...options, timers: options.timers ?? realTimers };
  }

  /** The shared socket (created on first use). */
  get socket(): CrewSocket {
    if (!this.socket_) {
      this.socket_ = new CrewSocket({
        url: this.opts.socketUrl,
        credentials: this.opts.credentials,
        createSocket: this.opts.createSocket,
        timers: this.opts.timers,
      });
      this.unsubscribeStatus = this.socket_.onStatus(() => {
        for (const listener of [...this.statusListeners]) listener();
      });
    }
    return this.socket_;
  }

  connectionStatus = (): ConnectionStatus => this.socket_?.status ?? 'idle';

  onConnectionStatus = (listener: () => void): (() => void) => {
    this.statusListeners.add(listener);
    return () => this.statusListeners.delete(listener);
  };

  crewMode = (): CrewMode => this.mode;

  onCrewMode = (listener: () => void): (() => void) => {
    this.modeListeners.add(listener);
    return () => this.modeListeners.delete(listener);
  };

  /** Ask the server once whether it runs Crew mode (`GET /crews`); the crew list keeps the answer current. */
  probeCrewMode(): void {
    if (this.mode !== 'unknown' || this.modeProbe) return;
    this.modeProbe = this.api
      .listCrews()
      .then(
        () => this.setCrewMode('on'),
        (err: unknown) => {
          if (err instanceof CrewApiError && err.status === 404) this.setCrewMode('off');
        },
      )
      .finally(() => {
        this.modeProbe = null;
      });
  }

  private setCrewMode(mode: 'on' | 'off'): void {
    if (this.mode === mode) return;
    this.mode = mode;
    for (const listener of [...this.modeListeners]) listener();
  }

  /** The store of a crew without starting it (for render-time reads). */
  storeFor(crewId: string): CrewStore {
    let entry = this.stores.get(crewId);
    if (!entry) {
      entry = {
        value: new CrewStore(crewId, { api: this.api, socket: this.socket, timers: this.opts.timers, pollMs: this.opts.pollMs }),
        leases: 0,
        started: false,
        linger: null,
      };
      this.stores.set(crewId, entry);
    }
    return entry.value;
  }

  /** Start (or keep) a crew's live store; call the returned function when done. */
  leaseCrew(crewId: string): () => void {
    this.storeFor(crewId);
    const entry = this.stores.get(crewId)!;
    this.hold(entry);
    return this.releaser(entry, () => {
      entry.value.stop();
      if (this.stores.get(crewId) === entry) this.stores.delete(crewId);
    });
  }

  crewList(): CrewListStore {
    if (!this.list) {
      this.list = {
        value: new CrewListStore({
          api: this.api,
          socket: this.socket,
          timers: this.opts.timers,
          onMode: (mode) => this.setCrewMode(mode),
        }),
        leases: 0,
        started: false,
        linger: null,
      };
    }
    return this.list.value;
  }

  leaseCrewList(): () => void {
    this.crewList();
    const entry = this.list!;
    this.hold(entry);
    return this.releaser(entry, () => {
      entry.value.stop();
      if (this.list === entry) this.list = null;
    });
  }

  /** Stop every store and close the socket (sign-out or provider unmount). */
  dispose(): void {
    for (const entry of this.stores.values()) {
      this.clearLinger(entry);
      entry.value.stop();
    }
    this.stores.clear();
    if (this.list) {
      this.clearLinger(this.list);
      this.list.value.stop();
      this.list = null;
    }
    this.unsubscribeStatus?.();
    this.unsubscribeStatus = null;
    this.socket_?.stop();
    this.socket_ = null;
    for (const listener of [...this.statusListeners]) listener();
  }

  private hold(entry: Entry<{ start(): void }>): void {
    entry.leases += 1;
    this.clearLinger(entry);
    if (!entry.started) {
      entry.started = true;
      entry.value.start();
    }
  }

  private releaser(entry: Entry<unknown>, stop: () => void): () => void {
    let released = false;
    return () => {
      if (released) return;
      released = true;
      entry.leases -= 1;
      if (entry.leases > 0) return;
      this.clearLinger(entry);
      entry.linger = this.opts.timers.setTimeout(() => {
        entry.linger = null;
        if (entry.leases === 0) stop();
      }, this.opts.lingerMs);
    };
  }

  private clearLinger(entry: Entry<unknown>): void {
    if (entry.linger !== null) this.opts.timers.clearTimeout(entry.linger);
    entry.linger = null;
  }
}
