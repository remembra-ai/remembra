// Billing: this period's smart credits (GET /cloud/usage/summary), what does
// and does not use them, and the self-serve plans (GET /billing/plans) with
// Paddle checkout. Team is sold per seat with a server-enforced minimum.

import { useId, useState } from 'react';
import clsx from 'clsx';
import { Check, CreditCard, ExternalLink, Loader2, Mail, Minus, Plus } from 'lucide-react';
import {
  api,
  ApiError,
  type BillingContextResponse,
  type BillingCycle,
  type PlanCatalogEntry,
  type PlansResponse,
  type UsageSummaryResponse,
} from '../lib/api';
import { checkoutRoute, planRowAction } from '../lib/checkout';
import { clampSeats, creditsView, formatUsd, parseSeatDraft, planLine, resetLabel } from '../lib/credits';
import { useResource } from '../hooks/useResource';
import { Card, CardHeader, ErrorNotice, Pill, Skeleton } from './relay/ui';
import { DegradedNotice, PixelMeter } from './credits/Credits';
import { initPaddle, paddleGlobal, rememberCheckout, sessionStore, successUrl } from '../lib/paddle';

function userEmail(): string | undefined {
  try {
    const raw = localStorage.getItem('remembra_user');
    return raw ? (JSON.parse(raw)?.email as string | undefined) : undefined;
  } catch {
    return undefined;
  }
}

/**
 * Open Paddle checkout. Single-quantity plans can use a client price (with the
 * server's signature over the account id in customData); per-seat Team and
 * Founding 100 always go through a server transaction, where the seat minimum
 * and the redemption cap are enforced. A subscribed account is sent to the
 * billing portal instead of a second subscription. Just before the checkout
 * opens, the tab notes the plan it is on and the plan it is buying, so the
 * return page waits for that plan instead of announcing the current one.
 */
async function startCheckout(
  plan: string,
  cycle: BillingCycle,
  seats: number | undefined,
  perSeat: boolean,
  currentPlan: string,
  onPortal: () => void,
): Promise<void> {
  const config = await api.getBillingClientConfig().catch(() => null);
  const P = paddleGlobal();
  if (P) initPaddle(P, config, window.location.origin);
  const route = checkoutRoute(config, plan, cycle, perSeat, api.getUserId());
  if (route.kind === 'portal') {
    onPortal();
    return;
  }
  if (P && route.kind === 'overlay') {
    const email = userEmail();
    rememberCheckout(sessionStore(), currentPlan, plan);
    P.Checkout.open({
      items: [{ priceId: route.priceId, quantity: 1 }],
      ...(email ? { customer: { email } } : {}),
      customData: route.customData,
      settings: { successUrl: successUrl(config, window.location.origin) },
    });
    return;
  }
  const response = await api.createCheckout(plan, cycle, perSeat ? seats : undefined);
  if (response.transaction_id && P) {
    rememberCheckout(sessionStore(), currentPlan, plan);
    P.Checkout.open({ transactionId: response.transaction_id });
  } else if (response.checkout_url) {
    rememberCheckout(sessionStore(), currentPlan, plan);
    window.location.href = response.checkout_url;
  } else {
    throw new Error('Checkout could not start: Paddle did not load in this browser. Disable blockers for paddle.com and try again.');
  }
}

function Legend() {
  return (
    <ul className="mt-2 flex flex-wrap gap-x-4 gap-y-1 font-mono text-[11px] text-ink-3">
      <li className="flex items-center gap-1.5">
        <span aria-hidden="true" className="h-2.5 w-2.5 bg-ink" /> spent
      </li>
      <li className="flex items-center gap-1.5">
        <span aria-hidden="true" className="h-2.5 w-2.5 bg-signal" /> held for enrichment in progress
      </li>
      <li className="flex items-center gap-1.5">
        <span aria-hidden="true" className="rr-cell-open h-2.5 w-2.5" /> left
      </li>
    </ul>
  );
}

function Stat({ label, value, note }: { label: string; value: string; note?: string }) {
  return (
    <div className="min-w-0 border-t border-rule pt-2">
      <dt className="font-mono text-[11px] uppercase tracking-[0.08em] text-ink-3">{label}</dt>
      <dd className="mt-0.5">
        <span className="tabular font-display text-xl font-bold text-ink">{value}</span>
        {note && <span className="mt-0.5 block text-xs text-ink-3">{note}</span>}
      </dd>
    </div>
  );
}

function PeriodCard({ summary, onPortal, portalBusy }: { summary: UsageSummaryResponse; onPortal: () => void; portalBusy: boolean }) {
  const titleId = useId();
  const view = creditsView(summary);
  const paid = summary.plan !== 'free';
  return (
    <Card labelledBy={titleId}>
      <CardHeader
        id={titleId}
        eyebrow={`This period · ${summary.credits.bank === 'yearly' ? 'yearly bank' : 'monthly'}`}
        title={planLine(summary)}
        action={
          paid && (
            <button type="button" onClick={onPortal} disabled={portalBusy} className="rr-btn-ghost inline-flex items-center gap-1.5 px-3 py-2 text-sm">
              {portalBusy ? <Loader2 className="h-4 w-4 animate-spin" aria-hidden="true" /> : <CreditCard className="h-4 w-4" aria-hidden="true" />}
              Manage subscription
            </button>
          )
        }
      />
      <div className="px-4 pb-5 pt-3 sm:px-5">
        <p className="flex flex-wrap items-baseline gap-x-2">
          <span className={clsx('font-display tabular text-5xl font-extrabold tracking-tight', view.degraded ? 'text-fail' : 'text-ink')}>
            {view.remaining.toLocaleString()}
          </span>
          <span className="text-ink-2">
            of {view.limit.toLocaleString()} smart credits left · {resetLabel(summary)}
          </span>
        </p>
        <PixelMeter summary={summary} cells={64} className="mt-3" />
        <Legend />
        {summary.credits.unverified_cap_applied && (
          <p className="mt-3 text-sm text-signal-ink">
            Free credits are held at a starter amount until you verify your email. Verify it from the link we sent to unlock the full
            monthly allowance.
          </p>
        )}
        {view.degraded && (
          <div className="mt-4">
            <DegradedNotice summary={summary} />
          </div>
        )}
        <dl className="mt-5 grid grid-cols-2 gap-x-6 gap-y-4 sm:grid-cols-4">
          <Stat
            label="Relay events"
            value={summary.relay_events.this_month.toLocaleString()}
            note={
              summary.relay_events.over_soft_cap
                ? `Past the ${summary.relay_events.soft_cap.toLocaleString()} fair-use mark this month. Still free.`
                : 'Always free, never uses credits'
            }
          />
          <Stat label="Recalls" value={summary.recalls.this_month.toLocaleString()} note={`of ${summary.recalls.limit.toLocaleString()} this month, free`} />
          <Stat label="Memories" value={summary.memories.stored.toLocaleString()} note={`of ${summary.memories.cap.toLocaleString()} stored`} />
          <Stat
            label="Stores"
            value={summary.stores.this_month.toLocaleString()}
            note={summary.stores.degraded_this_month > 0 ? `${summary.stores.degraded_this_month.toLocaleString()} saved without enrichment` : 'this month, all enriched'}
          />
        </dl>
        <p className="mt-4 font-mono text-[11px] text-ink-3">
          AI spend this period ${summary.credits.llm_usd_used.toFixed(2)} of a ${summary.credits.ceiling_usd.toFixed(2)} ceiling.
        </p>
      </div>
    </Card>
  );
}

function HowCreditsWork() {
  const titleId = useId();
  return (
    <Card labelledBy={titleId}>
      <CardHeader id={titleId} eyebrow="Smart credits" title="What uses them, what never does" />
      <div className="grid gap-5 px-4 pb-5 pt-3 sm:px-5 md:grid-cols-2">
        <div>
          <p className="font-mono text-[11px] uppercase tracking-[0.08em] text-ink-3">Never uses credits</p>
          <p className="mt-1 text-sm text-ink-2">
            <span className="font-semibold text-ink">Relay is always free.</span> Handoffs, checkpoints, status updates, inbox notes, pickup
            briefs, trail reads and recalls cost nothing on every plan.
          </p>
        </div>
        <div>
          <p className="font-mono text-[11px] uppercase tracking-[0.08em] text-ink-3">Uses credits</p>
          <p className="mt-1 text-sm text-ink-2">
            Storing a memory with AI enrichment (fact extraction, entity linking): one credit per 8,000 characters, or the actual AI cost if
            that is higher. One credit is $0.0025 of AI spend.
          </p>
        </div>
        <div>
          <p className="font-mono text-[11px] uppercase tracking-[0.08em] text-ink-3">When they run out</p>
          <p className="mt-1 text-sm text-ink-2">
            Nothing is rejected. New memories are saved in degraded mode: stored and searchable, without enrichment, until credits come back.
          </p>
        </div>
        <div>
          <p className="font-mono text-[11px] uppercase tracking-[0.08em] text-ink-3">Monthly or yearly</p>
          <p className="mt-1 text-sm text-ink-2">
            Monthly plans get the allowance each month. Yearly plans bank the whole year up front, so a busy month can draw on later ones.
          </p>
        </div>
      </div>
    </Card>
  );
}

// The field holds the raw text while someone types ("1" on the way to "10"),
// and snaps to the allowed range only on blur, on Enter, or from the buttons.
// While the text is not a valid count the parent gets null and checkout waits.
function SeatStepper({
  draft,
  min,
  onDraft,
  planName,
}: {
  draft: string;
  min: number;
  onDraft: (text: string) => void;
  planName: string;
}) {
  const inputId = useId();
  const hintId = `${inputId}-hint`;
  const seats = parseSeatDraft(draft, min);
  const settled = seats ?? clampSeats(draft.trim() === '' ? Number.NaN : Number(draft), min);
  const commit = () => onDraft(String(settled));
  return (
    <div className="flex flex-wrap items-center gap-2">
      <label htmlFor={inputId} className="font-mono text-[11px] uppercase tracking-[0.08em] text-ink-3">
        Seats
      </label>
      <div className={clsx('inline-flex items-stretch border', seats === null ? 'border-fail' : 'border-rule')}>
        <button
          type="button"
          onClick={() => onDraft(String(clampSeats(settled - 1, min)))}
          disabled={settled <= min}
          aria-label={`One fewer ${planName} seat`}
          className="px-2 text-ink-2 hover:bg-paper-2 disabled:opacity-40"
        >
          <Minus className="h-3.5 w-3.5" />
        </button>
        <input
          id={inputId}
          type="number"
          inputMode="numeric"
          min={min}
          max={1000}
          value={draft}
          onChange={(e) => onDraft(e.target.value)}
          onBlur={commit}
          onKeyDown={(e) => {
            if (e.key === 'Enter') {
              e.preventDefault();
              commit();
            }
          }}
          aria-invalid={seats === null}
          aria-describedby={hintId}
          className="tabular w-14 border-x border-rule bg-panel py-1 text-center font-mono text-sm text-ink [appearance:textfield] [&::-webkit-inner-spin-button]:appearance-none"
        />
        <button
          type="button"
          onClick={() => onDraft(String(clampSeats(settled + 1, min)))}
          disabled={settled >= 1000}
          aria-label={`One more ${planName} seat`}
          className="px-2 text-ink-2 hover:bg-paper-2 disabled:opacity-40"
        >
          <Plus className="h-3.5 w-3.5" />
        </button>
      </div>
      <span id={hintId} className={clsx('text-xs', seats === null ? 'text-fail' : 'text-ink-3')}>
        {seats === null ? `${min} to 1,000 seats` : `minimum ${min}`}
      </span>
    </div>
  );
}

function priceFor(plan: PlanCatalogEntry, cycle: BillingCycle): number | null {
  return cycle === 'yearly' ? plan.price_yearly : plan.price_monthly;
}

function PlanRow({
  plan,
  cycle,
  action,
  busy,
  onBuy,
  onManage,
}: {
  plan: PlanCatalogEntry;
  cycle: BillingCycle;
  action: 'current' | 'manage' | 'buy';
  busy: boolean;
  onBuy: (plan: PlanCatalogEntry, seats: number | undefined) => void;
  onManage: () => void;
}) {
  const current = action === 'current';
  const minSeats = Math.max(plan.min_seats, 1);
  const [seatDraft, setSeatDraft] = useState(String(minSeats));
  // null while the seat field holds something that is not a valid count yet.
  const seats = plan.per_seat ? parseSeatDraft(seatDraft, minSeats) : null;
  const seatsReady = !plan.per_seat || seats !== null;
  const price = priceFor(plan, cycle);
  const available = cycle === 'yearly' ? plan.available_yearly : plan.available_monthly;
  const per = cycle === 'yearly' ? 'yr' : 'mo';
  const total = price !== null && plan.per_seat ? (seats !== null ? price * seats : null) : price;
  return (
    <li className={clsx('grid gap-4 py-5 md:grid-cols-[minmax(0,1fr)_minmax(0,1.3fr)_auto] md:items-start', current && 'relative')}>
      {current && <span aria-hidden="true" className="absolute inset-y-4 -left-4 w-[3px] bg-signal sm:-left-5" />}
      <div className="min-w-0">
        <p className="flex items-center gap-2">
          <span className="font-display text-xl font-bold text-ink">{plan.name}</span>
          {current && <Pill tone="signal">current</Pill>}
        </p>
        <p className="mt-1">
          <span className="font-display tabular text-2xl font-extrabold text-ink">{price !== null ? formatUsd(price) : 'n/a'}</span>
          <span className="text-sm text-ink-3">
            {' '}
            /{plan.per_seat ? 'seat/' : ''}
            {per}
          </span>
        </p>
        {plan.per_seat && total !== null && (
          <p className="mt-0.5 font-mono text-[11px] text-ink-3">
            {seats} seats = {formatUsd(total)}/{per}
          </p>
        )}
      </div>
      <ul className="min-w-0 space-y-1 text-sm text-ink-2">
        {plan.features.map((feature) => (
          <li key={feature} className="flex gap-2">
            <Check className="mt-0.5 h-4 w-4 shrink-0 text-ok" aria-hidden="true" />
            {feature}
          </li>
        ))}
      </ul>
      <div className="flex flex-col items-start gap-2 md:items-end">
        {plan.per_seat && action === 'buy' && <SeatStepper draft={seatDraft} min={minSeats} onDraft={setSeatDraft} planName={plan.name} />}
        {action === 'manage' && (
          <button type="button" onClick={onManage} className="rr-btn-ghost inline-flex items-center gap-1.5 px-3.5 py-2 text-sm">
            <CreditCard className="h-4 w-4" aria-hidden="true" />
            Switch in Manage subscription
          </button>
        )}
        {action === 'buy' && (
          <button
            type="button"
            onClick={() => {
              if (!plan.per_seat) onBuy(plan, undefined);
              else if (seats !== null) onBuy(plan, clampSeats(seats, minSeats));
            }}
            disabled={busy || !available || price === null || !seatsReady}
            className="rr-btn-primary inline-flex items-center gap-1.5 px-3.5 py-2 text-sm"
          >
            {busy && <Loader2 className="h-4 w-4 animate-spin" aria-hidden="true" />}
            {plan.per_seat ? (seats !== null ? `Start Team with ${seats} seats` : 'Start Team') : `Choose ${plan.name}`}
          </button>
        )}
        {action === 'buy' && !available && (
          <p className="max-w-[26ch] text-xs text-ink-3 md:text-right">Checkout for {cycle} billing is not set up on this server yet.</p>
        )}
      </div>
    </li>
  );
}

function PlansSection({
  plans,
  currentPlan,
  subscribed,
  onPortal,
  onError,
}: {
  plans: PlansResponse;
  currentPlan: string;
  /** Holds an active subscription (legacy $49 / $199 included): plan changes go through the portal. */
  subscribed: boolean;
  onPortal: () => void;
  onError: (message: string | null) => void;
}) {
  const titleId = useId();
  const [cycle, setCycle] = useState<BillingCycle>('monthly');
  const [busy, setBusy] = useState<string | null>(null);

  const buy = async (planId: string, perSeat: boolean, seats: number | undefined, forceCycle?: BillingCycle) => {
    setBusy(planId);
    onError(null);
    try {
      await startCheckout(planId, forceCycle ?? cycle, seats, perSeat, currentPlan, onPortal);
    } catch (err) {
      onError(err instanceof Error ? err.message : 'Checkout could not start.');
    } finally {
      setBusy(null);
    }
  };

  const founding = plans.founding;
  return (
    <Card labelledBy={titleId}>
      <CardHeader
        id={titleId}
        eyebrow="Plans"
        title="Change plan"
        action={
          <div role="radiogroup" aria-label="Billing cycle" className="inline-flex border border-rule">
            {(['monthly', 'yearly'] as const).map((value) => (
              <button
                key={value}
                type="button"
                role="radio"
                aria-checked={cycle === value}
                onClick={() => setCycle(value)}
                className={clsx('px-3 py-1.5 font-mono text-xs', cycle === value ? 'bg-ink text-paper' : 'text-ink-2 hover:text-ink')}
              >
                {value}
              </button>
            ))}
          </div>
        }
      />
      <div className="px-4 pb-2 sm:px-5">
        {plans.provider !== 'paddle' && (
          <p className="mt-3 border-l-[3px] border-rule-strong px-3 py-2 text-sm text-ink-2">
            Self-serve checkout is not configured on this server. Prices are shown for reference.
          </p>
        )}
        {subscribed && (
          <p className="mt-3 border-l-[3px] border-signal px-3 py-2 text-sm text-ink-2">
            You already have a subscription. Switch plans or cancel from Manage subscription, so you are never billed for two.
          </p>
        )}
        {founding.available && !subscribed && (
          <div className="mt-4 flex flex-wrap items-center justify-between gap-3 border border-dashed border-signal px-4 py-3">
            <p className="text-sm text-ink-2">
              <span className="font-semibold text-ink">Founding 100:</span> Solo for {formatUsd(founding.price_yearly)}/yr, price locked for
              life, billed yearly.
              {founding.remaining !== null && <span className="font-mono text-xs text-ink-3"> {founding.remaining} left</span>}
            </p>
            <button
              type="button"
              disabled={busy !== null}
              onClick={() => buy('founding', false, undefined, 'yearly')}
              className="rr-btn-ghost inline-flex items-center gap-1.5 px-3 py-1.5 text-sm"
            >
              {busy === 'founding' && <Loader2 className="h-4 w-4 animate-spin" aria-hidden="true" />}
              Claim a founding seat
            </button>
          </div>
        )}
        <ul className="divide-y divide-rule">
          {plans.plans.map((plan) => (
            <PlanRow
              key={plan.id}
              plan={plan}
              cycle={cycle}
              action={planRowAction(plan.id, currentPlan, subscribed)}
              busy={busy === plan.id}
              onBuy={(p, seats) => buy(p.id, p.per_seat, seats)}
              onManage={onPortal}
            />
          ))}
          <li className="grid gap-3 py-5 md:grid-cols-[minmax(0,1fr)_auto] md:items-center">
            <div>
              <p className="font-display text-xl font-bold text-ink">Enterprise</p>
              <p className="mt-1 text-sm text-ink-2">SSO, custom limits, a contract and an SLA.</p>
            </div>
            <a
              href="mailto:sales@dolphytech.com?subject=Remembra%20Enterprise"
              className="rr-btn-ghost inline-flex items-center gap-1.5 justify-self-start px-3.5 py-2 text-sm md:justify-self-end"
            >
              <Mail className="h-4 w-4" aria-hidden="true" /> Talk to us
            </a>
          </li>
        </ul>
      </div>
    </Card>
  );
}

/** Team members who do not own billing see the team's plan and who to ask. */
function TeamBillingView({ context, summary }: { context: BillingContextResponse; summary: UsageSummaryResponse | undefined }) {
  const titleId = useId();
  const plan = context.team_plan || 'team';
  return (
    <div className="space-y-5">
      <Card labelledBy={titleId}>
        <CardHeader id={titleId} eyebrow={`Via ${context.team_name ?? 'your team'}`} title={`${plan[0].toUpperCase()}${plan.slice(1)} plan`} />
        <div className="px-4 pb-5 pt-3 sm:px-5">
          <p className="text-sm text-ink-2">
            You are a <span className="font-semibold text-ink">{context.role ?? 'member'}</span> of {context.team_name ?? 'this team'}. Credits
            and limits are pooled across the team's seats.
          </p>
          {summary && (
            <>
              <PixelMeter summary={summary} cells={48} className="mt-4" />
              <p className="mt-1.5 font-mono text-[11px] text-ink-3">
                {summary.credits.remaining.toLocaleString()} of {summary.credits.limit.toLocaleString()} credits left · {resetLabel(summary)}
              </p>
              <div className="mt-3">
                <DegradedNotice summary={summary} compact />
              </div>
            </>
          )}
          <p className="mt-4 flex items-start gap-2 text-sm text-ink-2">
            <Mail className="mt-0.5 h-4 w-4 shrink-0 text-ink-3" aria-hidden="true" />
            <span>
              Billing is managed by the team owner
              {context.owner_email ? (
                <>
                  {' '}
                  (
                  <a className="font-semibold text-ink underline decoration-signal decoration-2 underline-offset-4" href={`mailto:${context.owner_email}`}>
                    {context.owner_email}
                  </a>
                  )
                </>
              ) : null}
              . Ask them for more seats or a plan change.
            </span>
          </p>
        </div>
      </Card>
      <HowCreditsWork />
    </div>
  );
}

export function Billing() {
  const context = useResource('billing-context', () => api.getBillingContext());
  const summary = useResource('billing-usage-summary', () => api.getUsageSummary(), { pollMs: 60000 });
  const plans = useResource('billing-plans', () => api.getPlans());
  const [error, setError] = useState<string | null>(null);
  const [portalBusy, setPortalBusy] = useState(false);

  const openPortal = async () => {
    setPortalBusy(true);
    setError(null);
    try {
      const { portal_url } = await api.createPortalSession();
      window.location.href = portal_url;
    } catch (err) {
      setError(err instanceof Error ? err.message : 'The billing portal could not open.');
    } finally {
      setPortalBusy(false);
    }
  };

  if (context.loading || (summary.loading && !summary.error)) {
    return (
      <div className="space-y-5" role="status" aria-label="Loading billing">
        <Skeleton className="h-64 w-full" />
        <Skeleton className="h-40 w-full" />
      </div>
    );
  }

  if (context.data?.context === 'team' && !context.data.can_manage_billing) {
    return <TeamBillingView context={context.data} summary={summary.data} />;
  }

  const metered = !(summary.error instanceof ApiError && [404, 503].includes(summary.error.status));
  const currentPlan = summary.data?.plan ?? context.data?.team_plan ?? context.data?.plan ?? 'free';

  return (
    <div className="space-y-5">
      {error && (
        <p role="alert" className="border-l-[3px] border-fail bg-fail-wash px-4 py-3 text-sm text-ink">
          {error}
        </p>
      )}
      {summary.data && <PeriodCard summary={summary.data} onPortal={openPortal} portalBusy={portalBusy} />}
      {!summary.data && summary.error != null && metered && (
        <div className="rr-card rounded-[3px]">
          <ErrorNotice error={summary.error} what="your usage" onRetry={summary.refresh} />
        </div>
      )}
      {!metered && (
        <p className="rr-card rounded-[3px] px-5 py-4 text-sm text-ink-2">
          This server does not meter usage (self-hosted), so there are no credits to track. Everything is unlimited here.
        </p>
      )}
      <HowCreditsWork />
      {plans.data && (
        <PlansSection
          plans={plans.data}
          currentPlan={currentPlan}
          subscribed={summary.data?.subscription_active === true}
          onPortal={openPortal}
          onError={setError}
        />
      )}
      {!plans.data && plans.error != null && (
        <div className="rr-card rounded-[3px]">
          <ErrorNotice error={plans.error} what="the plans" onRetry={plans.refresh} />
        </div>
      )}
      <p className="flex items-center gap-1.5 text-xs text-ink-3">
        Prices in USD, before tax. Payments by Paddle.
        <a href="https://remembra.dev/pricing.html" className="inline-flex items-center gap-1 underline underline-offset-4" target="_blank" rel="noreferrer">
          Full pricing <ExternalLink className="h-3 w-3" aria-hidden="true" />
        </a>
      </p>
    </div>
  );
}
