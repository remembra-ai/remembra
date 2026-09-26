import { describe, expect, it } from 'vitest';
import type { CrewListItem } from '../../../lib/crew/types';
import { branch, buildTree, progressRail } from '../buildTree';
import { agentPageData } from '../agentPageModel';
import { liveStatus, quotaSources, trackBranch } from '../trackModel';
import { event, NOW, session, snapshot, state } from '../../../components/crew/lane/__tests__/fixture';

function listItem(over: Partial<CrewListItem> = {}): CrewListItem {
  const snap = snapshot();
  return {
    crew: snap.crew,
    role: 'owner',
    live: 3,
    needs_you: 1,
    crew_inbox: 1,
    moments_24h: 4,
    last_event_at: new Date(NOW - 40000).toISOString(),
    tasks_by_status: { in_progress: 2, stalled: 1, done: 12 },
    phases: [
      { phase: 'Phase 1', total: 12, done: 12 },
      { phase: 'Phase 2', total: 9, done: 5 },
      { phase: 'Phase 3', total: 4, done: 0 },
      { phase: 'Phase 4', total: 6, done: 0 },
    ],
    live_sessions: snap.sessions,
    live_sessions_truncated: false,
    ...over,
  };
}

describe('site board build tree', () => {
  it('puts each agent under the phase of its task, with batons where their task sits', () => {
    const tree = buildTree(listItem(), snapshot(), NOW);
    expect(tree.partial).toBe(false);
    expect(tree.phases.map((p) => [p.label, p.glyph, p.status])).toEqual([
      ['Phase 1', '✓', 'done'],
      ['Phase 2', '◉', 'in progress'],
      ['Phase 3', '◉', 'in progress'],
      ['Phase 4', '○', 'not started'],
    ]);
    const phase2 = tree.phases[1].children;
    expect(phase2.map((l) => l.kind)).toEqual(['session', 'session', 'baton']);
    const [cc1, cc2, baton] = phase2;
    expect(cc1).toMatchObject({
      kind: 'session',
      glyph: '◉',
      taskRef: 'T-1',
      taskTitle: 'POS split tender',
      right: 'POS ▨ excl · enforced',
    });
    expect(cc2).toMatchObject({ kind: 'session', glyph: '⚠', status: 'credits ran out (billing_error)', alarm: true });
    expect(baton).toMatchObject({ kind: 'baton', glyph: '✦' });
    expect(baton.kind === 'baton' && baton.right).toMatch(/^✦ baton ready \(work saved\) {2}credits ran out/);
    const codex = tree.phases[2].children[0];
    expect(codex).toMatchObject({ kind: 'session', monogram: 'CX', glyph: '◉', alarm: true });
    expect(codex.kind === 'session' && codex.right).toBe('REPORTS ▧ shared · ⧗ waiting on POS · commit gate: missing');
    expect(tree.loose).toHaveLength(0);
  });

  it('shows the list alone while the snapshot loads, agents unplaced', () => {
    const tree = buildTree(listItem(), null, NOW);
    expect(tree.partial).toBe(true);
    expect(tree.loose.map((l) => l.kind === 'session' && l.session.callsign)).toEqual(['cc-1', 'codex-1', 'cc-2']);
    expect(tree.phases.find((p) => p.label === 'Phase 2')!.glyph).toBe('◉'); // work done already
  });

  it('keeps agents without a task (and phases without a name) visible', () => {
    const snap = snapshot();
    snap.sessions = [session('cs_x', { callsign: 'cc-9', current_task_id: null, state: 'idle' })];
    snap.claims = [];
    snap.tasks = [];
    const tree = buildTree(listItem({ phases: [{ phase: null, total: 2, done: 0 }] }), snap, NOW);
    expect(tree.phases[0].label).toBe('Tasks');
    expect(tree.loose[0]).toMatchObject({ kind: 'session', glyph: '◌', taskRef: null });
  });

  it('draws rails and branches in mono', () => {
    expect(progressRail(5, 10, 10)).toBe('▰▰▰▰▰▱▱▱▱▱');
    expect(progressRail(0, 0, 4)).toBe('▱▱▱▱');
    expect(progressRail(12, 9, 3)).toBe('▰▰▰');
    expect([branch(false), branch(true)]).toEqual(['├─', '└─']);
  });
});

describe('track header and status strip', () => {
  it('shows the most recently active branch@head', () => {
    const s = state();
    expect(trackBranch(s)).toBe('main@abc1234');
    for (const x of Object.values(s.sessions)) x.branch = null;
    expect(trackBranch(s)).toBeNull();
  });

  it('leads special moves and ages the latest one', () => {
    const s = state();
    expect(liveStatus(s, null, NOW)).toMatchObject({ text: '3 live · no moves in the last hour', fresh: false });
    const pass = event('baton.passed', 0.01, { summary: 'baton for T-3 passed to cc-1 (adopt)' });
    expect(liveStatus(s, pass, NOW)).toMatchObject({ lead: 'Baton passed ·', age: 'just now', fresh: true });
    const multi = event('crew.mode_changed', 3, { summary: 'crew yaadbooks is multi', payload: { to: 'multi' } });
    expect(liveStatus(s, multi, NOW)).toMatchObject({ lead: 'Crew assembled ·', age: '3m ago', fresh: false });
  });

  it('remembers how each quota stop was learned', () => {
    const e = event('session.quota_blocked', 1, { refs: { session_id: 'cs_c' }, payload: { error: 'billing_error', source: 'reported' } });
    expect([...quotaSources([e]).entries()]).toEqual([['cs_c', 'reported']]);
  });
});

describe('agent page data', () => {
  it('reads the route loosely and labels claims with zone slugs', () => {
    const snap = snapshot();
    const page = agentPageData(
      {
        agent_id: 'claude-code',
        verified: true,
        current_sessions: [snap.sessions[0], { bogus: true }],
        sessions: snap.sessions,
        checkpoints: [{ id: 'ckp_1', session_id: 'cs_a', trigger: 'commit', headline: 'h', facts_source: 'relay-cli', created_at: 'x' }],
        batons_in: [
          {
            id: 'btn_1',
            to_session: 'cs_a',
            from_session: 'cs_c',
            from_callsign: 'cc-2',
            kind: 'adopt',
            restored: true,
            brief_text: 'YOUR BATON',
            created_at: 'x',
          },
        ],
        batons_out: [{ id: 'btn_2' }],
        claims: snap.claims,
        tasks: 'nope',
      },
      snap.zones,
    );
    expect(page.current).toHaveLength(1);
    expect(page.batonsIn[0]).toMatchObject({ from_callsign: 'cc-2', restored: true, brief_text: 'YOUR BATON' });
    expect(page.batonsOut).toHaveLength(0);
    expect(page.tasks).toEqual([]);
    expect(new Map(page.claimsZoneLabels).get('clm_pos')).toBe('pos');
    expect(agentPageData({}).sessions).toEqual([]);
  });
});
