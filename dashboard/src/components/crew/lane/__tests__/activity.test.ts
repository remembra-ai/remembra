import { describe, expect, it } from 'vitest';
import { buildStrip, marksFor, mergeEvents, primaryMark, touches } from '../activity';
import { NOW, event } from './fixture';

describe('activity marks', () => {
  it('maps each event kind to its glyph for the session it concerns', () => {
    expect(marksFor(event('checkpoint.created', 1), 'cs_a')).toEqual(['checkpoint']);
    expect(marksFor(event('checkpoint.created', 1), 'cs_b')).toEqual([]);
    expect(marksFor(event('guard.blocked', 1, { refs: { session_id: 'cs_b' } }), 'cs_b')).toEqual(['guard']);
    expect(marksFor(event('guard.tamper_blocked', 1), 'cs_a')).toEqual(['tamper']);
    expect(marksFor(event('activity.push', 1), 'cs_a')).toEqual(['push']);
    expect(marksFor(event('activity.test_verdict_changed', 1, { payload: { to: 'fail', failed: 2 } }), 'cs_a')).toEqual(['fail']);
    expect(marksFor(event('activity.test_verdict_changed', 1, { payload: { to: 'pass', failed: 0 } }), 'cs_a')).toEqual([]);
    expect(marksFor(event('activity.burst', 1, { payload: { tests: { pass: 3, fail: 1 } } }), 'cs_a')).toEqual(['fail']);
    const done = event('task.done', 1, {
      actor: { kind: 'human', id: 'u_1', verified: true },
      payload: { task: { owner_session_id: 'cs_b' } },
    });
    expect(marksFor(done, 'cs_b')).toEqual(['done']);
    const pass = event('baton.passed', 1, {
      actor: { kind: 'system', id: 'sys', verified: true },
      payload: { from_session: 'cs_c', to_session: 'cs_a' },
    });
    expect(marksFor(pass, 'cs_c')).toEqual(['baton_out']);
    expect(marksFor(pass, 'cs_a')).toEqual(['baton_in']);
    expect(touches(pass, 'cs_c')).toBe(true);
    expect(touches(pass, 'cs_b')).toBe(false);
  });

  it('picks alarms first when several marks share a minute', () => {
    expect(primaryMark(['checkpoint', 'guard'])).toBe('guard');
    expect(primaryMark(['push', 'baton_in'])).toBe('baton_in');
    expect(primaryMark([])).toBeNull();
  });
});

describe('the 60-minute strip', () => {
  it('buckets by minute, counts density and summarises for screen readers', () => {
    const events = [
      event('checkpoint.created', 0.2),
      event('activity.commit', 0.5),
      event('guard.blocked', 12),
      event('checkpoint.created', 25),
      event('checkpoint.created', 61), // outside the window
      event('checkpoint.created', 3, { refs: { session_id: 'cs_b' }, actor: { kind: 'session', id: 'cs_b', verified: false } }),
    ];
    const strip = buildStrip(events, 'cs_a', NOW);
    expect(strip.buckets).toHaveLength(60);
    const now = strip.buckets[59];
    expect(now).toMatchObject({ minutesAgo: 0, count: 2, marks: ['checkpoint'] });
    expect(strip.buckets[59 - 12].marks).toEqual(['guard']);
    expect(strip.total).toBe(4);
    expect(strip.checkpointTimes).toHaveLength(2);
    expect(strip.checkpointTimes[0]).toBeGreaterThan(strip.checkpointTimes[1]);
    expect(strip.summary).toBe('2 checkpoints, 1 guard block in the last hour');
    expect(buildStrip([], 'cs_a', NOW).summary).toBe('no activity in the last hour');
  });

  it('counts an event stamped a few seconds in the future as now (clock skew)', () => {
    const strip = buildStrip([event('checkpoint.created', -0.2)], 'cs_a', NOW);
    expect(strip.buckets[59].count).toBe(1);
    expect(buildStrip([event('checkpoint.created', -5)], 'cs_a', NOW).total).toBe(0);
  });

  it('merges pages by seq, keeps order and drops what left the window', () => {
    const a = event('checkpoint.created', 1, { seq: 10 });
    const b = event('guard.blocked', 2, { seq: 11 });
    const old = event('guard.blocked', 90, { seq: 3 });
    const merged = mergeEvents([b], [a, old, b], NOW);
    expect(merged.map((e) => e.seq)).toEqual([10, 11]);
    expect(mergeEvents(merged, [a], NOW)).toBe(merged); // unchanged: same array
    expect(mergeEvents(merged, [], NOW)).toBe(merged);
  });
});
