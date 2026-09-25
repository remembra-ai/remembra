import { useEffect, useRef, useState } from 'react';
import { AlertTriangle, CheckCircle2, Loader2, LogIn } from 'lucide-react';
import { API_V1 } from '../config';
import { BrandMark } from '../components/relay/ui';
import { confirmDashboardEmail, PENDING_VERIFY_KEY } from '../lib/verifyEmail';

interface VerifyEmailProps {
  /** The dashboard session, when signed in. */
  jwt: string | null;
  onContinue: () => void;
  onSignIn: () => void;
}

type Phase = { kind: 'working' } | { kind: 'done'; message: string } | { kind: 'error'; message: string } | { kind: 'sign-in' };

/**
 * Landing page for emailed verification links:
 * - /verify-email?token=...&account=api  API-signup accounts (token only, no session)
 * - /verify-email?token=...              dashboard accounts (confirmed with the session)
 */
export function VerifyEmail({ jwt, onContinue, onSignIn }: VerifyEmailProps) {
  const [params] = useState(() => new URLSearchParams(window.location.search));
  const token = params.get('token') ?? '';
  const apiAccount = params.get('account') === 'api';
  const [phase, setPhase] = useState<Phase>(() => {
    if (token.length < 16) return { kind: 'error', message: 'This verification link is incomplete. Open the link from the email again.' };
    if (!apiAccount && !jwt) return { kind: 'sign-in' };
    return { kind: 'working' };
  });
  const started = useRef(false);

  useEffect(() => {
    // The token is single use; keep it out of the address bar and history.
    window.history.replaceState({}, '', '/verify-email');
    if (started.current || phase.kind !== 'working') return;
    started.current = true;
    const run = async () => {
      if (apiAccount) {
        const response = await fetch(`${API_V1}/cloud/verify-email/confirm`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ token }),
        });
        const data = await response.json().catch(() => ({}));
        if (!response.ok) throw new Error(typeof data.detail === 'string' ? data.detail : 'Verification failed.');
        return 'Your email is verified. Your API account now has the full free allowance.';
      }
      return confirmDashboardEmail(jwt as string, token);
    };
    run()
      .then((message) => setPhase({ kind: 'done', message }))
      .catch((error: unknown) =>
        setPhase({ kind: 'error', message: error instanceof Error ? error.message : 'Verification failed.' }),
      );
    // Runs once for the token captured on first render.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const signIn = () => {
    try {
      sessionStorage.setItem(PENDING_VERIFY_KEY, token);
    } catch {
      // Storage unavailable: the user can open the link again after signing in.
    }
    window.history.replaceState({}, '', '/');
    onSignIn();
  };

  const finish = () => {
    window.history.replaceState({}, '', '/');
    onContinue();
  };

  return (
    <div className="min-h-screen flex items-center justify-center bg-[hsl(var(--background))] px-4">
      <div className="max-w-md w-full">
        <div className="mb-8 flex items-center justify-center gap-2.5" role="img" aria-label="Remembra">
          <BrandMark size={40} className="text-ink" />
          <span className="font-display text-2xl font-extrabold tracking-[-0.02em] text-ink">Remembra</span>
        </div>
        <div className="bg-[hsl(var(--card))] rounded-xl shadow-sm border border-[hsl(var(--border))] p-6">
          {phase.kind === 'working' && (
            <div className="flex flex-col items-center gap-3 py-6" role="status" aria-live="polite">
              <Loader2 className="h-7 w-7 animate-spin text-[hsl(var(--muted-foreground))]" aria-hidden="true" />
              <p className="font-medium text-[hsl(var(--foreground))]">Verifying your email…</p>
            </div>
          )}
          {phase.kind === 'sign-in' && (
            <div className="space-y-4">
              <h1 className="font-display text-xl font-bold text-[hsl(var(--foreground))]">Sign in to verify your email</h1>
              <p className="text-sm text-[hsl(var(--muted-foreground))]">
                Sign in to the account this email belongs to; verification finishes right after.
              </p>
              <button
                type="button"
                onClick={signIn}
                className="w-full py-3 px-4 rounded-lg bg-accent hover:bg-accent-hover text-white font-medium transition-colors flex items-center justify-center gap-2"
              >
                <LogIn className="w-5 h-5" aria-hidden="true" />
                Sign in
              </button>
            </div>
          )}
          {(phase.kind === 'done' || phase.kind === 'error') && (
            <div className="space-y-4">
              <div className="flex items-start gap-3" role={phase.kind === 'error' ? 'alert' : 'status'}>
                {phase.kind === 'done' ? (
                  <CheckCircle2 className="mt-0.5 h-5 w-5 flex-none text-green-500" aria-hidden="true" />
                ) : (
                  <AlertTriangle className="mt-0.5 h-5 w-5 flex-none text-red-400" aria-hidden="true" />
                )}
                <div>
                  <h1 className="font-display text-xl font-bold text-[hsl(var(--foreground))]">
                    {phase.kind === 'done' ? 'Email verified' : 'Could not verify your email'}
                  </h1>
                  <p className="mt-1 text-sm text-[hsl(var(--muted-foreground))]">{phase.message}</p>
                </div>
              </div>
              <button
                type="button"
                onClick={finish}
                className="w-full py-3 px-4 rounded-lg bg-accent hover:bg-accent-hover text-white font-medium transition-colors"
              >
                {jwt ? 'Go to dashboard' : 'Continue'}
              </button>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
