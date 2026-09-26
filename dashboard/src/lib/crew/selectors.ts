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

/** `cc-1 · claude-code (key-verified)` */
export function sessionLabel(session: Pick<SessionState, 'callsign' | 'agent_id' | 'agent_verified'>): string {
  return `${session.callsign} · ${session.agent_id} (${session.agent_verified ? 'key-verified' : 'self-declared'})`;
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
