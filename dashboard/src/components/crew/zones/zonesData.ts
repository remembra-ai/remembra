// Data the Zone Map and the Policy panel read beside the live reducer state:
// the zones listing (tree snapshot, bootstrap flag), zone changes, bypass codes
// and a rolling tail of the event log (for the drawer's claim history and
// near-misses, and the policy log). Each refetches when the live state says
// something relevant changed, and keeps showing the last good data meanwhile.

import { useCallback, useEffect, useMemo, useRef, useState, useSyncExternalStore } from 'react';
import { useCrewRuntime } from '../../../lib/crew/context';
import type { CrewDetail } from '../../../lib/crew/types';
import { EventTailStore, type EventTailView } from './eventTail';
import { zonesApi, type ZonesApi } from './zonesApi';

export function useZonesApi(): ZonesApi {
  const runtime = useCrewRuntime();
  return useMemo(() => zonesApi(runtime.api), [runtime]);
}

export interface Live<T> {
  data: T | undefined;
  error: unknown;
  loading: boolean;
  refresh: () => void;
}

/**
 * Load `loader()` for `key` (null: nothing) and again whenever `version`
 * changes (a signature of the live state the data depends on). Old data stays
 * on screen while the next load runs; a response for an older request is dropped.
 */
export function useLiveLoad<T>(key: string | null, version: string, loader: () => Promise<T>): Live<T> {
  const loaderRef = useRef(loader);
  useEffect(() => {
    loaderRef.current = loader;
  });
  const [state, setState] = useState<{ key: string | null; data: T | undefined; error: unknown; done: boolean }>({
    key: null,
    data: undefined,
    error: null,
    done: false,
  });
  const [nonce, setNonce] = useState(0);
  const ticket = useRef(0);

  useEffect(() => {
    if (key === null) return undefined;
    const mine = ++ticket.current;
    loaderRef.current().then(
      (data) => {
        if (mine === ticket.current) setState({ key, data, error: null, done: true });
      },
      (error: unknown) => {
        if (mine === ticket.current) setState((prev) => ({ key, data: prev.key === key ? prev.data : undefined, error, done: true }));
      },
    );
    return undefined;
  }, [key, version, nonce]);

  const refresh = useCallback(() => setNonce((n) => n + 1), []);
  const current = state.key === key;
  return {
    data: current ? state.data : undefined,
    error: current ? state.error : null,
    loading: key !== null && !(current && state.done),
    refresh,
  };
}

// ---------------------------------------------------------------------------
// Event tail
// ---------------------------------------------------------------------------

/** Follow the crew's event log from `lastSeq − window` onward (see EventTailStore). */
export function useEventTail(crewId: string | null, lastSeq: number, window = 400): EventTailView {
  const api = useZonesApi();
  const store = useMemo(() => (crewId ? new EventTailStore(api, crewId, window) : null), [api, crewId, window]);
  useEffect(() => {
    if (!store) return undefined;
    store.start();
    return () => store.stop();
  }, [store]);
  useEffect(() => {
    void store?.want(lastSeq);
  }, [store, lastSeq]);
  const subscribe = useCallback((fn: () => void) => (store ? store.subscribe(fn) : () => {}), [store]);
  const get = useCallback(() => (store ? store.getView() : EMPTY_TAIL), [store]);
  return useSyncExternalStore(subscribe, get, get);
}

const EMPTY_TAIL: EventTailView = { events: [], fromSeq: 0, error: null, loading: false };

/** The signed-in principal's role and human-ness on a crew (for enabling human actions). */
export function useCrewAccess(crewId: string | null): Live<CrewDetail> {
  const runtime = useCrewRuntime();
  return useLiveLoad(crewId, '', () => runtime.api.getCrew(crewId!));
}
