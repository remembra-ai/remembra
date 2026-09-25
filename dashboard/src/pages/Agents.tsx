// Agents: one card per agent identity seen on the trail, with its last
// activity, sessions this week and a 14-day sparkline. The pulse means it
// was active in the last hour.

import { ArrowRight, PenLine } from 'lucide-react';
import { useRelayData } from '../hooks/relayData';
import { useNow } from '../hooks/useResource';
import type { AgentActivity, TrailItem } from '../lib/relay';
import { agentMeta } from '../lib/agents';
import { hrefFor } from '../lib/nav';
import { absoluteTime, minutesSince, relativeTime } from '../lib/time';
import { AgentAvatar, ErrorNotice, Pill, PulseDot, Skeleton, Sparkline, StaleNotice } from '../components/relay/ui';
import { BranchLabel } from '../components/relay/Handoff';
import { ConnectChecklist } from '../components/relay/HomeCards';

function AgentCard({
  agent,
  latest,
  unread,
  days,
  now,
}: {
  agent: AgentActivity;
  latest?: TrailItem;
  unread: number;
  days: number;
  now: Date;
}) {
  const meta = agentMeta(agent.agent_id);
  const minutes = minutesSince(agent.last_active, now);
  const active = minutes !== null && minutes <= 60;
  const titleId = `agent-${agent.agent_id.replace(/[^A-Za-z0-9_-]/g, '_')}`;
  const total14 = agent.daily.reduce((sum, n) => sum + n, 0);
  return (
    <article aria-labelledby={titleId} className="rr-card flex min-w-0 flex-col rounded-[3px]">
      <div className="flex items-start gap-3 px-4 pt-4 sm:px-5">
        <AgentAvatar agentId={agent.agent_id} size="lg" />
        <div className="min-w-0 flex-1">
          <h2 id={titleId} className="font-display truncate text-xl font-bold leading-tight tracking-tight text-ink">
            {meta.name}
          </h2>
          <p className="truncate font-mono text-[11px] text-ink-3">{agent.agent_id}</p>
        </div>
        <span className="mt-1 inline-flex shrink-0 items-center gap-1.5 font-mono text-[11px] text-ink-2">
          <PulseDot active={active} />
          {active ? 'active now' : 'idle'}
        </span>
      </div>

      <dl className="mt-4 grid grid-cols-3 gap-2 border-y border-rule px-4 py-3 sm:px-5">
        <div className="min-w-0">
          <dt className="font-mono text-[10px] uppercase tracking-[0.08em] text-ink-3">This week</dt>
          <dd className="font-display tabular mt-0.5 text-2xl font-extrabold leading-none text-ink">{agent.sessions_7d}</dd>
          <dd className="text-[11px] text-ink-3">{agent.sessions_7d === 1 ? 'session' : 'sessions'}</dd>
        </div>
        <div className="min-w-0">
          <dt className="font-mono text-[10px] uppercase tracking-[0.08em] text-ink-3">Last active</dt>
          <dd className="mt-1 text-sm font-semibold text-ink" title={absoluteTime(agent.last_active)}>
            {relativeTime(agent.last_active, now)}
          </dd>
        </div>
        <div className="min-w-0">
          <dt className="font-mono text-[10px] uppercase tracking-[0.08em] text-ink-3">All time</dt>
          <dd className="mt-1 text-sm font-semibold text-ink">
            {agent.handoffs} <span className="font-normal text-ink-3">{agent.handoffs === 1 ? 'handoff' : 'handoffs'}</span>
          </dd>
        </div>
      </dl>

      <div className="px-4 pt-3 sm:px-5">
        <div className="flex items-baseline justify-between font-mono text-[10px] uppercase tracking-[0.08em] text-ink-3">
          <span>Last {days} days</span>
          <span className="tabular normal-case tracking-normal">
            {total14} {total14 === 1 ? 'entry' : 'entries'}
          </span>
        </div>
        <Sparkline
          className="mt-1.5"
          values={agent.daily}
          height={34}
          label={`${meta.name}: activity per day for the last ${days} days, ${agent.daily.join(', ')}; today is the last value`}
        />
      </div>

      <div className="flex-1 px-4 pt-3 sm:px-5">
        {latest ? (
          <div className="border-l-[3px] border-signal bg-paper px-3 py-2">
            <p className="font-mono text-[10px] uppercase tracking-[0.08em] text-ink-3">Latest</p>
            <p className="mt-0.5 line-clamp-2 text-sm text-ink [overflow-wrap:anywhere]">{latest.headline}</p>
            <div className="mt-1 flex flex-wrap items-center gap-1.5">
              <BranchLabel branch={latest.branch} sha={latest.head_commit} className="mr-1 max-w-full" />
              {latest.failing > 0 && <Pill tone="fail">{latest.failing} failing</Pill>}
              {latest.open > 0 && <Pill tone="open">{latest.open} open</Pill>}
            </div>
          </div>
        ) : null}
        {agent.projects.length > 0 && (
          <p className="mt-3 flex flex-wrap gap-1.5" aria-label="Projects">
            {agent.projects.map((project) => (
              <a key={project} href={hrefFor('trail', { project, agent: agent.agent_id })} className="hover:opacity-80">
                <Pill>{project}</Pill>
              </a>
            ))}
          </p>
        )}
      </div>

      <div className="mt-4 flex flex-wrap items-center gap-2 border-t border-rule px-4 py-3 sm:px-5">
        <a
          href={hrefFor('trail', { agent: agent.agent_id })}
          className="inline-flex items-center gap-1 text-sm font-semibold text-ink underline decoration-signal decoration-2 underline-offset-4"
        >
          Trail <ArrowRight className="h-3.5 w-3.5" aria-hidden="true" />
        </a>
        <a
          href={hrefFor('inbox', { compose: '1', to: agent.agent_id })}
          className="rr-btn-ghost ml-auto inline-flex items-center gap-1.5 px-2.5 py-1.5 text-sm"
        >
          <PenLine className="h-3.5 w-3.5" aria-hidden="true" /> Message
          {unread > 0 && <span className="font-mono text-[11px] text-signal-ink">{unread} unread</span>}
        </a>
      </div>
    </article>
  );
}

export function Agents() {
  const { summary, trail, inbox } = useRelayData();
  const now = useNow(30000);
  const agents = summary.data?.agents ?? [];
  const latestByAgent = new Map<string, TrailItem>();
  for (const item of trail.data?.items ?? []) {
    if (item.agent_id && item.memory_type === 'handoff' && !latestByAgent.has(item.agent_id)) {
      latestByAgent.set(item.agent_id, item);
    }
  }
  const unreadByAgent = new Map((inbox.data?.agents ?? []).map((a) => [a.agent_id, a.unread]));
  const activeNow = agents.filter((a) => {
    const m = minutesSince(a.last_active, now);
    return m !== null && m <= 60;
  }).length;

  return (
    <div className="space-y-4">
      {summary.data && agents.length > 0 && (
        <p className="font-mono text-xs text-ink-3" aria-live="polite">
          {agents.length} {agents.length === 1 ? 'agent' : 'agents'} · {activeNow} active in the last hour ·{' '}
          {summary.data.week.handoffs} handoffs this week
        </p>
      )}
      {summary.data && summary.error != null && <StaleNotice error={summary.error} what="agent activity" />}

      {summary.loading && (
        <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-3" role="status" aria-label="Loading agents">
          {[0, 1, 2].map((i) => (
            <div key={i} className="rr-card space-y-3 rounded-[3px] p-5">
              <div className="flex gap-3">
                <Skeleton className="h-11 w-11" />
                <div className="flex-1 space-y-2">
                  <Skeleton className="h-4 w-1/2" />
                  <Skeleton className="h-3 w-1/3" />
                </div>
              </div>
              <Skeleton className="h-12 w-full" />
              <Skeleton className="h-8 w-full" />
            </div>
          ))}
        </div>
      )}

      {!summary.data && !summary.loading && summary.error != null && (
        <div className="rr-card rounded-[3px]">
          <ErrorNotice error={summary.error} what="agent activity" onRetry={summary.refresh} />
        </div>
      )}

      {summary.data && agents.length === 0 && (
        <div className="grid gap-4 lg:grid-cols-[minmax(0,1fr)_minmax(0,1fr)]">
          <div className="rr-card rounded-[3px] px-5 py-6">
            <p className="rr-eyebrow">No agents yet</p>
            <p className="font-display mt-2 text-2xl font-bold leading-tight text-ink">Agents appear here after their first handoff.</p>
            <p className="mt-2 text-sm text-ink-2">
              Each card shows when the agent was last active, its sessions this week and two weeks of activity. An orange pulse means it
              is working right now.
            </p>
          </div>
          <ConnectChecklist agents={agents} now={now} />
        </div>
      )}

      {agents.length > 0 && (
        <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-3">
          {agents.map((agent) => (
            <AgentCard
              key={agent.agent_id}
              agent={agent}
              latest={latestByAgent.get(agent.agent_id)}
              unread={unreadByAgent.get(agent.agent_id) ?? 0}
              days={summary.data?.days ?? agent.daily.length}
              now={now}
            />
          ))}
        </div>
      )}
    </div>
  );
}
