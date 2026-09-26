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
