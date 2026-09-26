// Small pieces shared by the board, the report flow and the receipt. Every
// state carries a text label (status never depends on colour alone, §9).

import clsx from 'clsx';
import { useMemo } from 'react';
import { absoluteTime } from '../../../lib/time';
import type { FactsSource } from '../../../lib/crew/types';
import {
  isStrongSource,
  sourceLabel,
  type AcceptanceMeter,
  type CheckpointDot,
  type OwnerInfo,
  type SealItem,
  type ZoneChipInfo,
} from './model';
import { stampPixels } from './pixel';

export function ZoneChip({ chip }: { chip: ZoneChipInfo }) {
  const modeText = chip.reserved ? 'reserved for the next pickup' : chip.mode;
  return (
    <span
      className="cb-zone"
      data-mode={chip.mode}
      data-reserved={chip.reserved}
      title={`zone ${chip.slug} · ${modeText}${chip.inherited ? ' · inherited through a baton' : ''}${chip.frozen ? ' · frozen by a human' : ''}`}
    >
      {(chip.reserved || chip.inherited) && <span aria-hidden="true">✦</span>}
      {chip.slug}
      <span className="sr-only">
        {' '}
        ({modeText}
        {chip.inherited ? ', inherited' : ''}
        {chip.frozen ? ', frozen' : ''})
      </span>
      {chip.frozen && <span aria-hidden="true">❄</span>}
    </span>
  );
}

export function MeterBlocks({ meter }: { meter: AcceptanceMeter }) {
  if (!meter.items.length) return <span className="font-mono text-[10.5px] text-ink-3">no criteria</span>;
  const label = meter.reported
    ? `${meter.done} of ${meter.required} required criteria met or waived${meter.unmet ? `, ${meter.unmet} unmet` : ''}`
    : `${meter.required} required criteria, not reported yet${meter.waived ? `, ${meter.waived} waived` : ''}`;
  return (
    <span className="inline-flex items-center gap-1.5" title={label}>
      <span className="cb-meter" aria-hidden="true">
        {meter.items.map((i) => (
          <span
            key={i.id}
            className="cb-block"
            data-state={i.state}
            data-weak={i.state === 'met' && !isStrongSource(i.source)}
            data-optional={!i.required}
          />
        ))}
      </span>
      <span className="tabular font-mono text-[10.5px] text-ink-2">
        {meter.done}/{meter.required}
      </span>
      <span className="sr-only">{label}</span>
    </span>
  );
}

export function CheckpointDots({ dots }: { dots: CheckpointDot[] }) {
  if (!dots.length) return null;
  const last = dots[dots.length - 1];
  return (
    <span className="inline-flex items-center gap-[5px]" title={`${dots.length} recent checkpoint${dots.length === 1 ? '' : 's'}; last: ${last.headline || last.trigger}`}>
      {dots.map((d, i) => (
        <span key={d.id} className="cb-ckp" data-recent={d.recent} data-latest={i === dots.length - 1} aria-hidden="true" />
      ))}
      <span className="sr-only">
        {dots.length} checkpoints, last {last.trigger}
        {last.recent ? ' in the last 10 minutes' : ''}
      </span>
    </span>
  );
}

export function OwnerTag({ owner }: { owner: OwnerInfo }) {
  if (!owner.agentId) return <span className="font-mono text-[11px] text-ink-3">unassigned</span>;
  return (
    <span className="inline-flex min-w-0 items-center gap-1 font-mono text-[11px] text-ink-2">
      <span
        aria-hidden="true"
        className={clsx('inline-block h-1.5 w-1.5 shrink-0', owner.live ? 'bg-ink' : 'border border-ink-3')}
      />
      <span className={clsx('font-semibold text-ink', owner.note ? 'shrink-0' : 'truncate')}>{owner.label}</span>
      {owner.verified !== null && (
        <span className="shrink-0 text-ink-3" title={owner.verified ? 'Agent identity proven by its API key' : 'Agent named itself; not proven'}>
          {owner.verified ? '✓key' : 'self'}
        </span>
      )}
      {owner.note && (
        <span className="min-w-0 truncate text-signal-ink" title={owner.note}>
          · {owner.note}
        </span>
      )}
      <span className="sr-only">
        {owner.live ? ', running' : ', not running'}
        {owner.verified === null ? '' : owner.verified ? ', key-verified' : ', self-declared'}
      </span>
    </span>
  );
}

export function SourceTag({ source, className }: { source: FactsSource | string | null | undefined; className?: string }) {
  const strong = isStrongSource(source);
  return (
    <span
      className={clsx(
        'inline-flex items-center gap-1 rounded-[2px] border px-1 py-px font-mono text-[10.5px] leading-tight whitespace-nowrap',
        !source && 'border-rule text-ink-3',
        source && strong && 'border-ink/60 text-ink',
        source && !strong && 'border-dashed border-ink-3 text-ink-2',
        className,
      )}
      title={
        source === 'relay-cli'
          ? 'Observed by a hook or the crew CLI on the agent’s machine'
          : source === 'server-verified'
            ? 'Checked by the Remembra server itself'
            : source === 'agent-declared'
              ? 'The agent said so; nothing observed it'
              : source === 'server-inferred'
                ? 'Inferred by the server from the last checkpoint'
                : 'No evidence'
      }
    >
      {sourceLabel(source)}
    </span>
  );
}

const MARK_GLYPH: Record<SealItem['mark'], string> = { ok: '✓', fail: '✗', waived: '≈', unknown: '?' };

/** The seal line: one chip per evidence group with its source label. */
export function SealLine({ items, fallback }: { items: SealItem[]; fallback?: string }) {
  if (!items.length) return fallback ? <p className="font-mono text-[10.5px] text-ink-2">{fallback}</p> : null;
  return (
    <ul className="flex flex-wrap gap-x-2 gap-y-0.5 font-mono text-[10.5px] leading-snug" aria-label="Receipt seal">
      {items.map((item) => (
        <li
          key={item.group}
          className={clsx(
            'whitespace-nowrap',
            item.mark === 'ok' && (isStrongSource(item.source) ? 'text-ink' : 'text-ink-2'),
            item.mark === 'fail' && 'text-fail',
            (item.mark === 'waived' || item.mark === 'unknown') && 'text-ink-3',
          )}
        >
          {item.group} <span aria-hidden="true">{MARK_GLYPH[item.mark]}</span>
          <span className="sr-only">{item.mark === 'ok' ? ' met' : item.mark === 'fail' ? ' not met' : item.mark === 'waived' ? ' waived' : ' unknown'}</span>
          {item.source && <span className={isStrongSource(item.source) ? '' : 'italic'}> {sourceLabel(item.source)}</span>}
        </li>
      ))}
    </ul>
  );
}

/**
 * The dithered seal stamp: a hand-inked ring seeded by the report id, with
 * the verdict glyph at its centre. Orange ink only while the report still
 * needs a person; ink when accepted; fail tone when rejected.
 */
export function SealStamp({
  seed,
  verdict,
  size = 44,
}: {
  seed: string;
  verdict: 'accepted' | 'review' | 'rejected' | 'waived' | 'partial' | 'superseded';
  size?: number;
}) {
  const grid = 22;
  const pixels = useMemo(() => stampPixels(seed, grid), [seed]);
  const color =
    verdict === 'review' ? 'var(--signal)' : verdict === 'rejected' || verdict === 'partial' ? 'var(--fail)' : verdict === 'superseded' ? 'var(--ink-3)' : 'var(--ink)';
  const glyph: number[][] =
    verdict === 'accepted'
      ? [[7, 11], [8, 12], [9, 13], [10, 12], [11, 11], [12, 10], [13, 9], [14, 8]]
      : verdict === 'waived'
        ? [[7, 10], [8, 10], [9, 10], [10, 10], [11, 10], [12, 10], [13, 10], [14, 10], [7, 12], [8, 12], [9, 12], [10, 12], [11, 12], [12, 12], [13, 12], [14, 12]]
        : verdict === 'review'
          ? [[10, 7], [11, 7], [10, 8], [11, 8], [10, 9], [11, 9], [10, 10], [11, 10], [10, 11], [11, 11], [10, 13], [11, 13], [10, 14], [11, 14]]
          : [[7, 7], [8, 8], [9, 9], [10, 10], [11, 11], [12, 12], [13, 13], [14, 14], [14, 7], [13, 8], [12, 9], [9, 12], [8, 13], [7, 14]];
  return (
    <svg className="cb-stamp shrink-0" width={size} height={size} viewBox={`0 0 ${grid} ${grid}`} aria-hidden="true">
      <g fill={color}>
        {pixels.map((p) => (
          <rect key={`${p.x}-${p.y}`} x={p.x} y={p.y} width="1" height="1" />
        ))}
        {glyph.map(([x, y]) => (
          <rect key={`g${x}-${y}`} x={x} y={y} width="1.02" height="1.02" />
        ))}
      </g>
    </svg>
  );
}

export function TimeAgo({ at, text }: { at: string | null | undefined; text: string | null }) {
  if (!text) return null;
  return (
    <time dateTime={at ?? undefined} title={absoluteTime(at ?? null)} className="tabular font-mono text-[10.5px] text-ink-3">
      {text}
    </time>
  );
}
