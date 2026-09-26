// Smart-credit meter: a row of pixel cells (spent in ink, held for running
// enrichment in signal, what is left as open cells), plus the plan card on
// Home. Data: GET /cloud/usage/summary.

import { useId } from 'react';
import clsx from 'clsx';
import { AlertTriangle, ArrowRight } from 'lucide-react';
import type { UsageSummaryResponse } from '../../lib/api';
import { creditsView, degradedCopy, handoffsNote, planLine, resetLabel } from '../../lib/credits';
import { hrefFor } from '../../lib/nav';
import { Card, CardHeader } from '../relay/ui';

export function PixelMeter({ summary, cells = 40, className }: { summary: UsageSummaryResponse; cells?: number; className?: string }) {
  const view = creditsView(summary);
  const usedCells = Math.round(view.usedFraction * cells);
  const heldCells = Math.min(cells - usedCells, Math.round(view.reservedFraction * cells) || (view.reserved > 0 ? 1 : 0));
  return (
    <div
      role="meter"
      aria-label="Smart credits used this period"
      aria-valuemin={0}
      aria-valuemax={view.limit}
      aria-valuenow={Math.min(view.used, view.limit)}
      aria-valuetext={`${view.used.toLocaleString()} of ${view.limit.toLocaleString()} credits used, ${view.remaining.toLocaleString()} left`}
      className={clsx('flex gap-[2px]', className)}
    >
      {Array.from({ length: cells }, (_, i) => {
        const kind = i < usedCells ? 'used' : i < usedCells + heldCells ? 'held' : 'free';
        return (
          <span
            key={i}
            aria-hidden="true"
            className={clsx(
              'h-3 min-w-0 flex-1',
              kind === 'used' && 'bg-ink',
              kind === 'held' && 'bg-signal',
              kind === 'free' && 'rr-cell-open',
            )}
          />
        );
      })}
    </div>
  );
}

export function DegradedNotice({ summary, compact }: { summary: UsageSummaryResponse; compact?: boolean }) {
  const copy = degradedCopy(summary);
  if (!copy) return null;
  return (
    <div role="status" className={clsx('flex items-start gap-2.5 border-l-[3px] border-fail bg-fail-wash text-ink', compact ? 'px-3 py-2' : 'px-4 py-3')}>
      <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0 text-fail" aria-hidden="true" />
      <div className="min-w-0 text-sm">
        <p className="font-semibold">{copy.title}</p>
        <p className="mt-0.5 text-ink-2">{copy.body}</p>
      </div>
    </div>
  );
}

function Row({ label, value, note }: { label: string; value: string; note?: string }) {
  return (
    <div className="flex items-baseline justify-between gap-3 py-1.5">
      <dt className="text-sm text-ink-2">{label}</dt>
      <dd className="text-right">
        <span className="tabular font-mono text-xs text-ink">{value}</span>
        {note && <span className="ml-1.5 font-mono text-[11px] text-ink-3">{note}</span>}
      </dd>
    </div>
  );
}

/** Home: plan, credits left, and the free relay line. */
export function PlanMeter({ usage }: { usage: UsageSummaryResponse }) {
  const titleId = useId();
  const view = creditsView(usage);
  return (
    <Card labelledBy={titleId}>
      <CardHeader
        id={titleId}
        eyebrow={`Plan · ${planLine(usage)}`}
        title={
          <span className="flex items-baseline gap-2">
            <span className={clsx('tabular text-3xl font-extrabold', view.degraded && 'text-fail')}>{view.remaining.toLocaleString()}</span>
            <span className="text-base font-bold text-ink-2">credits left</span>
          </span>
        }
        action={
          <a
            href={hrefFor('billing')}
            className="inline-flex items-center gap-1 text-sm font-semibold text-ink underline decoration-signal decoration-2 underline-offset-4"
          >
            Billing <ArrowRight className="h-3.5 w-3.5" aria-hidden="true" />
          </a>
        }
      />
      <div className="px-4 pb-4 pt-3 sm:px-5">
        <PixelMeter summary={usage} cells={32} />
        <p className="mt-1.5 font-mono text-[11px] text-ink-3">
          {view.used.toLocaleString()} of {view.limit.toLocaleString()} used
          {view.reserved > 0 ? ` · ${view.reserved.toLocaleString()} held` : ''} · {resetLabel(usage)}
        </p>
        {view.degraded && (
          <div className="mt-3">
            <DegradedNotice summary={usage} compact />
          </div>
        )}
        {view.low && (
          <p className="mt-2 text-xs text-signal-ink">
            Running low. When credits run out, new memories still save, just without enrichment.
          </p>
        )}
        <dl className="mt-3 divide-y divide-rule border-y border-rule">
          <Row label="Relay events" value={usage.relay_events.this_month.toLocaleString()} note="always free" />
          <Row label="Recalls" value={`${usage.recalls.this_month.toLocaleString()} / ${usage.recalls.limit.toLocaleString()}`} note="free" />
          <Row
            label="Memories"
            value={`${usage.memories.stored.toLocaleString()} / ${usage.memories.cap.toLocaleString()}`}
            note={handoffsNote(usage) ?? undefined}
          />
        </dl>
        <p className="mt-2.5 text-xs text-ink-3">
          Relay is always free: handoffs, checkpoints, pickups, inbox and the trail never use credits. Credits pay for AI enrichment of
          stored memories.
        </p>
      </div>
    </Card>
  );
}
