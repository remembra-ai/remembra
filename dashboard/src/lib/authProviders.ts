// Sign-in configuration and helpers for Sign in with GitHub / Google.
//
// The API is the OAuth client. The dashboard only navigates the browser to the
// provider's start path, then trades the single-use login code that comes back
// in the URL fragment (#code=...) for the normal dashboard session.

import { API_BASE_URL, API_V1 } from '../config';

export type AuthPage = 'login' | 'signup';

/** The route path without the app's base URL or a trailing slash ("/oauth/callback/" -> "/oauth/callback"). */
export function appPath(pathname: string = window.location.pathname, base: string = import.meta.env.BASE_URL): string {
  const prefix = base.endsWith('/') ? base.slice(0, -1) : base;
  const path = prefix && pathname.startsWith(prefix) ? pathname.slice(prefix.length) : pathname;
  const trimmed = path.replace(/\/+$/, '');
  return trimmed || '/';
}

export interface AuthProvider {
  id: string;
  name: string;
  start_path: string;
}

export interface AuthConfig {
  providers: AuthProvider[];
  turnstile_site_key: string | null;
}

export const EMPTY_AUTH_CONFIG: AuthConfig = { providers: [], turnstile_site_key: null };

/** Keep only well-formed entries; the page must never navigate to something odd. */
export function normalizeAuthConfig(raw: unknown): AuthConfig {
  if (!raw || typeof raw !== 'object') return EMPTY_AUTH_CONFIG;
  const data = raw as { providers?: unknown; turnstile_site_key?: unknown };
  const providers = Array.isArray(data.providers)
    ? data.providers.filter(
        (p): p is AuthProvider =>
          !!p &&
          typeof p === 'object' &&
          typeof (p as AuthProvider).id === 'string' &&
          /^[a-z0-9-]+$/.test((p as AuthProvider).id) &&
          typeof (p as AuthProvider).name === 'string' &&
          typeof (p as AuthProvider).start_path === 'string' &&
          (p as AuthProvider).start_path === `/api/v1/auth/oauth/${(p as AuthProvider).id}/start`,
      )
    : [];
  const siteKey =
    typeof data.turnstile_site_key === 'string' && data.turnstile_site_key.trim() ? data.turnstile_site_key.trim() : null;
  return { providers, turnstile_site_key: siteKey };
}

export async function fetchAuthConfig(signal?: AbortSignal): Promise<AuthConfig> {
  const response = await fetch(`${API_V1}/auth/providers`, { signal });
  if (!response.ok) throw new Error(`auth providers: HTTP ${response.status}`);
  return normalizeAuthConfig(await response.json());
}

export function providerStartUrl(provider: AuthProvider, from: AuthPage): string {
  return `${API_BASE_URL}${provider.start_path}?from=${from}`;
}

/** Google first: its guidelines ask for at least equal prominence with other providers. */
export function orderProviders(providers: AuthProvider[]): AuthProvider[] {
  const rank = (id: string) => (id === 'google' ? 0 : id === 'github' ? 1 : 2);
  return [...providers].sort((a, b) => rank(a.id) - rank(b.id));
}

export interface OAuthFragment {
  code: string | null;
  error: string | null;
  provider: string | null;
  from: AuthPage;
}

export function parseOAuthFragment(hash: string): OAuthFragment {
  const params = new URLSearchParams(hash.startsWith('#') ? hash.slice(1) : hash);
  const from = params.get('from') === 'signup' ? 'signup' : 'login';
  return {
    code: params.get('code'),
    error: params.get('error'),
    provider: params.get('provider'),
    from,
  };
}

export function providerName(id: string | null): string {
  if (id === 'github') return 'GitHub';
  if (id === 'google') return 'Google';
  return 'the provider';
}

/** Fixed copy per server error code; nothing from the URL is ever shown verbatim. */
export function oauthErrorMessage(code: string | null, provider: string | null): string {
  const name = providerName(provider);
  switch (code) {
    case 'access_denied':
      return `Sign-in with ${name} was cancelled.`;
    case 'invalid_state':
      return 'That sign-in attempt expired or did not start in this browser. Please try again.';
    case 'email_unverified':
      return provider === 'github'
        ? 'Your GitHub account needs a verified primary email address. Verify it in GitHub settings (Emails), then try again.'
        : `${name} did not confirm your email address. Verify it with ${name}, then try again.`;
    case 'email_not_authoritative':
      return 'Google cannot confirm who owns this email address. Use a Gmail or Google Workspace account, or sign up with email and password.';
    case 'account_exists_unverified':
      return `An account with this email already exists but its email is not verified. Sign in with your password (or use "Forgot password"), then ${name} sign-in will work.`;
    case 'identity_conflict':
      return `This Remembra account is already linked to a different ${name} account. Sign in with that ${name} account or with your password.`;
    case 'account_disabled':
      return 'This account is deactivated. Contact support if you think this is a mistake.';
    case 'rate_limited':
      return 'Too many new accounts from your network. Please try again later.';
    default:
      return `Something went wrong signing in with ${name}. Please try again.`;
  }
}

export interface PasswordChecks {
  length: boolean;
  upper: boolean;
  lower: boolean;
  number: boolean;
  special: boolean;
}

/** Mirrors the server's signup rules (api/v1/auth.py SignupRequest). */
export function passwordChecks(password: string): PasswordChecks {
  return {
    length: password.length >= 8,
    upper: /[A-Z]/.test(password),
    lower: /[a-z]/.test(password),
    number: /\d/.test(password),
    special: /[!@#$%^&*(),.?":{}|<>_\-+=[\]\\/`~]/.test(password),
  };
}
