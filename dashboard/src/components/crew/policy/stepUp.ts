// Human-only actions with step-up (spec D27, §5.9): revoke, transfer, unfreeze,
// settings, bypass codes and zone-change approval need a dashboard login from
// the last 15 minutes. When the server answers 401 `step_up_required`, the
// dashboard asks for the password again (the account is the signed-in one),
// stores the fresh login and retries the action once. API-key sessions are
// never human: those get a plain explanation instead of a prompt.

import { CrewApiError } from '../../../lib/crew/api';

export interface SignedInUser {
  id: string;
  email: string;
}

/** The signed-in dashboard user (App stores it next to the JWT), or null for an API-key session. */
export function signedInUser(storage: Pick<Storage, 'getItem'> | null = safeStorage()): SignedInUser | null {
  try {
    const raw = storage?.getItem('remembra_user');
    if (!raw) return null;
    const user = JSON.parse(raw) as Partial<SignedInUser>;
    return typeof user.email === 'string' && user.email ? { id: String(user.id ?? ''), email: user.email } : null;
  } catch {
    return null;
  }
}

function safeStorage(): Storage | null {
  try {
    return typeof localStorage === 'undefined' ? null : localStorage;
  } catch {
    return null;
  }
}

/** Sign in again with the same account; returns the fresh access token. */
export async function reauthenticate(
  fetchFn: (input: string, init?: RequestInit) => Promise<Response>,
  apiV1: string,
  email: string,
  password: string,
): Promise<string> {
  let res: Response;
  try {
    res = await fetchFn(`${apiV1}/auth/login`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
      body: JSON.stringify({ email, password }),
    });
  } catch {
    throw new Error('Could not reach the Remembra server.');
  }
  const body = (await res.json().catch(() => null)) as { access_token?: string; detail?: unknown } | null;
  if (!res.ok || !body?.access_token) {
    const detail = typeof body?.detail === 'string' ? body.detail : null;
    throw new Error(res.status === 401 ? 'That password did not match this account.' : detail || `Sign-in failed (${res.status}).`);
  }
  return body.access_token;
}

/** A short, human message for a failed crew action. */
export function actionErrorText(err: unknown): string {
  if (err instanceof CrewApiError) {
    if (err.stepUpRequired) return 'Sign in again to confirm this action (a login within the last 15 minutes is required).';
    if (err.humanOnly) return 'Only a dashboard login can do this; API keys never can.';
    if (err.status === 404) return 'Not found, or you no longer have access to it.';
    if (err.status === 412) return 'It changed while you were looking. The view has been refreshed; try again.';
    if (err.status === 423) return err.message || 'Locked: the zone is frozen, protected or part of crew policy.';
    return err.message;
  }
  return err instanceof Error ? err.message : String(err);
}

export function needsStepUp(err: unknown): boolean {
  return err instanceof CrewApiError && err.stepUpRequired;
}
