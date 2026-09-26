// The L0 Zone Map (spec §9.5): the repository tree, set in mono with hand-drawn
// box guides, zones laid over the folders they cover. One row per folder:
// "pos/ · POS section · held by codex-1 (enforced) since 14:02 · T-14".
// role="tree" with roving focus: ↑/↓ move, → opens or steps in, ← closes or
// steps out, Home/End, Enter opens the zone drawer (or toggles a bare folder).

import { useMemo, useRef, useState, type KeyboardEvent } from 'react';
import type { CrewState, ZoneView } from '../../../lib/crew/types';
import { PixelGlyph } from './PixelGlyph';
import { PRIMARY_LABEL, flattenTree, rowHolderText, toneOf, type TreeRow, type ZoneStatus } from './zoneModel';

export function StateMark({ status }: { status: ZoneStatus }) {
  return (
    <span className="cz-state" data-tone={toneOf(status)}>
      <PixelGlyph name={status.primary} size={10} />
      {PRIMARY_LABEL[status.primary]}
      {status.pending && (
        <>
          <span aria-hidden="true">·</span>
          <PixelGlyph name="pending" size={10} />
          policy change pending
        </>
      )}
    </span>
  );
}

export function ZoneMark({ zone, status, inherited }: { zone: ZoneView; status?: ZoneStatus; inherited?: boolean }) {
  return (
    <span className="cz-chip" data-mode={zone.mode} data-state={status?.primary} data-inherited={inherited ? 'true' : undefined}>
      {status?.primary === 'reserved' && <span aria-hidden="true">✦</span>}
      {zone.slug}
    </span>
  );
}

export function ZoneTree({
  state,
  root,
  statuses,
  expanded,
  onToggle,
  selectedZoneId,
  onOpenZone,
  flashZoneIds,
}: {
  state: CrewState;
  root: TreeRow;
  statuses: ReadonlyMap<string, ZoneStatus>;
  expanded: ReadonlySet<string>;
  onToggle: (path: string, open?: boolean) => void;
  selectedZoneId: string | null;
  onOpenZone: (zoneId: string) => void;
  flashZoneIds: ReadonlySet<string>;
}) {
  const flat = useMemo(() => flattenTree(root, expanded), [root, expanded]);
  const [focusPath, setFocusPath] = useState<string | null>(null);
  const refs = useRef(new Map<string, HTMLDivElement>());
  const current = flat.some((f) => f.row.path === focusPath) ? focusPath : (flat[0]?.row.path ?? null);

  const focusRow = (path: string | null | undefined) => {
    if (path === null || path === undefined) return;
    setFocusPath(path);
    refs.current.get(path)?.focus();
  };

  const onKey = (e: KeyboardEvent<HTMLDivElement>, index: number) => {
    const item = flat[index];
    const { row } = item;
    const open = expanded.has(row.path);
    switch (e.key) {
      case 'ArrowDown':
        e.preventDefault();
        focusRow(flat[Math.min(flat.length - 1, index + 1)]?.row.path);
        break;
      case 'ArrowUp':
        e.preventDefault();
        focusRow(flat[Math.max(0, index - 1)]?.row.path);
        break;
      case 'Home':
        e.preventDefault();
        focusRow(flat[0]?.row.path);
        break;
      case 'End':
        e.preventDefault();
        focusRow(flat[flat.length - 1]?.row.path);
        break;
      case 'ArrowRight':
        e.preventDefault();
        if (row.children.length && !open) onToggle(row.path, true);
        else if (row.children.length) focusRow(flat[index + 1]?.row.path);
        break;
      case 'ArrowLeft':
        e.preventDefault();
        if (row.children.length && open) onToggle(row.path, false);
        else if (item.parentPath !== null) focusRow(item.parentPath);
        break;
      case 'Enter':
      case ' ':
        e.preventDefault();
        if (row.zoneIds.length) onOpenZone(row.zoneIds[0]);
        else if (row.children.length) onToggle(row.path);
        break;
    }
  };

  if (!flat.length) {
    return <p className="px-4 py-6 text-sm text-ink-3 sm:px-5">No folders or zones to show yet.</p>;
  }

  return (
    <div role="tree" aria-label="Repository folders and zones" className="cz-tree py-2">
      {flat.map((item, index) => {
        const { row } = item;
        const zones = row.zoneIds.map((id) => state.zones[id]).filter(Boolean);
        const inherited = row.inheritedIds.map((id) => state.zones[id]).filter(Boolean);
        const hasKids = row.children.length > 0;
        const open = expanded.has(row.path);
        const selected = zones.some((z) => z.id === selectedZoneId);
        const flash = zones.some((z) => flashZoneIds.has(z.id));
        const label = [
          `${row.path || 'repository root'}/`,
          ...zones.map((z) => {
            const s = statuses.get(z.id);
            return `zone ${z.slug}, ${s ? PRIMARY_LABEL[s.primary] : ''}${s?.pending ? ', policy change pending' : ''}: ${s ? rowHolderText(state, z, s) : ''}`;
          }),
          !zones.length && inherited.length ? `inside zone ${inherited[0].slug}` : '',
        ]
          .filter(Boolean)
          .join('. ');
        return (
          <div
            key={row.path || '.'}
            ref={(el) => {
              if (el) refs.current.set(row.path, el);
              else refs.current.delete(row.path);
            }}
            role="treeitem"
            aria-level={row.depth || 1}
            aria-expanded={hasKids ? open : undefined}
            aria-selected={selected}
            aria-label={label}
            tabIndex={row.path === current ? 0 : -1}
            data-dim={!zones.length && !row.zonesBelow ? 'true' : undefined}
            className={`cz-row${flash ? ' cz-flash' : ''}`}
            onKeyDown={(e) => onKey(e, index)}
            onFocus={() => setFocusPath(row.path)}
            onClick={() => {
              setFocusPath(row.path);
              if (zones.length) onOpenZone(zones[0].id);
              else if (hasKids) onToggle(row.path);
            }}
          >
            <span className="cz-guide" aria-hidden="true">
              {item.guide}
              {hasKids ? (open ? '▾ ' : '▸ ') : '  '}
            </span>
            <span className="min-w-0">
              <span className="flex flex-wrap items-baseline gap-x-2 gap-y-1">
                <span className="cz-name">{row.path ? `${row.name}/` : './ (repository root)'}</span>
                {row.files !== null && row.files > 0 && <span className="text-[11px] text-ink-3">{row.files} files</span>}
                {row.synthetic && <span className="text-[11px] text-ink-3" title="Known from a zone glob; not in the uploaded tree snapshot">from glob</span>}
                {zones.map((z) => {
                  const s = statuses.get(z.id);
                  return (
                    <span key={z.id} className="inline-flex flex-wrap items-baseline gap-x-2">
                      <button
                        type="button"
                        tabIndex={-1}
                        className="cz-chip"
                        data-mode={z.mode}
                        data-state={s?.primary}
                        onClick={(e) => {
                          e.stopPropagation();
                          onOpenZone(z.id);
                        }}
                      >
                        {s?.primary === 'reserved' && <span aria-hidden="true">✦</span>}
                        {z.slug}
                      </button>
                      <span className="font-sans text-[12.5px] text-ink-2">{z.title}</span>
                      {s && <StateMark status={s} />}
                    </span>
                  );
                })}
                {!zones.length && inherited.length > 0 && (
                  <span className="text-[11px] text-ink-3">
                    in <ZoneMark zone={inherited[0]} status={statuses.get(inherited[0].id)} inherited />
                  </span>
                )}
              </span>
              {zones.map((z) => {
                const s = statuses.get(z.id);
                if (!s) return null;
                return (
                  <span key={z.id} className="mt-0.5 block text-[11.5px] text-ink-2">
                    {zones.length > 1 ? `${z.slug}: ` : ''}
                    {rowHolderText(state, z, s)}
                  </span>
                );
              })}
            </span>
          </div>
        );
      })}
    </div>
  );
}
