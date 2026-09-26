// One task on the board (§9.7): owner, zone chips, acceptance meter, age,
// checkpoint dots, dependencies, the "saved work" chip for a baton with a
// ref, the receipt seal on Done cards, and the one action a stalled or
// reviewed card needs. Titles and reasons are agent text: plain text only.

import clsx from 'clsx';
import { memo, type DragEvent } from 'react';
import { ArrowUpRight, GitBranch, Hand, MoreHorizontal } from 'lucide-react';
import type { CheckpointDot, ColumnId, DepRef, OwnerInfo, SavedWork, SealItem, ZoneChipInfo, AcceptanceMeter } from './model';
import { CheckpointDots, MeterBlocks, OwnerTag, SealLine, SealStamp, TimeAgo, ZoneChip } from './parts';

export interface CardModel {
  id: string;
  number: number;
  title: string;
  column: ColumnId;
  cancelled: boolean;
  statusText: string;
  owner: OwnerInfo;
  zones: ZoneChipInfo[];
  meter: AcceptanceMeter;
  since: string | null;
  age: string | null;
  dots: CheckpointDot[];
  deps: DepRef[];
  saved: SavedWork | null;
  blockedReason: string | null;
  seal: { items: SealItem[]; fallback: string; reportId: string; href: string; review: string | null } | null;
  locked: boolean;
}

export interface CardHandlers {
  onOpen: (taskId: string) => void;
  onPickup: (taskId: string) => void;
  onReport: (taskId: string) => void;
  onMenu: (taskId: string, anchor: HTMLElement) => void;
  onDragStart: (taskId: string) => void;
  onDragEnd: () => void;
  onHover: (taskId: string | null) => void;
}

function TaskCardInner({
  card,
  human,
  selected,
  dragging,
  handlers,
}: {
  card: CardModel;
  human: boolean;
  selected: boolean;
  dragging: boolean;
  handlers: CardHandlers;
}) {
  const titleId = `task-${card.id}-title`;
  const onDragStart = (e: DragEvent<HTMLElement>) => {
    e.dataTransfer.setData('text/x-crew-task', card.id);
    e.dataTransfer.setData('text/plain', `T-${card.number}`);
    e.dataTransfer.effectAllowed = 'move';
    handlers.onDragStart(card.id);
  };
  return (
    <article
      aria-labelledby={titleId}
      data-task-id={card.id}
      data-col={card.column}
      data-cancelled={card.cancelled}
      data-selected={selected}
      data-dragging={dragging}
      draggable={human}
      onDragStart={human ? onDragStart : undefined}
      onDragEnd={handlers.onDragEnd}
      onMouseEnter={() => handlers.onHover(card.id)}
      onMouseLeave={() => handlers.onHover(null)}
      onFocus={() => handlers.onHover(card.id)}
      onBlur={() => handlers.onHover(null)}
      className={clsx('cb-card px-3 pb-2.5 pt-2', human && 'cursor-grab active:cursor-grabbing')}
    >
      <header className="flex items-start gap-2">
        <span className="tabular mt-[1px] shrink-0 font-mono text-[11px] font-semibold text-ink-3">T-{card.number}</span>
        <h3 id={titleId} className={clsx('min-w-0 flex-1 text-[13.5px] font-semibold leading-snug text-ink [overflow-wrap:anywhere]', card.cancelled && 'line-through')}>
          <button type="button" className="text-left hover:underline focus-visible:underline" onClick={() => handlers.onOpen(card.id)}>
            {card.title}
          </button>
        </h3>
        <button
          type="button"
          aria-label={`Actions for T-${card.number}`}
          aria-haspopup="menu"
          onClick={(e) => handlers.onMenu(card.id, e.currentTarget)}
          className="-mr-1 -mt-0.5 shrink-0 rounded-[2px] p-1 text-ink-3 hover:bg-paper-2 hover:text-ink"
        >
          <MoreHorizontal className="h-3.5 w-3.5" aria-hidden="true" />
        </button>
      </header>

      <div className="mt-1.5 flex items-center justify-between gap-2">
        <OwnerTag owner={card.owner} />
        <span className="flex shrink-0 items-center gap-2">
          <CheckpointDots dots={card.dots} />
          <TimeAgo at={card.since} text={card.age} />
        </span>
      </div>

      {card.zones.length > 0 && (
        <p className="mt-1.5 flex flex-wrap gap-1" aria-label="Zones">
          {card.zones.map((z) => (
            <ZoneChip key={z.zoneId} chip={z} />
          ))}
        </p>
      )}

      <div className="mt-2 flex flex-wrap items-center justify-between gap-x-2 gap-y-1">
        <MeterBlocks meter={card.meter} />
        {card.locked && card.column !== 'done' && (
          <span className="font-mono text-[10px] uppercase tracking-[0.06em] text-ink-3" title="Criteria are locked once work starts; only you can change them now">
            criteria locked
          </span>
        )}
      </div>

      {card.deps.length > 0 && (
        <p className="mt-1.5 flex flex-wrap items-center gap-1 font-mono text-[10.5px] text-ink-2">
          <span className="text-ink-3">after</span>
          {card.deps.map((d) => (
            <span key={d.id} className={clsx(d.done ? 'text-ink-3 line-through' : 'text-ink')}>
              {d.ref}
              <span className="sr-only">{d.done ? ' (done)' : ' (not done)'}</span>
            </span>
          ))}
        </p>
      )}

      {card.blockedReason && (
        <p className="mt-1.5 border-l-2 border-fail/60 pl-2 text-[12px] leading-snug text-ink-2 [overflow-wrap:anywhere]">
          <span className="sr-only">Blocked: </span>
          {card.blockedReason}
        </p>
      )}

      {card.saved && (
        <p className="mt-1.5">
          <span
            className="inline-flex items-center gap-1 rounded-[2px] border border-dashed border-signal bg-signal-wash px-1.5 py-px font-mono text-[10.5px] text-signal-ink"
            title={`Uncommitted work saved as ${card.saved.ref}; restored when the next agent adopts the baton`}
          >
            <GitBranch className="h-3 w-3" aria-hidden="true" />
            saved work
            {card.saved.dirtyFiles !== null && ` · ${card.saved.dirtyFiles} file${card.saved.dirtyFiles === 1 ? '' : 's'}`}
            {card.saved.unpushed ? ` · ${card.saved.unpushed} unpushed` : ''}
          </span>
        </p>
      )}

      {card.seal && (
        <a
          href={card.seal.href}
          className="mt-2 flex items-start gap-2 border-t border-dashed border-rule pt-2 hover:[&_.cb-seal-link]:underline"
          aria-label={`Receipt for T-${card.number}`}
        >
          <SealStamp seed={card.seal.reportId} verdict={card.seal.review === 'waived' ? 'waived' : card.cancelled ? 'superseded' : 'accepted'} size={30} />
          <span className="min-w-0 flex-1">
            <SealLine items={card.seal.items} fallback={card.seal.fallback} />
            <span className="cb-seal-link mt-0.5 inline-flex items-center gap-0.5 font-mono text-[10px] uppercase tracking-[0.06em] text-ink-3">
              receipt <ArrowUpRight className="h-3 w-3" aria-hidden="true" />
            </span>
          </span>
        </a>
      )}

      {card.column === 'stalled' && human && (
        <button
          type="button"
          onClick={() => handlers.onPickup(card.id)}
          className="rr-btn-primary mt-2 inline-flex w-full items-center justify-center gap-1.5 px-2 py-1.5 text-[12.5px]"
        >
          <Hand className="h-3.5 w-3.5" aria-hidden="true" /> Pick up baton
        </button>
      )}
      {card.column === 'review' && human && (
        <button
          type="button"
          onClick={() => handlers.onReport(card.id)}
          className="rr-btn-ghost mt-2 inline-flex w-full items-center justify-center gap-1.5 border-signal px-2 py-1.5 text-[12.5px] font-semibold text-signal-ink"
        >
          Review the report
        </button>
      )}
    </article>
  );
}

export const TaskCard = memo(TaskCardInner);
