// Feed filters. They live in the URL (§9.1: type, session, zone, task,
// moments), so a filtered feed can be bookmarked or opened from an alert.
// On a phone they fold into one "Filters" disclosure.

import clsx from 'clsx';
import { SlidersHorizontal, X } from 'lucide-react';
import type { FeedFilters as Filters } from '../../../lib/crew/routes';
import type { CrewState } from '../../../lib/crew/types';
import { NO_FILTERS, TYPE_GROUPS, groupActive, hasFilters, toggleGroup } from './model';

function activeCount(f: Filters): number {
  return TYPE_GROUPS.filter((g) => groupActive(f, g)).length + (f.session ? 1 : 0) + (f.zone ? 1 : 0) + (f.task ? 1 : 0) + (f.moments ? 1 : 0);
}

function Select({
  label,
  value,
  options,
  onChange,
}: {
  label: string;
  value: string | null;
  options: { value: string; label: string }[];
  onChange: (value: string | null) => void;
}) {
  // keep a value from the URL selectable even if it is not in the live state
  const opts = value && !options.some((o) => o.value === value) ? [{ value, label: value }, ...options] : options;
  return (
    <label className="flex min-w-0 items-center gap-1.5 font-mono text-[11px] text-ink-3">
      <span className="shrink-0 uppercase tracking-[0.06em]">{label}</span>
      <select
        value={value ?? ''}
        onChange={(e) => onChange(e.target.value || null)}
        className="rr-input min-w-0 max-w-[10rem] px-1.5 py-1 font-mono text-[12px] text-ink"
      >
        <option value="">all</option>
        {opts.map((o) => (
          <option key={o.value} value={o.value}>
            {o.label}
          </option>
        ))}
      </select>
    </label>
  );
}

function Controls({ filters, state, onChange }: { filters: Filters; state: CrewState | null; onChange: (next: Filters) => void }) {
  const sessions = state
    ? Object.values(state.sessions)
        .sort((a, b) => a.callsign.localeCompare(b.callsign, undefined, { numeric: true }))
        .map((s) => ({ value: s.callsign, label: `${s.callsign} · ${s.agent_id}` }))
    : [];
  const zones = state
    ? Object.values(state.zones)
        .sort((a, b) => a.slug.localeCompare(b.slug))
        .map((z) => ({ value: z.slug, label: z.slug }))
    : [];
  const tasks = state
    ? Object.values(state.tasks)
        .sort((a, b) => b.number - a.number)
        .map((t) => ({ value: `T-${t.number}`, label: `T-${t.number}` }))
    : [];
  return (
    <div className="flex flex-col gap-2.5">
      <div role="group" aria-label="Event types" className="flex flex-wrap gap-1.5">
        {TYPE_GROUPS.map((g) => {
          const on = groupActive(filters, g);
          return (
            <button
              key={g.id}
              type="button"
              aria-pressed={on}
              onClick={() => onChange(toggleGroup(filters, g))}
              className={clsx(
                'rounded-[2px] border px-2 py-1 font-mono text-[11px] leading-none transition-colors',
                on ? 'border-ink bg-ink text-paper' : 'border-rule text-ink-2 hover:border-ink hover:text-ink',
              )}
            >
              {on && <span aria-hidden="true">✓ </span>}
              {g.label}
            </button>
          );
        })}
      </div>
      <div className="flex flex-wrap items-center gap-x-4 gap-y-2">
        <Select label="Agent" value={filters.session} options={sessions} onChange={(session) => onChange({ ...filters, session })} />
        <Select label="Zone" value={filters.zone} options={zones} onChange={(zone) => onChange({ ...filters, zone })} />
        <Select label="Task" value={filters.task} options={tasks} onChange={(task) => onChange({ ...filters, task })} />
        <label className="flex items-center gap-1.5 font-mono text-[11px] uppercase tracking-[0.06em] text-ink-3">
          <input
            type="checkbox"
            checked={filters.moments}
            onChange={(e) => onChange({ ...filters, moments: e.target.checked })}
            className="h-3.5 w-3.5 accent-[var(--ink)]"
          />
          Moments only
        </label>
        {hasFilters(filters) && (
          <button type="button" onClick={() => onChange(NO_FILTERS)} className="inline-flex items-center gap-1 font-mono text-[11px] text-ink-2 underline-offset-2 hover:underline">
            <X className="h-3 w-3" aria-hidden="true" /> Clear
          </button>
        )}
      </div>
    </div>
  );
}

export function FeedFilters({ filters, state, onChange }: { filters: Filters; state: CrewState | null; onChange: (next: Filters) => void }) {
  const count = activeCount(filters);
  return (
    <>
      <div className="hidden md:block">
        <Controls filters={filters} state={state} onChange={onChange} />
      </div>
      <details className="group md:hidden">
        <summary className="flex cursor-pointer list-none items-center gap-2 font-mono text-[12px] text-ink-2 [&::-webkit-details-marker]:hidden">
          <SlidersHorizontal className="h-3.5 w-3.5" aria-hidden="true" />
          Filters{count ? ` · ${count} on` : ''}
          <span aria-hidden="true" className="text-ink-3 group-open:rotate-180">
            ▾
          </span>
        </summary>
        <div className="mt-3">
          <Controls filters={filters} state={state} onChange={onChange} />
        </div>
      </details>
    </>
  );
}
