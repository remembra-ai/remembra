// Route outlet for the crew tabs (#/crews and #/crew?project=…, §9.1).
//
// WP-12 owns the routes and the data layer; the designed screens (Site Board,
// Mission Control with lanes and pickup slots, Zone Map, Task Board, Channel,
// Feed, Policy) are WP-13's and plug in here per screen. Until they land, this
// renders a plain, fully live view of the same data so every crew route works
// end to end: the crew list with live counts, and per crew the sessions with
// presence, who holds each zone, reserved batons, Needs-you and the latest
// moments. Untrusted text (titles, messages) is rendered as plain text only.

import { HardHat, RadioTower } from 'lucide-react';
import { PolicyPage } from '../../pages/crew/Policy';
import { ZonesPage } from '../../pages/crew/Zones';
import { useCrewSocket } from '../../hooks/useCrewSocket';
import { useNow } from '../../hooks/useResource';
import { absoluteTime, relativeTime } from '../time';
import { Card, CardHeader, CopyCommand, ErrorNotice, Pill, PulseDot, StaleNotice, TrailSkeleton } from '../../components/relay/ui';
import { useCrewForProject, useCrewList } from './hooks';
import { crewHref, inboxHref, useCrewRoute } from './routes';
import { describeHolder, liveSessions, presenceText, sessionLabel, sortedZones, taskRef } from './selectors';
import type { ConnectionStatus } from './socket';
import type { CrewStreamStatus } from './store';
import type { CrewListItem, CrewState } from './types';

const INSTALL = 'pipx install remembra && remembra-crew connect --crew';

function NoCrews() {
  return (
    <Card className="p-4 sm:p-5">
      <p className="rr-eyebrow">No crews yet</p>
      <ol className="mt-2 list-decimal space-y-2 pl-5 text-sm text-ink">
        <li>
          Connect this machine (you will see every change before it is written):
          <CopyCommand className="mt-2" command={INSTALL} label="Crew install command" />
        </li>
        <li>Open the repo in any connected agent; it joins automatically.</li>
        <li>Name your zones so agents know what not to touch.</li>
      </ol>
    </Card>
  );
}

function CrewCard({ item, now }: { item: CrewListItem; now: Date }) {
  const titleId = `crew-${item.crew.id}`;
  return (
    <Card as="article" labelledBy={titleId} className="flex flex-col p-4 sm:p-5">
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <h2 id={titleId} className="font-display truncate text-lg font-bold text-ink">
            <a href={crewHref(item.crew.project_id)} className="hover:underline">
              {item.crew.name || item.crew.project_id}
            </a>
          </h2>
          <p className="truncate font-mono text-[11px] text-ink-3">{item.crew.project_id}</p>
        </div>
        <div className="flex shrink-0 flex-wrap items-center justify-end gap-1.5">
          <Pill tone={item.live > 0 ? 'ok' : 'neutral'}>{item.live} live</Pill>
          {item.needs_you > 0 && (
            <a href={inboxHref('needs-you', item.crew.project_id)}>
              <Pill tone="signal">
                {item.needs_you} need{item.needs_you === 1 ? 's' : ''} you
              </Pill>
            </a>
          )}
        </div>
      </div>
      <p className="mt-2 font-mono text-[11px] text-ink-3" title={absoluteTime(item.last_event_at)}>
        {item.last_event_at ? `last move ${relativeTime(item.last_event_at, now)}` : 'no events yet'} · {item.moments_24h} moments today
      </p>
      {item.phases.length > 0 && (
        <ul className="mt-3 space-y-1 font-mono text-[12px] text-ink-2">
          {item.phases.map((p) => (
            <li key={p.phase ?? ''} className="flex justify-between gap-2">
              <span className="truncate">{p.phase ?? 'Tasks'}</span>
              <span className="tabular shrink-0">
                {p.done}/{p.total}
              </span>
            </li>
          ))}
        </ul>
      )}
      {item.live_sessions.length > 0 && (
        <p className="mt-3 flex flex-wrap gap-1.5">
          {item.live_sessions.map((s) => (
            <Pill key={s.id} title={sessionLabel(s)}>
              {s.callsign} · {presenceText(s)}
            </Pill>
          ))}
        </p>
      )}
    </Card>
  );
}

function CrewList() {
  const list = useCrewList();
  const now = useNow();
  if (list.status === 'loading') return <TrailSkeleton rows={3} />;
  if (list.status === 'error' && list.items.length === 0) {
    return <ErrorNotice error={list.error} what="your crews" onRetry={list.refresh} />;
  }
  if (list.items.length === 0) return <NoCrews />;
  return (
    <div className="space-y-3">
      {list.error && <StaleNotice error={list.error} what="your crews" />}
      <div className="grid gap-3 lg:grid-cols-2">
        {list.items.map((item) => (
          <CrewCard key={item.crew.id} item={item} now={now} />
        ))}
      </div>
    </div>
  );
}

function connectionText(status: CrewStreamStatus, connection: ConnectionStatus): { text: string; live: boolean } {
  if (status === 'live') return { text: 'live', live: true };
  if (status === 'resyncing') return { text: 'catching up', live: false };
  if (connection === 'unauthorized') return { text: 'signed out: reload to sign in', live: false };
  if (connection === 'forbidden') return { text: 'access changed: retrying', live: false };
  if (status === 'polling') return { text: 'updating every few seconds', live: false };
  return { text: status, live: false };
}

function Sessions({ state, now }: { state: CrewState; now: Date }) {
  const sessions = liveSessions(state);
  if (!sessions.length) return <p className="px-4 pb-4 text-sm text-ink-3 sm:px-5">No agents running. Start one in this repo; it joins automatically.</p>;
  return (
    <ul className="divide-y divide-rule">
      {sessions.map((s) => {
        const action = s.presence?.last_action;
        const holding = Object.values(state.claims).filter((c) => c.holder_session_id === s.id && c.state === 'active');
        return (
          <li key={s.id} className="px-4 py-3 sm:px-5">
            <div className="flex flex-wrap items-center gap-2">
              <PulseDot active={s.state === 'active'} label={presenceText(s)} />
              <span className="font-mono text-sm font-semibold text-ink">{s.callsign}</span>
              <span className="text-xs text-ink-3">{sessionLabel(s).split(' · ')[1]}</span>
              <Pill>{presenceText(s)}</Pill>
              <Pill tone={s.adapter_enforcement === 'enforced' ? 'ok' : 'neutral'}>{s.adapter_enforcement}</Pill>
              {s.githook_state === 'missing' && <Pill tone="fail">commit gate: missing</Pill>}
            </div>
            <p className="mt-1 font-mono text-[12px] text-ink-2">
              {taskRef(state, s.current_task_id) ?? 'no task'}
              {holding.map((c) => ` · ${state.zones[c.zone_id ?? '']?.slug ?? c.resource ?? c.path_glob ?? c.id}`).join('')}
              {action && ` · ${action.tool}${action.path_rel ? ` ${action.path_rel}` : ''} ${action.age_s}s ago`}
              {!action && s.last_activity_at && ` · active ${relativeTime(s.last_activity_at, now)}`}
            </p>
          </li>
        );
      })}
    </ul>
  );
}

function CrewLive({ crewId, project }: { crewId: string; project: string }) {
  const crew = useCrewSocket(crewId);
  const now = useNow(5000);
  if (crew.status === 'not_found') return <ErrorNotice error={crew.error} what={`the ${project} crew`} />;
  if (!crew.state) {
    return crew.error ? <ErrorNotice error={crew.error} what={`the ${project} crew`} onRetry={crew.refresh} /> : <TrailSkeleton rows={3} />;
  }
  const state = crew.state;
  const conn = connectionText(crew.status, crew.connection);
  const reserved = Object.values(state.claims).filter((c) => c.state === 'reserved');
  const decisions = Object.values(state.decisions);
  return (
    <div className="space-y-3">
      <Card className="px-4 py-3 sm:px-5">
        <div className="flex flex-wrap items-center gap-2">
          <h2 className="font-display text-lg font-bold text-ink">{state.crew?.name || project}</h2>
          <Pill>{state.mode}</Pill>
          <Pill>{state.crew?.enforcement ?? 'enforce'}</Pill>
          <span className="ml-auto inline-flex items-center gap-1.5 font-mono text-[11px] text-ink-2" role="status">
            <RadioTower className="h-3.5 w-3.5" aria-hidden="true" />
            <PulseDot active={conn.live} label={conn.text} />
            {conn.text} · seq {state.last_seq}
          </span>
        </div>
        {state.inbox_counts.project > 0 && (
          <p className="mt-2 text-sm">
            <a className="font-semibold text-signal-ink hover:underline" href={inboxHref('needs-you', project)}>
              {state.inbox_counts.project} need{state.inbox_counts.project === 1 ? 's' : ''} you
            </a>
          </p>
        )}
      </Card>

      <Card labelledBy="crew-sessions">
        <CardHeader id="crew-sessions" eyebrow="The track" title="Who is working" />
        <div className="pt-2">
          <Sessions state={state} now={now} />
        </div>
      </Card>

      {reserved.length > 0 && (
        <Card labelledBy="crew-batons">
          <CardHeader id="crew-batons" eyebrow="Pickup" title="Waiting for the next runner" />
          <ul className="space-y-1 px-4 py-3 font-mono text-[12px] text-ink-2 sm:px-5">
            {reserved.map((c) => (
              <li key={c.id}>
                {state.zones[c.zone_id ?? '']?.slug ?? c.id} · {taskRef(state, c.task_id) ?? 'no task'} · reserved ({c.reserve_reason ?? 'baton'}), held until
                picked up or released
              </li>
            ))}
          </ul>
        </Card>
      )}

      <Card labelledBy="crew-zones">
        <CardHeader id="crew-zones" eyebrow="Zones" title="What must not be touched" />
        <ul className="space-y-1 px-4 py-3 text-sm sm:px-5">
          {sortedZones(state).map((z) => (
            <li key={z.id} className="flex flex-wrap gap-x-2">
              <span className="font-mono font-semibold text-ink">{z.slug}</span>
              <span className="text-ink-3">{z.title}</span>
              <span className="text-ink-2">· {describeHolder(state, z)}</span>
            </li>
          ))}
          {sortedZones(state).length === 0 && <li className="text-ink-3">No zones yet.</li>}
        </ul>
      </Card>

      {decisions.length > 0 && (
        <Card labelledBy="crew-decisions">
          <CardHeader id="crew-decisions" eyebrow="Decisions" title="In force and to confirm" />
          <ul className="space-y-1 px-4 py-3 text-sm sm:px-5">
            {decisions.map((d) => (
              <li key={d.id}>
                <span className="font-mono text-ink-3">D-{d.number}</span> <span className="text-ink">{d.title}</span>{' '}
                <Pill tone={d.state === 'in_force' ? 'ok' : 'open'}>{d.state === 'in_force' ? 'in force' : 'to confirm'}</Pill>
              </li>
            ))}
          </ul>
        </Card>
      )}

      <Card labelledBy="crew-moments">
        <CardHeader id="crew-moments" eyebrow="Moments" title="What just happened" />
        <ul className="space-y-1 px-4 py-3 font-mono text-[12px] text-ink-2 sm:px-5">
          {[...state.moments].reverse().slice(0, 12).map((m) => (
            <li key={m.seq} title={absoluteTime(m.ts)}>
              {m.ts ? relativeTime(m.ts, now) : ''} · {m.summary}
            </li>
          ))}
          {state.moments.length === 0 && <li className="text-ink-3">Nothing yet since this page opened.</li>}
        </ul>
      </Card>
    </div>
  );
}

function CrewScreen() {
  const route = useCrewRoute();
  const project = route?.project ?? null;
  const lookup = useCrewForProject(project);
  if (!project) {
    return (
      <p className="text-sm text-ink-2">
        Pick a crew on the <a className="font-semibold text-signal-ink hover:underline" href="#/crews">Crews</a> page.
      </p>
    );
  }
  if (lookup.status === 'loading') return <TrailSkeleton rows={3} />;
  if (lookup.status === 'error') return <ErrorNotice error={lookup.error} what="your crews" />;
  if (lookup.status === 'none') {
    return (
      <Card className="p-4 sm:p-5">
        <p className="rr-eyebrow">No crew for {project}</p>
        <p className="mt-2 text-sm text-ink-2">A crew starts when the first connected agent joins this project.</p>
        <CopyCommand className="mt-3" command={INSTALL} label="Crew install command" />
      </Card>
    );
  }
  // WP-13b screens (Zone Map, Policy); the other views keep the live overview until their WPs land.
  if (route?.screen === 'zones') return <ZonesPage crewId={lookup.crewId} project={project} zoneSlug={route.zone} />;
  if (route?.screen === 'policy') return <PolicyPage crewId={lookup.crewId} project={project} />;
  return <CrewLive crewId={lookup.crewId} project={project} />;
}

export function CrewRoutes({ tab }: { tab: 'crews' | 'crew' }) {
  return (
    <section aria-label={tab === 'crews' ? 'Crews' : 'Crew'} className="space-y-3">
      <h1 className="sr-only">
        <HardHat aria-hidden="true" /> {tab === 'crews' ? 'Crews' : 'Crew'}
      </h1>
      {tab === 'crews' ? <CrewList /> : <CrewScreen />}
    </section>
  );
}
