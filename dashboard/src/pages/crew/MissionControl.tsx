// Mission Control, "The Track" (spec §9.3, §9.4): one lane per running agent,
// pickup slots where a stopped agent's zones wait for the next runner, the
// baton pass between lanes, and a right rail with what needs you, the latest
// moves and the decisions in force.
//
// Design promise: within 5 seconds, know who is working where, what must not
// be touched, who went quiet, and what needs you.

import { useCallback, useId, useMemo, useRef, useState } from 'react';
import clsx from 'clsx';
import { toast } from 'sonner';
import { useCrewSocket } from '../../hooks/useCrewSocket';
import { useNow, useResource } from '../../hooks/useResource';
import { crewApi } from '../../lib/crew/api';
import { useNeedsYouOpen } from '../../lib/crew/delight';
import { flowErrorMessage } from '../../lib/crew/commands';
import { crewHref, inboxHref, type CrewScreen } from '../../lib/crew/routes';
import { callsignOf, liveSessions } from '../../lib/crew/selectors';
import type { ConnectionStatus } from '../../lib/crew/socket';
import type { CrewStreamStatus } from '../../lib/crew/store';
import type { CrewEvent, CrewState, DecisionView, InboxItemView } from '../../lib/crew/types';
import { absoluteTime, parseServerTime } from '../../lib/time';
import { Card, ErrorNotice, TrailSkeleton } from '../../components/relay/ui';
import { BatonTransit } from '../../components/crew/BatonTransit';
import { CrewAssembled } from '../../components/crew/CrewAssembled';
import { ActionDialog, type ActionRequest } from '../../components/crew/lane/ActionDialog';
import { buildStrip } from '../../components/crew/lane/activity';
import { CrewLane } from '../../components/crew/lane/CrewLane';
import { DitherField } from '../../components/crew/lane/DitherField';
import { laneOrder, shortAge } from '../../components/crew/lane/model';
import { pickupSlots } from '../../components/crew/lane/pickup';
import { PickupSlot } from '../../components/crew/lane/PickupSlot';
import { useCrewActivity } from '../../components/crew/lane/useCrewActivity';
import { liveStatus, quotaSources, trackBranch } from './trackModel';

const VIEWS: { screen: CrewScreen; label: string }[] = [
  { screen: 'track', label: 'Track' },
  { screen: 'zones', label: 'Zones' },
  { screen: 'board', label: 'Board' },
  { screen: 'channel', label: 'Channel' },
  { screen: 'feed', label: 'Feed' },
  { screen: 'policy', label: 'Policy' },
];

function connectionText(status: CrewStreamStatus, connection: ConnectionStatus): { text: string; live: boolean } {
  if (status === 'live') return { text: 'live', live: true };
  if (status === 'resyncing') return { text: 'catching up', live: false };
  if (connection === 'unauthorized') return { text: 'signed out: reload to sign in', live: false };
  if (connection === 'forbidden') return { text: 'access changed: retrying', live: false };
  if (status === 'polling') return { text: 'updating every few seconds', live: false };
  return { text: status, live: false };
}

function TrackHeader({
  state,
  project,
  conn,
  nowMs,
  latest,
}: {
  state: CrewState;
  project: string;
  conn: { text: string; live: boolean };
  nowMs: number;
  latest: CrewEvent | null;
}) {
  const live = liveSessions(state).length;
  const needs = state.inbox_counts.project;
  const branch = trackBranch(state);
  const status = liveStatus(state, latest, nowMs);
  return (
    <header className="rr-card relative overflow-hidden rounded-[3px]">
      <DitherField shape="banks" seed={project.length} />
      <div className="relative px-4 pb-3 pt-4 sm:px-5">
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div className="min-w-0">
            <p className="rr-eyebrow">The track · {state.mode === 'multi' ? 'crew' : 'solo'}</p>
            <h2 className="font-display mt-1.5 truncate text-[clamp(1.6rem,1.1rem+1.6vw,2.4rem)] font-extrabold uppercase leading-none tracking-[-0.03em] text-ink">
              {state.crew?.name || project}
            </h2>
            <p className="mt-1.5 font-mono text-[12px] text-ink-2">
              {branch && <span className="text-ink">{branch}</span>}
              {branch && ' · '}
              <span>{live} live</span>
              {' · '}
              {needs > 0 ? (
                <a
                  href={inboxHref('needs-you', project)}
                  className="font-bold text-signal-ink underline decoration-signal decoration-2 underline-offset-2"
                >
                  ⚑ {needs} need{needs === 1 ? 's' : ''} you
                </a>
              ) : (
                <span>⚑ 0 need you</span>
              )}
              {' · '}
              <span title="Project enforcement level (only a human can lower it)">{state.crew?.enforcement ?? 'enforce'}</span>
            </p>
          </div>
          {/* Not a live region: its seq changes with every event (§9.16: one polite, one assertive region only). */}
          <span
            className="inline-flex items-center gap-2 rounded-[2px] border border-rule bg-panel px-2 py-1 font-mono text-[11px] text-ink-2"
            aria-live="off"
          >
            <span aria-hidden="true" className={clsx('h-2 w-2', conn.live ? 'rr-pulse bg-signal' : 'bg-ink-3')} />
            <span className="sr-only">Connection: </span>
            {conn.text} · seq {state.last_seq}
          </span>
        </div>
        <nav aria-label="Crew views" className="mt-3 flex gap-1 overflow-x-auto scrollbar-hide">
          {VIEWS.map((v) => (
            <a
              key={v.screen}
              href={crewHref(project, v.screen)}
              aria-current={v.screen === 'track' ? 'page' : undefined}
              className={clsx(
                'shrink-0 rounded-[2px] px-2.5 py-1.5 font-mono text-[12px]',
                v.screen === 'track' ? 'bg-ink text-paper' : 'text-ink-2 hover:bg-paper-2 hover:text-ink',
              )}
            >
              {v.label}
            </a>
          ))}
        </nav>
      </div>
      <p
        aria-live="off"
        className="relative flex min-w-0 items-center gap-2.5 border-t border-rule bg-panel/90 px-4 py-2 font-mono text-[12px] text-ink-2 sm:px-5"
      >
        <span
          aria-hidden="true"
          className={clsx('h-2 w-2 shrink-0', status.fresh ? 'bg-signal shadow-[0_0_0_3px_var(--signal-wash)]' : 'bg-ink-3')}
        />
        <span className="min-w-0 truncate">
          {status.lead && <span className="font-bold text-ink">{status.lead} </span>}
          {status.text}
        </span>
        {status.age && <span className="ml-auto shrink-0 text-ink-3">{status.age}</span>}
      </p>
    </header>
  );
}

function NeedsYouRail({ crewId, project, count }: { crewId: string; project: string; count: number }) {
  const res = useResource(`crew-needs-${crewId}-${count}`, () => crewApi.inbox(crewId, 'project', 3));
  const [shown, setShown] = useState<InboxItemView[] | null>(null);
  if (res.data && res.data.items !== shown) setShown(res.data.items);
  const items = res.data?.items ?? shown ?? [];
  const id = useId();
  return (
    <Card labelledBy={id}>
      <div className="flex items-baseline justify-between gap-2 px-4 pt-3.5">
        <h3 id={id} className="rr-eyebrow">
          Needs you
        </h3>
        <a
          href={inboxHref('needs-you', project)}
          className="font-mono text-[11px] text-ink-2 underline decoration-signal underline-offset-2"
        >
          inbox
        </a>
      </div>
      {count === 0 ? (
        <p className="px-4 pb-4 pt-2 text-sm text-ink-3">Nothing needs you. The crew is running.</p>
      ) : (
        <ul className="px-4 pb-3 pt-2">
          {items.slice(0, 3).map((item) => (
            <li key={item.id} className="border-t border-dashed border-rule py-2 first:border-t-0">
              <a href={inboxHref('needs-you', project)} className="group block">
                <span className="block text-sm font-semibold text-ink group-hover:underline">{item.title}</span>
                <span className="font-mono text-[11px] text-ink-3">
                  {item.origin === 'server' ? 'safety · ' : item.origin === 'agent' ? 'from an agent · ' : ''}
                  {item.kind.replace(/_/g, ' ')}
                  {item.coalesced_count > 1 ? ` · ×${item.coalesced_count}` : ''}
                </span>
              </a>
            </li>
          ))}
          {items.length === 0 && <li className="py-2 text-sm text-ink-3">{count} waiting</li>}
        </ul>
      )}
    </Card>
  );
}

function FeedRail({ events, project, nowMs }: { events: CrewEvent[]; project: string; nowMs: number }) {
  const id = useId();
  const recent = events.slice(-10).reverse();
  return (
    <Card labelledBy={id}>
      <div className="flex items-baseline justify-between gap-2 px-4 pt-3.5">
        <h3 id={id} className="rr-eyebrow">
          Latest moves
        </h3>
        <a href={crewHref(project, 'feed')} className="font-mono text-[11px] text-ink-2 underline decoration-signal underline-offset-2">
          feed
        </a>
      </div>
      <ol className="relative px-4 pb-3 pt-2">
        <span aria-hidden="true" className="rr-rail absolute bottom-4 left-[19px] top-3 w-[2px]" />
        {recent.map((e) => {
          const at = parseServerTime(e.ts);
          return (
            <li key={e.seq} className="relative flex gap-2.5 py-1 pl-4" title={absoluteTime(e.ts)}>
              <span
                aria-hidden="true"
                className={clsx('absolute left-[1px] top-[9px] h-[6px] w-[6px]', e.moment ? 'bg-signal' : 'bg-ink-3')}
              />
              <span className={clsx('min-w-0 flex-1 font-mono text-[11px] leading-snug', e.moment ? 'text-ink' : 'text-ink-2')}>
                {e.summary}
              </span>
              <span className="shrink-0 font-mono text-[10px] text-ink-3">{at ? shortAge((nowMs - at.getTime()) / 1000) : ''}</span>
            </li>
          );
        })}
        {recent.length === 0 && <li className="py-1 pl-4 text-sm text-ink-3">Nothing in the last hour.</li>}
      </ol>
    </Card>
  );
}

function DecisionsRail({ decisions, canAct }: { decisions: DecisionView[]; canAct: boolean }) {
  const id = useId();
  const [busy, setBusy] = useState<string | null>(null);
  const inForce = decisions.filter((d) => d.state === 'in_force').sort((a, b) => a.number - b.number);
  const proposed = decisions.filter((d) => d.state === 'proposed').sort((a, b) => a.number - b.number);
  const act = async (d: DecisionView, confirm: boolean) => {
    setBusy(d.id);
    try {
      await (confirm ? crewApi.confirmDecision(d.id) : crewApi.rejectDecision(d.id));
      toast.success(`${confirm ? 'Confirmed' : 'Rejected'} D-${d.number}.`);
    } catch (err) {
      toast.error(flowErrorMessage(err));
    } finally {
      setBusy(null);
    }
  };
  if (!inForce.length && !proposed.length) return null;
  return (
    <Card labelledBy={id}>
      <h3 id={id} className="rr-eyebrow px-4 pt-3.5">
        Decisions
      </h3>
      <ul className="px-4 pb-3 pt-2 text-sm">
        {proposed.map((d) => (
          <li key={d.id} className="border-t border-dashed border-rule py-2 first:border-t-0">
            <p>
              <span className="font-mono text-[11px] text-signal-ink">D-{d.number} · to confirm</span>{' '}
              <span className="text-ink">{d.title}</span>
            </p>
            <p className="font-mono text-[11px] text-ink-3">proposed by an agent · not in any brief until you confirm</p>
            <div className="mt-1.5 flex gap-2">
              <button
                type="button"
                disabled={!canAct || busy === d.id}
                title={canAct ? undefined : 'Needs a dashboard login'}
                onClick={() => void act(d, true)}
                className="rr-btn-primary px-2 py-1 text-xs"
              >
                Confirm
              </button>
              <button
                type="button"
                disabled={!canAct || busy === d.id}
                title={canAct ? undefined : 'Needs a dashboard login'}
                onClick={() => void act(d, false)}
                className="rr-btn-ghost px-2 py-1 text-xs"
              >
                Reject
              </button>
            </div>
          </li>
        ))}
        {inForce.map((d) => (
          <li key={d.id} className="border-t border-dashed border-rule py-2 first:border-t-0">
            <span className="font-mono text-[11px] text-ink-3">D-{d.number} · in force</span> <span className="text-ink">{d.title}</span>
          </li>
        ))}
      </ul>
    </Card>
  );
}

export function MissionControl({ crewId, project }: { crewId: string; project: string }) {
  const crew = useCrewSocket(crewId);
  const now = useNow(5000);
  const nowMs = now.getTime();
  const state = crew.state;
  const activity = useCrewActivity(crewId, state ? state.last_seq : null);
  const access = useResource(`crew-access-${crewId}`, () => crewApi.getCrew(crewId));
  const [request, setRequest] = useState<ActionRequest | null>(null);
  const tracksRef = useRef<HTMLDivElement | null>(null);
  const canAct = access.data?.human === true && ['owner', 'admin'].includes(access.data.role);

  const sessions = useMemo(() => (state ? laneOrder(liveSessions(state)) : []), [state]);
  const slots = useMemo(() => (state ? pickupSlots(state, activity.events) : []), [state, activity.events]);
  const strips = useMemo(() => {
    const out = new Map<string, ReturnType<typeof buildStrip>>();
    for (const s of sessions) out.set(s.id, buildStrip(activity.events, s.id, nowMs));
    return out;
  }, [sessions, activity.events, nowMs]);
  const sources = useMemo(() => quotaSources(activity.events), [activity.events]);
  const callsign = useCallback((sid: string | null | undefined) => (state ? (callsignOf(state, sid) ?? 'an agent') : 'an agent'), [state]);
  // §9.13: no delight moment while this crew has a needs-you item open (the rail shows it)
  useNeedsYouOpen((state?.inbox_counts.project ?? 0) > 0);

  if (crew.status === 'not_found') return <ErrorNotice error={crew.error} what={`the ${project} crew`} />;
  if (!state) {
    return crew.error ? <ErrorNotice error={crew.error} what={`the ${project} crew`} onRetry={crew.refresh} /> : <TrailSkeleton rows={3} />;
  }
  const conn = connectionText(crew.status, crew.connection);
  const latest = activity.events.length ? activity.events[activity.events.length - 1] : null;

  return (
    <div className="space-y-4">
      <TrackHeader state={state} project={project} conn={conn} nowMs={nowMs} latest={latest} />
      {crew.error && <p className="font-mono text-[11px] text-ink-3">Showing the last loaded state: {flowErrorMessage(crew.error)}</p>}

      <div className="grid gap-4 min-[1200px]:grid-cols-[minmax(0,1fr)_340px]">
        <div ref={tracksRef} className="relative min-w-0 space-y-3" aria-label="Lanes" role="region">
          {sessions.map((s) => (
            <CrewLane
              key={s.id}
              state={state}
              session={s}
              project={project}
              strip={strips.get(s.id)!}
              nowMs={nowMs}
              canAct={canAct}
              quotaSource={sources.get(s.id) ?? null}
              onRequest={setRequest}
              presenceAt={crew.presenceAt[s.id] ?? null}
            />
          ))}
          {slots.map((slot) => (
            <PickupSlot key={slot.key} slot={slot} state={state} nowMs={nowMs} canAct={canAct} onRequest={setRequest} />
          ))}
          {sessions.length === 0 && slots.length === 0 && (
            <div className="rr-card rounded-[3px] px-5 py-6">
              <p className="rr-eyebrow">No agents running</p>
              <p className="mt-2 text-sm text-ink-2">
                Open this repo in any connected agent; it joins the crew automatically and gets its own lane.
              </p>
            </div>
          )}
          {sessions.length === 1 && (
            <p className="font-mono text-[11px] text-ink-3">
              One agent. Start another agent here; it gets the brief plus the zones this one holds.
            </p>
          )}
          <BatonTransit batons={state.batons} containerRef={tracksRef} callsignOf={callsign} />
          <CrewAssembled events={activity.events} sinceSeq={state.last_seq} agents={sessions.length} containerRef={tracksRef} />
        </div>

        <aside className="min-w-0 space-y-4" aria-label="Crew at a glance">
          <NeedsYouRail crewId={crewId} project={project} count={state.inbox_counts.project} />
          <DecisionsRail decisions={Object.values(state.decisions)} canAct={canAct} />
          <FeedRail events={activity.events} project={project} nowMs={nowMs} />
        </aside>
      </div>

      {request && <ActionDialog request={request} api={crewApi} onClose={() => setRequest(null)} />}
    </div>
  );
}
