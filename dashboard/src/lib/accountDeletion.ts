// What deleting an account does, in the same words as the Terms (7.2) and the
// Privacy page (Data Retention). tests/test_account_copy.py keeps them equal and
// checks the numbers against the server defaults (erasure grace, backup keep).

export const DELETION_COPY =
  'Deleting your account cancels any subscription at once, with no further charges, and signs you out everywhere. ' +
  '7 days later everything the account holds is erased for good: memories, handoffs, inbox, API keys, connections and settings. ' +
  'Until then, email support@remembra.dev to undo it: sign-in comes back, but a cancelled subscription and revoked API keys do not. ' +
  'Backups are not edited; the copies in them age out: the continuous backup keeps 24 hours of history and drops older copies ' +
  'every hour, so an erased copy leaves it within about 25 hours, ' +
  'and the database copy taken before each deploy is kept only until 3 newer deploys replace it.';

export type DeletionMethod = 'password' | 'code';

/** Six digits, as emailed by POST /auth/me/deletion-code. */
export function isDeletionCode(value: string): boolean {
  return /^\d{6}$/.test(value.trim());
}

/** The confirm body for DELETE /auth/me, or null while the form is incomplete. */
export function deletionConfirm(
  method: DeletionMethod,
  typed: string,
  password: string,
  code: string,
): { password: string } | { code: string } | null {
  if (typed !== 'DELETE') return null;
  if (method === 'password') return password ? { password } : null;
  return isDeletionCode(code) ? { code: code.trim() } : null;
}

/** Shown in the delete dialog: deleting refunds nothing by itself (refunds page, "Renewals and cancelling"). */
export const REFUND_BEFORE_DELETE =
  'Deleting does not refund anything by itself. If your first payment was less than 14 days ago, ask ' +
  'support@remembra.dev for the refund before you delete the account.';

/** A team this account owns that has other members (DELETE /auth/me refuses with 409 TEAM_OWNER until confirmed). */
export interface OwnedTeam {
  id: string;
  name: string;
  members: number;
}

/** The teams named by a TEAM_OWNER refusal, or null when the error is something else. */
export function teamOwnerRefusal(error: unknown): { message: string; teams: OwnedTeam[] } | null {
  if (!error || typeof error !== 'object') return null;
  const { code, data, message } = error as { code?: string; data?: Record<string, unknown>; message?: string };
  if (code !== 'TEAM_OWNER') return null;
  const raw = Array.isArray(data?.teams) ? (data?.teams as unknown[]) : [];
  const teams = raw.filter(
    (t): t is OwnedTeam => !!t && typeof t === 'object' && typeof (t as OwnedTeam).name === 'string',
  );
  return { message: message || 'You own a team with other members.', teams };
}

const NOTICE_KEY = 'remembra.account_deleted_notice';

/** Keep the server's "your account is deleted" message for the sign-in screen after the sign-out. */
export function rememberDeletionNotice(message: string, store: Pick<Storage, 'setItem'> | null = safeSession()): void {
  try {
    store?.setItem(NOTICE_KEY, message);
  } catch {
    /* private mode or blocked storage: the sign-in screen simply shows no notice */
  }
}

/** The kept message, once (it is removed as it is read). */
export function takeDeletionNotice(store: Pick<Storage, 'getItem' | 'removeItem'> | null = safeSession()): string | null {
  try {
    const message = store?.getItem(NOTICE_KEY) ?? null;
    if (message) store?.removeItem(NOTICE_KEY);
    return message;
  } catch {
    return null;
  }
}

function safeSession(): Storage | null {
  try {
    return typeof window !== 'undefined' ? window.sessionStorage : null;
  } catch {
    return null;
  }
}
