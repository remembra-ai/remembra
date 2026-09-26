import { describe, expect, it, vi } from 'vitest';
import { CrewApiError } from '../../../../lib/crew/api';
import { runLaneAction, validateAction, type LaneApi } from '../actions';
import { bayer, BAYER8, cloudDensity, lit, toneLevel, tonesFor } from '../dither';
import { batonText, bezierPath, cubicPoint } from '../transit';

function fakeApi(): LaneApi & { calls: [string, ...unknown[]][] } {
  const calls: [string, ...unknown[]][] = [];
  const rec =
    (name: string) =>
    async (...args: unknown[]) => {
      calls.push([name, ...args]);
      return {};
    };
  return {
    calls,
    requestCheckpoint: rec('requestCheckpoint'),
    pauseSession: rec('pauseSession'),
    resumeSession: rec('resumeSession'),
    releaseAllClaims: rec('releaseAllClaims'),
    overrideClaim: rec('overrideClaim'),
    assignTask: rec('assignTask'),
  } as LaneApi & { calls: [string, ...unknown[]][] };
}

describe('lane actions', () => {
  it('refuse to send without a reason, a target or zones', () => {
    expect(validateAction({ action: 'pause', reason: '  ', sessionId: 'cs_a' })).toMatch(/reason/);
    expect(validateAction({ action: 'pause', reason: 'x'.repeat(281), sessionId: 'cs_a' })).toMatch(/280/);
    expect(validateAction({ action: 'hand-over', reason: 'r', sessionId: 'cs_a', claimIds: ['c'] })).toBe('Choose who gets it.');
    expect(validateAction({ action: 'hand-over', reason: 'r', sessionId: 'cs_a', to: 'cs_b', claimIds: [] })).toBe(
      'Choose at least one zone.',
    );
    expect(validateAction({ action: 'hand-baton', reason: 'r', to: 'cs_b' })).toBe('Nothing to hand over.');
    expect(validateAction({ action: 'checkpoint', reason: 'r' })).toBe('No agent selected.');
    expect(validateAction({ action: 'release-baton', reason: 'r', claimIds: ['c1'] })).toBeNull();
  });

  it('call the right endpoint with the reason and confirm in words', async () => {
    const api = fakeApi();
    const names = { session: 'cc-1', to: 'cc-2' };
    expect(await runLaneAction(api, { action: 'checkpoint', reason: ' why ', sessionId: 'cs_a', names })).toBe(
      'Asked cc-1 for a checkpoint.',
    );
    expect(await runLaneAction(api, { action: 'pause', reason: 'why', sessionId: 'cs_a', names })).toMatch(/^Paused cc-1/);
    expect(await runLaneAction(api, { action: 'resume', reason: 'why', sessionId: 'cs_a', names })).toBe('Resumed cc-1.');
    expect(await runLaneAction(api, { action: 'release-all', reason: 'why', sessionId: 'cs_a', names })).toMatch(
      /Released every claim cc-1/,
    );
    expect(
      await runLaneAction(api, { action: 'hand-over', reason: 'why', sessionId: 'cs_a', to: 'cs_b', claimIds: ['c1', 'c2'], names }),
    ).toBe('Handed 2 zones to cc-2.');
    expect(await runLaneAction(api, { action: 'hand-baton', reason: 'why', taskId: 'tsk_1', to: 'cs_b', names })).toMatch(
      /^Handed the baton to cc-2/,
    );
    expect(await runLaneAction(api, { action: 'hand-baton', reason: 'why', claimIds: ['c3'], to: 'cs_b', names })).toBe(
      'Handed the held zones to cc-2.',
    );
    expect(await runLaneAction(api, { action: 'release-baton', reason: 'why', claimIds: ['c4'] })).toMatch(/^Released the zone/);
    expect(api.calls).toEqual([
      ['requestCheckpoint', 'cs_a', 'why'],
      ['pauseSession', 'cs_a', 'why'],
      ['resumeSession', 'cs_a', 'why'],
      ['releaseAllClaims', 'cs_a', 'why'],
      ['overrideClaim', 'c1', { action: 'transfer', to: 'cs_b', reason: 'why' }],
      ['overrideClaim', 'c2', { action: 'transfer', to: 'cs_b', reason: 'why' }],
      ['assignTask', 'tsk_1', 'cs_b'],
      ['overrideClaim', 'c3', { action: 'transfer', to: 'cs_b', reason: 'why' }],
      ['overrideClaim', 'c4', { action: 'revoke', reason: 'why' }],
    ]);
  });

  it('surface server refusals and never call the API for invalid input', async () => {
    const api = fakeApi();
    await expect(runLaneAction(api, { action: 'pause', reason: '', sessionId: 'cs_a' })).rejects.toThrow(/reason/);
    expect(api.calls).toHaveLength(0);
    api.pauseSession = vi.fn(async () => {
      throw new CrewApiError('Only a human', 403, 'human_only');
    });
    await expect(runLaneAction(api, { action: 'pause', reason: 'r', sessionId: 'cs_a' })).rejects.toMatchObject({ humanOnly: true });
  });
});

describe('baton transit', () => {
  it('draws an arc that stays inside the column and starts and ends on the lanes', () => {
    const c = bezierPath({ x: 500, y: 100 }, { x: 24, y: 300 }, 520);
    expect(c.d.startsWith('M500 100 C')).toBe(true);
    expect(c.d.endsWith('24 300')).toBe(true);
    for (const p of [c.p1, c.p2]) {
      expect(p.x).toBeGreaterThanOrEqual(8);
      expect(p.x).toBeLessThanOrEqual(512);
    }
    expect(cubicPoint(c, 0)).toEqual({ x: 500, y: 100 });
    const end = cubicPoint(c, 1);
    expect(end.x).toBeCloseTo(24, 6);
    expect(end.y).toBeCloseTo(300, 6);
  });

  it('words the pass, with the restore result', () => {
    const names: Record<string, string> = { cs_a: 'cc-1', cs_b: 'cc-3' };
    const name = (sid: string | null | undefined) => names[sid ?? ''] ?? 'someone';
    expect(batonText({ seq: 1, ts: null, from_session: 'cs_a', to_session: 'cs_b', kind: 'adopt', restored: true }, name)).toBe(
      'Baton passed cc-1 → cc-3 (adopted) · work restored ✓',
    );
    expect(
      batonText(
        { seq: 2, ts: null, from_session: null, to_session: 'cs_b', kind: 'human_assign', restored: false, baton_ref: 'refs/x' },
        name,
      ),
    ).toBe('Baton passed a reserved slot → cc-3 (handed over by you) · saved work not restored yet');
  });
});

describe('dithering', () => {
  it('uses a full 8x8 Bayer matrix of distinct thresholds in (0, 1)', () => {
    expect(new Set(BAYER8).size).toBe(64);
    expect(Math.min(...BAYER8)).toBeGreaterThan(0);
    expect(Math.max(...BAYER8)).toBeLessThan(1);
    expect(bayer(8, 8)).toBe(bayer(0, 0));
  });

  it('lights more cells as density rises, and none at zero', () => {
    const count = (v: number) => {
      let n = 0;
      for (let j = 0; j < 8; j += 1) for (let i = 0; i < 8; i += 1) if (lit(v, i, j)) n += 1;
      return n;
    };
    expect(count(0)).toBe(0);
    expect(count(0.5)).toBe(32);
    expect(count(1)).toBe(64);
    expect(toneLevel(0, 1, 1)).toBe(0);
    expect(toneLevel(1, 1, 1)).toBe(3);
  });

  it('keeps clouds where each shape puts them', () => {
    const banks = (x: number, y: number) => cloudDensity('banks', x, y, 600, 200, 0, 0);
    // nothing in the middle band of a header, where the title sits
    expect(banks(100, 100)).toBe(0);
    const right = (x: number) => cloudDensity('right', x, 50, 600, 100, 0, 0);
    expect(right(10)).toBe(0);
    expect(tonesFor(true).levels).not.toEqual(tonesFor(false).levels);
  });
});
