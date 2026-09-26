import { describe, expect, it } from 'vitest';
import {
  agentPageHref,
  checkpointStreak,
  enforcementView,
  laneOrder,
  limitMeter,
  nowLine,
  pickupCommand,
  presenceView,
  reportRing,
  sessionClaims,
  shortAge,
  zoneChips,
} from '../model';
import { NOW, iso, state } from './fixture';

describe('lane presence', () => {
  const s = state();

  it('words every state and never relies on colour', () => {
    const a = s.sessions.cs_a;
    expect(presenceView(a, [], NOW)).toMatchObject({ kind: 'ring', label: 'active', settled: false });
    expect(presenceView({ ...a, state: 'idle', last_activity_at: iso(7) }, [], NOW).label).toBe('idle 7m');
    expect(presenceView({ ...a, state: 'quiet', quiet_reason: 'host_unreachable', last_activity_at: iso(6) }, [], NOW)).toMatchObject({
      kind: 'hollow',
      label: 'quiet 6m',
      detail: 'host offline',
    });
    expect(presenceView({ ...a, state: 'quiet', quiet_reason: 'mcp_silent' }, [], NOW).detail).toBe('no MCP calls');
    expect(presenceView({ ...a, state: 'paused' }, [], NOW)).toMatchObject({ kind: 'paused', label: 'paused' });
    expect(presenceView({ ...a, state: 'lost', state_reason: 'process_exited' }, [], NOW)).toMatchObject({
      kind: 'dashed',
      detail: 'process exited',
      settled: true,
    });
  });

  it('shows a quota stop as a dropped baton with its error and source', () => {
    const c = s.sessions.cs_c;
    const view = presenceView(c, [], NOW, 'reported');
    expect(view).toMatchObject({ kind: 'dropped', label: 'credits ran out', detail: 'billing_error · reported', settled: true });
    expect(presenceView({ ...c, state_reason: 'rate_limit' }, [], NOW).label).toBe('rate limited');
    // without an event, the limit view is the source of an exhausted limit
    expect(presenceView({ ...c, limit: { level: 'exhausted', pct: 1, source: 'detected' } }, [], NOW).detail).toBe(
      'billing_error · detected',
    );
  });

  it('flags a fenced claim and stuck sessions', () => {
    const a = s.sessions.cs_a;
    const claims = sessionClaims(s, 'cs_a').map((c) => ({ ...c, fenced: true }));
    const view = presenceView({ ...a, stuck: true }, claims, NOW);
    expect(view.fenced).toBe(true);
    expect(view.stuck).toBe(true);
  });

  it('takes state and stuck from the server state, never from a presence overlay', () => {
    // an overlay from the last 5-second frame says "active"; the server has since said quota_blocked + stuck
    const overlay = { state: 'active' as const, stuck: false, calls_since_checkpoint: 3 };
    const a = { ...s.sessions.cs_a, state: 'quota_blocked' as const, stuck: true, state_reason: 'billing_error', presence: overlay };
    expect(presenceView(a, [], NOW)).toMatchObject({ kind: 'dropped', label: 'credits ran out', stuck: true, settled: true });
    const quiet = { ...s.sessions.cs_a, state: 'quiet' as const, quiet_reason: 'host_unreachable' as const, last_activity_at: iso(6), presence: overlay };
    expect(presenceView(quiet, [], NOW)).toMatchObject({ kind: 'hollow', label: 'quiet 6m', detail: 'host offline' });
  });
});

describe('enforcement layers', () => {
  it('reads every layer, and alarms when the commit gate is missing', () => {
    expect(enforcementView({ adapter_enforcement: 'enforced', githook_state: 'ok' }).text).toBe(
      'before write: enforced · commit ✓ · push ✓',
    );
    expect(enforcementView({ adapter_enforcement: 'advisory', githook_state: 'chained' }).text).toBe(
      'before write: advisory · commit ✓ · push ✓',
    );
    expect(enforcementView({ adapter_enforcement: 'enforced', githook_state: null }).text).toBe(
      'before write: enforced · commit ? · push ?',
    );
    const missing = enforcementView({ adapter_enforcement: 'enforced', githook_state: 'missing' });
    expect(missing).toMatchObject({ alarm: true, commit: 'missing', push: 'missing' });
    expect(missing.text).toBe('before write: enforced · commit gate: missing');
  });
});

describe('zone chips and the now line', () => {
  const s = state();

  it('styles chips by mode and marks inherited, waiting and reserved ones', () => {
    const b = zoneChips(s, sessionClaims(s, 'cs_b'));
    expect(b.map((c) => [c.label, c.style, c.inherited, c.waiting])).toEqual([
      ['reports', 'striped', true, false],
      ['pos', 'solid', false, true],
    ]);
    expect(b[0].description).toBe('zone reports, shared, inherited');
    expect(b[1].description).toBe('zone pos, exclusive, waiting');
    const c = zoneChips(s, sessionClaims(s, 'cs_c'));
    expect(c[0]).toMatchObject({ label: 'payroll', reserved: true, title: 'Payroll' });
    const watch = zoneChips(s, [{ ...s.claims.clm_pos, mode: 'watch' }]);
    expect(watch[0].style).toBe('outline');
  });

  it('carries the task and the last action from the presence frame', () => {
    const a = {
      ...s.sessions.cs_a,
      presence: {
        state: 'active' as const,
        stuck: false,
        calls_since_checkpoint: 3,
        last_action: { tool: 'Edit', path_rel: 'src/app/pos/cart.ts', age_s: 3 },
      },
    };
    const line = nowLine(s, a, 2000);
    expect(line).toMatchObject({
      taskRef: 'T-1',
      taskTitle: 'POS split tender',
      action: { tool: 'Edit', path: 'src/app/pos/cart.ts', ageS: 5 },
    });
    expect(nowLine(s, { ...a, current_task_id: null, presence: null }, 0)).toMatchObject({ taskRef: null, action: null });
    expect(nowLine(s, { ...a, current_task_id: 'tsk_other' }, 0).taskRef).toBe('tsk_other');
  });
});

describe('limit meter and report ring', () => {
  const s = state();

  it('reads the limit with its source', () => {
    expect(limitMeter(s.sessions.cs_a)).toBeNull();
    const m = limitMeter({ ...s.sessions.cs_a, limit: { level: 'warn', pct: 0.824, source: 'detected' } })!;
    expect(m).toMatchObject({ pct: 0.824, text: 'limit 82% · detected', alarm: false });
    expect(limitMeter({ ...s.sessions.cs_a, limit: { level: 'exhausted', pct: null, source: 'reported' } })).toMatchObject({
      text: 'limit exhausted · reported',
      alarm: true,
    });
    expect(limitMeter({ ...s.sessions.cs_a, limit: { level: 'ok', pct: 7, source: 'inferred' } })!.pct).toBe(1);
  });

  it('fills toward the next checkpoint by calls or time, and flags a missed one', () => {
    const a = s.sessions.cs_a;
    const byCalls = reportRing({ ...a, presence: { state: 'active', stuck: false, calls_since_checkpoint: 30 } }, NOW, null);
    expect(byCalls).toMatchObject({ fraction: 0.75, label: '30 calls since checkpoint', missed: false });
    const due = reportRing(
      { ...a, presence: { state: 'active', stuck: false, calls_since_checkpoint: 2, next_checkpoint_due_at: iso(-5) } },
      NOW,
      null,
    );
    expect(due.label).toBe('next checkpoint in 5m');
    expect(due.fraction).toBeCloseTo(0.5, 5);
    const late = reportRing({ ...a, presence: null }, NOW, NOW - 25 * 60000);
    expect(late).toMatchObject({ missed: true, label: 'checkpoint overdue', fraction: 1 });
    expect(reportRing({ ...a, presence: null }, NOW, NOW - 12 * 60000).label).toBe('checkpoint due');
    expect(reportRing(a, NOW, NOW - 3 * 60000, true).label).toBe('stopped · last checkpoint 3m ago');
  });

  it('counts checkpoints in a row that arrived within twice the interval', () => {
    const m = 60000;
    expect(checkpointStreak([], NOW)).toBe(0);
    expect(checkpointStreak([NOW - 2 * m, NOW - 12 * m, NOW - 25 * m], NOW)).toBe(3);
    expect(checkpointStreak([NOW - 2 * m, NOW - 30 * m], NOW)).toBe(1);
    expect(checkpointStreak([NOW - 30 * m], NOW)).toBe(0);
  });
});

describe('lane helpers', () => {
  it('orders working lanes first, then by callsign numerically', () => {
    const s = state();
    const extra = { ...s.sessions.cs_a, id: 'cs_z', callsign: 'cc-10' };
    expect(laneOrder([s.sessions.cs_c, extra, s.sessions.cs_b, s.sessions.cs_a]).map((x) => x.callsign)).toEqual([
      'cc-1',
      'cc-10',
      'codex-1',
      'cc-2',
    ]);
  });

  it('formats ages, pickup commands and agent links', () => {
    expect([shortAge(3), shortAge(90), shortAge(7200), shortAge(3 * 86400)]).toEqual(['3s', '1m', '2h', '3d']);
    expect(pickupCommand({ number: 12 })).toBe('remembra-crew adopt T-12');
    expect(pickupCommand(null)).toBeNull();
    expect(agentPageHref('claude-code', 'cs_a', 'yaadbooks')).toBe('#/agents?agent=claude-code&session=cs_a&project=yaadbooks');
    expect(agentPageHref('codex')).toBe('#/agents?agent=codex');
  });
});
