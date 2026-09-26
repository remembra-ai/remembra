import { useEffect, useRef, useState } from 'react';
import { AlertTriangle, CreditCard, Loader2 } from 'lucide-react';
import { API_V1 } from '../config';
import { BrandMark } from '../components/relay/ui';
import type { BillingClientConfigResponse } from '../lib/api';
import { initPaddle, paddleGlobal, paymentLinkTransaction } from '../lib/paddle';

type Phase = { kind: 'loading' } | { kind: 'open'; txn: string } | { kind: 'error'; message: string };

const PADDLE_WAIT_MS = 8000;

/** paddle.js is a deferred script in index.html; give it a moment before calling it blocked. */
async function waitForPaddle(timeoutMs: number) {
  const started = Date.now();
  while (Date.now() - started < timeoutMs) {
    const p = paddleGlobal();
    if (p) return p;
    await new Promise((r) => setTimeout(r, 150));
  }
  return paddleGlobal();
}

/**
 * /pay: Paddle's default payment link.
 *
 * Paddle sends buyers here with ?_ptxn=<transaction> for checkouts the server
 * created, payment reminders and payment-method updates. The page loads the
 * public checkout config, initializes Paddle.js, and Paddle.js opens that
 * transaction's checkout. No sign-in is needed: the transaction already names
 * the customer.
 */
export function Pay() {
  const [txn] = useState(() => paymentLinkTransaction(window.location.search));
  const [phase, setPhase] = useState<Phase>(() =>
    txn ? { kind: 'loading' } : { kind: 'error', message: 'This payment link is incomplete. Open it again from the email, or start from Billing in your dashboard.' },
  );
  const started = useRef(false);

  useEffect(() => {
    if (!txn || started.current) return;
    started.current = true;
    const run = async () => {
      const response = await fetch(`${API_V1}/billing/client-config`).catch(() => null);
      const config = response?.ok ? ((await response.json().catch(() => null)) as BillingClientConfigResponse | null) : null;
      if (!config) throw new Error('Could not reach Remembra to load the checkout. Try again in a moment.');
      if (config.provider !== 'paddle' || !config.client_token) throw new Error('Checkout is not set up on this server.');
      const p = await waitForPaddle(PADDLE_WAIT_MS);
      if (!p) throw new Error('The Paddle checkout did not load in this browser. Allow paddle.com in your content blocker and reload.');
      initPaddle(p, config, window.location.origin); // opens the checkout for ?_ptxn
      setPhase({ kind: 'open', txn });
    };
    run().catch((error: unknown) =>
      setPhase({ kind: 'error', message: error instanceof Error ? error.message : 'The checkout could not start.' }),
    );
  }, [txn]);

  const reopen = () => {
    if (phase.kind === 'open') paddleGlobal()?.Checkout.open({ transactionId: phase.txn });
  };

  return (
    <div className="flex min-h-screen items-center justify-center bg-paper px-4">
      <div className="w-full max-w-md">
        <div className="mb-8 flex items-center justify-center gap-2.5" role="img" aria-label="Remembra">
          <BrandMark size={40} className="text-ink" />
          <span className="font-display text-2xl font-extrabold tracking-[-0.02em] text-ink">Remembra</span>
        </div>
        <div className="border border-rule bg-panel p-6" role="status" aria-live="polite">
          {phase.kind === 'loading' && (
            <p className="flex items-center gap-3 text-ink">
              <Loader2 className="h-5 w-5 animate-spin text-ink-3" aria-hidden="true" />
              Opening the secure checkout…
            </p>
          )}
          {phase.kind === 'open' && (
            <>
              <p className="flex items-center gap-3 font-semibold text-ink">
                <CreditCard className="h-5 w-5 text-signal-ink" aria-hidden="true" />
                Your checkout is open.
              </p>
              <p className="mt-2 text-sm text-ink-2">
                Payments are taken by Paddle, our reseller. If you closed the checkout, open it again here.
              </p>
              <button type="button" onClick={reopen} className="mt-4 border border-ink px-4 py-2 text-sm font-semibold text-ink hover:bg-ink hover:text-paper">
                Open checkout
              </button>
            </>
          )}
          {phase.kind === 'error' && (
            <>
              <p className="flex items-center gap-3 font-semibold text-ink">
                <AlertTriangle className="h-5 w-5 text-fail" aria-hidden="true" />
                The checkout didn't open.
              </p>
              <p className="mt-2 text-sm text-ink-2">{phase.message}</p>
              <a href="/#/billing" className="mt-4 inline-block text-sm font-semibold text-ink underline decoration-signal decoration-2 underline-offset-4">
                Go to Billing
              </a>
            </>
          )}
        </div>
        <p className="mt-4 text-center text-xs text-ink-3">
          Questions about a payment? <a className="underline" href="mailto:support@remembra.dev">support@remembra.dev</a>
        </p>
      </div>
    </div>
  );
}
