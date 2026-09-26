// Live state of one crew for a component: snapshot first, then events over
// the shared WebSocket strictly by seq, presence overlays every ≤5 s, and a
// polling fallback while the socket is down (spec §4.4, §9.15).
//
//   const crew = useCrewSocket(crewId);
//   crew.status   'loading' | 'live' | 'polling' | 'resyncing' | 'not_found' | 'error' | 'stopped'
//   crew.state    reducer state (docs/crew/reducer.md), null until the snapshot arrives
//   crew.meta     snapshot extras: commons, ignore, footprints, server clock offset
//   crew.connection  the shared socket's status
//   crew.refresh()   refetch the snapshot now

import { useCallback, useMemo, useSyncExternalStore } from 'react';
import { useCrewRuntime } from '../lib/crew/context';
import { useCrewConnection } from '../lib/crew/hooks';
import type { CrewStreamView } from '../lib/crew/store';
import type { ConnectionStatus } from '../lib/crew/socket';

export interface UseCrewSocket extends CrewStreamView {
  connection: ConnectionStatus;
  refresh: () => void;
}

const NONE: CrewStreamView = { crewId: '', status: 'stopped', state: null, meta: null, error: null, lastFrameAt: 0 };
const noopSubscribe = () => () => {};

export function useCrewSocket(crewId: string | null): UseCrewSocket {
  const runtime = useCrewRuntime();
  const connection = useCrewConnection();
  const subscribe = useCallback(
    (onChange: () => void) => {
      if (!crewId) return noopSubscribe();
      const release = runtime.leaseCrew(crewId);
      const unsubscribe = runtime.storeFor(crewId).subscribe(onChange);
      return () => {
        unsubscribe();
        release();
      };
    },
    [runtime, crewId],
  );
  const getView = useCallback(() => (crewId ? runtime.storeFor(crewId).getView() : NONE), [runtime, crewId]);
  const view = useSyncExternalStore(subscribe, getView, getView);
  const refresh = useCallback(() => {
    if (crewId) void runtime.storeFor(crewId).refresh();
  }, [runtime, crewId]);
  return useMemo(() => ({ ...view, connection, refresh }), [view, connection, refresh]);
}
