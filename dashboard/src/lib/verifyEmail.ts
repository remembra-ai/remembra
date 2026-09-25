import { API_V1 } from '../config';

/** sessionStorage key for a verification token opened while signed out. */
export const PENDING_VERIFY_KEY = 'remembra_pending_verify_email';

/** Confirm a dashboard account's email with the emailed token (requires the session). */
export async function confirmDashboardEmail(jwt: string, token: string): Promise<string> {
  const response = await fetch(`${API_V1}/auth/verify-email/confirm`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${jwt}` },
    body: JSON.stringify({ token }),
  });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new Error(
      typeof data.detail === 'string'
        ? `${data.detail}. If you signed in to a different account, sign in to the one this email belongs to.`
        : 'Verification failed.',
    );
  }
  return 'Your email is verified.';
}

/** Take (and clear) a token saved by the verify page before sign-in. */
export function takePendingVerifyToken(): string | null {
  try {
    const token = sessionStorage.getItem(PENDING_VERIFY_KEY);
    if (token) sessionStorage.removeItem(PENDING_VERIFY_KEY);
    return token;
  } catch {
    return null;
  }
}
