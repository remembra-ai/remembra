import { describe, expect, it } from 'vitest';
import { checkoutRoute, planRowAction } from '../checkout';
import type { BillingClientConfigResponse } from '../api';

const config = (extra: Partial<BillingClientConfigResponse> = {}): BillingClientConfigResponse => ({
  provider: 'paddle',
  client_token: 'live_tok',
  prices: { solo: 'pri_solo_m', solo_annual: 'pri_solo_y', pro: 'pri_pro_m' },
  checkout_binding: 'sig-for-u1',
  has_subscription: false,
  ...extra,
});

describe('checkoutRoute', () => {
  it('opens the overlay with the signed account id', () => {
    expect(checkoutRoute(config(), 'solo', 'yearly', false, 'u1')).toEqual({
      kind: 'overlay',
      priceId: 'pri_solo_y',
      customData: { remembra_user_id: 'u1', remembra_binding: 'sig-for-u1', plan: 'solo' },
    });
  });

  it('falls back to the server transaction without a signature, a price or a user', () => {
    expect(checkoutRoute(config({ checkout_binding: null }), 'solo', 'monthly', false, 'u1')).toEqual({ kind: 'server' });
    expect(checkoutRoute(config(), 'pro', 'yearly', false, 'u1')).toEqual({ kind: 'server' });
    expect(checkoutRoute(config(), 'solo', 'monthly', false, '')).toEqual({ kind: 'server' });
    expect(checkoutRoute(null, 'solo', 'monthly', false, 'u1')).toEqual({ kind: 'server' });
    expect(checkoutRoute(config({ provider: 'none' }), 'solo', 'monthly', false, 'u1')).toEqual({ kind: 'server' });
  });

  it('never uses a client price for per-seat Team or Founding 100', () => {
    const prices = { team: 'pri_team_m', founding_annual: 'pri_f' };
    expect(checkoutRoute(config({ prices }), 'team', 'monthly', true, 'u1')).toEqual({ kind: 'server' });
    expect(checkoutRoute(config({ prices }), 'founding', 'yearly', false, 'u1')).toEqual({ kind: 'server' });
  });

  it('sends subscribers to the portal instead of a second subscription', () => {
    expect(checkoutRoute(config({ has_subscription: true, prices: {} }), 'pro', 'monthly', false, 'u1')).toEqual({ kind: 'portal' });
  });
});

describe('planRowAction', () => {
  it('offers the portal on every other plan while subscribed, including legacy tiers', () => {
    expect(planRowAction('solo', 'solo', true)).toBe('current');
    expect(planRowAction('pro', 'legacy_pro_49', true)).toBe('manage');
    expect(planRowAction('team', 'legacy_team_199', true)).toBe('manage');
    expect(planRowAction('pro', 'free', false)).toBe('buy');
    expect(planRowAction('solo', 'pro', false)).toBe('buy'); // promo trial: no subscription yet
  });
});
