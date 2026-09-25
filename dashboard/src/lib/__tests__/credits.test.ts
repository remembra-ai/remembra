import { describe, expect, it } from 'vitest';
import type { UsageSummaryResponse } from '../api';
import { clampSeats, creditsView, degradedCopy, formatUsd, planLine, resetLabel } from '../credits';

function summary(overrides: Partial<UsageSummaryResponse> = {}, credits: Partial<UsageSummaryResponse['credits']> = {}): UsageSummaryResponse {
  return {
    plan: 'free',
    plan_name: 'Relay Free',
    interval: 'month',
    seats: 1,
    founding: false,
    email_verified: true,
    period: { key: 'M:2026-09', type: 'month', start: '2026-09-01T00:00:00+00:00', end: '2026-10-01T00:00:00+00:00' },
    credits: {
      limit: 500,
      used: 100,
      reserved: 16,
      remaining: 384,
      bank: 'monthly',
      llm_usd_used: 0.25,
      ceiling_usd: 1.25,
      unverified_cap_applied: false,
      ...credits,
    },
    enrichment: { status: 'full', reason: null },
    relay_events: { this_month: 12, soft_cap: 5000, over_soft_cap: false, burst_per_min: 30, free: true },
    recalls: { this_month: 17, limit: 10000, burst_per_min: 20 },
    memories: { stored: 34, cap: 25000 },
    stores: { this_month: 3, degraded_this_month: 0 },
    ...overrides,
  };
}

describe('creditsView', () => {
  it('splits spent and held credits', () => {
    const v = creditsView(summary());
    expect(v.usedFraction).toBeCloseTo(0.2);
    expect(v.reservedFraction).toBeCloseTo(0.032);
    expect(v.remaining).toBe(384);
    expect(v.low).toBe(false);
    expect(v.degraded).toBe(false);
  });

  it('flags low credits at 10% left, but not once degraded', () => {
    expect(creditsView(summary({}, { used: 460, reserved: 0, remaining: 40 })).low).toBe(true);
    const out = creditsView(summary({ enrichment: { status: 'degraded', reason: 'credits_exhausted' } }, { used: 500, reserved: 0, remaining: 0 }));
    expect(out.low).toBe(false);
    expect(out.degraded).toBe(true);
    expect(out.usedFraction).toBe(1);
  });

  it('never overflows the bar or divides by zero', () => {
    const over = creditsView(summary({}, { used: 700, reserved: 50, remaining: -250 }));
    expect(over.usedFraction).toBe(1);
    expect(over.reservedFraction).toBe(0);
    expect(over.remaining).toBe(0);
    const zero = creditsView(summary({}, { limit: 0, used: 0, reserved: 0, remaining: 0 }));
    expect(zero.usedFraction).toBe(0);
    expect(zero.low).toBe(false);
  });
});

describe('resetLabel', () => {
  it('names the monthly reset day', () => {
    expect(resetLabel(summary())).toMatch(/^Resets .*1/);
  });
  it('describes the yearly bank', () => {
    const s = summary(
      { interval: 'year', period: { key: 'Y:2026-09-01', type: 'year', start: '2026-09-01T00:00:00+00:00', end: '2027-09-01T00:00:00+00:00' } },
      { bank: 'yearly' },
    );
    expect(resetLabel(s)).toMatch(/^Yearly bank, renews .*2027/);
  });
  it('survives a bad date', () => {
    expect(resetLabel(summary({ period: { key: 'x', type: 'month', start: 'x', end: 'nope' } }))).toBe('Monthly allowance');
  });
});

describe('degradedCopy', () => {
  it('is null while enrichment runs', () => {
    expect(degradedCopy(summary())).toBeNull();
  });
  it('explains running out of credits and that relay keeps working', () => {
    const copy = degradedCopy(summary({ enrichment: { status: 'degraded', reason: 'credits_exhausted' } }));
    expect(copy?.title).toMatch(/Out of smart credits/);
    expect(copy?.body).toMatch(/still save/);
    expect(copy?.body).toMatch(/Relay/);
  });
  it('explains the free-tier pause', () => {
    const copy = degradedCopy(summary({ enrichment: { status: 'degraded', reason: 'free_breaker_open' } }));
    expect(copy?.title).toMatch(/paused/);
    expect(copy?.body).toMatch(/Paid plans are never paused/);
  });
});

describe('planLine', () => {
  it('reads the plan, seats and cycle', () => {
    expect(planLine(summary())).toBe('Relay Free');
    expect(planLine(summary({ plan: 'team', plan_name: 'Team', seats: 4 }))).toBe('Team, 4 seats, monthly');
    expect(planLine(summary({ plan: 'solo', plan_name: 'Solo', interval: 'year', founding: true }))).toBe('Solo, yearly, Founding 100');
  });
});

describe('seats and prices', () => {
  it('keeps Team at or above its seat minimum', () => {
    expect(clampSeats(1, 3)).toBe(3);
    expect(clampSeats(2.6, 3)).toBe(3);
    expect(clampSeats(7, 3)).toBe(7);
    expect(clampSeats(Number.NaN, 3)).toBe(3);
    expect(clampSeats(5000, 3)).toBe(1000);
  });
  it('formats cents', () => {
    expect(formatUsd(1200)).toBe('$12');
    expect(formatUsd(1250)).toBe('$12.50');
    expect(formatUsd(15000)).toBe('$150');
  });
});
