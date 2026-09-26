import { useState, type ReactNode } from 'react';
import clsx from 'clsx';
import { Loader2 } from 'lucide-react';
import { BrandMark } from '../relay/ui';
import {
  accessLine,
  completeReview,
  itemCount,
  keepTwoFactor,
  keptCount,
  revokeReviewItem,
  webhookHost,
  type AccountReview as Review,
  type ReviewKind,
} from '../../lib/accountReview';
import { relativeTime } from '../../lib/time';

export interface ReviewOutcome {
  kept: string[];
  removed: string[];
}

interface AccountReviewProps {
  jwt: string;
  email?: string;
  review: Review;
  /** Removing the password or a sign-in link signs out every session; this one continues with a new token. */
  onToken: (token: string) => void;
  onDone: (outcome: ReviewOutcome) => void;
  onLater: () => void;
}

interface Row {
  kind: ReviewKind;
  id?: string;
  title: string;
  meta: string;
  action: string;
  /** Short confirm label, so the row still fits on a phone. */
  confirm: string;
}

function when(value: string | null, prefix: string): string | null {
  return value ? `${prefix} ${relativeTime(value)}` : null;
}

function rowsOf(review: Review): { heading: string; rows: Row[] }[] {
  const items = review.items;
  if (!items) return [];
  const groups: { heading: string; rows: Row[] }[] = [
    {
      heading: 'API keys',
      rows: items.keys.map((k) => ({
        kind: 'key',
        id: k.id,
        title: k.name || 'Unnamed key',
        meta: [accessLine(k.role, k.project_ids, k.agent_id), when(k.created_at, 'made'), k.last_used_at ? when(k.last_used_at, 'used') : 'never used']
          .filter(Boolean)
          .join(' · '),
        action: 'Revoke',
        confirm: 'Yes, revoke',
      })),
    },
    {
      heading: 'Connected apps',
      rows: items.connections.map((c) => ({
        kind: 'connection',
        id: c.id,
        title: c.name,
        meta: [accessLine(c.scopes.join(' ') || 'access', c.project_ids, c.agent_id), when(c.created_at, 'added'), when(c.last_used_at, 'used')]
          .filter(Boolean)
          .join(' · '),
        action: 'Revoke',
        confirm: 'Yes, revoke',
      })),
    },
    {
      heading: 'Webhooks',
      rows: items.webhooks.map((w) => ({
        kind: 'webhook',
        id: w.id,
        title: webhookHost(w.url),
        meta: [w.events.join(', ') || 'all events', when(w.created_at, 'added')].filter(Boolean).join(' · '),
        action: 'Turn off',
        confirm: 'Yes, turn off',
      })),
    },
    {
      heading: 'Sign-in',
      rows: [
        ...items.identities.map<Row>((i) => ({
          kind: 'identity',
          id: i.id,
          title: `${i.name} (${i.email})`,
          meta: when(i.created_at, 'added') ?? '',
          action: 'Disconnect',
          confirm: 'Yes, disconnect',
        })),
        ...(items.password
          ? [{ kind: 'password' as const, title: 'Password', meta: 'set before your email was confirmed', action: 'Remove password', confirm: 'Yes, remove' }]
          : []),
        ...(items.two_factor
          ? [
              {
                kind: 'two_factor' as const,
                title: 'Two-factor sign-in',
                meta: 'Turns off when you finish, unless you enter a code from your app.',
                action: 'Turn off',
                confirm: 'Yes, turn off',
              },
            ]
          : []),
      ],
    },
  ];
  return groups.filter((g) => g.rows.length > 0);
}

/**
 * Blocking, one-screen account check. Shown once after the first proof that
 * the signed-in person owns the account's email, and only when something is
 * listed; everything listed keeps working until they choose. "Keep all" is
 * one click and keeps exactly what is on screen.
 */
export function AccountReview({ jwt, email, review: initial, onToken, onDone, onLater }: AccountReviewProps) {
  const [review, setReview] = useState(initial);
  const [token, setToken] = useState(jwt);
  const [busy, setBusy] = useState<string | null>(null);
  const [confirming, setConfirming] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [code, setCode] = useState('');
  const groups = rowsOf(review);
  const count = itemCount(review.items);
  const keeps = keptCount(review.items);
  const twoFactorListed = review.items?.two_factor === true;

  const keyOf = (row: Row) => `${row.kind}:${row.id ?? ''}`;

  const revoke = async (row: Row) => {
    const key = keyOf(row);
    if (confirming !== key) {
      setConfirming(key);
      return;
    }
    setBusy(key);
    setError(null);
    try {
      const result = await revokeReviewItem(token, row.kind, row.id);
      if (result.accessToken) {
        setToken(result.accessToken);
        onToken(result.accessToken);
      }
      setConfirming(null);
      // The last item: the check finished on the server.
      if (!result.review.pending) {
        onDone({ kept: [], removed: [row.title] });
        return;
      }
      setReview(result.review);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Could not remove it. Try again.');
    } finally {
      setBusy(null);
    }
  };

  const keepCode = async () => {
    setBusy('keep-2fa');
    setError(null);
    try {
      const result = await keepTwoFactor(token, code.trim());
      setCode('');
      if (!result.review.pending) {
        onDone({ kept: ['Two-factor sign-in'], removed: [] });
        return;
      }
      setReview(result.review);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'That code did not work. Try the current one.');
    } finally {
      setBusy(null);
    }
  };

  const keepAll = async () => {
    if (!review.version) return;
    setBusy('keep');
    setError(null);
    try {
      const result = await completeReview(token, review.version);
      if (result.done) {
        onDone({ kept: result.kept, removed: result.removed });
        return;
      }
      // Something changed while this was on screen: show the new list, keep nothing yet.
      setReview(result.review);
      setConfirming(null);
      setError(result.message);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Could not save. Try again.');
    }
    setBusy(null);
  };

  return (
    <div className="min-h-dvh bg-paper px-4 pt-10 sm:py-16">
      <main className="mx-auto w-full max-w-2xl" aria-labelledby="review-title">
        <div className="mb-8 flex items-center gap-2.5" role="img" aria-label="Remembra">
          <BrandMark size={32} className="text-ink" />
          <span className="font-display text-xl font-extrabold tracking-[-0.02em] text-ink">Remembra</span>
        </div>

        <p className="rr-eyebrow">One-time check</p>
        <h1 id="review-title" className="font-display mt-2 text-[clamp(1.6rem,1.2rem+1.2vw,2.2rem)] font-extrabold leading-[1.05] tracking-[-0.02em] text-ink">
          Is all of this yours?
        </h1>
        <p className="mt-3 max-w-[56ch] text-sm leading-relaxed text-ink-2">
          {`Your email${email ? ` (${email})` : ''} is confirmed. These are set up on your account and all still work. Keep them, or remove anything you don't recognise.`}
        </p>

        {groups.length > 0 && (
          <figure className="rr-win mt-8" aria-label="Set up on your account">
            <figcaption className="rr-win-bar">
              <i aria-hidden="true" />
              Your account
              <span>
                {count} item{count === 1 ? '' : 's'}
              </span>
            </figcaption>
            <div className="px-3 pb-3 pt-1 sm:px-4">
              {groups.map((group) => (
                <section key={group.heading} className="pt-3" aria-label={group.heading}>
                  <h2 className="pb-1 font-mono text-[11px] uppercase tracking-[0.08em] text-ink-3">{group.heading}</h2>
                  <ul>
                    {group.rows.map((row) => (
                      <ReviewRow
                        key={keyOf(row)}
                        row={row}
                        busy={busy === keyOf(row)}
                        disabled={busy !== null}
                        confirming={confirming === keyOf(row)}
                        onAction={() => void revoke(row)}
                        onCancel={() => setConfirming(null)}
                        keeper={
                          row.kind === 'two_factor' ? (
                            <TwoFactorKeeper
                              code={code}
                              busy={busy === 'keep-2fa'}
                              disabled={busy !== null}
                              onCode={setCode}
                              onKeep={() => void keepCode()}
                            />
                          ) : null
                        }
                      />
                    ))}
                  </ul>
                </section>
              ))}
            </div>
          </figure>
        )}

        {/* Sticky on phones so "Keep all" (and why it did not go through) never sits below the fold. */}
        <div className="sticky bottom-0 -mx-4 mt-6 border-t border-rule bg-paper px-4 pb-4 pt-3 sm:static sm:mx-0 sm:mt-8 sm:border-0 sm:p-0">
          {error && (
            <p role="alert" className="mb-3 border-l-[3px] border-fail bg-fail-wash px-3 py-2 text-sm text-ink">
              {error}
            </p>
          )}
          <div className="flex flex-col-reverse gap-3 sm:flex-row sm:items-center">
            <button
              type="button"
              onClick={() => void keepAll()}
              disabled={busy !== null || !review.version}
              className="rr-btn-primary inline-flex items-center justify-center gap-2 px-5 py-3 text-sm"
            >
              {busy === 'keep' && <Loader2 className="h-4 w-4 animate-spin" aria-hidden="true" />}
              {keeps === 0 ? 'Finish' : keeps === 1 ? 'Keep it' : `Keep all ${keeps}`}
            </button>
            <button
              type="button"
              onClick={onLater}
              disabled={busy !== null}
              className="rr-btn-ghost px-4 py-3 text-sm sm:ml-auto"
            >
              Later
            </button>
          </div>
          <p className="mt-3 font-mono text-[11px] text-ink-3">
            {twoFactorListed ? 'Two-factor sign-in turns off unless you keep it with a code. ' : ''}We email you what you kept.
          </p>
        </div>
      </main>
    </div>
  );
}

function TwoFactorKeeper({
  code,
  busy,
  disabled,
  onCode,
  onKeep,
}: {
  code: string;
  busy: boolean;
  disabled: boolean;
  onCode: (value: string) => void;
  onKeep: () => void;
}): ReactNode {
  const ready = /^\d{6}$/.test(code.trim());
  return (
    <form
      className="flex items-center gap-2"
      onSubmit={(event) => {
        event.preventDefault();
        if (ready) onKeep();
      }}
    >
      <label className="sr-only" htmlFor="review-2fa-code">
        Code from your authenticator app
      </label>
      <input
        id="review-2fa-code"
        className="rr-input w-[7.5rem] px-2.5 py-1.5 font-mono text-xs tracking-[0.2em]"
        inputMode="numeric"
        autoComplete="one-time-code"
        maxLength={6}
        placeholder="123456"
        value={code}
        onChange={(event) => onCode(event.target.value.replace(/\D/g, '').slice(0, 6))}
        disabled={disabled}
      />
      <button type="submit" disabled={disabled || !ready} className="rr-btn-ghost inline-flex items-center gap-1.5 px-2.5 py-1.5 text-xs">
        {busy && <Loader2 className="h-3.5 w-3.5 animate-spin" aria-hidden="true" />}
        Keep
      </button>
    </form>
  );
}

function ReviewRow({
  row,
  busy,
  disabled,
  confirming,
  onAction,
  onCancel,
  keeper,
}: {
  row: Row;
  busy: boolean;
  disabled: boolean;
  confirming: boolean;
  onAction: () => void;
  onCancel: () => void;
  keeper: ReactNode;
}): ReactNode {
  return (
    <li className="flex flex-wrap items-center gap-x-3 gap-y-2 border-t border-dashed border-rule py-2.5 first:border-t-0">
      <span aria-hidden="true" className="h-2 w-2 shrink-0 rounded-full bg-signal" />
      <div className="min-w-[10rem] flex-1">
        <p className="break-words text-sm font-semibold text-ink">{row.title}</p>
        {row.meta && <p className="mt-0.5 break-words text-xs text-ink-3">{row.meta}</p>}
      </div>
      {/* On a phone the actions take their own line instead of squeezing the text. */}
      <div
        className={clsx(
          'ml-auto flex items-center justify-end gap-2',
          keeper || confirming ? 'basis-full sm:basis-auto' : 'shrink-0',
        )}
      >
        {keeper && !confirming && keeper}
        {confirming && !busy && (
          <button type="button" onClick={onCancel} className="px-2 py-1.5 text-xs text-ink-3 hover:text-ink">
            Cancel
          </button>
        )}
        <button
          type="button"
          onClick={onAction}
          disabled={disabled}
          className={clsx(
            'inline-flex shrink-0 items-center gap-1.5 whitespace-nowrap px-2.5 py-1.5 text-xs',
            confirming ? 'rounded-[3px] border border-fail bg-fail-wash font-semibold text-fail' : 'rr-btn-ghost',
          )}
        >
          {busy && <Loader2 className="h-3.5 w-3.5 animate-spin" aria-hidden="true" />}
          {confirming ? row.confirm : row.action}
        </button>
      </div>
    </li>
  );
}
