// React hooks over the crew runtime. Each hook leases the store it reads in
// useSyncExternalStore's subscribe, so a store runs exactly while something
// on screen shows it (plus a short linger).

import { useCallback, useEffect, useMemo, useSyncExternalStore } from 'react';
import { useCrewRuntime } from './context';
import type { CrewListView } from './crews';
import type { CrewMode } from './runtime';
import type { ConnectionStatus } from './socket';

/** Whether the server runs Crew mode ('off': hide the crew screens). Asks the server once per tab. */
export function useCrewMode(): CrewMode {
  const runtime = useCrewRuntime();
  useEffect(() => runtime.probeCrewMode(), [runtime]);
  return useSyncExternalStore(runtime.onCrewMode, runtime.crewMode, runtime.crewMode);
}

/** Status of the shared crew WebSocket (for the connection pill). */
export function useCrewConnection(): ConnectionStatus {
  const runtime = useCrewRuntime();
  return useSyncExternalStore(runtime.onConnectionStatus, runtime.connectionStatus, () => 'idle');
}

/**
 * Every crew the user can read, with live summary counts (Site Board, sidebar badge, palette).
 * With `enabled` false the list is read but not loaded or kept live (e.g. a closed palette).
 */
export function useCrewList(enabled = true): CrewListView & { refresh: () => void } {
  const runtime = useCrewRuntime();
  const subscribe = useCallback(
    (onChange: () => void) => {
      if (!enabled) return () => {};
      const release = runtime.leaseCrewList();
      const unsubscribe = runtime.crewList().subscribe(onChange);
      return () => {
        unsubscribe();
        release();
      };
    },
    [runtime, enabled],
  );
  const view = useSyncExternalStore(subscribe, () => runtime.crewList().getView(), () => runtime.crewList().getView());
  const refresh = useCallback(() => void runtime.crewList().refresh(), [runtime]);
  return useMemo(() => ({ ...view, refresh }), [view, refresh]);
}

export type CrewLookup =
  | { status: 'loading'; crewId: null }
  | { status: 'found'; crewId: string }
  | { status: 'none'; crewId: null }
  | { status: 'error'; crewId: null; error: unknown };

/** The crew of a project id (from the crew list: the same ACL as `/crews/resolve`). */
export function useCrewForProject(project: string | null): CrewLookup {
  const list = useCrewList();
  return useMemo((): CrewLookup => {
    if (!project) return { status: 'none', crewId: null };
    const item = list.items.find((c) => c.crew.project_id === project);
    if (item) return { status: 'found', crewId: item.crew.id };
    if (list.status === 'loading') return { status: 'loading', crewId: null };
    if (list.status === 'error') return { status: 'error', crewId: null, error: list.error };
    return { status: 'none', crewId: null };
  }, [project, list.items, list.status, list.error]);
}
