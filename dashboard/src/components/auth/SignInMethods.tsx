import { useCallback, useEffect, useState } from 'react';
import { CheckCircle, KeyRound, Loader2, LogIn } from 'lucide-react';
import clsx from 'clsx';
import { api } from '../../lib/api';
import {
  disconnectProvider,
  fetchIdentities,
  orderProviders,
  ReauthRequiredError,
  requestProviderLink,
  type IdentitiesState,
} from '../../lib/authProviders';

interface SignInMethodsProps {
  /** Sign out, so the user can sign in again when connecting needs a fresh session. */
  onLogout: () => void;
}

interface Row {
  id: string;
  name: string;
  email: string | null;
  enabled: boolean;
}

function rowsOf(state: IdentitiesState): Row[] {
  const byProvider = new Map(state.identities.map((i) => [i.provider, i]));
  const rows: Row[] = orderProviders(state.available).map((p) => ({
    id: p.id,
    name: p.name,
    email: byProvider.get(p.id)?.email ?? null,
    enabled: true,
  }));
  // A provider switched off on the server can still be disconnected.
  for (const identity of state.identities) {
    if (!rows.some((r) => r.id === identity.provider)) {
      rows.push({ id: identity.provider, name: identity.name || identity.provider, email: identity.email, enabled: false });
    }
  }
  return rows;
}

/**
 * Settings → Security: connect or disconnect Sign in with Google / GitHub.
 *
 * Connecting always starts here, from a signed-in session. GitHub is never
 * linked to an existing account by matching email (GitHub does not re-verify
 * addresses), so this is the way to add it to an account that already exists.
 */
export function SignInMethods({ onLogout }: SignInMethodsProps) {
  const [state, setState] = useState<IdentitiesState | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [reauth, setReauth] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [confirming, setConfirming] = useState<string | null>(null);

  const load = useCallback(async () => {
    const jwt = api.getJwtToken();
    if (!jwt) {
      setLoading(false);
      return;
    }
    try {
      setState(await fetchIdentities(jwt));
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Could not load sign-in methods.');
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const connect = async (provider: string) => {
    const jwt = api.getJwtToken();
    if (!jwt) return;
    setBusy(provider);
    setError(null);
    setReauth(null);
    try {
      window.location.assign(await requestProviderLink(jwt, provider));
    } catch (err) {
      if (err instanceof ReauthRequiredError) setReauth(err.message);
      else setError(err instanceof Error ? err.message : 'Could not start connecting the account.');
      setBusy(null);
    }
  };

  const disconnect = async (provider: string) => {
    const jwt = api.getJwtToken();
    if (!jwt) return;
    setBusy(provider);
    setError(null);
    try {
      await disconnectProvider(jwt, provider);
      setConfirming(null);
      await load();
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Could not disconnect.');
    } finally {
      setBusy(null);
    }
  };

  if (loading) {
    return (
      <div className="bg-white dark:bg-gray-800 rounded-xl border border-gray-200 dark:border-gray-700 p-6 flex justify-center">
        <Loader2 className="w-6 h-6 animate-spin text-signal-ink" aria-label="Loading sign-in methods" />
      </div>
    );
  }

  const rows = state ? rowsOf(state) : [];
  // No providers configured on this server and nothing connected: nothing to manage.
  if (!error && rows.length === 0) return null;

  return (
    <section
      aria-labelledby="sign-in-methods-title"
      className="bg-white dark:bg-gray-800 rounded-xl border border-gray-200 dark:border-gray-700 p-6"
    >
      <div className="flex items-start gap-4">
        <div className="p-3 rounded-lg bg-gray-100 dark:bg-gray-700">
          <KeyRound className="w-6 h-6 text-gray-500 dark:text-gray-400" aria-hidden="true" />
        </div>
        <div className="min-w-0 flex-1">
          <h3 id="sign-in-methods-title" className="text-lg font-semibold text-gray-900 dark:text-white">
            Sign-in methods
          </h3>
          <p className="text-sm text-gray-500 dark:text-gray-400 mt-1">
            Connect Google or GitHub to sign in without your password. Your email and password keep working.
          </p>
        </div>
      </div>

      {error && (
        <div className="mt-4 p-3 rounded-lg bg-red-50 dark:bg-red-900/20 text-red-600 dark:text-red-400 text-sm" role="alert">
          {error}
        </div>
      )}

      {reauth && (
        <div
          className="mt-4 p-3 rounded-lg bg-amber-50 dark:bg-amber-900/20 border border-amber-200 dark:border-amber-800 text-sm"
          role="alert"
        >
          <p className="text-amber-800 dark:text-amber-300">
            {reauth} Connecting a sign-in method needs a sign-in from the last 15 minutes.
          </p>
          <button
            type="button"
            onClick={onLogout}
            className="mt-2 inline-flex items-center gap-1.5 font-medium text-amber-900 dark:text-amber-200 underline underline-offset-2"
          >
            <LogIn className="w-4 h-4" aria-hidden="true" />
            Sign out and sign in again
          </button>
        </div>
      )}

      <ul className="mt-5 divide-y divide-gray-200 dark:divide-gray-700 border-t border-gray-200 dark:border-gray-700">
        {rows.map((row) => (
          <li key={row.id} className="py-4 flex flex-wrap items-center justify-between gap-3">
            <div className="min-w-0">
              <p className="text-sm font-medium text-gray-900 dark:text-white">{row.name}</p>
              {row.email ? (
                <p className="mt-0.5 flex items-center gap-1.5 text-xs text-green-700 dark:text-green-400 break-all">
                  <CheckCircle className="w-3.5 h-3.5 flex-none" aria-hidden="true" />
                  Connected as {row.email}
                </p>
              ) : (
                <p className="mt-0.5 text-xs text-gray-500 dark:text-gray-400">Not connected</p>
              )}
            </div>

            {row.email ? (
              confirming === row.id ? (
                <div className="flex items-center gap-2">
                  <button
                    type="button"
                    onClick={() => void disconnect(row.id)}
                    disabled={busy === row.id}
                    className="px-3 py-1.5 rounded-lg text-sm font-medium bg-red-600 hover:bg-red-700 text-white disabled:opacity-50"
                  >
                    {busy === row.id ? <Loader2 className="w-4 h-4 animate-spin" /> : `Disconnect ${row.name}`}
                  </button>
                  <button
                    type="button"
                    onClick={() => setConfirming(null)}
                    className="px-3 py-1.5 rounded-lg text-sm font-medium text-gray-600 dark:text-gray-300 hover:bg-gray-100 dark:hover:bg-gray-700"
                  >
                    Cancel
                  </button>
                </div>
              ) : (
                <button
                  type="button"
                  onClick={() => setConfirming(row.id)}
                  className="px-3 py-1.5 rounded-lg text-sm font-medium bg-red-100 dark:bg-red-900/30 text-red-700 dark:text-red-400 hover:bg-red-200 dark:hover:bg-red-900/50"
                >
                  Disconnect
                </button>
              )
            ) : row.enabled ? (
              <button
                type="button"
                onClick={() => void connect(row.id)}
                disabled={busy !== null}
                className={clsx(
                  'px-3 py-1.5 rounded-lg text-sm font-medium bg-accent hover:bg-accent-hover text-white inline-flex items-center gap-1.5',
                  busy !== null && 'opacity-50 cursor-not-allowed',
                )}
              >
                {busy === row.id ? <Loader2 className="w-4 h-4 animate-spin" aria-hidden="true" /> : null}
                Connect {row.name}
              </button>
            ) : null}
          </li>
        ))}
      </ul>
      {confirming && (
        <p className="text-xs text-gray-500 dark:text-gray-400">
          After disconnecting, sign in with your password. No password yet? Use &ldquo;Forgot password&rdquo; on the sign-in page
          to set one.
        </p>
      )}
    </section>
  );
}
