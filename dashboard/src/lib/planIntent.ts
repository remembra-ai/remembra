// A plan picked on the pricing page (https://app.remembra.dev/signup?plan=founding):
// kept through signup and sign-in, then Billing opens on it.

export const PLAN_INTENTS = ['founding', 'solo', 'pro', 'team'] as const;
export type PlanIntent = (typeof PLAN_INTENTS)[number];

const KEY = 'remembra_plan_intent';

export function parsePlanIntent(search: string): PlanIntent | null {
  const value = new URLSearchParams(search).get('plan');
  return (PLAN_INTENTS as readonly string[]).includes(value ?? '') ? (value as PlanIntent) : null;
}

type Store = Pick<Storage, 'getItem' | 'setItem' | 'removeItem'>;

function local(): Store | null {
  try {
    return typeof window !== 'undefined' ? window.localStorage : null;
  } catch {
    return null;
  }
}

export function rememberPlanIntent(intent: PlanIntent | null, store: Store | null = local()): void {
  if (!intent) return;
  try {
    store?.setItem(KEY, intent);
  } catch {
    /* storage blocked: Billing simply opens without the pick */
  }
}

/** The remembered pick, once. */
export function takePlanIntent(store: Store | null = local()): PlanIntent | null {
  try {
    const value = store?.getItem(KEY) ?? null;
    if (value) store?.removeItem(KEY);
    return (PLAN_INTENTS as readonly string[]).includes(value ?? '') ? (value as PlanIntent) : null;
  } catch {
    return null;
  }
}

/** What Billing says about the pick (null when nothing was picked). */
export function planIntentNote(intent: string | null, foundingAvailable: boolean): string | null {
  if (!intent || !(PLAN_INTENTS as readonly string[]).includes(intent)) return null;
  if (intent === 'founding') {
    return foundingAvailable
      ? 'You picked the Founding 100 on the pricing page: claim your seat below (billed yearly).'
      : 'You picked the Founding 100 on the pricing page, but no seat is open now. Solo is the same plan at the regular price.';
  }
  const name = intent === 'solo' ? 'Solo' : intent === 'pro' ? 'Pro' : 'Team';
  return `You picked ${name} on the pricing page: choose it below.`;
}
