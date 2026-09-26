// How a plan purchase starts, decided from GET /billing/client-config.
//
// A Paddle.js overlay with a client price writes customData in the browser, so
// the server only credits it to this account when customData carries the
// server's signature over the account id (`checkout_binding`, handed only to
// the signed-in account). Without it, the server-created transaction path is
// used. An account that already holds a subscription changes plans in the
// Paddle portal: a second subscription would bill twice.

import type { BillingClientConfigResponse, BillingCycle, FoundingOffer } from './api';

export type CheckoutRoute =
  | { kind: 'portal' }
  | { kind: 'overlay'; priceId: string; customData: Record<string, string> }
  | { kind: 'server' };

export function checkoutRoute(
  config: BillingClientConfigResponse | null,
  plan: string,
  cycle: BillingCycle,
  perSeat: boolean,
  userId: string,
): CheckoutRoute {
  if (config?.has_subscription) return { kind: 'portal' };
  const priceKey = cycle === 'yearly' ? `${plan}_annual` : plan;
  const priceId = !perSeat && plan !== 'founding' ? config?.prices?.[priceKey] : undefined;
  const binding = config?.checkout_binding;
  if (config?.provider === 'paddle' && priceId && binding && userId) {
    return { kind: 'overlay', priceId, customData: { remembra_user_id: userId, remembra_binding: binding, plan } };
  }
  return { kind: 'server' };
}

/**
 * What a plan row offers: nothing (it is the plan), the billing portal (already
 * subscribed), checkout, or nothing to act on (`view`: a session that cannot
 * open checkout or the portal, see billingActionsNote).
 */
export function planRowAction(
  planId: string,
  currentPlan: string,
  subscribed: boolean,
  canManage = true,
): 'current' | 'manage' | 'buy' | 'view' {
  if (planId === currentPlan) return 'current';
  if (!canManage) return 'view';
  return subscribed ? 'manage' : 'buy';
}

/**
 * Checkout and the billing portal take an email sign-in only: the server
 * refuses API keys (BILL-3), so an "API key instead" session sees its plan and
 * usage but is asked to sign in with email instead of getting a 403.
 */
export function billingActionsNote(authMode: 'jwt' | 'api_key' | 'none'): string | null {
  return authMode === 'api_key'
    ? 'You signed in with an API key. To buy a plan or manage your subscription, sign in with your email: API keys cannot open checkout or the billing portal.'
    : null;
}

/**
 * The Founding 100 line in Billing: the seats left, or, for a founder whose
 * subscription ended, that their price and seat are still theirs until a date
 * (the 14-day promise, which holds even when the other seats are all taken).
 */
export function foundingSeatNote(founding: FoundingOffer, formatDate: (iso: string) => string): string | null {
  if (founding.held_kind === 'lapsed' && founding.held_until) {
    return `Your Founding price and seat are kept until ${formatDate(founding.held_until)}.`;
  }
  if (founding.held_kind === 'pending' && founding.held_until) {
    return `A seat is held for your open checkout until ${formatDate(founding.held_until)}.`;
  }
  return founding.remaining !== null ? `${founding.remaining} left` : null;
}
