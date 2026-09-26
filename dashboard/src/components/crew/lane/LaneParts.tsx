// The small pieces of a CrewLane (spec §9.15): PresencePulse, EnforcementBadge,
// ZoneChip, ReportRing and LimitMeter. Each one carries its meaning in text or
// a glyph as well as in colour (§9 "status never depends on colour alone").

import clsx from 'clsx';
import type { EnforcementView, LimitMeterView, PresenceView, ReportRingView, ZoneChipView } from './model';
import './lane.css';

export function PresencePulse({ presence, className }: { presence: PresenceView; className?: string }) {
  return (
    <span className={clsx('inline-flex min-w-0 items-center gap-2', className)}>
      <span aria-hidden="true" className="crew-pulse" data-kind={presence.kind} />
      <span className="min-w-0">
        <span className={clsx('font-mono text-[12px] font-semibold', presence.settled ? 'text-signal-ink' : 'text-ink')}>
          {presence.label}
        </span>
        {presence.detail && <span className="whitespace-nowrap font-mono text-[11px] text-ink-3"> · {presence.detail}</span>}
      </span>
      {presence.stuck && (
        <span className="rounded-[2px] border border-signal px-1 font-mono text-[10px] font-bold leading-4 text-signal-ink">⚠ stuck</span>
      )}
      {presence.fenced && (
        <span
          className="rounded-[2px] border border-dashed border-signal px-1 font-mono text-[10px] leading-4 text-signal-ink"
          title="Its lease could not be renewed: its own-zone writes are denied until it reconnects"
        >
          lease unconfirmed
        </span>
      )}
    </span>
  );
}

const LAYER_TEXT = { ok: '✓', missing: '✕', unknown: '?' } as const;

export function EnforcementBadge({ view }: { view: EnforcementView }) {
  if (view.alarm) {
    return (
      <span
        className="inline-flex items-center gap-1 rounded-[2px] bg-signal px-1.5 py-0.5 font-mono text-[11px] font-bold leading-none text-on-signal"
        title={`${view.text}. The git gates are not installed in this checkout: commits and pushes are not checked.`}
      >
        ⛔ commit gate: missing
      </span>
    );
  }
  const layers = [
    { key: 'write', label: 'before write', value: view.beforeWrite, ok: view.beforeWrite === 'enforced' },
    { key: 'commit', label: 'commit', value: LAYER_TEXT[view.commit], ok: view.commit === 'ok' },
    { key: 'push', label: 'push', value: LAYER_TEXT[view.push], ok: view.push === 'ok' },
  ];
  return (
    <span
      className="inline-flex flex-wrap items-center gap-x-1.5 gap-y-0.5 font-mono text-[11px] leading-none text-ink-3"
      title={`${view.text}. Local gating coordinates cooperative agents; every bypass is recorded.`}
    >
      <span className="sr-only">{view.text}</span>
      {layers.map((layer, i) => (
        <span key={layer.key} aria-hidden="true" className="inline-flex items-center gap-1">
          {i > 0 && <span className="text-rule">·</span>}
          {layer.label}
          <span className={clsx('font-semibold', layer.ok ? 'text-ok' : 'text-ink-2')}>{layer.value}</span>
        </span>
      ))}
    </span>
  );
}

export function ZoneChip({ chip }: { chip: ZoneChipView }) {
  const alarm = chip.fenced || chip.unconfirmed;
  return (
    <span
      className="crew-chip"
      data-style={chip.style}
      data-waiting={chip.waiting ? 'true' : undefined}
      data-reserved={chip.reserved ? 'true' : undefined}
      data-alarm={alarm ? 'true' : undefined}
      title={chip.title ? `${chip.description} · ${chip.title}` : chip.description}
    >
      <span className="sr-only">{chip.description}</span>
      <span aria-hidden="true" className="inline-flex items-center gap-1">
        {(chip.inherited || chip.reserved) && <span className="text-signal">✦</span>}
        {chip.waiting && <span>⧗</span>}
        {chip.label.toUpperCase()}
        {chip.mode !== 'exclusive' && <span className="opacity-70">{chip.mode}</span>}
        {chip.offered && <span className="opacity-70">→ offered</span>}
        {chip.reserved && <span className="opacity-70">reserved</span>}
        {chip.fenced && <span>fenced</span>}
      </span>
    </span>
  );
}

const RING_SEGMENTS = 12;

/** Twelve pixel ticks round a square: filled ticks = the way to the next checkpoint. */
export function ReportRing({ ring, streak }: { ring: ReportRingView; streak: number }) {
  const filled = Math.round(ring.fraction * RING_SEGMENTS);
  const ticks = Array.from({ length: RING_SEGMENTS }, (_, i) => {
    const angle = (i / RING_SEGMENTS) * Math.PI * 2 - Math.PI / 2;
    return { x: 11 + Math.cos(angle) * 8 - 1.5, y: 11 + Math.sin(angle) * 8 - 1.5, on: i < filled };
  });
  const tone = ring.missed ? 'var(--signal)' : 'var(--ink)';
  return (
    <span className="inline-flex items-center gap-2" title={`${ring.calls} tool calls since the last checkpoint`}>
      <svg width="22" height="22" viewBox="0 0 22 22" aria-hidden="true" shapeRendering="crispEdges">
        {ticks.map((t, i) => (
          <rect key={i} x={t.x} y={t.y} width="3" height="3" fill={t.on ? tone : 'var(--rule)'} />
        ))}
        <text x="11" y="13.5" textAnchor="middle" fontSize="7" fontFamily="var(--f-mono)" fill="var(--ink-2)">
          {Math.min(99, ring.calls)}
        </text>
      </svg>
      <span className="font-mono text-[11px] leading-tight">
        <span className={ring.missed ? 'font-bold text-signal-ink' : 'text-ink-2'}>{ring.label}</span>
        {ring.dueInS !== null || streak > 0 ? (
          <span className="block text-ink-3">{streak > 0 ? `streak ${streak} on time` : 'no checkpoint this hour'}</span>
        ) : null}
      </span>
    </span>
  );
}

const METER_CELLS = 10;

/** Usage limit as ten pixel cells, with its source (reported, detected, inferred). */
export function LimitMeter({ meter }: { meter: LimitMeterView | null }) {
  if (!meter) {
    return <span className="font-mono text-[11px] text-ink-3">limit: not reported</span>;
  }
  const filled =
    meter.pct !== null
      ? Math.round(meter.pct * METER_CELLS)
      : meter.level === 'exhausted'
        ? METER_CELLS
        : meter.level === 'critical'
          ? 9
          : meter.level === 'warn'
            ? 7
            : 3;
  return (
    <span className="inline-flex items-center gap-2" title={`Usage limit ${meter.text}`}>
      <span aria-hidden="true" className="inline-flex gap-[2px]">
        {Array.from({ length: METER_CELLS }, (_, i) => (
          <span
            key={i}
            className={clsx(
              'h-[9px] w-[5px]',
              i < filled ? (meter.alarm || (i >= 7 && meter.level !== 'ok') ? 'bg-signal' : 'bg-ink-2') : 'bg-rule',
            )}
          />
        ))}
      </span>
      <span className={clsx('font-mono text-[11px]', meter.alarm ? 'font-bold text-signal-ink' : 'text-ink-2')}>{meter.text}</span>
    </span>
  );
}
