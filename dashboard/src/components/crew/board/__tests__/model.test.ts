import { describe, expect, it } from 'vitest';
import { applyEvent, emptyState } from '../../../../lib/crew/reducer';
import type { ClaimView, CrewState, SessionState, TaskView, ZoneView } from '../../../../lib/crew/types';
import {
  acceptanceMeter,
  ageText,
  buildLanes,
  checkpointDots,
  columnCounts,
  columnOf,
  commandPatternProblems,
  dependencyEdges,
  dropIntent,
  dropTargets,
  emptyDraft,
  fromDraft,
  isPathRel,
  mergeCheckpoints,
  mergeTasks,
  nextCriterionId,
  ownerOf,
  receiptHref,
  resolveTaskParam,
  restIsStale,
  savedWork,
  sealItems,
  sealLine,
  sealText,
  statusMoves,
  statusSince,
  taskForReport,
  toDraft,
  validateCriteria,
  waiverReasonError,
  zoneChips,
  type ReportDetail,
  type TaskDetail,
} from '../model';

function task(n: number, patch: Partial<TaskDetail> = {}): TaskDetail {
  return {
    id: `tsk_${n}`,
    number: n,
    title: `Task ${n}`,
    status: 'ready',
    priority: 2,
    zone_ids: [],
    depends_on: [],
    acceptance: [],
    acceptance_locked: false,
    version: 1,
    ...patch,
  };
}

function session(id: string, callsign: string, patch: Partial<SessionState> = {}): SessionState {
  return {
    id,
    callsign,
    agent_id: 'claude-code',
    member_key: `claude-code:mbp:${id}`,
    agent_verified: true,
    adapter_enforcement: 'enforced',
    state: 'active',
    stuck: false,
    joined_at: '2026-09-26T10:00:00.000Z',
    presence: null,
    ...patch,
  };
}

function zone(id: string, slug: string, patch: Partial<ZoneView> = {}): ZoneView {
  return {
    id,
    slug,
    title: slug.toUpperCase(),
    is_leaf: true,
    builtin: false,
    include_globs: [`src/${slug}/**`],
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
    ...patch,
  };
}

function claim(id: string, patch: Partial<ClaimView>): ClaimView {
  return {
    id,
    mode: 'exclusive',
    holder_kind: 'session',
    state: 'active',
    source: 'task',
    epoch: 1,
    unconfirmed: false,
    fenced: false,
    version: 1,
    ...patch,
  };
}

function crewState(): CrewState {
  const s = emptyState();
  s.sessions = {
    cs_a: session('cs_a', 'cc-1'),
    cs_b: session('cs_b', 'codex-1', { agent_id: 'codex', agent_verified: false, adapter_enforcement: 'advisory' }),
    cs_c: session('cs_c', 'cc-2', { state: 'quota_blocked', state_reason: 'billing_error' }),
  };
  s.zones = { zn_pos: zone('zn_pos', 'pos'), zn_rep: zone('zn_rep', 'reports', { mode: 'shared' }), zn_bil: zone('zn_bil', 'billing', { frozen_by: 'u_mani' }) };
  s.claims = {
    clm_1: claim('clm_1', { zone_id: 'zn_pos', holder_session_id: 'cs_a', task_id: 'tsk_14' }),
    clm_2: claim('clm_2', { zone_id: 'zn_rep', holder_session_id: 'cs_c', task_id: 'tsk_12', state: 'reserved', reserve_reason: 'quota', baton_ref: 'refs/remembra/baton/T-12/7' }),
    clm_3: claim('clm_3', { zone_id: 'zn_bil', holder_session_id: 'cs_b', task_id: 'tsk_15', source: 'adopt', mode: 'watch' }),
  };
  return s;
}

describe('columns and lanes', () => {
  it('map every status to its column; cancelled only on request', () => {
    expect(columnOf('backlog')).toBe('next');
    expect(columnOf('ready')).toBe('next');
    expect(columnOf('claimed')).toBe('next');
    expect(columnOf('in_progress')).toBe('progress');
    expect(columnOf('blocked')).toBe('blocked');
    expect(columnOf('review')).toBe('review');
    expect(columnOf('done')).toBe('done');
    expect(columnOf('stalled')).toBe('stalled');
    expect(columnOf('cancelled')).toBeNull();
    expect(columnOf('cancelled', true)).toBe('done');
  });

  it('group by phase with the catch-all lane last, sorted inside each cell', () => {
    const tasks = [
      task(3, { phase: 'Phase 2', status: 'in_progress' }),
      task(1, { phase: 'Phase 1', status: 'done', done_at: '2026-09-25T10:00:00Z' }),
      task(2, { phase: 'Phase 1', status: 'done', done_at: '2026-09-26T10:00:00Z' }),
      task(4, { status: 'ready' }),
      task(5, { phase: 'Phase 2', status: 'ready', priority: 0 }),
      task(6, { phase: 'Phase 2', status: 'claimed', priority: 3 }),
      task(7, { phase: 'Phase 2', status: 'cancelled' }),
    ];
    const lanes = buildLanes(tasks, 'phase', null);
    expect(lanes.map((l) => l.title)).toEqual(['Phase 1', 'Phase 2', 'No phase']);
    expect(lanes[0].cells.done.map((t) => t.number)).toEqual([2, 1]); // newest done first
    expect(lanes[1].cells.next.map((t) => t.number)).toEqual([6, 5]); // claimed before ready, then priority
    expect(lanes[1].count).toBe(3); // cancelled hidden
    expect(buildLanes(tasks, 'phase', null, { showCancelled: true })[1].cells.done.map((t) => t.number)).toEqual([7]);
    expect(columnCounts(tasks)).toEqual({ next: 3, progress: 1, blocked: 0, review: 0, done: 2, stalled: 0 });
  });

  it('group by agent (callsign, verification noted) and by first zone', () => {
    const state = crewState();
    const tasks = [
      task(14, { owner_session_id: 'cs_a', owner_agent_id: 'claude-code', zone_ids: ['zn_pos'], status: 'in_progress' }),
      task(12, { owner_session_id: 'cs_b', owner_agent_id: 'codex', zone_ids: ['zn_rep', 'zn_pos'] }),
      task(9),
    ];
    const byAgent = buildLanes(tasks, 'agent', state);
    expect(byAgent.map((l) => [l.title, l.note])).toEqual([
      ['codex-1', 'self-declared'],
      ['cc-1', 'key-verified'],
      ['Unassigned', null],
    ]);
    const byZone = buildLanes(tasks, 'zone', state);
    expect(byZone.map((l) => l.title)).toEqual(['reports', 'pos', 'No zone']);
    expect(buildLanes(tasks, 'none', state).map((l) => l.title)).toEqual(['All tasks']);
  });
});

describe('REST and live merge', () => {
  it('prefer the newer live version, keep REST-only fields and unchanged waivers', () => {
    const rest = [
      task(1, {
        version: 3,
        started_at: '2026-09-26T09:00:00Z',
        acceptance: [
          { id: 'c1', text: 'tests pass', kind: 'test', match: 'npm test', required: true, waived: { reason: 'flaky runner' } },
          { id: 'c2', text: 'live', kind: 'deploy', url: 'https://x.dev', required: true, waived: { reason: 'later' } },
        ],
      }),
      task(2, { version: 5, status: 'done', done_at: '2026-09-26T08:00:00Z' }),
    ];
    const live: Record<string, TaskView> = {
      tsk_1: {
        ...task(1, { version: 4, status: 'review' }),
        acceptance: [
          { id: 'c1', text: 'tests pass', kind: 'test', match: 'npm test', required: true },
          { id: 'c2', text: 'live changed', kind: 'deploy', url: 'https://x.dev', required: true },
        ],
      },
      tsk_2: { ...task(2, { version: 4, status: 'in_progress' }) },
      tsk_3: { ...task(3, { version: 1 }) },
    };
    const merged = new Map(mergeTasks(rest, live).map((t) => [t.id, t]));
    expect(merged.get('tsk_1')?.status).toBe('review');
    expect(merged.get('tsk_1')?.started_at).toBe('2026-09-26T09:00:00Z');
    expect(merged.get('tsk_1')?.acceptance[0].waived?.reason).toBe('flaky runner');
    expect(merged.get('tsk_1')?.acceptance[1].waived).toBeUndefined(); // criterion text changed
    expect(merged.get('tsk_2')?.status).toBe('done'); // REST is newer
    expect(merged.get('tsk_3')?.number).toBe(3);
    expect(restIsStale(rest, live)).toBe(true);
    expect(restIsStale(rest, { tsk_2: live.tsk_2 })).toBe(false);
    expect(restIsStale(null, live)).toBe(false);
  });

  it('track live checkpoints with the time they were first seen', () => {
    const loaded = [{ id: 'ckp_1', session_id: 'cs_a', task_id: 'tsk_1', trigger: 'commit', headline: 'a', facts_source: 'relay-cli' as const, created_at: '2026-09-26T10:00:00Z' }];
    const merged = mergeCheckpoints(loaded, { cs_a: { id: 'ckp_2', session_id: 'cs_a', task_id: 'tsk_1', trigger: 'turn', headline: 'b', facts_source: 'relay-cli' } }, { ckp_2: '2026-09-26T10:05:00Z' });
    const now = new Date('2026-09-26T10:08:00Z');
    const dots = checkpointDots(merged, 'tsk_1', now);
    expect(dots.map((d) => [d.id, d.recent])).toEqual([
      ['ckp_1', true],
      ['ckp_2', true],
    ]);
    expect(checkpointDots(merged, 'tsk_1', new Date('2026-09-26T10:14:00Z')).map((d) => d.recent)).toEqual([false, true]);
    expect(checkpointDots(merged, 'tsk_9', now)).toEqual([]);
  });
});

describe('receipt seal', () => {
  // Generated from remembra.crew.reports.receipt_seal (server) for the same inputs.
  const SERVER = [
    {
      criteria: [
        { kind: 'test', status: 'met', source: 'relay-cli' },
        { kind: 'deploy', status: 'met', source: 'server-verified' },
      ],
      pushed: true,
      pushed_source: 'relay-cli',
      reasons: [],
      seal: 'tests ✓ (observed) · pushed ✓ (observed) · live ✓ (server-verified)',
    },
    {
      criteria: [
        { kind: 'test', status: 'met', source: 'agent-declared' },
        { kind: 'test', status: 'met', source: 'relay-cli' },
      ],
      pushed: false,
      pushed_source: null,
      reasons: ['not pushed'],
      seal: 'tests ✓ (self-reported) · pushed ✗',
    },
    {
      criteria: [
        { kind: 'test', status: 'unmet', source: 'relay-cli' },
        { kind: 'file', status: 'met', source: 'relay-cli' },
        { kind: 'manual', status: 'waived', source: null },
      ],
      pushed: false,
      pushed_source: null,
      reasons: ['criterion c0 unmet'],
      seal: 'tests ✗ · files ✓ (observed) · manual waived',
    },
    {
      criteria: [
        { kind: 'command', status: 'unknown', source: null },
        { kind: 'commit', status: 'met', source: 'server-verified' },
        { kind: 'deploy', status: 'waived', source: null },
      ],
      pushed: true,
      pushed_source: 'agent-declared',
      reasons: [],
      seal: 'commands ? · commits ✓ (server-verified) · pushed ✓ (self-reported) · live waived',
    },
    { criteria: [{ kind: 'manual', status: 'met', source: 'agent-declared' }], pushed: false, pushed_source: null, reasons: [], seal: 'manual ✓ (self-reported)' },
  ];

  it('matches the server seal text item by item, with the weakest source per group', () => {
    for (const v of SERVER) {
      const report = {
        id: 'rep_1',
        task_id: 'tsk_1',
        kind: 'completion',
        is_current: true,
        facts_source: 'relay-cli',
        criteria: v.criteria.map((c, i) => ({ id: `c${i}`, status: c.status, source: c.source })),
        criteria_detail: v.criteria.map((c, i) => ({ id: `c${i}`, kind: c.kind, status: c.status, source: c.source })),
        deploy: { pushed: v.pushed, pushed_source: v.pushed_source, reasons: v.reasons },
      } as unknown as ReportDetail;
      expect(sealText(sealItems(report))).toBe(v.seal);
    }
  });

  it('label sources per item: observed, server-verified, self-reported', () => {
    const report = {
      id: 'rep_2',
      task_id: 'tsk_1',
      kind: 'completion',
      is_current: true,
      facts_source: 'agent-declared',
      criteria: [
        { id: 'c1', status: 'met', source: 'agent-declared' },
        { id: 'c2', status: 'met', source: 'server-verified' },
      ],
    } as ReportDetail;
    const acceptance = [
      { id: 'c1', text: 't', kind: 'test' as const, match: 'npm test', required: true },
      { id: 'c2', text: 'l', kind: 'deploy' as const, url: 'https://a.dev', required: true },
    ];
    const items = sealItems(report, acceptance);
    expect(items.map((i) => [i.group, i.mark, i.source])).toEqual([
      ['tests', 'ok', 'agent-declared'],
      ['live', 'ok', 'server-verified'],
    ]);
    expect(sealLine(report, acceptance)).toBe('tests ✓ (self-reported) · live ✓ (server-verified)');
    expect(sealLine({ ...report, seal: 'server text' }, acceptance)).toBe('server text');
    expect(sealLine({ ...report, kind: 'waived' }, acceptance)).toBe('waived by a human');
  });

  it('meter counts required criteria met or waived, with waivers known before any report', () => {
    const t = task(1, {
      acceptance: [
        { id: 'c1', text: 'a', kind: 'test', match: 'npm test', required: true },
        { id: 'c2', text: 'b', kind: 'manual', required: true, waived: { reason: 'checked by hand' } },
        { id: 'c3', text: 'c', kind: 'file', match: 'a.ts', required: false },
      ],
    });
    const none = acceptanceMeter(t, null);
    expect([none.done, none.required, none.waived, none.reported]).toEqual([1, 2, 1, false]);
    const reported = acceptanceMeter(t, { criteria: [{ id: 'c1', status: 'unmet', source: 'relay-cli' }, { id: 'c3', status: 'met', source: 'relay-cli' }] });
    expect([reported.done, reported.required, reported.unmet, reported.met, reported.reported]).toEqual([1, 2, 1, 1, true]);
    expect(reported.items.map((i) => i.state)).toEqual(['unmet', 'waived', 'met']);
  });
});

describe('card details', () => {
  it('zone chips carry mode, reservation, inheritance and freeze', () => {
    const state = crewState();
    expect(zoneChips(task(14, { zone_ids: ['zn_pos'] }), state)).toEqual([
      { zoneId: 'zn_pos', slug: 'pos', mode: 'exclusive', reserved: false, inherited: false, frozen: false },
    ]);
    expect(zoneChips(task(12, { zone_ids: ['zn_rep'] }), state)[0]).toMatchObject({ slug: 'reports', mode: 'exclusive', reserved: true });
    expect(zoneChips(task(15, { zone_ids: ['zn_bil'] }), state)[0]).toMatchObject({ mode: 'watch', inherited: true, frozen: true });
    expect(zoneChips(task(16, { zone_ids: ['zn_rep'] }), state)[0]).toMatchObject({ mode: 'shared', reserved: false });
    expect(zoneChips(task(16, { zone_ids: ['zn_gone'] }), null)[0]).toMatchObject({ slug: 'zn_gone' });
  });

  it('saved work comes from the newest baton ref, else a reserved claim', () => {
    let state = crewState();
    expect(savedWork(task(12), state)).toEqual({ ref: 'refs/remembra/baton/T-12/7', dirtyFiles: null, unpushed: null });
    state = applyEvent(state, {
      seq: 1,
      type: 'baton.ref_created',
      refs: { task_id: 'tsk_12', session_id: 'cs_c' },
      actor: { kind: 'session', id: 'cs_c', verified: true },
      payload: { ref: 'refs/remembra/baton/T-12/8', task_id: 'tsk_12', dirty_files: 3, unpushed: 2 },
      ts: '2026-09-26T10:00:00Z',
    } as never);
    expect(savedWork(task(12), state)).toEqual({ ref: 'refs/remembra/baton/T-12/8', dirtyFiles: 3, unpushed: 2 });
    expect(savedWork(task(99), state)).toBeNull();
    expect(savedWork(task(12), null)).toBeNull();
  });

  it('owner is the live callsign, else the agent id, else unassigned', () => {
    const state = crewState();
    expect(ownerOf({ owner_session_id: 'cs_b', owner_agent_id: 'codex' }, state)).toMatchObject({ label: 'codex-1', verified: false, live: true, note: null });
    expect(ownerOf({ owner_session_id: 'cs_c', owner_agent_id: 'claude-code' }, state)).toMatchObject({ label: 'cc-2', note: 'stopped (billing_error)' });
    expect(ownerOf({ owner_session_id: 'cs_gone', owner_agent_id: 'codex' }, state)).toMatchObject({ label: 'codex', verified: null, live: false });
    expect(ownerOf({ owner_session_id: null, owner_agent_id: null }, state).label).toBe('unassigned');
  });

  it('dependency edges only between visible tasks, satisfied when done', () => {
    const tasks = [task(1, { status: 'done' }), task(2, { depends_on: ['tsk_1', 'tsk_9'] }), task(3, { depends_on: ['tsk_2'] })];
    expect(dependencyEdges(tasks)).toEqual([
      { from: 'tsk_1', to: 'tsk_2', satisfied: true },
      { from: 'tsk_2', to: 'tsk_3', satisfied: false },
    ]);
  });

  it('age from the status timestamp, or the moment this page saw it move', () => {
    const t = task(1, { status: 'in_progress', started_at: '2026-09-26T09:00:00Z', updated_at: '2026-09-26T09:30:00Z' });
    expect(statusSince(t)).toBe('2026-09-26T09:00:00Z');
    expect(statusSince(t, { tsk_1: '2026-09-26T09:59:00Z' })).toBe('2026-09-26T09:59:00Z');
    expect(statusSince(task(2, { status: 'ready', created_at: '2026-09-26T08:00:00Z' }))).toBe('2026-09-26T08:00:00Z');
    const now = new Date('2026-09-26T10:00:00Z');
    expect(ageText('2026-09-26T09:59:20Z', now)).toBe('40s');
    expect(ageText('2026-09-26T09:48:00Z', now)).toBe('12m');
    expect(ageText('2026-09-26T07:00:00Z', now)).toBe('3h');
    expect(ageText('2026-09-24T09:00:00Z', now)).toBe('2d');
    expect(ageText(null, now)).toBeNull();
  });

  it('status moves between two reducer states', () => {
    const before = { tsk_1: task(1, { status: 'in_progress' }), tsk_2: task(2) } as Record<string, TaskView>;
    const after = { tsk_1: task(1, { status: 'review', version: 2 }), tsk_2: task(2, { status: 'claimed', version: 2 }), tsk_3: task(3) } as Record<string, TaskView>;
    expect(statusMoves(before, after)).toEqual([
      { taskId: 'tsk_1', number: 1, from: 'progress', to: 'review', toStatus: 'review' },
      { taskId: 'tsk_3', number: 3, from: null, to: 'next', toStatus: 'ready' },
    ]);
    expect(statusMoves(null, after)).toEqual([]);
  });
});

describe('what a drag means', () => {
  it('no report means no Done: Done opens the report or waiver flow', () => {
    expect(dropIntent({ status: 'in_progress' }, 'done', true)).toEqual({ kind: 'report', mode: 'waive' });
    expect(dropIntent({ status: 'review' }, 'done', true)).toEqual({ kind: 'report', mode: 'review' });
    expect(dropIntent({ status: 'stalled' }, 'done', true)).toEqual({ kind: 'report', mode: 'waive' });
    expect(dropIntent({ status: 'done' }, 'done', true)).toEqual({ kind: 'none' });
  });

  it('stalled cards are picked up, done cards reopen, agents move the rest', () => {
    expect(dropIntent({ status: 'stalled' }, 'progress', true)).toEqual({ kind: 'pickup' });
    expect(dropIntent({ status: 'done' }, 'next', true)).toEqual({ kind: 'reopen' });
    expect(dropIntent({ status: 'ready' }, 'progress', true).kind).toBe('refused');
    expect(dropIntent({ status: 'in_progress' }, 'done', false).kind).toBe('refused');
    expect(dropIntent({ status: 'cancelled' }, 'next', true)).toEqual({ kind: 'refused', reason: 'This task was cancelled.' });
    expect(dropTargets({ status: 'stalled' }, true)).toEqual(['next', 'progress', 'done']);
    expect(dropTargets({ status: 'in_progress' }, false)).toEqual([]);
  });
});

describe('links and lookups', () => {
  it('resolve task= as an id or a T-number', () => {
    const tasks = [task(14), task(2)];
    expect(resolveTaskParam('tsk_2', tasks)?.number).toBe(2);
    expect(resolveTaskParam('T-14', tasks)?.id).toBe('tsk_14');
    expect(resolveTaskParam('t14', tasks)?.id).toBe('tsk_14');
    expect(resolveTaskParam('14', tasks)?.id).toBe('tsk_14');
    expect(resolveTaskParam('T-99', tasks)).toBeNull();
    expect(resolveTaskParam(null, tasks)).toBeNull();
  });

  it('receipt links carry the task, and a report id resolves from live or loaded state', () => {
    expect(receiptHref('yaadbooks', 'rep_1', 'tsk_14')).toBe('#/crew?project=yaadbooks&view=report&report=rep_1&task=tsk_14');
    const state = crewState();
    state.reports = { tsk_14: { id: 'rep_live', task_id: 'tsk_14', kind: 'completion', is_current: true, facts_source: 'relay-cli', criteria: [] } };
    expect(taskForReport('rep_live', state, [])).toBe('tsk_14');
    expect(taskForReport('rep_2', null, [task(2, { current_report_id: 'rep_2' })])).toBe('tsk_2');
    expect(taskForReport('rep_x', state, [])).toBeNull();
  });
});

describe('criteria editing mirrors the server', () => {
  it('accept valid criteria and name each problem', () => {
    const ok = [
      { ...emptyDraft([]), text: 'POS tests pass', match: 'npm test -- pos' },
      { id: 'c2', text: 'Live health OK', kind: 'deploy' as const, match: '', url: 'https://yaadbooks.com/api/health', required: true },
      { id: 'c3', text: 'Receipt file', kind: 'file' as const, match: 'src/app/pos/Receipt.tsx', url: '', required: false },
      { id: 'c4', text: 'Mani checked', kind: 'manual' as const, match: '', url: '', required: true },
    ];
    expect(validateCriteria(ok)).toEqual([]);
    expect(fromDraft(ok[1])).toEqual({ id: 'c2', text: 'Live health OK', kind: 'deploy', match: null, url: 'https://yaadbooks.com/api/health', required: true });
    expect(toDraft(fromDraft(ok[0]))).toEqual({ ...ok[0], url: '' });
    const bad = validateCriteria([
      { id: 'c1', text: '', kind: 'test', match: '* test', url: '', required: true },
      { id: 'c1', text: 'x', kind: 'file', match: '../etc/passwd', url: '', required: true },
      { id: '-x', text: 'x', kind: 'deploy', match: '', url: 'http://plain.dev', required: true },
      { id: 'c5', text: 'x', kind: 'commit', match: 'abc', url: '', required: true },
    ]);
    expect(bad.join('\n')).toMatch(/describe what "done" means/);
    expect(bad.join('\n')).toMatch(/start with the program name/);
    expect(bad.join('\n')).toMatch(/id is used twice/);
    expect(bad.join('\n')).toMatch(/repo-relative path/);
    expect(bad.join('\n')).toMatch(/1–32 letters/);
    expect(bad.join('\n')).toMatch(/https URL/);
    expect(bad.join('\n')).toMatch(/sha prefix/);
  });

  it('command patterns: literal program, whole-word *, no regex', () => {
    expect(commandPatternProblems('supabase db push *')).toEqual([]);
    expect(commandPatternProblems('npm  test')).toEqual(['separate words with single spaces']);
    expect(commandPatternProblems('npm te*t')[0]).toMatch(/whole word/);
    expect(commandPatternProblems('npm (test|lint)')[0]).toMatch(/no regex/);
    expect(isPathRel('src/a.ts')).toBe(true);
    expect(isPathRel('/etc/passwd')).toBe(false);
    expect(isPathRel('a\\b')).toBe(false);
    expect(isPathRel('a/\u0001')).toBe(false);
    expect(nextCriterionId([{ id: 'c1' }, { id: 'c3' }])).toBe('c4');
    expect(nextCriterionId([{ id: 'c2' }])).toBe('c3');
  });

  it('waivers need a reason of at most 280 characters', () => {
    expect(waiverReasonError('  ')).toMatch(/Say why/);
    expect(waiverReasonError('x'.repeat(281))).toMatch(/280/);
    expect(waiverReasonError('Verified by hand on staging')).toBeNull();
  });
});
