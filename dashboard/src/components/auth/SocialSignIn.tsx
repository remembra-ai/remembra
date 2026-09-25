import { useEffect, useState } from 'react';
import { Loader2 } from 'lucide-react';
import { orderProviders, providerStartUrl, type AuthPage, type AuthProvider } from '../../lib/authProviders';

const GOOGLE_SANS_HREF = 'https://fonts.googleapis.com/css2?family=Google+Sans:wght@500&display=swap';

/** Google's button typeface, loaded only when the Google button is on screen. */
function useGoogleSans() {
  useEffect(() => {
    if (document.querySelector(`link[href="${GOOGLE_SANS_HREF}"]`)) return;
    const link = document.createElement('link');
    link.rel = 'stylesheet';
    link.href = GOOGLE_SANS_HREF;
    document.head.appendChild(link);
  }, []);
}

/** The standard four-colour Google "G", unmodified (required on the button). */
function GoogleG() {
  return (
    <svg className="social-btn__logo" viewBox="0 0 48 48" aria-hidden="true" focusable="false">
      <path
        fill="#EA4335"
        d="M24 9.5c3.54 0 6.71 1.22 9.21 3.6l6.85-6.85C35.9 2.38 30.47 0 24 0 14.62 0 6.51 5.38 2.56 13.22l7.98 6.19C12.43 13.72 17.74 9.5 24 9.5z"
      />
      <path
        fill="#4285F4"
        d="M46.98 24.55c0-1.57-.15-3.09-.38-4.55H24v9.02h12.94c-.58 2.96-2.26 5.48-4.78 7.18l7.73 6c4.51-4.18 7.09-10.36 7.09-17.65z"
      />
      <path
        fill="#FBBC05"
        d="M10.53 28.59c-.48-1.45-.76-2.99-.76-4.59s.27-3.14.76-4.59l-7.98-6.19C.92 16.46 0 20.12 0 24c0 3.88.92 7.54 2.56 10.78l7.97-6.19z"
      />
      <path
        fill="#34A853"
        d="M24 48c6.48 0 11.93-2.13 15.89-5.81l-7.73-6c-2.15 1.45-4.92 2.3-8.16 2.3-6.26 0-11.57-4.22-13.47-9.91l-7.98 6.19C6.51 42.62 14.62 48 24 48z"
      />
      <path fill="none" d="M0 0h48v48H0z" />
    </svg>
  );
}

function GoogleButton({ onClick, disabled }: { onClick: () => void; disabled: boolean }) {
  useGoogleSans();
  return (
    <button type="button" className="social-btn" onClick={onClick} disabled={disabled}>
      <GoogleG />
      <span>Continue with Google</span>
    </button>
  );
}

/** Text only: GitHub's logo may not be used without GitHub's written permission. */
function TextButton({ name, onClick, disabled }: { name: string; onClick: () => void; disabled: boolean }) {
  return (
    <button type="button" className="social-btn" onClick={onClick} disabled={disabled}>
      <span>Continue with {name}</span>
    </button>
  );
}

interface SocialSignInProps {
  providers: AuthProvider[];
  from: AuthPage;
  /** Another action on the page is running (e.g. the password form is submitting). */
  disabled?: boolean;
  dividerLabel: string;
}

/** "Continue with Google / GitHub" buttons above the email form. Renders nothing when none are enabled. */
export function SocialSignIn({ providers, from, disabled = false, dividerLabel }: SocialSignInProps) {
  const [redirecting, setRedirecting] = useState<AuthProvider | null>(null);

  // Coming back with the browser's Back button restores this page from the
  // bfcache with the "redirecting" state still set: clear it.
  useEffect(() => {
    const onShow = (event: PageTransitionEvent) => {
      if (event.persisted) setRedirecting(null);
    };
    window.addEventListener('pageshow', onShow);
    return () => window.removeEventListener('pageshow', onShow);
  }, []);

  if (providers.length === 0) return null;

  const go = (provider: AuthProvider) => {
    setRedirecting(provider);
    window.location.assign(providerStartUrl(provider, from));
  };
  const busy = disabled || redirecting !== null;

  return (
    <div className="space-y-3">
      <div className="space-y-2.5" aria-busy={redirecting !== null}>
        {orderProviders(providers).map((provider) =>
          provider.id === 'google' ? (
            <GoogleButton key={provider.id} onClick={() => go(provider)} disabled={busy} />
          ) : (
            <TextButton key={provider.id} name={provider.name} onClick={() => go(provider)} disabled={busy} />
          ),
        )}
      </div>
      <p className="text-center text-sm text-[hsl(var(--muted-foreground))]" role="status" aria-live="polite">
        {redirecting && (
          <span className="inline-flex items-center gap-2">
            <Loader2 className="h-4 w-4 animate-spin" aria-hidden="true" />
            Redirecting to {redirecting.name}…
          </span>
        )}
      </p>
      <div className="auth-divider" role="separator">
        {dividerLabel}
      </div>
    </div>
  );
}
