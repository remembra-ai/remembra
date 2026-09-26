// `GET /crews/{id}/agents/{agent_id}` (WP-8, spec §9.10) read into typed
// data for the agent page. The route returns a loose object; anything
// missing or malformed becomes an empty list rather than a crash.

import type { CheckpointView, ClaimView, SessionView, TaskView, ZoneView } from '../../lib/crew/types';

export interface BatonRowView {
  id: string;
  task_id: string | null;
  from_session: string | null;
  to_session: string;
  from_callsign: string | null;
  to_callsign: string | null;
  kind: string;
  baton_ref: string | null;
  restored: boolean | null;
  /** The brief the adopter received (agent-authored parts inside the data block). Plain text. */
  brief_text: string | null;
  created_at: string;
}

export interface AgentPageData {
  agentId: string;
  verified: boolean;
  current: SessionView[];
  sessions: SessionView[];
  checkpoints: (CheckpointView & { created_at: string })[];
  batonsIn: BatonRowView[];
  batonsOut: BatonRowView[];
  claims: ClaimView[];
  tasks: TaskView[];
  /** claim id → zone slug, when the crew's zones are known. */
  claimsZoneLabels: [string, string][];
}

function list<T>(value: unknown, keep: (v: Record<string, unknown>) => boolean): T[] {
  if (!Array.isArray(value)) return [];
  return value.filter((v): v is Record<string, unknown> => typeof v === 'object' && v !== null && keep(v)) as T[];
}

const hasId = (v: Record<string, unknown>) => typeof v.id === 'string';

function batons(value: unknown): BatonRowView[] {
  return list<Record<string, unknown>>(value, (v) => hasId(v) && typeof v.to_session === 'string').map((b) => ({
    id: String(b.id),
    task_id: typeof b.task_id === 'string' ? b.task_id : null,
    from_session: typeof b.from_session === 'string' ? b.from_session : null,
    to_session: String(b.to_session),
    from_callsign: typeof b.from_callsign === 'string' ? b.from_callsign : null,
    to_callsign: typeof b.to_callsign === 'string' ? b.to_callsign : null,
    kind: typeof b.kind === 'string' ? b.kind : 'adopt',
    baton_ref: typeof b.baton_ref === 'string' ? b.baton_ref : null,
    restored: typeof b.restored === 'boolean' ? b.restored : null,
    brief_text: typeof b.brief_text === 'string' ? b.brief_text : null,
    created_at: typeof b.created_at === 'string' ? b.created_at : '',
  }));
}

export function agentPageData(raw: Record<string, unknown>, zones: ZoneView[] = []): AgentPageData {
  const claims = list<ClaimView>(raw.claims, hasId);
  const slugs = new Map(zones.map((z) => [z.id, z.slug]));
  return {
    agentId: typeof raw.agent_id === 'string' ? raw.agent_id : '',
    verified: raw.verified === true,
    current: list<SessionView>(raw.current_sessions, hasId),
    sessions: list<SessionView>(raw.sessions, hasId),
    checkpoints: list<CheckpointView & { created_at: string }>(raw.checkpoints, hasId),
    batonsIn: batons(raw.batons_in),
    batonsOut: batons(raw.batons_out),
    claims,
    tasks: list<TaskView>(raw.tasks, hasId),
    claimsZoneLabels: claims
      .filter((c) => c.zone_id && slugs.has(c.zone_id))
      .map((c) => [c.id, slugs.get(c.zone_id!)!] as [string, string]),
  };
}
