import { describe, expect, it } from 'vitest';
import { parsePlanIntent, planIntentNote, rememberPlanIntent, takePlanIntent } from '../planIntent';

function memory() {
  const map = new Map<string, string>();
  return {
    getItem: (k: string) => map.get(k) ?? null,
    setItem: (k: string, v: string) => void map.set(k, v),
    removeItem: (k: string) => void map.delete(k),
  };
}

describe('plan intent from the pricing page', () => {
  it('accepts every plan the pricing page links, founding and solo included', () => {
    for (const plan of ['founding', 'solo', 'pro', 'team']) expect(parsePlanIntent(`?plan=${plan}`)).toBe(plan);
    expect(parsePlanIntent('?plan=enterprise')).toBeNull();
    expect(parsePlanIntent('')).toBeNull();
  });

  it('survives signup and sign-in once', () => {
    const store = memory();
    rememberPlanIntent('founding', store);
    expect(takePlanIntent(store)).toBe('founding');
    expect(takePlanIntent(store)).toBeNull();
  });

  it('tells the buyer where the pick is, and when the Founding offer is not open', () => {
    expect(planIntentNote('founding', true)).toContain('claim your seat below');
    expect(planIntentNote('founding', false)).toContain('no seat is open now');
    expect(planIntentNote('pro', true)).toBe('You picked Pro on the pricing page: choose it below.');
    expect(planIntentNote(null, true)).toBeNull();
    expect(planIntentNote('bogus', true)).toBeNull();
  });
});
