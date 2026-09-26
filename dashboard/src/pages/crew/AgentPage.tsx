// The agent page, L0 minimal (#/agents?agent=…[&session=…][&project=…],
// spec §9.10): per crew the agent works in, its current sessions with their
// enforcement layers, the last 20 sessions (start and end reason), recent
// checkpoints, batons in and out (with the brief each adopter received),
// and the claims and tasks it holds now. The full timeline is L1.
//
// Without `project`, every crew the viewer can read is asked (a crew where
// the agent never ran answers 404 and is skipped).

import { useId, type ReactNode } from 'react';
import clsx from 'clsx';
import { ArrowLeft } from 'lucide-react';
import { useNow, useResource } from '../../hooks/useResource';
import { agentMeta } from '../../lib/agents';
import { CrewApiError, crewApi } from '../../lib/crew/api';
import { useCrewList } from '../../lib/crew/hooks';
import { crewHref } from '../../lib/crew/routes';
import type { CheckpointView, ClaimView, CrewListItem, SessionView, TaskView } from '../../lib/crew/types';
import { hrefFor, useRoute } from '../../lib/nav';
import { absoluteTime, relativeTime } from '../../lib/time';
import { AgentAvatar, Card, ErrorNotice, Pill, TrailSkeleton } from '../../components/relay/ui';
import { EnforcementBadge } from '../../components/crew/lane/LaneParts';
import { enforcementView } from '../../components/crew/lane/model';
import { agentPageData, type AgentPageData, type BatonRowView } from './agentPageModel';

const MAX_CREWS = 12;

async function loadAgent(crews: CrewListItem[], agent: string): Promise<{ crew: CrewListItem; page: AgentPageData }[]> {
  const results = await Promise.all(
    crews.slice(0, MAX_CREWS).map(async (crew) => {
      try {
        const raw = await crewApi.agentPage(crew.crew.id, agent);
        // zone slugs for the claims it holds come from the crew snapshot
        const holdsZones = Array.isArray(raw.claims) && raw.claims.length > 0;
        const snap = holdsZones ? await crewApi.snapshot(crew.crew.id).catch(() => null) : null;
        return { crew, page: agentPageData(raw, snap?.data?.zones ?? []) };
      } catch (err) {
        if (err instanceof CrewApiError && err.status === 404) return null;
        throw err;
      }
    }),
  );
  return results.filter((r): r is { crew: CrewListItem; page: AgentPageData } => r !== null);
}

function Section({ title, children, empty }: { title: string; children: ReactNode; empty?: string | false }) {
  const id = useId();
  return (
    <section aria-labelledby={id} className="border-t border-dashed border-rule px-4 py-3 sm:px-5">
      <h3 id={id} className="rr-eyebrow">
        {title}
      </h3>
      {empty ? <p className="mt-2 text-sm text-ink-3">{empty}</p> : <div className="mt-2">{children}</div>}
    </section>
  );
}

function SessionLine({ s, focus, now }: { s: SessionView; focus: boolean; now: Date }) {
  const ended = s.ended_at
    ? `ended ${relativeTime(s.ended_at, now)}${s.end_reason ? ` (${s.end_reason})` : ''}`
    : s.state.replace('_', ' ');
  return (
    <li className={clsx('grid grid-cols-[auto_minmax(0,1fr)] gap-x-3 py-1.5 font-mono text-[12px]', focus && 'bg-signal-wash')}>
      <span className="font-semibold text-ink">{s.callsign}</span>
      <span className="min-w-0 text-ink-2">
        <span title={absoluteTime(s.joined_at)}>joined {relativeTime(s.joined_at, now)}</span> · {ended}
        {s.branch ? ` · ${s.branch}` : ''}
        {s.agent_verified ? ' · key-verified' : ' · self-declared'}
      </span>
    </li>
  );
}

function BatonLine({ b, dir, now, own }: { b: BatonRowView; dir: 'in' | 'out'; now: Date; own: Map<string, string> }) {
  const other = dir === 'in' ? (b.from_callsign ?? (b.from_session ? b.from_session : 'a reserved slot')) : (b.to_callsign ?? b.to_session);
  const self = own.get(dir === 'in' ? b.to_session : (b.from_session ?? '')) ?? 'this agent';
  return (
    <li className="py-1.5">
      <p className="font-mono text-[12px] text-ink">
        <span className="font-semibold">{self}</span> <span className="text-signal">{dir === 'in' ? '⇠' : '⇢'}</span>{' '}
        {dir === 'in' ? `from ${other}` : `to ${other}`} · {b.kind.replace(/_/g, ' ')}
        {b.restored === true ? ' · work restored ✓' : ''}
        <span className="text-ink-3" title={absoluteTime(b.created_at)}>
          {' '}
          · {relativeTime(b.created_at, now)}
        </span>
      </p>
      {b.brief_text && (
        <details className="mt-1">
          <summary className="cursor-pointer font-mono text-[11px] text-ink-3">brief it received</summary>
          <pre className="mt-1 max-h-56 overflow-auto whitespace-pre-wrap rounded-[2px] bg-paper-2 p-2 font-mono text-[11px] text-ink-2 [overflow-wrap:anywhere]">
            {b.brief_text}
          </pre>
        </details>
      )}
    </li>
  );
}

function CrewSection({ crew, page, session, now }: { crew: CrewListItem; page: AgentPageData; session: string | null; now: Date }) {
  const titleId = useId();
  const project = crew.crew.project_id;
  const byId = new Map(page.claimsZoneLabels);
  const own = new Map([...page.sessions, ...page.current].map((s) => [s.id, s.callsign] as [string, string]));
  return (
    <Card labelledBy={titleId}>
      <div className="flex flex-wrap items-baseline justify-between gap-2 px-4 pb-3 pt-4 sm:px-5">
        <h2 id={titleId} className="font-display text-lg font-extrabold uppercase text-ink">
          <a href={crewHref(project)} className="hover:underline">
            {crew.crew.name || project}
          </a>
        </h2>
        <span className="font-mono text-[11px] text-ink-3">
          {page.current.length} running · {page.sessions.length} recent sessions
        </span>
      </div>
      <Section title="Running now" empty={page.current.length === 0 && 'Not running in this crew right now.'}>
        <ul className="space-y-2">
          {page.current.map((s) => (
            <li key={s.id} className={clsx('flex flex-wrap items-center gap-2', s.id === session && 'bg-signal-wash')}>
              <span className="font-mono text-sm font-semibold text-ink">{s.callsign}</span>
              <Pill>{s.state.replace('_', ' ')}</Pill>
              <EnforcementBadge view={enforcementView(s)} />
            </li>
          ))}
        </ul>
      </Section>
      <Section title="Holds" empty={page.claims.length === 0 && page.tasks.length === 0 && 'No claims or open tasks.'}>
        <ul className="space-y-1 font-mono text-[12px] text-ink-2">
          {page.tasks.map((t: TaskView) => (
            <li key={t.id}>
              <span className="font-semibold text-ink">T-{t.number}</span> <span className="font-sans text-sm">{t.title}</span> ·{' '}
              {t.status.replace('_', ' ')}
            </li>
          ))}
          {page.claims.map((c: ClaimView) => (
            <li key={c.id}>
              {byId.get(c.id) ?? c.resource ?? c.id} · {c.mode} · {c.state}
              {c.state === 'reserved' && c.reserve_reason ? ` (${c.reserve_reason})` : ''} · epoch {c.epoch}
            </li>
          ))}
        </ul>
      </Section>
      <Section title="Batons" empty={page.batonsIn.length === 0 && page.batonsOut.length === 0 && 'No batons passed yet.'}>
        <ul className="divide-y divide-dashed divide-rule">
          {page.batonsIn.map((b) => (
            <BatonLine key={`in-${b.id}`} b={b} dir="in" now={now} own={own} />
          ))}
          {page.batonsOut.map((b) => (
            <BatonLine key={`out-${b.id}`} b={b} dir="out" now={now} own={own} />
          ))}
        </ul>
      </Section>
      <Section title="Checkpoints" empty={page.checkpoints.length === 0 && 'No checkpoints yet.'}>
        <ul className="space-y-1">
          {page.checkpoints.map((c: CheckpointView & { created_at: string }) => (
            <li key={c.id} className="grid grid-cols-[auto_minmax(0,1fr)] gap-x-2 font-mono text-[12px]">
              <span className="text-ink">◆</span>
              <span className="min-w-0 text-ink-2">
                <span className="text-ink">{c.headline || c.trigger}</span> · {c.trigger} · {c.facts_source}
                <span className="text-ink-3" title={absoluteTime(c.created_at)}>
                  {' '}
                  · {relativeTime(c.created_at, now)}
                </span>
              </span>
            </li>
          ))}
        </ul>
      </Section>
      <Section title="Last 20 sessions" empty={page.sessions.length === 0 && 'No sessions yet.'}>
        <ul className="divide-y divide-dashed divide-rule">
          {page.sessions.map((s) => (
            <SessionLine key={s.id} s={s} focus={s.id === session} now={now} />
          ))}
        </ul>
      </Section>
    </Card>
  );
}

export function AgentPage({ agent, session, project }: { agent: string; session: string | null; project: string | null }) {
  const list = useCrewList();
  const now = useNow(30000);
  const crews = list.items.filter((c) => !project || c.crew.project_id === project);
  const key = list.status === 'loading' ? null : `agent-page-${agent}-${crews.map((c) => c.crew.id).join(',')}`;
  const res = useResource(key, () => loadAgent(crews, agent), { pollMs: 30000 });
  const meta = agentMeta(agent);
  const verified = res.data?.some((r) => r.page.verified) ?? false;

  return (
    <div className="space-y-3">
      <a
        href={project ? crewHref(project) : hrefFor('agents')}
        className="inline-flex items-center gap-1 font-mono text-[12px] text-ink-2 hover:text-ink"
      >
        <ArrowLeft className="h-3.5 w-3.5" aria-hidden="true" /> {project ? project : 'All agents'}
      </a>
      <header className="flex items-center gap-3">
        <AgentAvatar agentId={agent} size="lg" />
        <div className="min-w-0">
          <h1 className="font-display truncate text-2xl font-extrabold tracking-tight text-ink">{meta.name}</h1>
          <p className="font-mono text-[12px] text-ink-3">
            {agent}
            {res.data && res.data.length > 0 && (verified ? ' · key-verified' : ' · self-declared')}
          </p>
        </div>
      </header>
      {(list.status === 'loading' || res.loading) && <TrailSkeleton rows={2} />}
      {res.error != null && !res.data && <ErrorNotice error={res.error} what="this agent's crew record" onRetry={res.refresh} />}
      {res.data && res.data.length === 0 && (
        <div className="rr-card rounded-[3px] p-4 text-sm text-ink-2 sm:p-5">
          {meta.name} has not joined a crew{project ? ` on ${project}` : ''} yet. It joins automatically when it opens a crew repo.
        </div>
      )}
      {res.data?.map(({ crew, page }) => (
        <CrewSection key={crew.crew.id} crew={crew} page={page} session={session} now={now} />
      ))}
    </div>
  );
}

/** `#/agents`: the agent page when `agent` is set, otherwise the Agents overview (`fallback`). */
export function AgentRoute({ fallback }: { fallback: ReactNode }) {
  const route = useRoute();
  const agent = route.tab === 'agents' ? route.params.get('agent')?.trim() : null;
  if (!agent) return <>{fallback}</>;
  return (
    <AgentPage
      key={agent}
      agent={agent}
      session={route.params.get('session')?.trim() || null}
      project={route.params.get('project')?.trim() || null}
    />
  );
}
