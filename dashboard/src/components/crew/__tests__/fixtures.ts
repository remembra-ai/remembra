// A small crew for the WP-13d model tests, built through the real reducer
// (fromSnapshot), so the state has exactly the shape the screens get.

import { fromSnapshot } from '../../../lib/crew/reducer';
import type { ClaimView, CrewSnapshot, CrewState, DecisionView, SessionView, TaskView, ZoneView } from '../../../lib/crew/types';

export function session(id: string, callsign: string, extra: Partial<SessionView> = {}): SessionView {
  return {
    id,
    callsign,
    agent_id: 'claude-code',
    member_key: 'claude-code:mbp:a1b2c3d4',
    agent_verified: true,
    adapter: 'claude-code',
    adapter_enforcement: 'enforced',
    client_kind: 'hook',
    state: 'active',
    stuck: false,
    joined_at: '2026-09-26T10:00:00Z',
    ...extra,
  };
}

export function zone(id: string, slug: string, extra: Partial<ZoneView> = {}): ZoneView {
  return {
    id,
    slug,
    title: slug.toUpperCase(),
    is_leaf: true,
    builtin: false,
    include_globs: [`src/app/${slug}/**`],
    exclude_globs: [],
    services: [],
    command_patterns: [],
    mcp_tools: [],
    mode: 'exclusive',
    auto_claim: true,
    protected: false,
    fail_closed: false,
    source: 'repo',
    version: 1,
    ...extra,
  };
}

export function task(id: string, number: number, title: string, extra: Partial<TaskView> = {}): TaskView {
  return {
    id,
    number,
    title,
    status: 'in_progress',
    priority: 2,
    zone_ids: [],
    depends_on: [],
    acceptance: [],
    acceptance_locked: false,
    version: 1,
    ...extra,
  };
}

export function claim(id: string, zoneId: string, holder: string, extra: Partial<ClaimView> = {}): ClaimView {
  return {
    id,
    zone_id: zoneId,
    mode: 'exclusive',
    holder_kind: 'session',
    holder_session_id: holder,
    state: 'active',
    source: 'task',
    epoch: 1,
    unconfirmed: false,
    fenced: false,
    version: 1,
    ...extra,
  };
}

export function decision(id: string, number: number, state: DecisionView['state'], extra: Partial<DecisionView> = {}): DecisionView {
  return {
    id,
    number,
    title: `Decision ${number}`,
    decision: `Decision ${number} body`,
    state,
    source: 'direct',
    decided_by_kind: 'agent',
    decided_by: 'cs_b',
    ...extra,
  };
}

/** yaadbooks: cc-1 (hook, verified) holds pos for T-14; codex-1 (MCP, advisory); cc-9 self-declared; cc-3 ended. */
export function crewState(overrides: Partial<CrewSnapshot> = {}): CrewState {
  const snapshot: CrewSnapshot = {
    crew: { id: 'crw_1', project_id: 'yaadbooks', name: 'yaadbooks', mode: 'multi', enforcement: 'enforce', settings_version: 1, last_seq: 40 },
    server_time: '2026-09-26T12:00:00Z',
    as_of_seq: 40,
    etag: '"40"',
    sessions: [
      session('cs_a', 'cc-1', { current_task_id: 'tsk_14' }),
      session('cs_b', 'codex-1', { agent_id: 'codex', adapter: 'codex', adapter_enforcement: 'advisory', client_kind: 'mcp' }),
      session('cs_c', 'cc-9', { agent_verified: false }),
      session('cs_d', 'cc-3', { state: 'ended' }),
      session('cs_e', 'gemini-1', { agent_id: 'gemini', state: 'quota_blocked', state_reason: 'billing_error' }),
    ],
    claims: [claim('clm_pos', 'zn_pos', 'cs_a', { task_id: 'tsk_14' })],
    zones: [zone('zn_pos', 'pos', { title: 'POS section' }), zone('zn_rep', 'reports'), zone('zn_pol', 'crew-policy', { builtin: true, protected: true })],
    commons: [],
    ignore: [],
    tasks: [task('tsk_14', 14, 'Split tender payments', { owner_session_id: 'cs_a' }), task('tsk_2', 2, 'Reports export', { status: 'ready' }), task('tsk_1', 1, 'Old', { status: 'done' })],
    collisions: [],
    decisions: [decision('dec_7', 7, 'in_force', { decided_by_kind: 'human', decided_by: 'u_mani' }), decision('dec_9', 9, 'proposed')],
    offers: [],
    footprints: [],
    inbox_counts: { project: 2, crew: 1 },
    pending_zone_changes: [],
    ...overrides,
  };
  return fromSnapshot(snapshot);
}
