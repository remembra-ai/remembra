// The thread list (§9.8 L0 "thread list plus thread"): every thread with its
// first line, reply count and last move; open questions are flagged and can
// be filtered to.

import { useState } from 'react';
import clsx from 'clsx';
import type { ChannelThread } from './model';
import { PixelGlyph } from './pixels';

function excerpt(text: string): string {
  const flat = text.replace(/\s+/g, ' ').trim();
  return flat.length > 110 ? `${flat.slice(0, 109)}…` : flat;
}

export function ChannelThreads({
  threads,
  selected,
  onSelect,
  nameOf,
}: {
  threads: ChannelThread[];
  selected: string | null;
  onSelect: (id: string | null) => void;
  nameOf: (thread: ChannelThread) => string;
}) {
  const [onlyOpen, setOnlyOpen] = useState(false);
  const open = threads.filter((t) => t.openQuestion).length;
  const shown = onlyOpen ? threads.filter((t) => t.openQuestion) : threads;
  return (
    <nav aria-label="Threads" className="min-w-0">
      <div className="flex items-center justify-between gap-2 px-4 pb-2 pt-4 sm:px-5">
        <p className="rr-eyebrow">Threads</p>
        {open > 0 && (
          <button
            type="button"
            aria-pressed={onlyOpen}
            onClick={() => setOnlyOpen(!onlyOpen)}
            className={clsx(
              'inline-flex items-center gap-1 rounded-[2px] border px-1.5 py-0.5 font-mono text-[11px]',
              onlyOpen ? 'border-signal bg-signal text-on-signal' : 'border-signal/40 text-signal-ink hover:border-signal',
            )}
          >
            <PixelGlyph name="question" size={10} mono={onlyOpen} /> {open} open
          </button>
        )}
      </div>
      <ul className="max-h-[52vh] overflow-y-auto pb-2 lg:max-h-[64vh]">
        <li>
          <button
            type="button"
            onClick={() => onSelect(null)}
            aria-current={selected === null ? 'true' : undefined}
            className={clsx(
              'relative flex w-full items-center gap-2 px-4 py-2 text-left font-mono text-[12px] sm:px-5',
              selected === null ? 'bg-paper-2 font-bold text-ink' : 'text-ink-2 hover:bg-paper-2',
            )}
          >
            {selected === null && <span aria-hidden="true" className="absolute inset-y-1 left-0 w-[3px] bg-signal" />}
            <PixelGlyph name="chat" size={12} /> Whole channel
          </button>
        </li>
        {shown.map((t) => {
          const on = selected === t.id;
          return (
            <li key={t.id}>
              <button
                type="button"
                onClick={() => onSelect(t.id)}
                aria-current={on ? 'true' : undefined}
                className={clsx('relative block w-full px-4 py-2.5 text-left sm:px-5', on ? 'bg-paper-2' : 'hover:bg-paper-2')}
              >
                {on && <span aria-hidden="true" className="absolute inset-y-1 left-0 w-[3px] bg-signal" />}
                <span className="flex items-center gap-2 font-mono text-[11px] text-ink-3">
                  <span className="truncate font-bold text-ink-2">{nameOf(t)}</span>
                  {t.openQuestion && (
                    <span className="inline-flex shrink-0 items-center gap-1 text-signal-ink">
                      <PixelGlyph name="question" size={9} /> open
                    </span>
                  )}
                  <span className="ml-auto shrink-0 tabular">
                    {t.replies.length ? `${t.replies.length} repl${t.replies.length === 1 ? 'y' : 'ies'}` : 'no replies'}
                  </span>
                </span>
                <span className="mt-0.5 line-clamp-2 block text-[13px] leading-snug text-ink [overflow-wrap:anywhere]">
                  {t.root ? (t.root.redacted ? 'Redacted message' : t.root.collapsed ? 'Collapsed: reads like instructions to agents' : excerpt(t.root.body)) : 'Earlier message (open to load)'}
                </span>
              </button>
            </li>
          );
        })}
        {shown.length === 0 && <li className="px-4 py-3 text-sm text-ink-3 sm:px-5">{onlyOpen ? 'No open questions.' : 'No threads yet.'}</li>}
      </ul>
    </nav>
  );
}
