import { useState } from 'react';
import { CheckCircle, Loader2, MailWarning } from 'lucide-react';
import { api } from '../../lib/api';
import { requestVerificationEmail } from '../../lib/authProviders';

interface EmailVerificationStatusProps {
  verified: boolean;
  /** Reload the profile (the address may have been verified in another tab). */
  onVerified: () => void;
}

/**
 * Profile: whether the account email is verified, and a way to get a new
 * verification link (the signup link expires after 24 hours). Verifying lifts
 * the unverified-email credit hold and lets Sign in with Google link to the account.
 */
export function EmailVerificationStatus({ verified, onVerified }: EmailVerificationStatusProps) {
  const [sending, setSending] = useState(false);
  const [notice, setNotice] = useState<{ kind: 'ok' | 'error'; text: string } | null>(null);

  if (verified) {
    return (
      <p className="mt-1 flex items-center gap-1.5 text-xs text-green-700 dark:text-green-400">
        <CheckCircle className="w-3.5 h-3.5" aria-hidden="true" />
        Verified
      </p>
    );
  }

  const resend = async () => {
    const jwt = api.getJwtToken();
    if (!jwt) return;
    setSending(true);
    setNotice(null);
    try {
      const result = await requestVerificationEmail(jwt);
      setNotice({ kind: 'ok', text: result.message });
      if (result.verified) onVerified();
    } catch (err) {
      setNotice({ kind: 'error', text: err instanceof Error ? err.message : 'Could not send the verification email.' });
    } finally {
      setSending(false);
    }
  };

  return (
    <div className="mt-2 p-3 rounded-lg border border-amber-200 bg-amber-50 dark:border-amber-800 dark:bg-amber-900/20">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <p className="flex items-center gap-1.5 text-sm text-amber-800 dark:text-amber-300">
          <MailWarning className="w-4 h-4 flex-none" aria-hidden="true" />
          Email not verified
        </p>
        <button
          type="button"
          onClick={() => void resend()}
          disabled={sending}
          className="px-3 py-1.5 rounded-lg text-sm font-medium bg-accent hover:bg-accent-hover text-white inline-flex items-center gap-1.5 disabled:opacity-50 disabled:cursor-not-allowed"
        >
          {sending ? <Loader2 className="w-4 h-4 animate-spin" aria-hidden="true" /> : null}
          {sending ? 'Sending…' : 'Resend verification email'}
        </button>
      </div>
      <p className="mt-2 text-xs text-amber-800/80 dark:text-amber-300/80">
        Verifying proves you own this address. Sign in with Google can only connect to a verified account.
      </p>
      {notice && (
        <p
          role={notice.kind === 'error' ? 'alert' : 'status'}
          className={
            notice.kind === 'error' ? 'mt-2 text-sm text-red-600 dark:text-red-400' : 'mt-2 text-sm text-green-700 dark:text-green-400'
          }
        >
          {notice.text}
        </p>
      )}
    </div>
  );
}
