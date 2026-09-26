// Sign-in configuration and helpers for Sign in with GitHub / Google.
//
// The API is the OAuth client. The dashboard only navigates the browser to the
// provider's start path, then trades the single-use login code that comes back
// in the URL fragment (#code=...) for the normal dashboard session.

import { API_BASE_URL, API_V1 } from '../config';

export type AuthPage = 'login' | 'signup';
/** Where a provider round trip started: a sign-in page, or Settings (connecting a provider). */
export type OAuthOrigin = AuthPage | 'settings';

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
  from: OAuthOrigin;
  /** A provider was connected to the signed-in account (Settings flow); there is no code. */
  linked: boolean;
}

export function parseOAuthFragment(hash: string): OAuthFragment {
  const params = new URLSearchParams(hash.startsWith('#') ? hash.slice(1) : hash);
  const rawFrom = params.get('from');
  const from: OAuthOrigin = rawFrom === 'signup' || rawFrom === 'settings' ? rawFrom : 'login';
  return {
    code: params.get('code'),
    error: params.get('error'),
    provider: params.get('provider'),
    from,
    linked: params.get('linked') === '1',
  };
}

/**
 * Trade the single-use login code for a session. `credentials: 'include'` sends
 * the HttpOnly cookie the API set alongside the code: the API refuses a code
 * that arrives without it (a code planted from another browser).
 */
export function exchangeLoginCode(code: string, totpCode?: string): Promise<Response> {
  return fetch(`${API_V1}/auth/oauth/exchange`, {
    method: 'POST',
    credentials: 'include',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(totpCode ? { code, totp_code: totpCode } : { code }),
  });
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
      return `An account with this email already exists. Sign in with your password, then connect ${name} in Settings → Security.`;
    case 'account_exists_link_required':
      return `You already have an account with this email. Sign in with Google or your password, then connect ${name} in Settings → Security.`;
    case 'email_in_use':
      return 'This email is already used by another Remembra account. Sign in to that one, or use a different email.';
    case 'identity_conflict':
      return `This Remembra account is already linked to a different ${name} account. Sign in with that ${name} account or with your password.`;
    case 'identity_in_use':
      return `That ${name} account is already connected to a different Remembra account. Disconnect it there first, or use another ${name} account.`;
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

// ---------------------------------------------------------------------------
// Connected sign-in methods (Settings → Security)
// ---------------------------------------------------------------------------

export interface ConnectedIdentity {
  provider: string;
  name: string;
  email: string;
  created_at: string | null;
  last_login_at: string | null;
}

export interface IdentitiesState {
  identities: ConnectedIdentity[];
  available: AuthProvider[];
}

/** Thrown when connecting needs a fresh sign-in (the API wants a session from the last 15 minutes). */
export class ReauthRequiredError extends Error {}

async function detailOf(response: Response, fallback: string): Promise<string> {
  const data = await response.json().catch(() => ({}));
  return typeof data.detail === 'string' ? data.detail : fallback;
}

export async function fetchIdentities(jwt: string): Promise<IdentitiesState> {
  const response = await fetch(`${API_V1}/auth/identities`, { headers: { Authorization: `Bearer ${jwt}` } });
  if (!response.ok) throw new Error(await detailOf(response, 'Could not load sign-in methods.'));
  const data = (await response.json()) as { identities?: unknown; available?: unknown };
  const identities = Array.isArray(data.identities)
    ? data.identities.filter(
        (i): i is ConnectedIdentity =>
          !!i && typeof i === 'object' && typeof (i as ConnectedIdentity).provider === 'string' && typeof (i as ConnectedIdentity).email === 'string',
      )
    : [];
  return { identities, available: normalizeAuthConfig({ providers: data.available }).providers };
}

/**
 * The browser URL that starts connecting `provider` to the signed-in account.
 * Only a path on the API origin is accepted, so a bad response cannot send the
 * browser anywhere else. `credentials: 'include'` stores the HttpOnly cookie
 * that binds the ticket to this browser: the API refuses the start path in any
 * other browser (an attacker's ticket opened by a victim).
 */
export async function requestProviderLink(jwt: string, provider: string): Promise<string> {
  const response = await fetch(`${API_V1}/auth/oauth/${encodeURIComponent(provider)}/link`, {
    method: 'POST',
    credentials: 'include',
    headers: { Authorization: `Bearer ${jwt}` },
  });
  if (response.status === 403) throw new ReauthRequiredError(await detailOf(response, 'Sign in again to continue.'));
  if (!response.ok) throw new Error(await detailOf(response, 'Could not start connecting the account.'));
  const data = (await response.json()) as { start_path?: unknown };
  const path = typeof data.start_path === 'string' ? data.start_path : '';
  if (!path.startsWith(`/api/v1/auth/oauth/${provider}/start?link=`)) throw new Error('Could not start connecting the account.');
  return `${API_BASE_URL}${path}`;
}

export async function disconnectProvider(jwt: string, provider: string): Promise<void> {
  const response = await fetch(`${API_V1}/auth/identities/${encodeURIComponent(provider)}`, {
    method: 'DELETE',
    headers: { Authorization: `Bearer ${jwt}` },
  });
  if (!response.ok) throw new Error(await detailOf(response, 'Could not disconnect.'));
}

/** Email the verification link again (signed-in dashboard account). */
export async function requestVerificationEmail(jwt: string): Promise<{ message: string; verified: boolean }> {
  const response = await fetch(`${API_V1}/auth/verify-email/request`, {
    method: 'POST',
    headers: { Authorization: `Bearer ${jwt}` },
  });
  if (response.status === 429) throw new Error('Too many requests. Wait a minute, then try again.');
  if (!response.ok) throw new Error(await detailOf(response, 'Could not send the verification email.'));
  const data = (await response.json().catch(() => ({}))) as { email_verified?: unknown };
  const verified = data.email_verified === true;
  return {
    verified,
    message: verified ? 'Your email is already verified.' : 'Verification email sent. Open the link in it to finish.',
  };
}
