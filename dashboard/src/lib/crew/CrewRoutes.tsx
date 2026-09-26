// Route outlet for the crew tabs (#/crews and #/crew?project=…, §9.1).
//
// WP-12 owns the routes and the data layer; the designed screens are WP-13's
// and plug in here per screen: the Site Board (#/crews) and Mission Control
// (the track view) are WP-13a's, Zone Map and Policy WP-13b's, Task Board and
// report receipts WP-13c's, the Channel WP-13d's, the Event Feed, empty states
// and live regions WP-13e's. Any other view renders a plain, fully live view
// of the same data so every crew route works end to end: per crew the
// sessions with presence, who holds each zone, reserved batons, Needs-you and
// the latest moments. Untrusted text (titles, messages) is rendered as plain
// text only.

import { HardHat, RadioTower } from 'lucide-react';
import { useCrewSocket } from '../../hooks/useCrewSocket';
import { useNow } from '../../hooks/useResource';
import { absoluteTime, relativeTime } from '../time';
import { Card, CardHeader, ErrorNotice, Pill, PulseDot, TrailSkeleton } from '../../components/relay/ui';
import { CrewGoKeys, CrewLiveRegions, CrewMomentAnnouncer } from '../../components/crew/a11y/CrewA11y';
import { ChannelScreen } from '../../components/crew/channel/ChannelScreen';
import { NoCrewsEmpty } from '../../components/crew/empty/EmptyStates';
import { EventFeed } from '../../components/crew/feed/EventFeed';
import { Board } from '../../pages/crew/Board';
import { MissionControl } from '../../pages/crew/MissionControl';
import { PolicyPage } from '../../pages/crew/Policy';
import { Receipt } from '../../pages/crew/Receipt';
import { SiteBoard } from '../../pages/crew/SiteBoard';
import { ZonesPage } from '../../pages/crew/Zones';
import { useCrewForProject } from './hooks';
import { inboxHref, useCrewRoute, type CrewRoute } from './routes';
import { describeHolder, liveSessions, presenceText, sessionLabel, sortedZones, taskRef } from './selectors';
import type { ConnectionStatus } from './socket';
import type { CrewStreamStatus } from './store';
import type { CrewState } from './types';

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

// WP-13a: the track is Mission Control; WP-13b: Zone Map and Policy;
// WP-13c: Task Board and report receipts; WP-13d: the Channel; WP-13e: the
// Event Feed. Any other view keeps the live overview.
function CrewView({ crewId, project, route }: { crewId: string; project: string; route: CrewRoute | null }) {
  if (route?.screen === 'track') return <MissionControl crewId={crewId} project={project} />;
  if (route?.screen === 'zones') return <ZonesPage crewId={crewId} project={project} zoneSlug={route.zone} />;
  if (route?.screen === 'policy') return <PolicyPage crewId={crewId} project={project} />;
  if (route?.screen === 'board') return <Board crewId={crewId} project={project} />;
  if (route?.screen === 'report' && route.report) {
    return <Receipt crewId={crewId} project={project} reportId={route.report} taskParam={route.task} />;
  }
  if (route?.screen === 'channel') return <ChannelScreen crewId={crewId} project={project} thread={route.thread} />;
  if (route?.screen === 'feed') return <EventFeed crewId={crewId} project={project} />;
  return <CrewLive crewId={crewId} project={project} />;
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
  if (lookup.status === 'none') return <NoCrewsEmpty project={project} />;
  return (
    <>
      <CrewMomentAnnouncer crewId={lookup.crewId} />
      <CrewView crewId={lookup.crewId} project={project} route={route} />
    </>
  );
}

export function CrewRoutes({ tab }: { tab: 'crews' | 'crew' }) {
  const route = useCrewRoute();
  return (
    <section aria-label={tab === 'crews' ? 'Crews' : 'Crew'} className="space-y-3">
      <h1 className="sr-only">
        <HardHat aria-hidden="true" /> {tab === 'crews' ? 'Crews' : 'Crew'}
      </h1>
      <CrewLiveRegions />
      <CrewGoKeys project={tab === 'crew' ? (route?.project ?? null) : null} />
      {tab === 'crews' ? <SiteBoard /> : <CrewScreen />}
    </section>
  );
}
