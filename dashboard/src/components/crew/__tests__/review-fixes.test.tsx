// Launch-review fixes to the crew screens: sub-agents are labelled and never offered a baton in the
// pickers, one before-write label everywhere, running vs stopped counts, owners stopped on credits
// read "not running", feed and ticker lines without raw ids, the bypass and enforcement copy, and the
// phone board's selected column tab kept in view.

import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it } from 'vitest';
import { batonTargets, beforeWriteLabel, callsignWithParent, liveSplit, liveSplitText, sessionLabel } from '../../../lib/crew/selectors';
import { humanSummary } from '../../../lib/crew/summary';
import type { CrewListItem } from '../../../lib/crew/types';
import { buildTree } from '../../../pages/crew/buildTree';
import { liveStatus } from '../../../pages/crew/trackModel';
import { ownerOf } from '../board/model';
import { revealSelectedTab } from '../board/tabs';
import { enforcementView } from '../lane/model';
import { GitHookStatus } from '../policy/GitHookStatus';
import { ENFORCEMENT_CHOICES, TRUST_FOOTNOTE } from '../policy/policyModel';
import { enforcementLayers, holderLabel } from '../zones/zoneModel';
import { claim, crewState, session, task } from './fixtures';

function withSubAgent() {
  return crewState({
    sessions: [
      session('cs_a', 'cc-1', { current_task_id: 'tsk_14' }),
      session('cs_s', 'cc-2', { parent_session_id: 'cs_a' }),
      session('cs_x', 'cursor-1', { agent_id: 'cursor', adapter: 'cursor', adapter_enforcement: 'advisory', client_kind: 'hook' }),
      session('cs_q', 'cc-4', { state: 'quota_blocked', state_reason: 'billing_error' }),
    ],
    claims: [claim('clm_pos', 'zn_pos', 'cs_s', { task_id: 'tsk_14' })],
  });
}

describe('sub-agents', () => {
  it('are left out of the baton pickers and labelled with their parent elsewhere', () => {
    const state = withSubAgent();
    expect(batonTargets(state).map((s) => s.callsign)).toEqual(['cc-1', 'cursor-1']);
    expect(batonTargets(state, ['cs_a']).map((s) => s.callsign)).toEqual(['cursor-1']);
    expect(callsignWithParent(state, state.sessions.cs_s)).toBe('cc-2 (sub-agent of cc-1)');
    expect(callsignWithParent(state, state.sessions.cs_a)).toBe('cc-1');
    expect(sessionLabel(state.sessions.cs_s)).toBe('cc-2 · claude-code (key-verified) · sub-agent');
    expect(holderLabel(state, state.claims.clm_pos)).toBe('cc-2 (enforced, sub-agent of cc-1)');
  });

  it('sit right under their parent on the Site Board, where the parent works', () => {
    const state = withSubAgent();
    const snap = {
      crew: state.crew!,
      server_time: '2026-09-26T12:00:00Z',
      as_of_seq: 40,
      etag: '"40"',
      sessions: Object.values(state.sessions),
      claims: Object.values(state.claims),
      zones: Object.values(state.zones),
      commons: [],
      ignore: [],
      tasks: [task('tsk_14', 14, 'Split tender', { owner_session_id: 'cs_a', phase: 'Phase 2' })],
      collisions: [],
      decisions: [],
      offers: [],
      footprints: [],
      inbox_counts: { project: 0, crew: 0 },
      pending_zone_changes: [],
    };
    const item: CrewListItem = {
      crew: snap.crew,
      role: 'owner',
      live: 4,
      needs_you: 0,
      crew_inbox: 0,
      moments_24h: 0,
      last_event_at: null,
      tasks_by_status: {},
      phases: [{ phase: 'Phase 2', total: 1, done: 0 }],
      live_sessions: snap.sessions,
      live_sessions_truncated: false,
    };
    const tree = buildTree(item, snap, Date.parse('2026-09-26T12:00:00Z'));
    const phase = tree.phases.find((p) => p.label === 'Phase 2')!;
    const names = phase.children.map((l) => (l.kind === 'session' ? l.session.callsign : 'baton'));
    expect(names.slice(0, 2)).toEqual(['cc-1', 'cc-2']);
    const sub = phase.children.find((l) => l.kind === 'session' && l.session.id === 'cs_s');
    expect(sub && sub.kind === 'session' && sub.parentCallsign).toBe('cc-1');
  });
});

describe('one before-write label', () => {
  it('reads the same on the Track lane, the zone holder line and the Policy list', () => {
    const state = withSubAgent();
    const cursor = state.sessions.cs_x;
    expect(beforeWriteLabel(cursor)).toBe('read-only fence');
    expect(enforcementView(cursor).beforeWrite).toBe('read-only fence');
    expect(enforcementLayers(cursor).beforeWrite).toBe('read-only fence');
    expect(holderLabel(state, claim('c', 'zn_pos', 'cs_x'))).toBe('cursor-1 (read-only fence)');
    const mcp = { ...cursor, client_kind: 'mcp' as const };
    expect([beforeWriteLabel(mcp), enforcementView(mcp).beforeWrite, enforcementLayers(mcp).beforeWrite]).toEqual([
      'advisory',
      'advisory',
      'advisory',
    ]);
  });

  it('qualifies what enforce means and the git gates a checkout has not reported', () => {
    expect(ENFORCEMENT_CHOICES[0].meaning).toContain('where their hooks enforce it');
    expect(ENFORCEMENT_CHOICES[0].meaning).toContain('where the git gates are installed');
    expect(TRUST_FOOTNOTE).not.toMatch(/\bL1\b/);
    const row = { key: 'w', worktreeId: 'wt-1', hostId: null, branches: ['main'], sessions: [{ ...session('cs_x', 'cursor-1'), presence: null }], hook: 'unknown' as const };
    const html = renderToStaticMarkup(<GitHookStatus rows={[row]} />);
    expect(html).toContain('has not reported its git gates yet');
    expect(html).not.toContain('Every live checkout runs');
  });
});

describe('running, stopped and paused', () => {
  it('counts a session stopped on its credits apart from the running ones', () => {
    const item = {
      live: 4,
      live_sessions: [session('a', 'cc-1'), session('b', 'cc-2'), session('c', 'cc-3', { state: 'quota_blocked' }), session('d', 'cc-4', { state: 'paused' })],
    };
    expect(liveSplit(item)).toEqual({ running: 2, stopped: 1, paused: 1 });
    expect(liveSplitText(liveSplit(item))).toBe('2 running · 1 stopped · 1 paused');
    expect(liveSplitText({ running: 3, stopped: 0, paused: 0 })).toBe('3 running');
  });

  it('says an owner stopped on its credits is not running', () => {
    const state = withSubAgent();
    const owner = ownerOf({ owner_session_id: 'cs_q', owner_agent_id: 'claude-code' }, state);
    expect(owner.live).toBe(false);
    expect(owner.note).toContain('stopped');
    expect(ownerOf({ owner_session_id: 'cs_a', owner_agent_id: 'claude-code' }, state).live).toBe(true);
  });
});

describe('summaries without raw ids', () => {
  // real ids are ULIDs
  const T14 = 'tsk_01M3F1TASK000014';
  const POS = 'clm_01M3F1CLAIMPOS0';
  const CC1 = 'cs_01M3F1SESSION0A';
  const state = crewState({
    sessions: [session(CC1, 'cc-1', { current_task_id: T14 })],
    tasks: [task(T14, 14, 'Split tender', { owner_session_id: CC1 })],
    claims: [claim(POS, 'zn_pos', CC1, { task_id: T14 })],
  });
  it('turns ids into what they name, drops bracketed ids and fixes plurals', () => {
    const cases: [string, Record<string, string>, string][] = [
      ['inbox project item decision_to_confirm (inb_01M3F1RTTKJTBGBWKMFVNSGNNZ)', {}, 'inbox project item decision to confirm'],
      [`baton ${POS} offered to cursor-1`, {}, 'baton for T-14 offered to cursor-1'],
      ['cc-2 posted a note (msg_01M3F1ABCDEF)', {}, 'cc-2 posted a note'],
      [`inbox task_ready opened for ${T14}`, {}, 'inbox task ready opened for T-14'],
      [`claim ${POS} transferred to cursor-1`, {}, 'claim on pos transferred to cursor-1'],
      ['host vm unreachable (1 sessions quiet)', {}, 'host vm unreachable (1 session quiet)'],
      ['human redacted msg_01M3F1ABCDEF', {}, 'human redacted a message'],
      ['report rpt_01M3F1ABCD for T-14 accepted by the gate', {}, 'report for T-14 accepted by the gate'],
      ['claim clm_UNKNOWN01 released', { zone_id: 'zn_rep' }, 'claim on reports released'],
      [`${CC1} took tsk_01M3F1NOSUCHTASK`, {}, 'cc-1 took a task'],
      ['zone my_zone held by cc-1', {}, 'zone my_zone held by cc-1'],
    ];
    for (const [summary, refs, want] of cases) expect(humanSummary(state, { summary, refs })).toBe(want);
    expect(humanSummary(null, { summary: `baton ${POS} offered to cc-3` })).toBe('a baton offered to cc-3');
  });

  it('feeds the Track ticker', () => {
    const ev = {
      seq: 41,
      id: 'evt_41',
      crew_id: 'crw_1',
      project_id: 'yaadbooks',
      ts: '2026-09-26T12:00:00Z',
      type: 'claim.offered_in_brief',
      v: 1,
      origin: 'server',
      actor: { kind: 'system', id: 'server', verified: true },
      refs: { claim_id: POS },
      severity: 'info',
      moment: false,
      summary: `baton ${POS} offered to cc-9`,
      payload: {},
    };
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    expect(liveStatus(state, ev as any, Date.parse('2026-09-26T12:00:03Z')).text).toBe('baton for T-14 offered to cc-9');
  });
});

describe('phone board tabs', () => {
  it('scrolls the strip, not the page, to show the selected tab', () => {
    // no DOM in these tests: a strip and a tab with the geometry of a 375px screen
    const tabBox = { left: 420, right: 500 };
    const tab = { getBoundingClientRect: () => tabBox };
    const strip = {
      scrollLeft: 0,
      getBoundingClientRect: () => ({ left: 0, right: 375 }),
      querySelector: (sel: string) => (sel === '[aria-selected="true"]' ? tab : null),
    };
    revealSelectedTab(strip as unknown as HTMLElement);
    expect(strip.scrollLeft).toBe(141); // 500 - 375 + 16
    tabBox.left = -60;
    tabBox.right = 20;
    strip.scrollLeft = 200;
    revealSelectedTab(strip as unknown as HTMLElement);
    expect(strip.scrollLeft).toBe(124); // 200 - (0 - -60) - 16
    const none = { ...strip, querySelector: () => null, scrollLeft: 7 };
    revealSelectedTab(none as unknown as HTMLElement);
    expect(none.scrollLeft).toBe(7);
  });
});
