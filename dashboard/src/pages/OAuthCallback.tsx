import { useEffect, useRef, useState } from 'react';
import { AlertTriangle, ArrowLeft, CheckCircle2, Loader2, ShieldCheck } from 'lucide-react';
import { BrandMark } from '../components/relay/ui';
import { exchangeLoginCode, oauthErrorMessage, parseOAuthFragment, providerName, type OAuthOrigin } from '../lib/authProviders';

type SessionUser = { id: string; email: string; name?: string; is_admin?: boolean };

interface OAuthCallbackProps {
  onLogin: (token: string, user: SessionUser) => void;
  /** Back to where the round trip started: a sign-in page, or Settings after connecting a provider. */
  onBack: (page: OAuthOrigin) => void;
}

type Phase =
  | { kind: 'exchanging' }
  | { kind: 'totp'; code: string }
  | { kind: 'linked' }
  | { kind: 'error'; message: string };

/**
 * Landing page for /oauth/callback#code=... (or #error=..., or #linked=1 after
 * connecting a provider from Settings). Trades the single-use login code for a
 * dashboard session, asking for a 2FA code when the account has it on. The
 * fragment is wiped from the address bar at once.
 */
export function OAuthCallback({ onLogin, onBack }: OAuthCallbackProps) {
  const [fragment] = useState(() => parseOAuthFragment(window.location.hash));
  const [phase, setPhase] = useState<Phase>(() => {
    if (fragment.code) return { kind: 'exchanging' };
    if (fragment.linked && !fragment.error) return { kind: 'linked' };
    return { kind: 'error', message: oauthErrorMessage(fragment.error ?? 'provider_error', fragment.provider) };
  });
  const [totp, setTotp] = useState('');
  const [submitting, setSubmitting] = useState(false);
  const [totpError, setTotpError] = useState<string | null>(null);
  const started = useRef(false);
  const name = providerName(fragment.provider);

  const exchange = async (code: string, totpCode?: string): Promise<void> => {
    const response = await exchangeLoginCode(code, totpCode);
    const data = await response.json().catch(() => ({}));
    if (!response.ok) {
      const detail = typeof data.detail === 'string' ? data.detail : 'Sign-in failed. Please try again.';
      if (totpCode && response.status === 401 && detail === 'Invalid 2FA code') {
        setTotpError('That code did not work. Enter the current code from your authenticator app.');
        return;
      }
      setPhase({ kind: 'error', message: detail });
      return;
    }
    if (data.requires_2fa) {
      setPhase({ kind: 'totp', code });
      return;
    }
    if (typeof data.access_token !== 'string' || !data.user) {
      setPhase({ kind: 'error', message: 'Sign-in failed. Please try again.' });
      return;
    }
    window.history.replaceState({}, '', '/');
    onLogin(data.access_token, data.user as SessionUser);
  };

  useEffect(() => {
    // Keep the login code out of history and out of any later Referer.
    window.history.replaceState({}, '', '/oauth/callback');
    if (started.current || !fragment.code) return;
    started.current = true;
    exchange(fragment.code).catch(() =>
      setPhase({ kind: 'error', message: 'Could not reach Remembra. Check your connection and try again.' }),
    );
    // The exchange runs exactly once for the code captured on first render.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const submitTotp = async (e: React.FormEvent) => {
    e.preventDefault();
    if (phase.kind !== 'totp' || !/^\d{6}$/.test(totp)) {
      setTotpError('Enter the 6-digit code from your authenticator app.');
      return;
    }
    setSubmitting(true);
    setTotpError(null);
    try {
      await exchange(phase.code, totp);
    } catch {
      setTotpError('Could not reach Remembra. Check your connection and try again.');
    } finally {
      setSubmitting(false);
    }
  };

  const back = () => {
    window.history.replaceState({}, '', fragment.from === 'signup' ? '/signup' : '/');
    onBack(fragment.from);
  };
  const backLabel =
    fragment.from === 'settings' ? 'Back to settings' : fragment.from === 'signup' ? 'Back to sign up' : 'Back to sign in';

  return (
    <div className="min-h-screen flex items-center justify-center bg-[hsl(var(--background))] px-4">
      <div className="max-w-md w-full">
        <div className="text-center mb-8">
          <div className="mb-5 flex items-center justify-center gap-2.5" role="img" aria-label="Remembra">
            <BrandMark size={40} className="text-ink" />
            <span className="font-display text-2xl font-extrabold tracking-[-0.02em] text-ink">Remembra</span>
          </div>
        </div>

        <div className="bg-[hsl(var(--card))] rounded-xl shadow-sm border border-[hsl(var(--border))] p-6">
          {phase.kind === 'exchanging' && (
            <div className="flex flex-col items-center gap-3 py-6 text-center" role="status" aria-live="polite">
              <Loader2 className="h-7 w-7 animate-spin text-[hsl(var(--muted-foreground))]" aria-hidden="true" />
              <p className="text-[hsl(var(--foreground))] font-medium">Signing you in with {name}…</p>
            </div>
          )}

          {phase.kind === 'totp' && (
            <form onSubmit={submitTotp} className="space-y-4">
              <div className="flex items-center gap-3">
                <ShieldCheck className="h-6 w-6 text-signal-ink" aria-hidden="true" />
                <h1 className="font-display text-xl font-bold text-[hsl(var(--foreground))]">Two-factor authentication</h1>
              </div>
              <p className="text-sm text-[hsl(var(--muted-foreground))]">
                Your account has 2FA on. Enter the 6-digit code from your authenticator app to finish signing in with {name}.
              </p>
              <div>
                <label htmlFor="totp" className="block text-sm font-medium text-[hsl(var(--foreground))] mb-2">
                  Authentication code
                </label>
                <input
                  id="totp"
                  inputMode="numeric"
                  autoComplete="one-time-code"
                  pattern="\d{6}"
                  maxLength={6}
                  value={totp}
                  onChange={(e) => setTotp(e.target.value.replace(/\D/g, '').slice(0, 6))}
                  autoFocus
                  className="w-full px-4 py-3 rounded-lg bg-[hsl(var(--input))] border border-[hsl(var(--border))] text-[hsl(var(--foreground))] tracking-[0.3em] font-mono focus:outline-none focus:ring-2 focus:ring-signal focus:border-transparent"
                />
              </div>
              {totpError && (
                <div className="p-3 rounded-lg bg-red-900/20 border border-red-800" role="alert">
                  <p className="text-sm text-red-400">{totpError}</p>
                </div>
              )}
              <button
                type="submit"
                disabled={submitting || totp.length !== 6}
                className="w-full py-3 px-4 rounded-lg bg-accent hover:bg-accent-hover disabled:bg-accent/50 disabled:cursor-not-allowed text-white font-medium transition-colors flex items-center justify-center gap-2"
              >
                {submitting ? <Loader2 className="w-5 h-5 animate-spin" /> : null}
                {submitting ? 'Verifying…' : 'Verify and sign in'}
              </button>
            </form>
          )}

          {phase.kind === 'linked' && (
            <div className="space-y-4">
              <div className="flex items-start gap-3" role="status">
                <CheckCircle2 className="mt-0.5 h-5 w-5 flex-none text-green-500" aria-hidden="true" />
                <div>
                  <h1 className="font-display text-xl font-bold text-[hsl(var(--foreground))]">{name} connected</h1>
                  <p className="mt-1 text-sm text-[hsl(var(--muted-foreground))]">
                    You can now sign in with {name}. Manage sign-in methods in Settings → Security.
                  </p>
                </div>
              </div>
              <button
                type="button"
                onClick={back}
                className="w-full py-3 px-4 rounded-lg bg-accent hover:bg-accent-hover text-white font-medium transition-colors flex items-center justify-center gap-2"
              >
                <ArrowLeft className="w-5 h-5" aria-hidden="true" />
                {backLabel}
              </button>
            </div>
          )}

          {phase.kind === 'error' && (
            <div className="space-y-4">
              <div className="flex items-start gap-3" role="alert">
                <AlertTriangle className="mt-0.5 h-5 w-5 flex-none text-red-400" aria-hidden="true" />
                <div>
                  <h1 className="font-display text-xl font-bold text-[hsl(var(--foreground))]">
                    {fragment.from === 'settings' ? `Couldn't connect ${name}` : "Couldn't sign you in"}
                  </h1>
                  <p className="mt-1 text-sm text-[hsl(var(--muted-foreground))]">{phase.message}</p>
                </div>
              </div>
              <button
                type="button"
                onClick={back}
                className="w-full py-3 px-4 rounded-lg bg-accent hover:bg-accent-hover text-white font-medium transition-colors flex items-center justify-center gap-2"
              >
                <ArrowLeft className="w-5 h-5" aria-hidden="true" />
                {backLabel}
              </button>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
