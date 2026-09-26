// Session queues (§5.8, under "details"): what is waiting for each live
// session (mentions, handover offers, overrides, collisions) until the agent
// picks it up at its next hook or MCP call. There is no per-session REST
// listing for humans, so this folds the crew's recent event log.

import { useEffect, useMemo, useRef } from 'react';
import { useCrewSocket } from '../../../hooks/useCrewSocket';
import { useResource } from '../../../hooks/useResource';
import { crewApi } from '../../../lib/crew/api';
import type { CrewEvent } from '../../../lib/crew/types';
import { ErrorNotice, Pill, TrailSkeleton } from '../../relay/ui';
import { isLiveSession, when } from '../channel/model';
import { PixelGlyph } from '../channel/pixels';
import { QUEUE_WINDOW, foldSessionQueues, kindLabel } from './model';

async function recentEvents(crewId: string, lastSeq: number): Promise<CrewEvent[]> {
  let since = Math.max(0, lastSeq - QUEUE_WINDOW);
  const out: CrewEvent[] = [];
  for (let page = 0; page < Math.ceil(QUEUE_WINDOW / 200) + 1; page++) {
    const res = await crewApi.events(crewId, since, { limit: 200 });
    const events = res.data?.events ?? [];
    out.push(...events);
    if (!res.data?.has_more || !events.length) break;
    since = events[events.length - 1].seq;
  }
  return out;
}

export function SessionQueues({ crewId, project }: { crewId: string; project: string }) {
  const crew = useCrewSocket(crewId);
  const state = crew.state;
  const lastSeq = state?.last_seq ?? null;
  const res = useResource(lastSeq === null ? null : `session-queues:${crewId}`, () => recentEvents(crewId, lastSeq ?? 0));
  const { refresh } = res;
  const inbox = state?.inbox;
  const first = useRef(true);
  useEffect(() => {
    if (first.current) {
      first.current = false;
      return;
    }
    refresh();
  }, [inbox, refresh]);

  const queues = useMemo(() => foldSessionQueues(res.data ?? []), [res.data]);
  const sessions = state ? Object.values(state.sessions).filter(isLiveSession).sort((a, b) => a.callsign.localeCompare(b.callsign, undefined, { numeric: true })) : [];

  if (!state || res.loading) return <TrailSkeleton rows={2} />;
  if (res.error && !res.data) return <ErrorNotice error={res.error} what={`the ${project} session queues`} onRetry={refresh} />;
  if (!sessions.length) return <p className="px-4 py-4 text-sm text-ink-3 sm:px-5">No live sessions on {project}, so no queues.</p>;
  return (
    <ul className="divide-y divide-rule">
      {sessions.map((s) => {
        const items = queues.get(s.id) ?? [];
        return (
          <li key={s.id} className="px-4 py-3 sm:px-5">
            <p className="flex flex-wrap items-center gap-2">
              <span className="font-mono text-[13px] font-bold text-ink">{s.callsign}</span>
              <span className="font-mono text-[11px] text-ink-3">
                {s.agent_id} ({s.agent_verified ? 'key-verified' : 'self-declared'})
              </span>
              <Pill tone={items.length ? 'open' : 'neutral'}>{items.length ? `${items.length} queued` : 'nothing queued'}</Pill>
              <span className="font-mono text-[11px] text-ink-3">sees new items {when(s)}</span>
            </p>
            {items.length > 0 && (
              <ul className="mt-2 space-y-1.5">
                {items.map((i) => (
                  <li key={i.id} className="flex gap-2 text-sm text-ink-2">
                    <PixelGlyph name={i.kind === 'mention' ? 'chat' : i.kind === 'handover_offer' ? 'baton' : i.kind.startsWith('collision') ? 'collision' : 'inbox'} size={12} className="mt-1" />
                    <span className="min-w-0">
                      <span className="font-mono text-[11px] uppercase tracking-[0.06em] text-ink-3">{kindLabel(i.kind)}</span>{' '}
                      <span className="[overflow-wrap:anywhere]">{i.title}</span>
                    </span>
                  </li>
                ))}
              </ul>
            )}
          </li>
        );
      })}
      <li className="px-4 py-2.5 font-mono text-[11px] text-ink-3 sm:px-5">From the last {QUEUE_WINDOW} crew events. An item stays listed after the agent has seen it, until it is resolved.</li>
    </ul>
  );
}
