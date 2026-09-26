import { describe, expect, it } from 'vitest';
import { CREW_MAX_MS, DelightGate, crewMotion, prefersReducedMotion } from '../motion';

describe('crew motion presets (reduced set)', () => {
  it('reduced motion: nothing travels, scales or blurs; no packets; the cloud holds still', () => {
    const m = crewMotion(true);
    expect(m.reduced).toBe(true);
    expect(m.packetMs).toBe(0);
    expect(m.ambient).toBe(false);
    expect(m.batonPass).toEqual({ duration: 0 });
    for (const variants of [m.rowEnter, m.pill, m.sheet]) {
      const json = JSON.stringify(variants);
      expect(json).not.toMatch(/"x"|"y"|scale|blur|filter/);
    }
  });

  it('full motion stays within 600 ms', () => {
    const m = crewMotion(false);
    expect(m.batonPass.duration).toBe(CREW_MAX_MS / 1000);
    expect(m.packetMs).toBeLessThanOrEqual(900);
    for (const variants of [m.rowEnter, m.pill, m.sheet]) {
      const animate = variants.animate as { transition?: { duration?: number } };
      expect(animate.transition?.duration ?? 0).toBeLessThanOrEqual(CREW_MAX_MS / 1000);
    }
  });

  it('reads the preference safely', () => {
    expect(prefersReducedMotion(null)).toBe(false);
    expect(prefersReducedMotion({ matchMedia: () => ({ matches: true }) })).toBe(true);
    expect(
      prefersReducedMotion({
        matchMedia: () => {
          throw new Error('no');
        },
      }),
    ).toBe(false);
  });
});

describe('DelightGate (§9.13 rules)', () => {
  function gate(reduced = false) {
    let now = 0;
    const g = new DelightGate({ now: () => now, reduced: () => reduced });
    return { g, tick: (ms: number) => (now += ms) };
  }

  it('one animation at a time, clamped to 600 ms', () => {
    const { g, tick } = gate();
    const first = g.request({ kind: 'baton_pass', ms: 5000 });
    expect(first).toMatchObject({ kind: 'baton_pass', ms: 600 });
    expect(g.request({ kind: 'crew_assembled', ms: 300 })).toBe('busy');
    tick(600);
    expect(g.playing).toBeNull();
    expect(g.request({ kind: 'crew_assembled', ms: 300 })).toMatchObject({ ms: 300 });
  });

  it('none while a needs-you item is open; opening one stops a running delight', () => {
    const { g } = gate();
    g.request({ kind: 'baton_pass', ms: 600 });
    g.setNeedsYouOpen(true);
    expect(g.playing).toBeNull();
    expect(g.request({ kind: 'baton_pass', ms: 600 })).toBe('needs_you_open');
    g.setNeedsYouOpen(false);
    expect(typeof g.request({ kind: 'baton_pass', ms: 600 })).toBe('object');
  });

  it('dismissible, and reduced motion gets the instant form', () => {
    const { g } = gate();
    const grant = g.request({ kind: 'baton_pass', ms: 600 });
    if (typeof grant === 'string') throw new Error('refused');
    grant.dismiss();
    expect(g.playing).toBeNull();
    const reduced = gate(true).g.request({ kind: 'baton_pass', ms: 600 });
    expect(reduced).toMatchObject({ ms: 0 });
  });
});
