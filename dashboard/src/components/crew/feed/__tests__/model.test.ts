import { describe, expect, it } from 'vitest';
import { fromSnapshot } from '../../../../lib/crew/reducer';
import type { CrewSnapshot, CrewState } from '../../../../lib/crew/types';
import {
  NO_FILTERS,
  TYPE_GROUPS,
  actorText,
  ageText,
  batonText,
  buildRows,
  detailFromAgent,
  detailText,
  groupActive,
  matchesFilters,
  moveSelection,
  rowKind,
  rowLook,
  rowTarget,
  runKeyOf,
  scrollToReveal,
  toggleGroup,
  windowRange,
} from '../model';
import { CREW, ev, sample, samples } from './fixtures';

function state(): CrewState {
  const snap: CrewSnapshot = {
    crew: { id: CREW, project_id: 'yaadbooks', name: 'YaadBooks', mode: 'multi', enforcement: 'enforce', settings_version: 1, last_seq: 0 },
    server_time: '2026-09-25T20:00:00.000Z',
    as_of_seq: 0,
    etag: '"0"',
    sessions: [
      { id: 'cs_a', callsign: 'cc-1', agent_id: 'claude-code', member_key: 'k', agent_verified: true, adapter_enforcement: 'enforced', state: 'active', stuck: false, joined_at: '2026-09-25T19:00:00Z' },
      { id: 'cs_b', callsign: 'codex-1', agent_id: 'codex', member_key: 'k2', agent_verified: false, adapter_enforcement: 'advisory', state: 'active', stuck: false, joined_at: '2026-09-25T19:00:00Z' },
      { id: 'cs_c', callsign: 'cc-2', agent_id: 'claude-code', member_key: 'k3', agent_verified: true, adapter_enforcement: 'enforced', state: 'active', stuck: false, joined_at: '2026-09-25T19:00:00Z' },
    ],
    claims: [],
    zones: [
      { id: 'zn_pos', slug: 'pos', title: 'POS section', is_leaf: true, builtin: false, include_globs: ['src/app/pos/**'], exclude_globs: [], services: [], command_patterns: [], mcp_tools: [], mode: 'exclusive', auto_claim: true, protected: false, fail_closed: false, source: 'repo', version: 1 },
    ],
    commons: [],
    ignore: [],
    tasks: [
      { id: 'tsk_14', number: 14, title: 'POS split tender', status: 'in_progress', priority: 2, zone_ids: ['zn_pos'], depends_on: [], acceptance: [], acceptance_locked: true, version: 1 },
    ],
    collisions: [],
    decisions: [],
    offers: [],
    footprints: [],
    inbox_counts: { project: 0, crew: 0 },
    pending_zone_changes: [],
  };
  return fromSnapshot(snap);
}

describe('classification and looks over every L0 event type', () => {
  it('gives every contract sample a glyph, a status word and a known kind', () => {
    const all = samples();
    expect(all.length).toBeGreaterThan(90);
    for (const e of all) {
      const kind = rowKind(e.type);
      const look = rowLook({ kind: kind === 'checkpoint' ? 'checkpoints' : kind, event: e, collapsed: false, events: [e] });
      expect(look.glyph.length, e.type).toBeGreaterThan(0);
      expect(look.label.trim().length, e.type).toBeGreaterThan(0);
      expect(look.label, e.type).not.toMatch(/undefined|null/);
    }
  });

  it('maps the special rows of §9.6', () => {
    expect(rowKind('baton.passed')).toBe('baton');
    expect(rowKind('checkpoint.created')).toBe('checkpoint');
    expect(rowKind('task.done')).toBe('completion');
    expect(rowKind('collision.detected')).toBe('collision');
    expect(rowKind('decision.proposed')).toBe('decision');
    expect(rowKind('guard.blocked')).toBe('guard');
    expect(rowKind('guard.bypass_used')).toBe('bypass');
    expect(rowKind('guard.tamper_blocked')).toBe('tamper');
    expect(rowKind('gate.tampered')).toBe('tamper');
    expect(rowKind('claim.granted')).toBe('event');
  });

  it('uses signal orange only for things that move or need action, fail for alarms', () => {
    const tone = (type: string) => {
      const e = sample(type);
      const k = rowKind(type);
      return rowLook({ kind: k === 'checkpoint' ? 'checkpoints' : k, event: e, collapsed: false, events: [e] }).tone;
    };
    expect(tone('baton.passed')).toBe('signal');
    expect(tone('decision.proposed')).toBe('signal');
    expect(tone('zone.change_pending')).toBe('signal');
    expect(tone('guard.bypass_used')).toBe('fail');
    expect(tone('guard.tamper_blocked')).toBe('fail');
    expect(tone('collision.detected')).toBe('fail'); // payload severity high, envelope severity info
    expect(tone('task.done')).toBe('ok');
    expect(tone('claim.granted')).toBe('neutral');
    expect(tone('activity.commit')).toBe('neutral');
  });

  it('reads guard observe-mode denials as "would block"', () => {
    const e = sample('guard.blocked');
    expect(rowLook({ kind: 'guard', event: e, collapsed: false, events: [e] }).label).toBe('would block (observe)');
  });
});

describe('text', () => {
  it('names actors with their trust label', () => {
    expect(actorText(sample('checkpoint.created'))).toEqual({ name: 'cc-1', trust: 'key-verified' });
    expect(actorText(ev(1, { actor: { kind: 'session', id: 'cs_b', callsign: 'codex-1', verified: false } }))).toEqual({ name: 'codex-1', trust: 'self-declared' });
    expect(actorText(sample('task.done'))).toEqual({ name: 'server', trust: 'server' });
    expect(actorText(sample('crew.settings_changed'))).toEqual({ name: 'you', trust: 'human' });
  });

  it('pulls the free text out of payloads, one line, clipped, and says when an agent wrote it', () => {
    expect(detailText(sample('checkpoint.created'))).toBe('a1b2c3d fix rounding · 41/41 tests');
    expect(detailText(sample('decision.proposed'))).toBe('Decision 2: GCT rounding half-up per line');
    expect(detailText(sample('message.posted'))).toBe('hello');
    expect(detailText(sample('guard.blocked'))).toBe('src/app/pos/cart.ts · zone pos · held by cc-1');
    const long = sample('message.posted');
    (long.payload.message as { body: string }).body = `line one\n\n${'x'.repeat(400)}`;
    const text = detailText(long)!;
    expect(text.length).toBe(180);
    expect(text).not.toContain('\n');
    expect(text.endsWith('…')).toBe(true);
    expect(detailFromAgent(sample('message.posted'))).toBe(true);
    expect(detailFromAgent(sample('guard.blocked'))).toBe(false);
    expect(detailFromAgent(sample('task.done'))).toBe(false);
  });

  it('renders markup in agent text as data, never as HTML (strings in, strings out)', () => {
    const e = sample('message.posted');
    (e.payload.message as { body: string }).body = '<img src=x onerror=alert(1)> [click](javascript:alert(1))';
    expect(detailText(e)).toBe('<img src=x onerror=alert(1)> [click](javascript:alert(1))');
  });

  it('describes a baton pass with callsigns, zone slugs and saved work', () => {
    const s = state();
    const e = sample('baton.passed');
    expect(batonText(e, s)).toEqual({ from: 'cc-1', to: 'cc-2', zones: ['pos'], task: 'T-14', restored: null, savedWork: false, kind: 'same checkout' });
    const withRef = { ...e, payload: { ...e.payload, baton_ref: 'refs/remembra/baton/T-14/7', restored: true } };
    expect(batonText(withRef, s)).toMatchObject({ savedWork: true, restored: true });
    expect(batonText(e, null)).toMatchObject({ from: 'cs_a', to: 'cs_c', zones: ['zn_pos'] });
  });

  it('ages in seconds, minutes and hours, then a date', () => {
    const now = Date.parse('2026-09-25T20:10:00Z');
    expect(ageText('2026-09-25T20:09:57Z', now)).toBe('3s');
    expect(ageText('2026-09-25T20:06:00Z', now)).toBe('4m');
    expect(ageText('2026-09-25T18:10:00Z', now)).toBe('2h');
    expect(ageText('2026-09-20T18:10:00Z', now)).toMatch(/Sep/);
    expect(ageText(null, now)).toBe('');
    expect(ageText('garbage', now)).toBe('');
  });
});

describe('filters', () => {
  const s = state();
  const all = samples();

  it('filters by type prefix groups (chips round-trip through the URL list)', () => {
    const batons = TYPE_GROUPS.find((g) => g.id === 'batons')!;
    const on = toggleGroup(NO_FILTERS, batons);
    expect(on.types).toEqual(['baton.', 'handoff.']);
    expect(groupActive(on, batons)).toBe(true);
    const kept = all.filter((e) => matchesFilters(e, on, s)).map((e) => e.type);
    expect(kept.sort()).toEqual(['baton.passed', 'baton.ref_created', 'baton.restored', 'handoff.created']);
    expect(toggleGroup(on, batons).types).toEqual([]);
  });

  it('covers every L0 type with exactly one chip group', () => {
    for (const e of all) {
      const groups = TYPE_GROUPS.filter((g) => g.prefixes.some((p) => e.type.startsWith(p)));
      expect(groups.length, e.type).toBe(1);
    }
  });

  it('filters by session (id or callsign), including baton ends and collision parties', () => {
    const byCallsign = all.filter((e) => matchesFilters(e, { ...NO_FILTERS, session: 'cc-2' }, s)).map((e) => e.type);
    expect(byCallsign).toContain('baton.passed'); // cc-2 is to_session
    const byB = all.filter((e) => matchesFilters(e, { ...NO_FILTERS, session: 'cs_b' }, s)).map((e) => e.type);
    expect(byB).toContain('collision.detected'); // session_b
    expect(byB).toContain('guard.blocked'); // actor
    expect(byB).not.toContain('crew.created');
  });

  it('filters by zone slug or id, including guard blocks that carry the slug', () => {
    const bySlug = all.filter((e) => matchesFilters(e, { ...NO_FILTERS, zone: 'pos' }, s)).map((e) => e.type);
    const byId = all.filter((e) => matchesFilters(e, { ...NO_FILTERS, zone: 'zn_pos' }, s)).map((e) => e.type);
    expect(bySlug).toEqual(byId);
    for (const t of ['claim.granted', 'guard.blocked', 'collision.detected', 'baton.passed', 'task.done']) expect(bySlug).toContain(t);
    expect(bySlug).not.toContain('message.posted');
  });

  it('filters by task (T-14, 14 or id) and by moments', () => {
    const a = all.filter((e) => matchesFilters(e, { ...NO_FILTERS, task: 'T-14' }, s)).map((e) => e.seq);
    const b = all.filter((e) => matchesFilters(e, { ...NO_FILTERS, task: 'tsk_14' }, s)).map((e) => e.seq);
    const c = all.filter((e) => matchesFilters(e, { ...NO_FILTERS, task: '14' }, s)).map((e) => e.seq);
    expect(a).toEqual(b);
    expect(c).toEqual(b);
    expect(a.length).toBeGreaterThan(5);
    const moments = all.filter((e) => matchesFilters(e, { ...NO_FILTERS, moments: true }, s));
    expect(moments.length).toBeGreaterThan(0);
    expect(moments.every((e) => e.moment)).toBe(true);
  });
});

describe('rows', () => {
  const cp = (seq: number, session = 'cs_a') =>
    ev(seq, {
      type: 'checkpoint.created',
      actor: { kind: 'session', id: session, callsign: session === 'cs_a' ? 'cc-1' : 'codex-1', verified: true },
      refs: { session_id: session },
      payload: { checkpoint: { id: `ckp_${seq}`, session_id: session, task_id: 'tsk_14', trigger: 'commit', headline: `h${seq}`, facts_source: 'relay-cli' } },
    });

  it('is newest first and collapses a run of checkpoints by one session', () => {
    const events = [ev(1), cp(2), cp(3), cp(4), cp(5, 'cs_b'), ev(6), cp(7)];
    const rows = buildRows(events, NO_FILTERS, null);
    expect(rows.map((r) => r.key)).toEqual(['e:7', 'e:6', 'e:5', 'cp:2', 'e:1']);
    const run = rows[3];
    expect(run).toMatchObject({ kind: 'checkpoints', collapsed: true });
    expect(run.events.map((e) => e.seq)).toEqual([4, 3, 2]);
    expect(rowLook(run).label).toBe('3 checkpoints');
  });

  it('keeps a run key stable as new checkpoints join, expands it and finds it again to collapse', () => {
    const base = [cp(2), cp(3)];
    expect(buildRows(base, NO_FILTERS, null)[0].key).toBe('cp:2');
    expect(buildRows([...base, cp(4)], NO_FILTERS, null)[0].key).toBe('cp:2');
    const open = buildRows([...base, cp(4), ev(5)], NO_FILTERS, null, new Set(['cp:2']));
    expect(open.map((r) => r.key)).toEqual(['e:5', 'e:4', 'e:3', 'e:2']);
    expect(runKeyOf(open, 2)).toBe('cp:2');
    expect(runKeyOf(open, 0)).toBeNull();
  });

  it('applies filters before collapsing', () => {
    const rows = buildRows([cp(1), ev(2), cp(3)], { ...NO_FILTERS, types: ['checkpoint.'] }, null);
    expect(rows).toHaveLength(1);
    expect(rows[0]).toMatchObject({ key: 'cp:1', collapsed: true });
  });
});

describe('targets (Enter opens the exact item)', () => {
  const s = state();
  it('routes each kind of row to its screen', () => {
    expect(rowTarget(sample('task.done'), s)).toEqual({ screen: 'report', report: 'rpt_1' });
    expect(rowTarget(sample('report.accepted'), s)).toEqual({ screen: 'report', report: 'rpt_1' });
    expect(rowTarget(sample('collision.detected'), s)).toEqual({ screen: 'zones', zone: 'pos' });
    expect(rowTarget(sample('claim.granted'), s)).toEqual({ screen: 'zones', zone: 'pos' });
    expect(rowTarget(sample('guard.blocked'), s)).toEqual({ screen: 'zones', zone: 'pos' });
    expect(rowTarget(sample('guard.bypass_used'), s)).toEqual({ screen: 'policy' });
    expect(rowTarget(sample('zone.change_pending'), s)).toEqual({ screen: 'policy' });
    expect(rowTarget(sample('message.posted'), s)).toEqual({ screen: 'channel', thread: 'msg_1' });
    expect(rowTarget(sample('decision.proposed'), s)).toMatchObject({ screen: 'channel' });
    expect(rowTarget(sample('checkpoint.created'), s)).toEqual({ screen: 'board', task: 'T-14' });
    expect(rowTarget(sample('baton.passed'), s)).toEqual({ screen: 'board', task: 'T-14' });
    expect(rowTarget(sample('session.quota_blocked'), s)).toEqual({ screen: 'agent', agent: 'claude-code', session: 'cs_a' });
  });

  it('never throws on any contract sample', () => {
    for (const e of samples()) expect(() => rowTarget(e, s)).not.toThrow();
  });
});

describe('windowing and keyboard selection', () => {
  it('renders only the rows in view plus overscan, with spacer heights', () => {
    expect(windowRange(0, 600, 1000, 60, 5)).toEqual({ start: 0, end: 16, padTop: 0, padBottom: 984 * 60 });
    const mid = windowRange(6000, 600, 1000, 60, 5);
    expect(mid).toEqual({ start: 95, end: 116, padTop: 95 * 60, padBottom: (1000 - 116) * 60 });
    expect(windowRange(0, 600, 0, 60)).toEqual({ start: 0, end: 0, padTop: 0, padBottom: 0 });
    const end = windowRange(1e9, 600, 10, 60, 2);
    expect(end.end).toBe(10);
    expect(end.start).toBeLessThanOrEqual(9);
  });

  it('scrolls a selected row into view only when it is outside', () => {
    expect(scrollToReveal(5, 0, 600, 60)).toBeNull();
    expect(scrollToReveal(12, 0, 600, 60)).toBe(13 * 60 - 600);
    expect(scrollToReveal(2, 400, 600, 60)).toBe(120);
  });

  it('moves j/k by row key, clamped at both ends', () => {
    const rows = buildRows([ev(1), ev(2), ev(3)], NO_FILTERS, null);
    expect(moveSelection(rows, null, 1)).toBe('e:3');
    expect(moveSelection(rows, 'e:3', 1)).toBe('e:2');
    expect(moveSelection(rows, 'e:1', 1)).toBe('e:1');
    expect(moveSelection(rows, 'e:3', -1)).toBe('e:3');
    expect(moveSelection(rows, 'gone', -1)).toBe('e:3');
    expect(moveSelection([], null, 1)).toBeNull();
  });
});
