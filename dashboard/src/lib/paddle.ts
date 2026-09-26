// Paddle checkout plumbing shared by Billing and the /pay page.
//
// - /pay is Paddle's default payment link: Paddle sends buyers there with
//   ?_ptxn=<transaction id> (server-created checkouts, dunning emails,
//   payment-method updates), and Paddle.js opens that transaction's checkout
//   once it is initialized on the page.
// - After a successful payment Paddle.js sends the buyer to the dashboard home
//   with ?checkout=success. A checkout started from Billing leaves a note in
//   sessionStorage (the plan the account was on and the plan being bought);
//   the dashboard then waits for the webhook to move the account to that plan
//   and says which plan it is on. Without the note (a payment link, a
//   payment-method update) it names no plan: the account may already have
//   been on a paid plan, and the first read would announce the old one.

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

/** A checkout started from Billing: the plan the account was on, and the plan it is buying. */
export interface CheckoutIntent {
  from: string;
  to: string;
  at: number;
}

type IntentStorage = Pick<Storage, 'getItem' | 'setItem' | 'removeItem'>;

const INTENT_KEY = 'remembra.checkout';
/** A checkout that returns later than this is not trusted to be the one noted. */
const INTENT_MAX_AGE_MS = 60 * 60 * 1000;
const PLAN_ID = /^[a-z][a-z0-9_]{0,31}$/;

/** This tab's sessionStorage, or null where the browser refuses it. */
export function sessionStore(win: unknown = typeof window === 'undefined' ? undefined : window): IntentStorage | null {
  try {
    return (win as { sessionStorage?: IntentStorage } | undefined)?.sessionStorage ?? null;
  } catch {
    return null;
  }
}

/** The plan an account is on after buying `planId`: a Founding 100 seat is Solo. */
export function planAfterCheckout(planId: string): string {
  return planId === 'founding' ? 'solo' : planId;
}

/** Note what this tab is buying, just before Paddle's checkout opens. */
export function rememberCheckout(storage: IntentStorage | null, from: string, planId: string, now = Date.now()): void {
  if (!storage) return;
  const intent: CheckoutIntent = { from, to: planAfterCheckout(planId), at: now };
  try {
    storage.setItem(INTENT_KEY, JSON.stringify(intent));
  } catch {
    // Storage full or blocked: the return names no plan, which is still true.
  }
}

/** Read and clear the note. Null when there is none, or it is malformed or stale. */
export function takeCheckoutIntent(storage: IntentStorage | null, now = Date.now()): CheckoutIntent | null {
  if (!storage) return null;
  let raw: string | null;
  try {
    raw = storage.getItem(INTENT_KEY);
    storage.removeItem(INTENT_KEY);
  } catch {
    return null;
  }
  if (!raw) return null;
  try {
    const value = JSON.parse(raw) as Partial<CheckoutIntent> | null;
    if (!value || typeof value.from !== 'string' || typeof value.to !== 'string' || typeof value.at !== 'number') return null;
    if (!PLAN_ID.test(value.from) || !PLAN_ID.test(value.to)) return null;
    const age = now - value.at;
    if (!(age >= 0 && age <= INTENT_MAX_AGE_MS)) return null;
    return { from: value.from, to: value.to, at: value.at };
  } catch {
    return null;
  }
}

/**
 * Poll the usage summary until the account is on `target`. The webhook that
 * changes the plan can arrive a few seconds after Paddle redirects the buyer,
 * and until then the summary still shows the plan the account was on.
 * Returns that summary, or null if the plan did not change in time.
 */
export async function waitForPlan(
  load: () => Promise<PlanSummary>,
  target: string,
  { tries = 6, delayMs = 2500, sleep = (ms: number) => new Promise<void>((r) => setTimeout(r, ms)) } = {},
): Promise<PlanSummary | null> {
  for (let attempt = 0; attempt < tries; attempt += 1) {
    try {
      const summary = await load();
      if (summary.plan === target) return summary;
    } catch {
      // A failed read is retried like a plan that has not changed yet.
    }
    if (attempt < tries - 1) await sleep(delayMs);
  }
  return null;
}

export type CheckoutNotice = { tone: 'success' | 'info'; text: string };

/** What the dashboard says once the checkout return has been checked. */
export function checkoutMessage(summary: PlanSummary | null): CheckoutNotice {
  if (summary) {
    const name = summary.plan_name?.trim() || summary.plan.charAt(0).toUpperCase() + summary.plan.slice(1);
    return { tone: 'success', text: `Payment received. You're on ${name}.` };
  }
  return {
    tone: 'info',
    text: 'Payment received. Your plan can take a minute to update; Billing shows it as soon as Paddle confirms.',
  };
}

/** No note from Billing: a payment link or a payment-method update. Nothing to name. */
export const UNNOTED_CHECKOUT: CheckoutNotice = {
  tone: 'info',
  text: 'Checkout complete. Billing shows your plan as soon as Paddle confirms it.',
};

/** Same plan, other billing cycle: the plan name cannot show whether it changed. */
export const SAME_PLAN_CHECKOUT: CheckoutNotice = {
  tone: 'info',
  text: 'Payment received. Billing shows the change as soon as Paddle confirms it.',
};

/**
 * The notice for a checkout return: the bought plan once the account is on
 * it, never the plan it was on before.
 */
export async function confirmCheckout(
  load: () => Promise<PlanSummary>,
  intent: CheckoutIntent | null,
  options?: Parameters<typeof waitForPlan>[2],
): Promise<CheckoutNotice> {
  if (!intent) return UNNOTED_CHECKOUT;
  if (intent.from === intent.to) return SAME_PLAN_CHECKOUT;
  return checkoutMessage(await waitForPlan(load, intent.to, options));
}
