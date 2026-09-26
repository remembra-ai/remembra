// Inbox (`#/inbox?scope=needs-you|crew[&project=X]`, spec §5.8, §9.9).
//
// Two visible tabs: Needs you ("things only a person can decide") and Crew
// ("work any agent on this crew can pick up"). "Details" reveals the agent
// inbox (notes between agents, the former Inbox page) and the per-session
// queues. The real-time alert targets live at the foot of Needs you.
// Old agent-inbox links (?compose=1, ?open=…, ?to=…) open the agent inbox.

import { useEffect } from 'react';
import clsx from 'clsx';
import { useCrewList } from '../lib/crew/hooks';
import { hrefFor, useRoute } from '../lib/nav';
import { AgentInbox } from '../components/crew/inbox/AgentInbox';
import { InboxList } from '../components/crew/inbox/InboxList';
import { InboxTabs } from '../components/crew/inbox/InboxTabs';
import { inboxParams, isSafety, parseInboxRoute, type InboxDetails } from '../components/crew/inbox/model';
import { SessionQueues } from '../components/crew/inbox/SessionQueues';
import { useInboxItems } from '../components/crew/inbox/useInbox';
import { PixelGlyph } from '../components/crew/channel/pixels';
import { NotifyTargets } from '../components/crew/notify/NotifyTargets';

function Details({ details, project, crewId }: { details: InboxDetails; project: string | null; crewId: string | null }) {
  const { params } = useRoute();
  const route = parseInboxRoute(params);
  const link = (next: InboxDetails) => hrefFor('inbox', inboxParams({ ...route, details: next }));
  const crews = useCrewList();
  return (
    <section aria-label="Details" className="space-y-3">
      <div className="flex flex-wrap items-center gap-2">
        <a
          href={link(details ? null : 'agent')}
          aria-expanded={details !== null}
          className="inline-flex items-center gap-2 font-mono text-[12px] text-ink-2 hover:text-ink"
        >
          <PixelGlyph name="inbox" size={13} />
          {details ? 'Hide details' : 'Details: agent inbox and session queues'}
        </a>
        {details && (
          <div role="tablist" aria-label="Details" className="ml-2 flex gap-1">
            {(['agent', 'sessions'] as const).map((d) => (
              <a
                key={d}
                role="tab"
                aria-selected={details === d}
                href={link(d)}
                className={clsx(
                  'rounded-[2px] border px-2.5 py-1 font-mono text-[11px]',
                  details === d ? 'border-ink bg-ink text-paper' : 'border-rule text-ink-2 hover:border-ink hover:text-ink',
                )}
              >
                {d === 'agent' ? 'Agent inbox' : 'Session queues'}
              </a>
            ))}
          </div>
        )}
      </div>
      {details === 'agent' && <AgentInbox />}
      {details === 'sessions' &&
        (project && crewId ? (
          <div className="rr-card rounded-[3px]">
            <SessionQueues crewId={crewId} project={project} />
          </div>
        ) : (
          <div className="rr-card rounded-[3px] px-4 py-4 sm:px-5">
            <p className="text-sm text-ink-2">Session queues are per crew. Pick a project:</p>
            <p className="mt-2 flex flex-wrap gap-1.5">
              {crews.items.map((c) => (
                <a
                  key={c.crew.id}
                  href={hrefFor('inbox', inboxParams({ ...route, project: c.crew.project_id, details: 'sessions' }))}
                  className="rounded-[2px] border border-rule px-2 py-0.5 font-mono text-[11px] text-ink-2 hover:border-ink"
                >
                  {c.crew.project_id}
                </a>
              ))}
              {crews.items.length === 0 && <span className="font-mono text-[11px] text-ink-3">No crews yet.</span>}
            </p>
          </div>
        ))}
    </section>
  );
}

export function Inbox() {
  const { params } = useRoute();
  const route = parseInboxRoute(params);
  const data = useInboxItems(route.scope, route.project);
  const safetyCount = data.items.filter((i) => i.audience === 'project' && isSafety(i)).length;

  useEffect(() => {
    if (route.alerts) document.getElementById('realtime-alerts')?.scrollIntoView({ block: 'start', behavior: 'smooth' });
  }, [route.alerts]);

  return (
    <div className="space-y-4">
      <InboxTabs route={route} crews={data.crews} safetyCount={safetyCount} />
      <InboxList scope={route.scope} project={route.project} data={data} />
      <Details details={route.details} project={route.project} crewId={data.crewId} />
      {(route.scope === 'needs-you' || route.alerts) && <NotifyTargets highlight={route.alerts} />}
    </div>
  );
}
