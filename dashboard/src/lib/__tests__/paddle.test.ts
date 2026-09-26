import { describe, expect, it, vi } from 'vitest';
import {
  checkoutMessage,
  initPaddle,
  isCheckoutReturn,
  paddleEnvironment,
  paddleGlobal,
  paymentLinkTransaction,
  successUrl,
  waitForPaidPlan,
  withoutCheckoutParam,
  type PaddleLike,
  type PlanSummary,
} from '../paddle';

function fakePaddle(): PaddleLike & { calls: string[]; init: unknown[]; env: string[] } {
  const calls: string[] = [];
  const init: unknown[] = [];
  const env: string[] = [];
  return {
    calls,
    init,
    env,
    Environment: { set: (e) => { env.push(e); calls.push('env'); } },
    Initialize: (opts) => { init.push(opts); calls.push('init'); },
    Checkout: { open: () => calls.push('open') },
  };
}

const CONFIG = { provider: 'paddle', client_token: 'live_abc', prices: {}, success_url: 'https://app.remembra.dev/?checkout=success' };

describe('payment link (/pay?_ptxn=)', () => {
  it('reads a well-formed transaction id and rejects anything else', () => {
    expect(paymentLinkTransaction('?_ptxn=txn_01hv8x2kq3m9zq4w5e6r7t8y9u')).toBe('txn_01hv8x2kq3m9zq4w5e6r7t8y9u');
    expect(paymentLinkTransaction('')).toBeNull();
    expect(paymentLinkTransaction('?_ptxn=')).toBeNull();
    expect(paymentLinkTransaction('?_ptxn=pri_01hv8x2kq3m9zq4w5e6r7t8y9u')).toBeNull();
    expect(paymentLinkTransaction('?_ptxn=txn_<script>')).toBeNull();
    expect(paymentLinkTransaction('?_ptxn=txn_short')).toBeNull();
  });
});

describe('initPaddle', () => {
  it('initializes once with the client token and the success URL', () => {
    const p = fakePaddle();
    expect(initPaddle(p, CONFIG, 'https://app.remembra.dev')).toBe(true);
    expect(initPaddle(p, CONFIG, 'https://app.remembra.dev')).toBe(true);
    expect(p.init).toEqual([{ token: 'live_abc', checkout: { settings: { successUrl: 'https://app.remembra.dev/?checkout=success' } } }]);
    expect(p.env).toEqual([]);
  });

  it('switches Paddle.js to the sandbox for a test_ token, before initializing', () => {
    const p = fakePaddle();
    initPaddle(p, { ...CONFIG, client_token: 'test_abc' }, 'https://app.remembra.dev');
    expect(p.calls).toEqual(['env', 'init']);
    expect(p.env).toEqual(['sandbox']);
    expect(paddleEnvironment('live_x')).toBe('production');
  });

  it('does nothing without a Paddle config or a token', () => {
    const p = fakePaddle();
    expect(initPaddle(p, null, 'https://app.remembra.dev')).toBe(false);
    expect(initPaddle(p, { ...CONFIG, provider: 'none' }, 'https://app.remembra.dev')).toBe(false);
    expect(initPaddle(p, { ...CONFIG, client_token: undefined }, 'https://app.remembra.dev')).toBe(false);
    expect(p.calls).toEqual([]);
  });

  it('falls back to this dashboard for the success URL', () => {
    expect(successUrl(null, 'https://dash.example.com/')).toBe('https://dash.example.com/?checkout=success');
    expect(successUrl({ success_url: '  ' }, 'https://dash.example.com')).toBe('https://dash.example.com/?checkout=success');
    expect(successUrl({ success_url: 'https://app.remembra.dev/?checkout=success' }, 'https://x')).toBe('https://app.remembra.dev/?checkout=success');
  });

  it('finds window.Paddle only once paddle.js has loaded', () => {
    expect(paddleGlobal({})).toBeNull();
    expect(paddleGlobal({ Paddle: { Checkout: {} } })).toBeNull();
    const p = fakePaddle();
    expect(paddleGlobal({ Paddle: p })).toBe(p);
    expect(paddleGlobal(undefined)).toBeNull();
  });
});

describe('the checkout return (/?checkout=success)', () => {
  it('is recognised and removed from the URL, keeping the rest', () => {
    expect(isCheckoutReturn('?checkout=success')).toBe(true);
    expect(isCheckoutReturn('?checkout=cancelled')).toBe(false);
    expect(isCheckoutReturn('')).toBe(false);
    expect(withoutCheckoutParam('https://app.remembra.dev/?checkout=success')).toBe('/');
    expect(withoutCheckoutParam('https://app.remembra.dev/?checkout=success&ref=hn#/billing')).toBe('/?ref=hn#/billing');
  });

  it('waits for the webhook to move the account to a paid plan', async () => {
    const plans: PlanSummary[] = [
      { plan: 'free', plan_name: 'Free' },
      { plan: 'free', plan_name: 'Free' },
      { plan: 'solo', plan_name: 'Solo' },
    ];
    const load = vi.fn(async () => plans.shift() as PlanSummary);
    const sleep = vi.fn(async () => {});
    const summary = await waitForPaidPlan(load, { tries: 5, delayMs: 10, sleep });
    expect(summary).toEqual({ plan: 'solo', plan_name: 'Solo' });
    expect(load).toHaveBeenCalledTimes(3);
    expect(sleep).toHaveBeenCalledTimes(2);
    expect(checkoutMessage(summary)).toEqual({ tone: 'success', text: "Payment received. You're on Solo." });
  });

  it('retries failed reads and gives up without claiming a plan', async () => {
    const load = vi.fn(async (): Promise<PlanSummary> => {
      throw new Error('offline');
    });
    const sleep = vi.fn(async () => {});
    const summary = await waitForPaidPlan(load, { tries: 3, delayMs: 10, sleep });
    expect(summary).toBeNull();
    expect(load).toHaveBeenCalledTimes(3);
    expect(sleep).toHaveBeenCalledTimes(2);
    const message = checkoutMessage(summary);
    expect(message.tone).toBe('info');
    expect(message.text).not.toMatch(/You're on/);
  });

  it('names the plan from its id when the summary has no display name', () => {
    expect(checkoutMessage({ plan: 'pro', plan_name: '' }).text).toBe("Payment received. You're on Pro.");
  });
});
