import { useState, type ReactNode } from 'react';
import clsx from 'clsx';
import { Loader2 } from 'lucide-react';
import { BrandMark, Pill } from '../relay/ui';
import {
  accessLine,
  completeReview,
  itemCount,
  revokeReviewItem,
  webhookHost,
  type AccountReview as Review,
  type ReviewKind,
} from '../../lib/accountReview';
import { relativeTime } from '../../lib/time';

interface AccountReviewProps {
  jwt: string;
  email?: string;
  review: Review;
  /** Removing the password or a sign-in link signs out every session; this one continues with a new token. */
  onToken: (token: string) => void;
  onDone: () => void;
  onLater: () => void;
}

interface Row {
  kind: ReviewKind;
  id?: string;
  title: string;
  meta: string;
  late?: boolean;
  action: string;
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
        late: !k.before_verification,
        action: 'Revoke',
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
        late: !c.before_verification,
        action: 'Revoke',
      })),
    },
    {
      heading: 'Webhooks',
      rows: items.webhooks.map((w) => ({
        kind: 'webhook',
        id: w.id,
        title: webhookHost(w.url),
        meta: [w.events.join(', ') || 'all events', when(w.created_at, 'added')].filter(Boolean).join(' · '),
        late: !w.before_verification,
        action: 'Turn off',
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
        })),
        ...(items.password
          ? [{ kind: 'password' as const, title: 'Password', meta: 'set before your email was confirmed', action: 'Remove password' }]
          : []),
        ...(items.two_factor
          ? [{ kind: 'two_factor' as const, title: 'Two-factor sign-in', meta: 'turned on before your email was confirmed', action: 'Turn off' }]
          : []),
      ],
    },
  ];
  return groups.filter((g) => g.rows.length > 0);
}

/**
 * Blocking, one-screen account check. Shown once after the first proof that
 * the signed-in person owns the account's email; everything listed keeps
 * working until they choose. "Keep all" is one click.
 */
export function AccountReview({ jwt, email, review: initial, onToken, onDone, onLater }: AccountReviewProps) {
  const [review, setReview] = useState(initial);
  const [token, setToken] = useState(jwt);
  const [busy, setBusy] = useState<string | null>(null);
  const [confirming, setConfirming] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const groups = rowsOf(review);
  const count = itemCount(review.items);

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
      if (result.review.pending) setReview(result.review);
      setConfirming(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Could not remove it. Try again.');
    } finally {
      setBusy(null);
    }
  };

  const keepAll = async () => {
    setBusy('keep');
    setError(null);
    try {
      await completeReview(token);
      onDone();
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Could not save. Try again.');
      setBusy(null);
    }
  };

  return (
    <div className="min-h-dvh bg-paper px-4 py-10 sm:py-16">
      <main className="mx-auto w-full max-w-2xl" aria-labelledby="review-title">
        <div className="mb-8 flex items-center gap-2.5" role="img" aria-label="Remembra">
          <BrandMark size={32} className="text-ink" />
          <span className="font-display text-xl font-extrabold tracking-[-0.02em] text-ink">Remembra</span>
        </div>

        <p className="rr-eyebrow">One-time check</p>
        <h1 id="review-title" className="font-display mt-2 text-[clamp(1.6rem,1.2rem+1.2vw,2.2rem)] font-extrabold leading-[1.05] tracking-[-0.02em] text-ink">
          {count ? 'Is all of this yours?' : 'Nothing left to check.'}
        </h1>
        <p className="mt-3 max-w-[56ch] text-sm leading-relaxed text-ink-2">
          {count
            ? `Your email${email ? ` (${email})` : ''} is confirmed. These were set up on the account before that. They all still work. Keep them, or remove anything you don't recognise.`
            : 'Everything you did not keep is gone.'}
        </p>

        {groups.length > 0 && (
          <figure className="rr-win mt-8" aria-label="Set up on this account">
            <figcaption className="rr-win-bar">
              <i aria-hidden="true" />
              account.check
              <span>{count} item{count === 1 ? '' : 's'}</span>
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
                      />
                    ))}
                  </ul>
                </section>
              ))}
            </div>
          </figure>
        )}

        {error && (
          <p role="alert" className="mt-4 border-l-[3px] border-fail bg-fail-wash px-3 py-2 text-sm text-ink">
            {error}
          </p>
        )}

        <div className="mt-8 flex flex-col-reverse gap-3 sm:flex-row sm:items-center">
          <button
            type="button"
            onClick={() => void keepAll()}
            disabled={busy !== null}
            className="rr-btn-primary inline-flex items-center justify-center gap-2 px-5 py-3 text-sm"
          >
            {busy === 'keep' && <Loader2 className="h-4 w-4 animate-spin" aria-hidden="true" />}
            {count ? (count === 1 ? 'Keep it' : `Keep all ${count}`) : 'Done'}
          </button>
          {count > 0 && (
            <button
              type="button"
              onClick={onLater}
              disabled={busy !== null}
              className="rr-btn-ghost px-4 py-3 text-sm sm:ml-auto"
            >
              Later
            </button>
          )}
        </div>
        <p className="mt-4 font-mono text-[11px] text-ink-3">We email you what you kept.</p>
      </main>
    </div>
  );
}

function ReviewRow({
  row,
  busy,
  disabled,
  confirming,
  onAction,
  onCancel,
}: {
  row: Row;
  busy: boolean;
  disabled: boolean;
  confirming: boolean;
  onAction: () => void;
  onCancel: () => void;
}): ReactNode {
  return (
    <li className="flex flex-wrap items-center gap-x-3 gap-y-1.5 border-t border-dashed border-rule py-2.5 first:border-t-0">
      <span aria-hidden="true" className="h-2 w-2 shrink-0 rounded-full bg-signal" />
      <div className="min-w-0 flex-1">
        <p className="flex flex-wrap items-center gap-2 break-words text-sm font-semibold text-ink">
          {row.title}
          {row.late && <Pill tone="open">added since</Pill>}
        </p>
        {row.meta && <p className="mt-0.5 break-words text-xs text-ink-3">{row.meta}</p>}
      </div>
      <div className="flex shrink-0 items-center gap-2">
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
            'inline-flex items-center gap-1.5 px-2.5 py-1.5 text-xs',
            confirming ? 'rounded-[3px] border border-fail bg-fail-wash font-semibold text-fail' : 'rr-btn-ghost',
          )}
        >
          {busy && <Loader2 className="h-3.5 w-3.5 animate-spin" aria-hidden="true" />}
          {confirming ? `Yes, ${row.action.toLowerCase()}` : row.action}
        </button>
      </div>
    </li>
  );
}
