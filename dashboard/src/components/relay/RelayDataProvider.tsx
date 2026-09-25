import { useCallback, type ReactNode } from 'react';
import { useResource } from '../../hooks/useResource';
import { RELAY_POLL_MS, RelayDataContext, TRAIL_HEAD_LIMIT, type RelayData } from '../../hooks/relayData';
import { api } from '../../lib/api';
import { relay } from '../../lib/relay';

export function RelayDataProvider({ userKey, children }: { userKey: string; children: ReactNode }) {
  const trail = useResource(`trail:${userKey}`, () => relay.trail({ limit: TRAIL_HEAD_LIMIT }), { pollMs: RELAY_POLL_MS });
  const summary = useResource(`summary:${userKey}`, () => relay.summary(14), { pollMs: RELAY_POLL_MS });
  const inbox = useResource(`inbox:${userKey}`, () => relay.inboxSummary(), { pollMs: RELAY_POLL_MS });
  const usage = useResource(`usage:${userKey}`, () => api.getUsageSummary(), { pollMs: 2 * 60000 });

  const { refresh: refreshTrail } = trail;
  const { refresh: refreshSummary } = summary;
  const { refresh: refreshInbox } = inbox;
  const refreshAll = useCallback(() => {
    refreshTrail();
    refreshSummary();
    refreshInbox();
  }, [refreshTrail, refreshSummary, refreshInbox]);

  const value: RelayData = { trail, summary, inbox, usage, refreshAll };
  return <RelayDataContext.Provider value={value}>{children}</RelayDataContext.Provider>;
}
