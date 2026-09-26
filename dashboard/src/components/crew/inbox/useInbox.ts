// Data for the two inbox tabs. One project: that crew's REST inbox, refetched
// whenever its live stream reports an inbox change. All projects: "My inbox"
// (`/crews/inbox/overview`) for Needs-you, or every crew with open crew work,
// refetched when the live crew-list counts move and polled every 30 s.

import { useEffect, useMemo, useRef } from 'react';
import { useCrewSocket } from '../../../hooks/useCrewSocket';
import { useResource, type Resource } from '../../../hooks/useResource';
import { crewApi } from '../../../lib/crew/api';
import { useCrewList } from '../../../lib/crew/hooks';
import type { InboxScope } from '../../../lib/crew/routes';
import type { CrewListItem, CrewState, SessionView } from '../../../lib/crew/types';
import { sortItems, type InboxItem } from './model';

/** Crews fetched at most for the all-projects Crew tab. */
const MAX_CREWS = 12;

export interface InboxData {
  items: InboxItem[];
  loading: boolean;
  error: unknown;
  refresh: () => void;
  /** Live state of the one crew (project mode), for session labels and decisions. */
  state: CrewState | null;
  crews: CrewListItem[];
  crewsLoading: boolean;
  /** The crew id of `project` (null in all-projects mode, or when it has no crew). */
  crewId: string | null;
  sessions: SessionView[];
}

function withProject(items: InboxItem[], crews: CrewListItem[]): InboxItem[] {
  const byId = new Map(crews.map((c) => [c.crew.id, c.crew.project_id]));
  return items.map((i) => (i.project_id ? i : { ...i, project_id: byId.get(i.crew_id) }));
}

export function useInboxItems(scope: InboxScope, project: string | null): InboxData {
  const list = useCrewList();
  const crew = project ? list.items.find((c) => c.crew.project_id === project) : undefined;
  const crewId = crew?.crew.id ?? null;
  const stream = useCrewSocket(crewId);
  const state = stream.state;
  const audience = scope === 'crew' ? 'crew' : 'project';

  const crewIdsWithWork = useMemo(
    () =>
      list.items
        .filter((c) => (scope === 'crew' ? c.crew_inbox > 0 : c.needs_you > 0))
        .slice(0, MAX_CREWS)
        .map((c) => c.crew.id),
    [list.items, scope],
  );

  const key = project ? (crewId ? `inbox:${audience}:${crewId}` : null) : list.status === 'loading' ? null : `inbox:${audience}:*`;
  const res: Resource<InboxItem[]> = useResource(
    key,
    async () => {
      if (crewId) return (await crewApi.inbox(crewId, audience, 200)).items as InboxItem[];
      if (scope === 'needs-you') return (await crewApi.inboxOverview(200)).items as unknown as InboxItem[];
      const pages = await Promise.all(crewIdsWithWork.map((id) => crewApi.inbox(id, 'crew', 100)));
      return pages.flatMap((p) => p.items as InboxItem[]);
    },
    { pollMs: 30000 },
  );

  // Live: refetch on inbox events (project) or when the crew-list counts move (all projects).
  const version = project
    ? state
      ? `${state.inbox_counts.project}:${state.inbox_counts.crew}:${Object.keys(state.inbox).join(',')}`
      : ''
    : list.items.map((c) => `${c.crew.id}:${c.needs_you}:${c.crew_inbox}`).join('|');
  const seen = useRef<string | null>(null);
  const { refresh } = res;
  useEffect(() => {
    if (!version) return;
    if (seen.current !== null && seen.current !== version) refresh();
    seen.current = version;
  }, [version, refresh]);

  // Sessions of the crews with agent-originated items (for "from cc-2 (self-declared)").
  const agentCrews = useMemo(
    () => (crewId ? [] : [...new Set((res.data ?? []).filter((i) => i.origin === 'agent').map((i) => i.crew_id))].sort()),
    [res.data, crewId],
  );
  const dir = useResource(agentCrews.length ? `inbox-sessions:${agentCrews.join(',')}` : null, async () => {
    const pages = await Promise.all(agentCrews.map((id) => crewApi.sessions(id).catch(() => ({ crew_id: id, sessions: [] as SessionView[] }))));
    return pages.flatMap((p) => p.sessions);
  });
  const sessions = useMemo(() => (state ? (Object.values(state.sessions) as SessionView[]) : (dir.data ?? [])), [state, dir.data]);

  const items = useMemo(() => sortItems(withProject(res.data ?? [], list.items)), [res.data, list.items]);
  return {
    items,
    loading: res.loading || (list.status === 'loading' && !list.items.length),
    error: res.error ?? (list.status === 'error' ? list.error : null),
    refresh,
    state,
    crews: list.items,
    crewsLoading: list.status === 'loading',
    crewId,
    sessions,
  };
}

/** Needs-you across every crew (the sidebar Inbox badge, §9.1). Live over the crew list's summary counts. */
export function useNeedsYouTotal(): number {
  const list = useCrewList();
  return list.items.reduce((n, c) => n + Math.max(0, c.needs_you), 0);
}
