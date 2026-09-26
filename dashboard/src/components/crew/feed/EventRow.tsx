// One feed row (§9.6). Fixed height so the list can be virtualised. A glyph
// node on the dashed trail, the age, who did it (with the trust label), the
// status word, the server's summary and, on a second line, the event's own
// text rendered as plain text. Baton passes get their own row (BatonRow).

import type { CSSProperties, KeyboardEvent, MouseEvent } from 'react';
import clsx from 'clsx';
import { ChevronRight } from 'lucide-react';
import { absoluteTime } from '../../../lib/time';
import type { CrewState } from '../../../lib/crew/types';
import { humanSummary } from '../../../lib/crew/summary';
import { motion } from 'framer-motion';
import { useCrewMotion } from '../../../lib/motion';
import { BatonRow } from './BatonRow';
import { TONE_CLASS, targetHref, targetLabel } from './links';
import { actorText, ageText, detailFromAgent, detailText, refChips, rowLook, rowTarget, type FeedRow } from './model';

export const ROW_H = 64;

export interface EventRowProps {
  row: FeedRow;
  state: CrewState | null;
  project: string;
  nowMs: number;
  index: number;
  total: number;
  selected: boolean;
  /** An expanded checkpoint run this row belongs to (offers "collapse"). */
  runKey: string | null;
  fresh: boolean;
  onSelect: (key: string) => void;
  onToggleRun: (key: string) => void;
  onOpen: (href: string) => void;
  style?: CSSProperties;
}

export function EventRow({ row, state, project, nowMs, index, total, selected, runKey, fresh, onSelect, onToggleRun, onOpen, style }: EventRowProps) {
  const e = row.event;
  const look = rowLook(row);
  const tone = TONE_CLASS[look.tone];
  const actor = actorText(e);
  const detail = detailText(e);
  const fromAgent = detail !== null && detailFromAgent(e);
  const target = rowTarget(e, state);
  const href = targetHref(project, target);
  const chips = refChips(row, state);
  const titleId = `feed-row-${row.key}`;
  const collapsedRun = row.collapsed;
  const crewMotion = useCrewMotion();

  const onKeyDown = (ev: KeyboardEvent<HTMLElement>) => {
    if (ev.target !== ev.currentTarget) return;
    if (ev.key === 'Enter') {
      if (collapsedRun) {
        ev.preventDefault();
        onToggleRun(row.key);
      } else if (href) {
        ev.preventDefault();
        onOpen(href);
      }
    }
  };
  const onClick = (ev: MouseEvent<HTMLElement>) => {
    if ((ev.target as HTMLElement).closest('a,button')) return;
    onSelect(row.key);
  };

  return (
    <motion.article
      initial={fresh ? 'initial' : false}
      animate="animate"
      variants={crewMotion.rowEnter}
      role="article"
      aria-labelledby={titleId}
      aria-posinset={index + 1}
      aria-setsize={total}
      tabIndex={selected ? 0 : -1}
      data-key={row.key}
      data-kind={row.kind}
      onKeyDown={onKeyDown}
      onClick={onClick}
      style={{ height: 64, ...style }}
      className={clsx(
        'group relative flex items-stretch gap-3 overflow-hidden border-b border-rule pr-3 outline-none sm:pr-4',
        selected ? 'bg-paper-2 shadow-[inset_3px_0_0_var(--ink)]' : 'hover:bg-paper-2/60',
      )}
    >
      {/* the trail: a dashed rail with this row's node on it */}
      <div aria-hidden="true" className="relative flex w-9 shrink-0 justify-center sm:w-10">
        <span className="rr-rail absolute inset-y-0 left-1/2 w-[2px] -translate-x-1/2" />
        <span
          className={clsx(
            'relative mt-[13px] flex h-[22px] w-[22px] items-center justify-center rounded-[3px] border font-mono text-[12px] leading-none',
            tone.node,
            row.kind === 'baton' && 'rr-baton border-transparent text-on-signal',
          )}
        >
          {look.glyph}
        </span>
      </div>

      <div className="flex min-w-0 flex-1 flex-col justify-center py-2">
        <p id={titleId} className="flex min-w-0 items-baseline gap-2 text-[13px] leading-[18px]">
          <span className="shrink-0 font-mono text-[11px] tabular text-ink-3" title={absoluteTime(e.ts)}>
            <time dateTime={e.ts}>{ageText(e.ts, nowMs)}</time>
          </span>
          <span className="shrink-0 font-mono text-[12px] font-semibold text-ink" title={actor.trust}>
            {actor.name}
          </span>
          <span className={clsx('shrink-0 font-mono text-[11px] uppercase tracking-[0.06em]', tone.word)}>{look.label}</span>
          <span className="sr-only">. </span>
          {row.kind === 'baton' ? (
            <BatonRow event={e} state={state} />
          ) : (
            <span className="min-w-0 truncate text-ink-2">{humanSummary(state, e)}</span>
          )}
          {e.moment && (
            <span className="ml-auto hidden shrink-0 rounded-[2px] border border-rule px-1 font-mono text-[10px] text-ink-3 sm:inline" title="A moment: kept forever">
              moment
            </span>
          )}
        </p>
        <p className="flex min-w-0 items-center gap-2 text-[12px] leading-4 text-ink-3">
          {detail ? (
            <span className="min-w-0 truncate">
              <span className="text-ink-2">{detail}</span>
              {fromAgent && <span className="ml-1.5 font-mono text-[10px]">({actor.trust})</span>}
            </span>
          ) : (
            <span className="min-w-0 truncate font-mono text-[11px]">{e.type}</span>
          )}
          {chips.length > 0 && (
            <span className="ml-auto hidden shrink-0 gap-1 sm:flex">
              {chips.map((c) => (
                <span key={c} className="rounded-[2px] border border-rule px-1 font-mono text-[10px] leading-4 text-ink-3">
                  {c}
                </span>
              ))}
            </span>
          )}
        </p>
      </div>

      <div className="flex shrink-0 items-center gap-1">
        {collapsedRun && (
          <button
            type="button"
            tabIndex={selected ? 0 : -1}
            aria-expanded={false}
            onClick={() => onToggleRun(row.key)}
            className="rr-btn-ghost px-2 py-1 font-mono text-[11px]"
          >
            show {row.events.length}
          </button>
        )}
        {runKey && (
          <button
            type="button"
            tabIndex={selected ? 0 : -1}
            aria-expanded
            onClick={() => onToggleRun(runKey)}
            className="rr-btn-ghost px-2 py-1 font-mono text-[11px]"
          >
            collapse
          </button>
        )}
        {href && !collapsedRun && (
          <a
            href={href}
            tabIndex={selected ? 0 : -1}
            aria-label={targetLabel(target)}
            title={targetLabel(target)}
            className="flex h-8 w-8 items-center justify-center rounded-[3px] text-ink-3 hover:bg-panel hover:text-ink"
          >
            <ChevronRight className="h-4 w-4" aria-hidden="true" />
          </a>
        )}
      </div>
    </motion.article>
  );
}
