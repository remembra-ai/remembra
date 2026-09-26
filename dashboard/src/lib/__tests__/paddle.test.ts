import { describe, expect, it, vi } from 'vitest';
import {
  SAME_PLAN_CHECKOUT,
  UNNOTED_CHECKOUT,
  checkoutMessage,
  confirmCheckout,
  initPaddle,
  isCheckoutReturn,
  paddleEnvironment,
  paddleGlobal,
  paymentLinkTransaction,
  planAfterCheckout,
  rememberCheckout,
  sessionStore,
  successUrl,
  takeCheckoutIntent,
  waitForPlan,
  withoutCheckoutParam,
  type PaddleLike,
  type PlanSummary,
} from '../paddle';

function memoryStorage() {
  const data = new Map<string, string>();
  return {
    data,
    getItem: (k: string) => data.get(k) ?? null,
    setItem: (k: string, v: string) => void data.set(k, v),
    removeItem: (k: string) => void data.delete(k),
  };
}

/** A usage summary that reads `plans` in turn, then keeps returning the last one. */
function summaries(...plans: string[]) {
  const queue = [...plans];
  return vi.fn(async (): Promise<PlanSummary> => {
    const plan = (queue.length > 1 ? queue.shift() : queue[0]) as string;
    return { plan, plan_name: plan.charAt(0).toUpperCase() + plan.slice(1) };
  });
}

const FAST = { tries: 6, delayMs: 1, sleep: async () => {} };
const T0 = 1_790_000_000_000;

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

  it('waits for the webhook to move the account to the plan that was bought', async () => {
    const load = summaries('free', 'free', 'solo');
    const sleep = vi.fn(async () => {});
    const summary = await waitForPlan(load, 'solo', { tries: 5, delayMs: 10, sleep });
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
    const summary = await waitForPlan(load, 'pro', { tries: 3, delayMs: 10, sleep });
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

describe('an upgrade confirms the new plan, never the old one', () => {
  it('solo -> pro: waits past the reads that still say Solo', async () => {
    const store = memoryStorage();
    rememberCheckout(store, 'solo', 'pro', T0);
    const load = summaries('solo', 'solo', 'pro');
    const notice = await confirmCheckout(load, takeCheckoutIntent(store, T0 + 30_000), FAST);
    expect(notice).toEqual({ tone: 'success', text: "Payment received. You're on Pro." });
    expect(load).toHaveBeenCalledTimes(3);
    expect(store.data.size).toBe(0); // read once, then gone: a reload does not reuse it
  });

  it('solo -> pro: says it is still confirming when the webhook is late, not "You\'re on Solo"', async () => {
    const store = memoryStorage();
    rememberCheckout(store, 'solo', 'pro', T0);
    const load = summaries('solo');
    const notice = await confirmCheckout(load, takeCheckoutIntent(store, T0 + 1_000), FAST);
    expect(notice.tone).toBe('info');
    expect(notice.text).not.toMatch(/You're on/);
    expect(load).toHaveBeenCalledTimes(FAST.tries);
  });

  it('free -> solo still works, and a Founding 100 seat is confirmed as Solo', async () => {
    const store = memoryStorage();
    rememberCheckout(store, 'free', 'solo', T0);
    expect(await confirmCheckout(summaries('free', 'solo'), takeCheckoutIntent(store, T0), FAST)).toEqual({
      tone: 'success',
      text: "Payment received. You're on Solo.",
    });
    rememberCheckout(store, 'free', 'founding', T0);
    expect(takeCheckoutIntent(store, T0)).toEqual({ from: 'free', to: 'solo', at: T0 });
    expect(planAfterCheckout('team')).toBe('team');
  });

  it('pro -> team never stops at a different paid plan', async () => {
    const store = memoryStorage();
    rememberCheckout(store, 'pro', 'team', T0);
    const load = summaries('pro', 'solo', 'team');
    const notice = await confirmCheckout(load, takeCheckoutIntent(store, T0), FAST);
    expect(notice.text).toBe("Payment received. You're on Team.");
  });

  it('names no plan without a note (payment link, payment-method update), and reads nothing', async () => {
    const load = summaries('solo');
    expect(await confirmCheckout(load, takeCheckoutIntent(memoryStorage(), T0), FAST)).toEqual(UNNOTED_CHECKOUT);
    expect(await confirmCheckout(load, takeCheckoutIntent(null, T0), FAST)).toEqual(UNNOTED_CHECKOUT);
    expect(UNNOTED_CHECKOUT.text).not.toMatch(/Payment received|You're on/);
    expect(load).not.toHaveBeenCalled();
  });

  it('a new billing cycle on the same plan is not announced as a plan', async () => {
    const store = memoryStorage();
    rememberCheckout(store, 'solo', 'solo', T0);
    const load = summaries('solo');
    expect(await confirmCheckout(load, takeCheckoutIntent(store, T0), FAST)).toEqual(SAME_PLAN_CHECKOUT);
    expect(load).not.toHaveBeenCalled();
  });

  it('ignores a stale, future-dated or malformed note', () => {
    const store = memoryStorage();
    rememberCheckout(store, 'solo', 'pro', T0);
    expect(takeCheckoutIntent(store, T0 + 61 * 60_000)).toBeNull();
    rememberCheckout(store, 'solo', 'pro', T0);
    expect(takeCheckoutIntent(store, T0 - 1)).toBeNull();
    for (const raw of ['not json', 'null', '{"from":"solo","to":"pro"}', '{"from":"solo","to":"<b>","at":1}', '{"from":1,"to":"pro","at":1}']) {
      store.setItem('remembra.checkout', raw);
      expect(takeCheckoutIntent(store, T0)).toBeNull();
      expect(store.data.size).toBe(0);
    }
  });

  it('survives storage that throws, on write and on read', async () => {
    const broken = {
      getItem: () => {
        throw new Error('SecurityError');
      },
      setItem: () => {
        throw new Error('QuotaExceededError');
      },
      removeItem: () => {
        throw new Error('SecurityError');
      },
    };
    expect(() => rememberCheckout(broken, 'solo', 'pro', T0)).not.toThrow();
    expect(takeCheckoutIntent(broken, T0)).toBeNull();
    expect(sessionStore(undefined)).toBeNull();
    const blocked = Object.defineProperty({}, 'sessionStorage', {
      get() {
        throw new Error('SecurityError');
      },
    });
    expect(sessionStore(blocked)).toBeNull();
    const store = memoryStorage();
    expect(sessionStore({ sessionStorage: store })).toBe(store);
  });
});
