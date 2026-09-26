// The one-time account check shown after the first proof that the signed-in
// person owns the account's email (Sign in with Google, or an emailed password
// reset). Everything set up before keeps working until the owner keeps it
// (one click) or revokes single items. See remembra.auth.account_review.

import { API_V1 } from '../config';

export type ReviewKind = 'key' | 'connection' | 'webhook' | 'identity' | 'two_factor' | 'password';

export interface ReviewKey {
  id: string;
  name: string | null;
  created_at: string | null;
  last_used_at: string | null;
  role: string;
  project_ids: string[];
  scopes: string[];
  agent_id: string | null;
  before_verification: boolean;
}

export interface ReviewConnection {
  id: string;
  name: string;
  created_at: string | null;
  last_used_at: string | null;
  project_ids: string[];
  scopes: string[];
  agent_id: string | null;
  before_verification: boolean;
}

export interface ReviewWebhook {
  id: string;
  url: string;
  events: string[];
  created_at: string | null;
  before_verification: boolean;
}

export interface ReviewIdentity {
  id: string;
  provider: string;
  name: string;
  email: string;
  created_at: string | null;
}

export interface ReviewItems {
  keys: ReviewKey[];
  connections: ReviewConnection[];
  webhooks: ReviewWebhook[];
  identities: ReviewIdentity[];
  two_factor: boolean;
  password: boolean;
}

export interface AccountReview {
  pending: boolean;
  canReview: boolean;
  origin: 'google' | 'password_reset' | null;
  verifiedAt: string | null;
  items: ReviewItems | null;
}

export const NO_REVIEW: AccountReview = { pending: false, canReview: false, origin: null, verifiedAt: null, items: null };

const str = (v: unknown): string | null => (typeof v === 'string' && v ? v : null);
const strs = (v: unknown): string[] => (Array.isArray(v) ? v.filter((x): x is string => typeof x === 'string') : []);
const objs = (v: unknown): Record<string, unknown>[] =>
  Array.isArray(v) ? v.filter((x): x is Record<string, unknown> => !!x && typeof x === 'object') : [];

/** Keep only well-formed fields; a malformed answer means "nothing to show", never a crash. */
export function normalizeReview(raw: unknown): AccountReview {
  if (!raw || typeof raw !== 'object') return NO_REVIEW;
  const data = raw as Record<string, unknown>;
  if (data.pending !== true) return NO_REVIEW;
  const canReview = data.can_review === true;
  const rawItems = data.items && typeof data.items === 'object' ? (data.items as Record<string, unknown>) : null;
  const items: ReviewItems | null =
    canReview && rawItems
      ? {
          keys: objs(rawItems.keys)
            .filter((k) => str(k.id))
            .map((k) => ({
              id: String(k.id),
              name: str(k.name),
              created_at: str(k.created_at),
              last_used_at: str(k.last_used_at),
              role: str(k.role) ?? 'editor',
              project_ids: strs(k.project_ids),
              scopes: strs(k.scopes),
              agent_id: str(k.agent_id),
              before_verification: k.before_verification !== false,
            })),
          connections: objs(rawItems.connections)
            .filter((c) => str(c.id))
            .map((c) => ({
              id: String(c.id),
              name: str(c.name) ?? 'App',
              created_at: str(c.created_at),
              last_used_at: str(c.last_used_at),
              project_ids: strs(c.project_ids),
              scopes: strs(c.scopes),
              agent_id: str(c.agent_id),
              before_verification: c.before_verification !== false,
            })),
          webhooks: objs(rawItems.webhooks)
            .filter((w) => str(w.id) && str(w.url))
            .map((w) => ({
              id: String(w.id),
              url: String(w.url),
              events: strs(w.events),
              created_at: str(w.created_at),
              before_verification: w.before_verification !== false,
            })),
          identities: objs(rawItems.identities)
            .filter((i) => str(i.id) && str(i.provider))
            .map((i) => ({
              id: String(i.id),
              provider: String(i.provider),
              name: str(i.name) ?? String(i.provider),
              email: str(i.email) ?? '',
              created_at: str(i.created_at),
            })),
          two_factor: rawItems.two_factor === true,
          password: rawItems.password === true,
        }
      : null;
  const origin = data.origin === 'google' || data.origin === 'password_reset' ? data.origin : null;
  return { pending: true, canReview: canReview && items !== null, origin, verifiedAt: str(data.verified_at), items };
}

export function itemCount(items: ReviewItems | null): number {
  if (!items) return 0;
  return (
    items.keys.length +
    items.connections.length +
    items.webhooks.length +
    items.identities.length +
    (items.two_factor ? 1 : 0) +
    (items.password ? 1 : 0)
  );
}

/** The check blocks the dashboard only for a session that may act on it, and not after "Later" this session. */
export function shouldShowReview(review: AccountReview, deferred: boolean): boolean {
  return review.pending && review.canReview && !deferred;
}

/** "Later" lasts for this browser session (sessionStorage), keyed to the account. */
const DEFER_KEY = 'remembra_review_later';

export function isDeferred(userId: string | null | undefined, store: Pick<Storage, 'getItem'> | null = safeSession()): boolean {
  if (!userId || !store) return false;
  try {
    return store.getItem(DEFER_KEY) === userId;
  } catch {
    return false;
  }
}

export function rememberDeferred(userId: string, store: Pick<Storage, 'setItem'> | null = safeSession()): void {
  try {
    store?.setItem(DEFER_KEY, userId);
  } catch {
    // Storage unavailable: the check simply shows again.
  }
}

export function clearDeferred(store: Pick<Storage, 'removeItem'> | null = safeSession()): void {
  try {
    store?.removeItem(DEFER_KEY);
  } catch {
    // Nothing to clear.
  }
}

function safeSession(): Storage | null {
  try {
    return typeof sessionStorage === 'undefined' ? null : sessionStorage;
  } catch {
    return null;
  }
}

async function detailOf(response: Response, fallback: string): Promise<string> {
  const data = await response.json().catch(() => ({}));
  return typeof data.detail === 'string' ? data.detail : fallback;
}

export async function fetchReview(jwt: string, signal?: AbortSignal): Promise<AccountReview> {
  const response = await fetch(`${API_V1}/auth/review`, { headers: { Authorization: `Bearer ${jwt}` }, signal });
  if (!response.ok) throw new Error(await detailOf(response, 'Could not load the account check.'));
  return normalizeReview(await response.json());
}

export interface RevokeResult {
  review: AccountReview;
  /** A new session token when every session was signed out (password or a sign-in link removed). */
  accessToken: string | null;
}

export async function revokeReviewItem(jwt: string, kind: ReviewKind, id?: string): Promise<RevokeResult> {
  const response = await fetch(`${API_V1}/auth/review/revoke`, {
    method: 'POST',
    headers: { Authorization: `Bearer ${jwt}`, 'Content-Type': 'application/json' },
    body: JSON.stringify(id ? { kind, id } : { kind }),
  });
  if (!response.ok) throw new Error(await detailOf(response, 'Could not remove it. Try again.'));
  const data = (await response.json()) as { review?: unknown; access_token?: unknown };
  return {
    review: normalizeReview(data.review),
    accessToken: typeof data.access_token === 'string' && data.access_token ? data.access_token : null,
  };
}

export async function completeReview(jwt: string): Promise<void> {
  const response = await fetch(`${API_V1}/auth/review/complete`, {
    method: 'POST',
    headers: { Authorization: `Bearer ${jwt}` },
  });
  // 409: already finished (another tab); nothing left to do.
  if (!response.ok && response.status !== 409) throw new Error(await detailOf(response, 'Could not save. Try again.'));
}

export async function deferReview(jwt: string): Promise<void> {
  await fetch(`${API_V1}/auth/review/defer`, { method: 'POST', headers: { Authorization: `Bearer ${jwt}` } }).catch(
    () => undefined,
  );
}

/** "editor · all projects · agent codex" */
export function accessLine(role: string, projectIds: string[], agentId: string | null): string {
  const parts = [role, projectIds.length ? projectIds.join(', ') : 'all projects'];
  if (agentId) parts.push(`agent ${agentId}`);
  return parts.join(' · ');
}

/** The host of a webhook URL (the path may carry a secret). */
export function webhookHost(url: string): string {
  try {
    return new URL(url).host || url;
  } catch {
    return url;
  }
}
