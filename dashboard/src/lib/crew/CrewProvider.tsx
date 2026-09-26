// Mounts the crew data layer for the signed-in user: one CrewRuntime (shared
// /ws connection, per-crew live stores, crew list). Give the provider a `key`
// per user so signing in as someone else starts a fresh runtime; unmounting
// (sign-out) disposes it and closes its socket.

import { useEffect, useState, type ReactNode } from 'react';
import { api, getApiBaseUrl } from '../api';
import { crewApi } from './api';
import { CrewContext } from './context';
import { CrewRuntime } from './runtime';
import { crewSocketUrl } from './socket';

export function CrewProvider({ children }: { children: ReactNode }) {
  const [runtime] = useState(
    () =>
      new CrewRuntime({
        api: crewApi,
        socketUrl: crewSocketUrl(getApiBaseUrl()),
        credentials: () => ({ jwt: api.getJwtToken(), apiKey: api.getApiKey() }),
        createSocket: (url) => new WebSocket(url),
      }),
  );
  useEffect(() => () => runtime.dispose(), [runtime]);
  return <CrewContext.Provider value={runtime}>{children}</CrewContext.Provider>;
}
