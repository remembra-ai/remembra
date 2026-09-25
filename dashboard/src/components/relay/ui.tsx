// Small building blocks for the relay pages, in the "baton trail" language:
// stone paper, hairline rules, mono metadata, signal orange for the baton.

import type { ReactNode } from 'react';
import clsx from 'clsx';
import { AlertTriangle, Check, Copy, RefreshCw } from 'lucide-react';
import { agentMeta } from '../../lib/agents';
import { explainError } from '../../lib/relay';
import { useCopy } from '../../hooks/useCopy';

export { BrandMark } from '../../brand/Brand';

export function Card({
  children,
  className,
  as: Tag = 'section',
  labelledBy,
}: {
  children: ReactNode;
  className?: string;
  as?: 'section' | 'div' | 'article';
  labelledBy?: string;
}) {
  return (
    <Tag aria-labelledby={labelledBy} className={clsx('rr-card rounded-[3px] min-w-0', className)}>
      {children}
    </Tag>
  );
}

export function CardHeader({
  id,
  eyebrow,
  title,
  action,
}: {
  id?: string;
  eyebrow?: string;
  title: ReactNode;
  action?: ReactNode;
}) {
  return (
    <div className="flex items-start justify-between gap-3 px-4 pt-4 sm:px-5">
      <div className="min-w-0">
        {eyebrow && <p className="rr-eyebrow">{eyebrow}</p>}
        <h2 id={id} className="font-display mt-1 text-lg font-bold leading-tight text-ink">
          {title}
        </h2>
      </div>
      {action && <div className="shrink-0">{action}</div>}
    </div>
  );
}

export function AgentAvatar({ agentId, size = 'md' }: { agentId: string | null | undefined; size?: 'sm' | 'md' | 'lg' }) {
  const meta = agentMeta(agentId);
  return (
    <span
      aria-hidden="true"
      className={clsx(
        'inline-flex shrink-0 select-none items-center justify-center rounded-[3px] font-mono font-bold text-white',
        size === 'sm' && 'h-6 w-6 text-[9px]',
        size === 'md' && 'h-8 w-8 text-[10px]',
        size === 'lg' && 'h-11 w-11 text-xs',
      )}
      style={{ background: meta.lane }}
    >
      {meta.monogram}
    </span>
  );
}

export function AgentName({ agentId, className }: { agentId: string | null | undefined; className?: string }) {
  const meta = agentMeta(agentId);
  return (
    <span className={clsx('font-display font-bold tracking-tight text-ink', className)} title={meta.id || undefined}>
      {meta.name}
    </span>
  );
}

/** Orange pulse: active in the last hour. */
export function PulseDot({ active, label }: { active: boolean; label?: string }) {
  return (
    <span className="inline-flex items-center gap-1.5">
      <span
        aria-hidden="true"
        className={clsx('h-2 w-2 rounded-full', active ? 'rr-pulse bg-signal' : 'bg-rule')}
      />
      {label && <span className="sr-only">{label}</span>}
    </span>
  );
}

/** Daily activity as bars; today (the last bar) in signal orange. */
export function Sparkline({
  values,
  label,
  height = 32,
  className,
}: {
  values: number[];
  label: string;
  height?: number;
  className?: string;
}) {
  const max = Math.max(1, ...values);
  const width = values.length * 8;
  return (
    <svg
      role="img"
      aria-label={label}
      viewBox={`0 0 ${width} ${height}`}
      preserveAspectRatio="none"
      className={clsx('block w-full', className)}
      style={{ height }}
    >
      <line x1="0" x2={width} y1={height - 0.5} y2={height - 0.5} stroke="var(--rule)" strokeWidth="1" />
      {values.map((value, index) => {
        const h = value === 0 ? 0 : Math.max(3, Math.round((value / max) * (height - 3)));
        const isToday = index === values.length - 1;
        return (
          <rect
            key={index}
            x={index * 8 + 1.5}
            y={height - 1 - h}
            width="5"
            height={h}
            rx="1"
            fill={isToday ? 'var(--signal)' : 'var(--ink-3)'}
            opacity={isToday ? 1 : 0.75}
          />
        );
      })}
    </svg>
  );
}

/** A terminal line with a copy button (the landing page's install command). */
export function CopyCommand({
  command,
  label,
  className,
  toastText = 'Command copied',
}: {
  command: string;
  label: string;
  className?: string;
  toastText?: string;
}) {
  const [copy, copied] = useCopy();
  return (
    <div role="group" aria-label={label} className={clsx('rr-cmd flex items-stretch rounded-[3px]', className)}>
      <code className="flex min-w-0 flex-1 items-start gap-2 overflow-x-auto px-3 py-2.5 font-mono text-[13px] leading-relaxed [font-variant-ligatures:none]">
        <span aria-hidden="true" className="select-none text-signal">
          $
        </span>
        <span className="whitespace-pre-wrap [overflow-wrap:anywhere]">{command}</span>
      </code>
      <button
        type="button"
        onClick={() => copy(command, toastText)}
        aria-label={`Copy: ${label}`}
        className="flex shrink-0 items-center gap-1.5 border-l border-white/15 px-3 font-mono text-xs text-head-ink transition-colors hover:bg-signal/20"
      >
        {copied ? <Check className="h-3.5 w-3.5 text-signal" /> : <Copy className="h-3.5 w-3.5" />}
        <span className="hidden sm:inline">{copied ? 'Copied' : 'Copy'}</span>
      </button>
    </div>
  );
}

export function Skeleton({ className }: { className?: string }) {
  return <span aria-hidden="true" className={clsx('rr-skeleton block', className)} />;
}

/** Loading placeholder shaped like a list of trail nodes. */
export function TrailSkeleton({ rows = 4 }: { rows?: number }) {
  return (
    <div role="status" aria-label="Loading" className="space-y-5 px-4 py-4 sm:px-5">
      {Array.from({ length: rows }, (_, i) => (
        <div key={i} className="flex gap-3">
          <Skeleton className="h-8 w-8 shrink-0" />
          <div className="flex-1 space-y-2">
            <Skeleton className="h-3.5 w-2/5" />
            <Skeleton className="h-3 w-4/5" />
          </div>
        </div>
      ))}
    </div>
  );
}

/** An error that says what went wrong and how to fix it, with a retry. */
export function ErrorNotice({
  error,
  what,
  onRetry,
  compact,
}: {
  error: unknown;
  what: string;
  onRetry?: () => void;
  compact?: boolean;
}) {
  const { title, fix } = explainError(error, what);
  return (
    <div
      role="alert"
      className={clsx(
        'flex items-start gap-3 border-l-[3px] border-fail bg-fail-wash text-ink',
        compact ? 'px-3 py-2.5' : 'm-4 px-4 py-3 sm:m-5',
      )}
    >
      <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0 text-fail" aria-hidden="true" />
      <div className="min-w-0 flex-1 text-sm">
        <p className="font-semibold">{title}</p>
        <p className="mt-0.5 text-ink-2">{fix}</p>
      </div>
      {onRetry && (
        <button
          type="button"
          onClick={onRetry}
          className="rr-btn-ghost inline-flex shrink-0 items-center gap-1.5 px-2.5 py-1.5 text-xs"
        >
          <RefreshCw className="h-3.5 w-3.5" aria-hidden="true" /> Retry
        </button>
      )}
    </div>
  );
}

/** A quiet banner for a failed background refresh while older data stays on screen. */
export function StaleNotice({ error, what }: { error: unknown; what: string }) {
  const { title } = explainError(error, what);
  return (
    <p role="status" className="flex items-center gap-2 px-4 py-2 font-mono text-[11px] text-ink-3 sm:px-5">
      <span aria-hidden="true" className="h-1.5 w-1.5 rounded-full bg-fail" />
      Showing the last loaded data. {title}
    </p>
  );
}

export function Pill({
  tone = 'neutral',
  children,
  title,
}: {
  tone?: 'neutral' | 'fail' | 'open' | 'ok' | 'signal';
  children: ReactNode;
  title?: string;
}) {
  return (
    <span
      title={title}
      className={clsx(
        'inline-flex items-center gap-1 rounded-[2px] border px-1.5 py-0.5 font-mono text-[11px] leading-none whitespace-nowrap',
        tone === 'neutral' && 'border-rule text-ink-2',
        tone === 'fail' && 'border-fail/40 bg-fail-wash text-fail',
        tone === 'open' && 'border-signal/40 bg-signal-wash text-signal-ink',
        tone === 'ok' && 'border-ok/40 bg-ok-wash text-ok',
        tone === 'signal' && 'border-signal bg-signal text-on-signal',
      )}
    >
      {children}
    </span>
  );
}

export function StatNumber({ value, label, hint }: { value: ReactNode; label: string; hint?: string }) {
  return (
    <div className="min-w-0">
      <p className="font-display tabular text-3xl font-extrabold leading-none tracking-tight text-ink">{value}</p>
      <p className="mt-1 font-mono text-[11px] uppercase tracking-[0.08em] text-ink-3">{label}</p>
      {hint && <p className="mt-0.5 text-xs text-ink-3">{hint}</p>}
    </div>
  );
}
