// A small crew for the WP-13a view-model tests: cc-1 (claude-code) holding
// POS for T-1, codex-1 queued on POS while working Reports for T-2, and cc-2
// out of credits with its Payroll zone reserved for the next pickup of T-3.

import { fromSnapshot } from '../../../../lib/crew/reducer';
import type { ClaimView, CrewEvent, CrewSnapshot, CrewState, SessionView, TaskView, ZoneView } from '../../../../lib/crew/types';

export const NOW = Date.parse('2026-09-26T12:00:00Z');

export function iso(minutesAgo: number): string {
  return new Date(NOW - minutesAgo * 60000).toISOString();
}

export function session(id: string, over: Partial<SessionView> = {}): SessionView {
  return {
    id,
    callsign: id,
    agent_id: 'claude-code',
    member_key: `claude-code:mbp:${id}`,
    agent_verified: true,
    adapter: 'claude-code',
    adapter_enforcement: 'enforced',
    client_kind: 'hook',
    model: null,
    host_id: 'hst_1',
    state: 'active',
    quiet_reason: null,
    state_reason: null,
    stuck: false,
    branch: 'main',
    head_commit: 'abc1234def',
    worktree_id: 'wt-a',
    githook_state: 'ok',
    current_task_id: null,
    limit: null,
    joined_at: iso(30),
    last_activity_at: iso(1),
    ended_at: null,
    end_reason: null,
    ...over,
  };
}

export function zone(id: string, slug: string, title: string): ZoneView {
  return {
    id,
    slug,
    title,
    parent_id: null,
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
    reserve_for: null,
    fail_closed: false,
    frozen_by: null,
    frozen_note: null,
    frozen_until: null,
    source: 'repo',
    version: 1,
  };
}

export function claim(id: string, over: Partial<ClaimView>): ClaimView {
  return {
    id,
    zone_id: null,
    path_glob: null,
    resource: null,
    mode: 'exclusive',
    holder_kind: 'session',
    holder_session_id: null,
    holder_agent_id: 'claude-code',
    holder_user_id: 'u_1',
    task_id: null,
    state: 'active',
    source: 'task',
    epoch: 1,
    unconfirmed: false,
    fenced: false,
    lease_expires_at: iso(-9),
    reserve_reason: null,
    reserved_for: null,
    offered_to: null,
    queue_pos: null,
    baton_ref: null,
    granted_at: iso(20),
    version: 1,
    ...over,
  };
}

export function task(id: string, number: number, title: string, over: Partial<TaskView> = {}): TaskView {
  return {
    id,
    number,
    title,
    status: 'in_progress',
    status_before_stall: null,
    phase: null,
    priority: 2,
    zone_ids: [],
    owner_session_id: null,
    owner_agent_id: null,
    reviewer: null,
    depends_on: [],
    acceptance: [],
    acceptance_locked: false,
    started_head: null,
    current_report_id: null,
    blocked_reason: null,
    version: 1,
    ...over,
  };
}

export function snapshot(): CrewSnapshot {
  return {
    crew: {
      id: 'crw_1',
      project_id: 'yaadbooks',
      name: 'yaadbooks',
      mode: 'multi',
      enforcement: 'enforce',
      settings_version: 1,
      last_seq: 40,
    },
    server_time: iso(0),
    as_of_seq: 40,
    etag: '"40"',
    sessions: [
      session('cs_a', { callsign: 'cc-1', current_task_id: 'tsk_1', model: 'opus' }),
      session('cs_b', {
        callsign: 'codex-1',
        agent_id: 'codex',
        adapter: 'codex',
        adapter_enforcement: 'advisory',
        agent_verified: false,
        current_task_id: 'tsk_2',
        githook_state: 'missing',
      }),
      session('cs_c', {
        callsign: 'cc-2',
        state: 'quota_blocked',
        state_reason: 'billing_error',
        current_task_id: 'tsk_3',
        last_activity_at: iso(12),
      }),
    ],
    claims: [
      claim('clm_pos', { zone_id: 'zn_pos', holder_session_id: 'cs_a', task_id: 'tsk_1' }),
      claim('clm_rep', { zone_id: 'zn_rep', holder_session_id: 'cs_b', task_id: 'tsk_2', mode: 'shared', source: 'adopt' }),
      claim('clm_q', { zone_id: 'zn_pos', holder_session_id: 'cs_b', state: 'queued', queue_pos: 1 }),
      claim('clm_pay', {
        zone_id: 'zn_pay',
        holder_session_id: 'cs_c',
        task_id: 'tsk_3',
        state: 'reserved',
        reserve_reason: 'quota',
        baton_ref: 'refs/remembra/baton/T-3/1',
      }),
    ],
    zones: [zone('zn_pos', 'pos', 'POS section'), zone('zn_rep', 'reports', 'Reports'), zone('zn_pay', 'payroll', 'Payroll')],
    commons: [],
    ignore: [],
    tasks: [
      task('tsk_1', 1, 'POS split tender', { phase: 'Phase 2', zone_ids: ['zn_pos'], owner_session_id: 'cs_a' }),
      task('tsk_2', 2, 'Reports export', { phase: 'Phase 3', zone_ids: ['zn_rep'], owner_session_id: 'cs_b' }),
      task('tsk_3', 3, 'Payroll ledger', { phase: 'Phase 2', zone_ids: ['zn_pay'], status: 'stalled', owner_session_id: 'cs_c' }),
    ],
    collisions: [],
    decisions: [],
    offers: [{ id: 'off_1', claim_id: 'clm_pay', task_id: 'tsk_3', to_session: 'cs_a', via: 'brief' }],
    footprints: [],
    inbox_counts: { project: 1, crew: 1 },
    pending_zone_changes: [],
  };
}

export function state(): CrewState {
  return fromSnapshot(snapshot());
}

let seq = 0;
export function event(type: string, minutesAgo: number, over: Partial<CrewEvent> = {}): CrewEvent {
  seq += 1;
  return {
    seq: over.seq ?? seq,
    id: `evt_${seq}`,
    crew_id: 'crw_1',
    project_id: 'yaadbooks',
    ts: iso(minutesAgo),
    type,
    v: 1,
    origin: 'server',
    actor: { kind: 'session', id: 'cs_a', callsign: 'cc-1', verified: true },
    refs: {},
    severity: 'info',
    moment: false,
    summary: type,
    payload: {},
    ...over,
  };
}
