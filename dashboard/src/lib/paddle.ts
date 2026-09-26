// Paddle checkout plumbing shared by Billing and the /pay page.
//
// - /pay is Paddle's default payment link: Paddle sends buyers there with
//   ?_ptxn=<transaction id> (server-created checkouts, dunning emails,
//   payment-method updates), and Paddle.js opens that transaction's checkout
//   once it is initialized on the page.
// - After a successful payment Paddle.js sends the buyer to the dashboard home
//   with ?checkout=success; the dashboard then waits for the webhook to move
//   the account to the paid plan and says which plan it is on.

import type { BillingClientConfigResponse, UsageSummaryResponse } from './api';

export interface PaddleLike {
  Initialized?: boolean;
  Environment?: { set: (env: 'sandbox' | 'production') => void };
  Initialize: (opts: { token: string; checkout?: { settings: Record<string, unknown> } }) => void;
  Checkout: { open: (opts: Record<string, unknown>) => void };
}

/** window.Paddle once paddle.js has loaded, else null. */
export function paddleGlobal(win: unknown = typeof window === 'undefined' ? undefined : window): PaddleLike | null {
  const p = (win as { Paddle?: PaddleLike } | undefined)?.Paddle;
  return p && typeof p.Checkout?.open === 'function' && typeof p.Initialize === 'function' ? p : null;
}

/** Paddle client-side tokens start with test_ in the sandbox and live_ in production. */
export function paddleEnvironment(token: string): 'sandbox' | 'production' {
  return token.startsWith('test_') ? 'sandbox' : 'production';
}

/** Where Paddle.js sends a buyer after paying: the server's value, else this dashboard's home. */
export function successUrl(config: Pick<BillingClientConfigResponse, 'success_url'> | null, origin: string): string {
  const fromServer = config?.success_url?.trim();
  return fromServer || `${origin.replace(/\/+$/, '')}/?checkout=success`;
}

/**
 * Initialize Paddle.js once per page with the server's client token.
 * Returns false when there is nothing to initialize with (Paddle is not the
 * provider, or the server has no client token).
 */
export function initPaddle(p: PaddleLike, config: BillingClientConfigResponse | null, origin: string): boolean {
  if (!config || config.provider !== 'paddle' || !config.client_token) return false;
  if (p.Initialized) return true;
  if (paddleEnvironment(config.client_token) === 'sandbox') p.Environment?.set('sandbox');
  p.Initialize({ token: config.client_token, checkout: { settings: { successUrl: successUrl(config, origin) } } });
  p.Initialized = true;
  return true;
}

const TXN_ID = /^txn_[a-z0-9]{10,64}$/;

/** The transaction a payment link carries (?_ptxn=txn_...), or null when it is missing or malformed. */
export function paymentLinkTransaction(search: string): string | null {
  const value = new URLSearchParams(search).get('_ptxn');
  return value && TXN_ID.test(value) ? value : null;
}

/** True when Paddle sent the buyer back after a successful checkout. */
export function isCheckoutReturn(search: string): boolean {
  return new URLSearchParams(search).get('checkout') === 'success';
}

/** The same URL without ?checkout=success, so a reload does not announce the payment again. */
export function withoutCheckoutParam(href: string): string {
  const url = new URL(href);
  url.searchParams.delete('checkout');
  return url.pathname + (url.search === '?' ? '' : url.search) + url.hash;
}

export type PlanSummary = Pick<UsageSummaryResponse, 'plan' | 'plan_name'>;

/**
 * Poll the usage summary until the account is on a paid plan. The webhook that
 * upgrades it can arrive a few seconds after Paddle redirects the buyer.
 * Returns the paid summary, or null if it did not change in time.
 */
export async function waitForPaidPlan(
  load: () => Promise<PlanSummary>,
  { tries = 6, delayMs = 2500, sleep = (ms: number) => new Promise<void>((r) => setTimeout(r, ms)) } = {},
): Promise<PlanSummary | null> {
  for (let attempt = 0; attempt < tries; attempt += 1) {
    try {
      const summary = await load();
      if (summary.plan && summary.plan !== 'free') return summary;
    } catch {
      // A failed read is retried like a not-yet-upgraded plan.
    }
    if (attempt < tries - 1) await sleep(delayMs);
  }
  return null;
}

/** What the dashboard says once the checkout return has been checked. */
export function checkoutMessage(summary: PlanSummary | null): { tone: 'success' | 'info'; text: string } {
  if (summary) {
    const name = summary.plan_name?.trim() || summary.plan.charAt(0).toUpperCase() + summary.plan.slice(1);
    return { tone: 'success', text: `Payment received. You're on ${name}.` };
  }
  return {
    tone: 'info',
    text: 'Payment received. Your plan can take a minute to update; Billing shows it as soon as Paddle confirms.',
  };
}
