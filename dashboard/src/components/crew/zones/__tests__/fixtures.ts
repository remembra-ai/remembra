// A small but complete crew for the Zone Map and Policy tests: three sessions
// (one advisory, one with a missing git gate), zones in every state, a pending
// loosening change, and a folder tree snapshot.

import { fromSnapshot } from '../../../../lib/crew/reducer';
import type { ClaimView, CrewSnapshot, CrewState, SessionView, ZoneView } from '../../../../lib/crew/types';
import type { RepoTreeNode, ZoneChange } from '../zoneModel';

export const NOW = Date.parse('2026-09-26T14:10:00Z');

function zone(id: string, slug: string, globs: string[], extra: Partial<ZoneView> = {}): ZoneView {
  return {
    id,
    slug,
    title: slug.toUpperCase(),
    parent_id: null,
    is_leaf: true,
    builtin: false,
    include_globs: globs,
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
    ...extra,
  };
}

function session(id: string, callsign: string, extra: Partial<SessionView> = {}): SessionView {
  return {
    id,
    callsign,
    agent_id: 'claude-code',
    member_key: 'claude-code:mbp:a1b2c3d4',
    agent_verified: true,
    adapter: 'claude-code',
    adapter_enforcement: 'enforced',
    client_kind: 'hook',
    host_id: 'hst_1',
    state: 'active',
    stuck: false,
    branch: 'main',
    worktree_id: `wt-${callsign}`,
    githook_state: 'ok',
    current_task_id: null,
    joined_at: '2026-09-26T13:00:00Z',
    ...extra,
  };
}

function claim(id: string, zoneId: string, holder: string | null, extra: Partial<ClaimView> = {}): ClaimView {
  return {
    id,
    zone_id: zoneId,
    mode: 'exclusive',
    holder_kind: 'session',
    holder_session_id: holder,
    holder_agent_id: 'claude-code',
    task_id: null,
    state: 'active',
    source: 'task',
    epoch: 1,
    unconfirmed: false,
    fenced: false,
    lease_expires_at: '2026-09-26T14:18:00Z',
    granted_at: '2026-09-26T14:02:00Z',
    version: 1,
    ...extra,
  };
}

export const SNAPSHOT: CrewSnapshot = {
  crew: { id: 'crw_0000000000000a01', project_id: 'yaadbooks', name: 'yaadbooks', mode: 'multi', enforcement: 'enforce', settings_version: 3, last_seq: 40 },
  server_time: '2026-09-26T14:10:00Z',
  as_of_seq: 40,
  etag: '"40"',
  sessions: [
    session('cs_a', 'cc-1', { current_task_id: 'tsk_14' }),
    session('cs_b', 'codex-1', { agent_id: 'codex', adapter: 'codex', adapter_enforcement: 'advisory', githook_state: 'missing', worktree_id: 'wt-b' }),
    session('cs_c', 'cc-2', { worktree_id: 'wt-b', githook_state: 'chained' }),
  ],
  claims: [
    claim('clm_pos', 'zn_pos', 'cs_a', { task_id: 'tsk_14' }),
    claim('clm_q', 'zn_pos', 'cs_c', { state: 'queued', queue_pos: 1, granted_at: null }),
    claim('clm_inv', 'zn_invoices', 'cs_b', { state: 'reserved', reserve_reason: 'quota', task_id: 'tsk_12', baton_ref: 'refs/remembra/baton/T-12/7' }),
    claim('clm_rep', 'zn_reports', 'cs_c', { mode: 'shared' }),
  ],
  zones: [
    zone('zn_app', 'app', ['src/app/**'], { is_leaf: false }),
    zone('zn_pos', 'pos', ['src/app/pos/**'], { parent_id: 'zn_app' }),
    zone('zn_invoices', 'invoices', ['src/app/invoices/**'], { parent_id: 'zn_app' }),
    zone('zn_reports', 'reports', ['src/app/reports/**'], { mode: 'shared', parent_id: 'zn_app' }),
    zone('zn_billing', 'billing', ['src/billing/**'], { frozen_by: 'u_mani', frozen_note: 'Mani is editing billing himself' }),
    zone('zn_payroll', 'payroll', ['src/{payroll,hr}/**'], { protected: true }),
    zone('zn_policy', 'crew-policy', ['.remembra/**', '.git/hooks/**', '~/.remembra/**'], { builtin: true, protected: true, source: 'builtin', auto_claim: false }),
  ],
  commons: [],
  ignore: [],
  tasks: [
    { id: 'tsk_14', number: 14, title: 'Split tender payments', status: 'in_progress', priority: 2, zone_ids: ['zn_pos'], depends_on: [], acceptance: [], acceptance_locked: true, version: 1 },
    { id: 'tsk_12', number: 12, title: 'Invoice PDF', status: 'stalled', priority: 2, zone_ids: ['zn_invoices'], depends_on: [], acceptance: [], acceptance_locked: true, version: 1 },
  ],
  collisions: [
    { id: 'col_1', kind: 'exclusive_breach', severity: 'high', subject: 'src/billing/rates.ts', zone_id: 'zn_billing', session_a: 'cs_a', session_b: 'cs_c', attribution: 'probable', state: 'open', escalated: true },
  ],
  decisions: [],
  offers: [],
  footprints: [],
  inbox_counts: { project: 2, crew: 1 },
  pending_zone_changes: ['zch_1'],
};

export function crewState(): CrewState {
  return fromSnapshot(SNAPSHOT);
}

export const TREE: RepoTreeNode = {
  name: '.',
  files: 4,
  children: [
    {
      name: 'src',
      files: 1,
      children: [
        { name: 'app', files: 2, children: [{ name: 'pos', files: 12 }, { name: 'invoices', files: 8 }, { name: 'reports', files: 5 }, { name: 'settings', files: 3 }] },
        { name: 'billing', files: 6 },
        { name: 'lib', files: 9 },
      ],
    },
    { name: 'docs', files: 7 },
  ],
};

export const PENDING: ZoneChange = {
  id: 'zch_1',
  yaml_sha: '9f2c1ab7d3e4',
  state: 'pending',
  loosening: true,
  summary: 'removed payroll; changed pos; loosening: remove payroll',
  items: [
    { op: 'remove', target: 'zone:payroll', field: null, loosening: true, reason: 'zone removed' },
    { op: 'change', target: 'zone:pos', field: 'title', loosening: false, reason: null },
  ],
  uploaded_by_session: 'cs_b',
  uploaded_by_user: 'u_mani',
  decided_by: null,
  decided_at: null,
  created_at: '2026-09-26T14:05:00Z',
};
