// React binding for FeedLog: one log per crew while the feed is on screen,
// tapping the shared crew socket of the signed-in tab. It starts once the
// crew store has its snapshot (so the store's own subscription, with its
// replay cursor, is always the one that opens the server subscription) and
// follows the store's head for the polling fallback.

import { useEffect, useMemo, useSyncExternalStore } from 'react';
import { useCrewRuntime } from '../../../lib/crew/context';
import { FeedLog, type FeedView } from './feedLog';

export interface UseFeedLog extends FeedView {
  loadOlder: () => void;
  retry: () => void;
}

const IDLE: FeedView = {
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

/** A stable external store that follows whichever FeedLog is attached (a log is single-use). */
class FeedHandle {
  log: FeedLog | null = null;
  private unsub: (() => void) | null = null;
  private readonly listeners = new Set<() => void>();

  getView = (): FeedView => this.log?.getView() ?? IDLE;

  subscribe = (listener: () => void): (() => void) => {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  };

  private emit(): void {
    for (const l of [...this.listeners]) l();
  }

  attach(log: FeedLog): void {
    this.unsub?.();
    this.log = log;
    this.unsub = log.subscribe(() => this.emit());
    this.emit();
  }

  detach(log: FeedLog): void {
    if (this.log !== log) return;
    this.unsub?.();
    this.unsub = null;
    this.log = null;
    this.emit();
  }
}

export function useFeedLog(crewId: string, headSeq: number | null): UseFeedLog {
  const runtime = useCrewRuntime();
  const handle = useMemo(() => new FeedHandle(), []);
  const ready = headSeq !== null;

  useEffect(() => {
    if (!ready) return undefined;
    const release = runtime.leaseCrew(crewId); // the store outlives the tap
    const log = new FeedLog(crewId, {
      api: runtime.api,
      tap: (id, handler) => runtime.socket.subscribeCrew(id, handler, () => null),
    });
    handle.attach(log);
    log.start(runtime.storeFor(crewId).getView().state?.last_seq ?? null);
    return () => {
      log.stop();
      handle.detach(log);
      release();
    };
  }, [handle, ready, runtime, crewId]);

  useEffect(() => {
    if (headSeq !== null) handle.log?.notifyHead(headSeq);
  }, [handle, headSeq]);

  const view = useSyncExternalStore(handle.subscribe, handle.getView, handle.getView);
  return useMemo(
    () => ({
      ...view,
      loadOlder: () => void handle.log?.loadOlder(),
      retry: () => handle.log?.retry(),
    }),
    [view, handle],
  );
}
