// One running agent on The Track (spec §9.3 "CrewLane anatomy"): identity
// with verified/self-declared and enforcement layers, presence pulse, the now
// line (task, zone chips, last action), the last hour as an activity strip,
// report ring and streak, limit meter and the lane menu.

import { useState, type KeyboardEvent } from 'react';
import clsx from 'clsx';
import { useCopy } from '../../../hooks/useCopy';
import { agentMeta } from '../../../lib/agents';
import { crewHref } from '../../../lib/crew/routes';
import { parseServerTime } from '../../../lib/time';
import { presenceText } from '../../../lib/crew/selectors';
import type { CrewState, SessionState } from '../../../lib/crew/types';
import { ActivityStrip } from './ActivityStrip';
import type { Strip } from './activity';
import type { ActionRequest } from './ActionDialog';
import type { LaneActionId } from './actions';
import { EnforcementBadge, LimitMeter, PresencePulse, ReportRing, ZoneChip } from './LaneParts';
import { LaneMenu, type LaneMenuItem } from './LaneMenu';
import {
  agentPageHref,
  checkpointStreak,
  enforcementView,
  limitMeter,
  nowLine,
  pickupCommand,
  presenceView,
  laneDepth,
  reportRing,
  sessionClaims,
  shortAge,
  subAgentView,
  zoneChips,
} from './model';
import './lane.css';

export function CrewLane({
  state,
  session,
  project,
  strip,
  nowMs,
  canAct,
  quotaSource,
  onRequest,
  presenceAt = null,
}: {
  state: CrewState;
  session: SessionState;
  project: string;
  strip: Strip;
  nowMs: number;
  /** Client time (ms) the last presence frame for this session arrived (`CrewStreamView.presenceAt`). */
  presenceAt?: number | null;
  /** The viewer is a human principal (dashboard login). */
  canAct: boolean;
  /** Source of the last quota stop ("reported" / "detected"), when an event said so. */
  quotaSource: string | null;
  onRequest: (request: ActionRequest) => void;
}) {
  const [menuOpen, setMenuOpen] = useState(false);
  const [copy] = useCopy();
  const meta = agentMeta(session.agent_id);
  const claims = sessionClaims(state, session.id);
  const presence = presenceView(session, claims, nowMs, quotaSource);
  const enforcement = enforcementView(session);
  const chips = zoneChips(state, claims);
  const line = nowLine(state, session, presenceAt ? nowMs - presenceAt : 0);
  const ring = reportRing(session, nowMs, strip.checkpointTimes[0] ?? null, presence.settled);
  const streak = presence.settled ? 0 : checkpointStreak(strip.checkpointTimes, nowMs);
  const meter = limitMeter(session);
  const guardBlocks = state.guard_blocks[session.id] ?? 0;
  const tamper = state.tamper_blocks[session.id] ?? 0;
  const running = session.state === 'active' && !presence.settled;
  const held = claims.filter((c) => c.state === 'active');
  const pickup = pickupCommand(line.task);
  const labelId = `lane-${session.id}`;
  const family = subAgentView(state, session);
  const depth = laneDepth(state, session);

  const targets = Object.values(state.sessions)
    .filter((s) => s.id !== session.id && !['ended', 'lost'].includes(s.state))
    .map((s) => ({ id: s.id, label: s.callsign, detail: `${agentMeta(s.agent_id).name} · ${presenceText(s)}` }));
  const request = (id: LaneActionId): ActionRequest => ({
    input: { action: id, sessionId: session.id, names: { session: session.callsign } },
    subject: session.callsign,
    targets: id === 'hand-over' ? targets : undefined,
    claims:
      id === 'hand-over'
        ? held.map((c) => ({
            id: c.id,
            label: state.zones[c.zone_id ?? '']?.slug ?? c.resource ?? c.id,
            title: state.zones[c.zone_id ?? '']?.title,
          }))
        : undefined,
  });

  const paused = session.state === 'paused';
  const items: LaneMenuItem[] = [
    {
      kind: 'action',
      id: 'checkpoint',
      label: 'Request checkpoint',
      human: true,
      disabled: presence.settled ? 'The agent is not running' : undefined,
    },
    paused
      ? { kind: 'action', id: 'resume', label: 'Resume', human: true }
      : { kind: 'action', id: 'pause', label: 'Pause', human: true, disabled: presence.settled ? 'The agent is not running' : undefined },
    { kind: 'link', id: 'message', label: `Message @${session.callsign}`, href: crewHref(project, 'channel') },
    { kind: 'action', id: 'hand-over', label: 'Hand over zones…', human: true, disabled: held.length ? undefined : 'It holds no zones' },
    {
      kind: 'action',
      id: 'release-all',
      label: 'Release all claims',
      human: true,
      disabled: claims.length ? undefined : 'It holds no claims',
    },
    { kind: 'link', id: 'agent', label: 'Open agent page', href: agentPageHref(session.agent_id, session.id, project) },
  ];
  if (pickup) items.push({ kind: 'copy', id: 'pickup', label: 'Copy pickup command', text: pickup });

  const onKeyDown = (e: KeyboardEvent<HTMLElement>) => {
    if (e.key === '.' && e.target === e.currentTarget) {
      e.preventDefault();
      setMenuOpen(true);
    }
  };

  const accessibleName = [
    session.callsign,
    ...(family.parentLabel ? [family.parentLabel] : []),
    meta.name,
    session.agent_verified ? 'key-verified' : 'self-declared',
    presence.label,
    line.taskRef ?? 'no task',
  ].join(', ');

  return (
    <article
      data-lane={session.id}
      data-parent={session.parent_session_id ?? undefined}
      data-settled={presence.settled ? 'true' : undefined}
      style={depth > 0 ? { marginLeft: `${Math.min(depth, 3) * 1.5}rem` } : undefined}
      aria-label={accessibleName}
      aria-describedby={labelId}
      tabIndex={0}
      onKeyDown={onKeyDown}
      className="crew-lane rr-card relative min-w-0 rounded-[3px] focus-visible:outline-2"
    >
      <span
        aria-hidden="true"
        className="absolute inset-y-0 left-0 w-[4px]"
        style={{ background: presence.settled ? 'var(--rule)' : meta.lane }}
      />
      <div className="flex flex-wrap items-start gap-x-3 gap-y-2 py-3 pl-5 pr-3 sm:pr-4">
        <span
          aria-hidden="true"
          className="inline-flex h-9 w-9 shrink-0 items-center justify-center rounded-[3px] font-mono text-[11px] font-bold text-white"
          style={{ background: presence.settled ? 'var(--ink-3)' : meta.lane }}
        >
          {meta.monogram}
        </span>
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-baseline gap-x-2 gap-y-0.5">
            <h3 className="font-display text-lg font-bold leading-none tracking-tight text-ink">{session.callsign}</h3>
            <span className="font-mono text-[11px] text-ink-2">
              {meta.name}
              {session.model ? ` · ${session.model}` : ''}
            </span>
            <span
              className={clsx(
                'rounded-[2px] border px-1 font-mono text-[10px] leading-4',
                session.agent_verified ? 'border-ok/40 text-ok' : 'border-dashed border-rule text-ink-3',
              )}
              title={session.agent_verified ? 'Joined with an agent-scoped key' : 'Named itself; not verified by its key'}
            >
              {session.agent_verified ? 'key-verified' : 'self-declared'}
            </span>
            {family.parentLabel && (
              <span
                data-testid="lane-parent"
                className="rounded-[2px] border border-dashed border-rule px-1 font-mono text-[10px] leading-4 text-ink-2"
                title="A sub-agent: its own session, and the session that started it answers for its claims and tasks"
              >
                {family.parentLabel}
              </span>
            )}
            {family.subAgents.length > 0 && (
              <span data-testid="lane-sub-agents" className="font-mono text-[10px] leading-4 text-ink-3">
                sub-agents: {family.subAgents.join(', ')}
              </span>
            )}
          </div>
          <div className="mt-1.5 flex flex-wrap items-center justify-between gap-x-4 gap-y-1.5">
            <PresencePulse presence={presence} />
            <EnforcementBadge view={enforcement} />
          </div>
        </div>
        <div className="flex items-start">
          <LaneMenu
            label={session.callsign}
            items={items}
            canAct={canAct}
            open={menuOpen}
            onOpenChange={setMenuOpen}
            onAction={(id) => onRequest(request(id))}
            onCopy={(text) => copy(text, 'Pickup command copied')}
          />
        </div>
      </div>

      <div id={labelId} className="space-y-3 border-t border-dashed border-rule py-3 pl-5 pr-3 sm:pr-4">
        <p className="flex min-w-0 flex-wrap items-center gap-x-2 gap-y-1.5 text-sm">
          {line.taskRef ? (
            <span className="min-w-0">
              <span className="font-mono font-semibold text-ink">{line.taskRef}</span>{' '}
              {line.taskTitle && <span className="text-ink-2">{line.taskTitle}</span>}
            </span>
          ) : (
            <span className="font-mono text-ink-3">no task</span>
          )}
          {chips.map((chip) => (
            <ZoneChip key={chip.claimId} chip={chip} />
          ))}
        </p>
        <p className="min-w-0 truncate font-mono text-[12px] text-ink-2">
          {line.action ? (
            <span className={clsx(line.action.stale && 'opacity-60')} title={line.action.stale ? 'No new activity since' : undefined}>
              <span className={line.action.stale ? 'text-ink-2' : 'text-ink'}>{line.action.tool}</span>
              {line.action.path && <span> {line.action.path}</span>}
              <span className="text-ink-3"> · {line.action.ageS < 5 ? 'just now' : `${shortAge(line.action.ageS)} ago`}</span>
            </span>
          ) : session.last_activity_at ? (
            <span className="text-ink-3">
              last activity {shortAge(Math.max(0, (nowMs - (parseServerTime(session.last_activity_at)?.getTime() ?? nowMs)) / 1000))} ago
            </span>
          ) : (
            <span className="text-ink-3">no activity yet</span>
          )}
        </p>
        <ActivityStrip strip={strip} running={running} label={`${session.callsign} activity`} />
      </div>

      <div className="flex flex-wrap items-center gap-x-5 gap-y-2 border-t border-rule py-2.5 pl-5 pr-3 sm:pr-4">
        <ReportRing ring={ring} streak={streak} />
        <LimitMeter meter={meter} />
        {(guardBlocks > 0 || tamper > 0) && (
          <span className="font-mono text-[11px] text-signal-ink">
            {guardBlocks > 0 && `▲ ${guardBlocks} blocked`}
            {guardBlocks > 0 && tamper > 0 && ' · '}
            {tamper > 0 && `⛔ ${tamper} tamper`}
          </span>
        )}
      </div>
    </article>
  );
}
