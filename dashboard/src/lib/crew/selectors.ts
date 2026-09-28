// Read helpers over the reducer state, shared by the command palette and the
// crew screens. Pure functions; text is built from ids, slugs and callsigns
// (titles are rendered by the screens as plain text, §9 "untrusted text").

import { LIVE_PRESENCE_STATES, type ClaimView, type CrewState, type SessionState, type ZoneView } from './types';

export function isLive(session: Pick<SessionState, 'state'>): boolean {
  return LIVE_PRESENCE_STATES.includes(session.state);
}

/** Live sessions ordered by callsign (cc-1, cc-2, codex-1 …). */
export function liveSessions(state: CrewState): SessionState[] {
  return Object.values(state.sessions)
    .filter(isLive)
    .sort((a, b) => a.callsign.localeCompare(b.callsign, undefined, { numeric: true }));
}

/** `cc-1 · claude-code (key-verified)`; a sub-agent session ends with ` · sub-agent`. */
export function sessionLabel(
  session: Pick<SessionState, 'callsign' | 'agent_id' | 'agent_verified'> & Partial<Pick<SessionState, 'parent_session_id'>>,
): string {
  const base = `${session.callsign} · ${session.agent_id} (${session.agent_verified ? 'key-verified' : 'self-declared'})`;
  return session.parent_session_id ? `${base} · sub-agent` : base;
}

/**
 * How an agent is stopped before a write, one label everywhere (Track, Zones, Policy): `enforced` (its
 * hooks deny), `read-only fence` (Cursor, or an agent not verified in its own worktree: crewd makes held
 * zones read-only) or `advisory` (MCP-only: it is told, nothing stops it).
 */
export type BeforeWrite = 'enforced' | 'read-only fence' | 'advisory';

export function beforeWriteLabel(session: Pick<SessionState, 'adapter_enforcement'> & Partial<Pick<SessionState, 'client_kind'>>): BeforeWrite {
  if (session.client_kind === 'mcp') return 'advisory';
  return session.adapter_enforcement === 'enforced' ? 'enforced' : 'read-only fence';
}

/**
 * A crew list item's live sessions split by what they are doing: `running` (joining, active, idle,
 * quiet), `stopped` (on its credits: waiting for pickup) and `paused` (by a human). Uses the listed
 * sessions; when the list was truncated, the sessions not listed count as running.
 */
export function liveSplit(item: { live: number; live_sessions: Pick<SessionState, 'state'>[] }): {
  running: number;
  stopped: number;
  paused: number;
} {
  const stopped = item.live_sessions.filter((s) => s.state === 'quota_blocked').length;
  const paused = item.live_sessions.filter((s) => s.state === 'paused').length;
  return { running: Math.max(0, item.live - stopped - paused), stopped, paused };
}

/** `3 running · 1 stopped · 1 paused`, leaving out the zero parts after the first. */
export function liveSplitText(split: { running: number; stopped: number; paused: number }): string {
  const parts = [`${split.running} running`];
  if (split.stopped) parts.push(`${split.stopped} stopped`);
  if (split.paused) parts.push(`${split.paused} paused`);
  return parts.join(' · ');
}

/** Human wording for a presence state (status never depends on colour alone, §9). */
export function presenceText(session: Pick<SessionState, 'state' | 'quiet_reason' | 'state_reason' | 'stuck'>): string {
  let text: string;
  switch (session.state) {
    case 'quiet':
      text = session.quiet_reason === 'host_unreachable' ? 'quiet (host offline)' : session.quiet_reason === 'mcp_silent' ? 'quiet (no MCP calls)' : 'quiet';
      break;
    case 'quota_blocked':
      text = session.state_reason ? `stopped (${session.state_reason})` : 'stopped (credits)';
      break;
    case 'paused':
      text = 'paused';
      break;
    default:
      text = session.state.replace('_', ' ');
  }
  return session.stuck ? `${text} · stuck` : text;
}

/** `T-14` for a task id, or the id itself when the task is not in state. */
export function taskRef(state: CrewState, taskId: string | null | undefined): string | null {
  if (!taskId) return null;
  const task = state.tasks[taskId];
  return task ? `T-${task.number}` : taskId;
}

export function callsignOf(state: CrewState, sessionId: string | null | undefined): string | null {
  if (!sessionId) return null;
  return state.sessions[sessionId]?.callsign ?? sessionId;
}

/** A sub-agent session (joined with `parent_session_id`): it works for its parent (the first 2 sit on its seat). */
export function isSubAgent(session: Pick<SessionState, 'parent_session_id'>): boolean {
  return Boolean(session.parent_session_id);
}

/** `cc-3 (sub-agent of cc-2)` for a sub-agent, the plain callsign otherwise. */
export function callsignWithParent(state: CrewState, session: Pick<SessionState, 'callsign' | 'parent_session_id'>): string {
  if (!session.parent_session_id) return session.callsign;
  return `${session.callsign} (sub-agent of ${callsignOf(state, session.parent_session_id)})`;
}

/**
 * Sessions a baton can be handed to: live, not stopped on credits, and not sub-agents (the server
 * offers batons to top-level sessions only; a sub-agent's parent takes it). `exclude` drops the
 * session the baton comes from.
 */
export function batonTargets(state: CrewState, exclude: ReadonlyArray<string | null | undefined> = []): SessionState[] {
  return liveSessions(state).filter((s) => !exclude.includes(s.id) && s.state !== 'quota_blocked' && !isSubAgent(s));
}

/** Zones by slug, the built-in crew-policy zone last. */
export function sortedZones(state: CrewState): ZoneView[] {
  return Object.values(state.zones).sort((a, b) => Number(a.builtin) - Number(b.builtin) || a.slug.localeCompare(b.slug));
}

export function zoneBySlug(state: CrewState, slug: string): ZoneView | null {
  return Object.values(state.zones).find((z) => z.slug === slug) ?? null;
}

const HOLDING: ClaimView['state'][] = ['active', 'offered', 'reserved'];

/** Live claims on a zone: holders first (active, offered, reserved), then the queue by position. */
export function zoneClaims(state: CrewState, zoneId: string): ClaimView[] {
  const claims = Object.values(state.claims).filter((c) => c.zone_id === zoneId);
  return claims.sort((a, b) => {
    const ha = HOLDING.includes(a.state) ? 0 : 1;
    const hb = HOLDING.includes(b.state) ? 0 : 1;
    return ha - hb || (a.queue_pos ?? 0) - (b.queue_pos ?? 0) || a.id.localeCompare(b.id);
  });
}

function holderName(state: CrewState, claim: ClaimView): string {
  if (claim.holder_kind === 'human') return 'a human';
  return callsignOf(state, claim.holder_session_id) ?? claim.holder_agent_id ?? 'a session';
}

/**
 * One line answering "who holds this zone": e.g.
 * `held EXCLUSIVELY by codex-1 for T-14 · active`, `RESERVED for the next pickup of T-12 (quota)`,
 * `frozen by a human`, `free`.
 */
export function describeHolder(state: CrewState, zone: ZoneView): string {
  const parts: string[] = [];
  const holders = zoneClaims(state, zone.id).filter((c) => HOLDING.includes(c.state));
  if (zone.frozen_by) parts.push('frozen by a human');
  for (const claim of holders) {
    const task = taskRef(state, claim.task_id);
    if (claim.state === 'reserved') {
      const reason = claim.reserve_reason ? ` (${claim.reserve_reason})` : '';
      parts.push(`RESERVED for the next pickup${task ? ` of ${task}` : ''}${reason}`);
      continue;
    }
    if (claim.holder_kind === 'human' && zone.frozen_by) continue;
    const mode = claim.mode === 'exclusive' ? 'EXCLUSIVELY' : claim.mode;
    const holder = holderName(state, claim);
    const session = claim.holder_session_id ? state.sessions[claim.holder_session_id] : undefined;
    const presence = session ? ` · ${presenceText(session)}` : '';
    const offered = claim.state === 'offered' ? ' (handover offered)' : '';
    parts.push(`held ${mode} by ${holder}${task ? ` for ${task}` : ''}${presence}${offered}`);
  }
  const queued = zoneClaims(state, zone.id).filter((c) => c.state === 'queued' || c.state === 'requested').length;
  if (queued) parts.push(`${queued} waiting`);
  if (!parts.length) return zone.protected ? 'free (protected: needs a grant)' : 'free';
  return parts.join(' · ');
}

/** Needs-you items (project audience) for this crew. */
export function needsYouCount(state: CrewState): number {
  return state.inbox_counts.project;
}
