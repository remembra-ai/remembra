// The in-app notification list behind the bell: polled every 60 s and
// refetched as soon as a crew's live moment count moves; "mark read" is
// applied locally at once and then on the server (per-crew read cursors).

import { useCallback, useEffect, useRef, useState } from 'react';
import { useResource } from '../../../hooks/useResource';
import { crewApi } from '../../../lib/crew/api';
import { useCrewList } from '../../../lib/crew/hooks';
import { markReadLocally, type NotificationList } from './model';

export interface Notifications {
  list: NotificationList | undefined;
  loading: boolean;
  error: unknown;
  refresh: () => void;
  /** Mark one crew read up to a seq, or everything (crewId null). */
  markRead: (crewId: string | null, uptoSeq?: number | null) => Promise<void>;
}

export function useNotifications(): Notifications {
  const res = useResource('crew-notifications', () => crewApi.notifications({ limit: 30 }) as Promise<unknown> as Promise<NotificationList>, {
    pollMs: 60000,
  });
  const [local, setLocal] = useState<{ base: NotificationList; list: NotificationList } | null>(null);
  const list = local && local.base === res.data ? local.list : res.data;

  const crews = useCrewList();
  const version = crews.items.map((c) => `${c.crew.id}:${c.moments_24h}:${c.needs_you}`).join('|');
  const seen = useRef<string | null>(null);
  const { refresh } = res;
  useEffect(() => {
    if (!version) return;
    if (seen.current !== null && seen.current !== version) refresh();
    seen.current = version;
  }, [version, refresh]);

  const markRead = useCallback(
    async (crewId: string | null, uptoSeq: number | null = null) => {
      if (res.data && list) setLocal({ base: res.data, list: markReadLocally(list, crewId, uptoSeq) });
      try {
        if (crewId === null) await crewApi.markNotificationsRead({ all: true });
        else await crewApi.markNotificationsRead(uptoSeq === null ? { crew_id: crewId } : { crew_id: crewId, upto_seq: uptoSeq });
      } finally {
        refresh();
      }
    },
    [res.data, list, refresh],
  );

  return { list, loading: res.loading, error: res.error, refresh, markRead };
}
