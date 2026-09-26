// Home's "Live crews" strip (spec §9.1): one pixel row per crew with an agent
// on it, a square per running agent (orange = working), live and needs-you
// counts, and a way into Mission Control. Hidden when no crew exists, so a
// relay-only user never sees crew chrome.

import { ArrowRight } from 'lucide-react';
import clsx from 'clsx';
import { useCrewList } from '../../lib/crew/hooks';
import { crewHref, crewsHref, inboxHref } from '../../lib/crew/routes';
import { liveSplit, liveSplitText, presenceText } from '../../lib/crew/selectors';
import type { SessionView } from '../../lib/crew/types';

const SHOWN = 6;

function squareClass(s: SessionView): string {
  switch (s.state) {
    case 'active':
      return 'bg-signal';
    case 'idle':
    case 'joining':
      return 'bg-ink-2';
    case 'paused':
      return 'bg-ink-3';
    case 'quota_blocked':
      return 'bg-transparent border-2 border-signal';
    default:
      return 'bg-transparent border-2 border-ink-3';
  }
}

export function LiveCrewsStrip() {
  const list = useCrewList();
  if (list.status !== 'ready' || list.items.length === 0) return null;
  const items = list.items.slice(0, SHOWN);
  const totals = list.items.map(liveSplit).reduce(
    (a, s) => ({ running: a.running + s.running, stopped: a.stopped + s.stopped, paused: a.paused + s.paused }),
    { running: 0, stopped: 0, paused: 0 },
  );
  const idle = totals.stopped + totals.paused;
  return (
    <section aria-labelledby="home-live-crews" className="rr-card rounded-[3px]">
      <div className="flex items-baseline justify-between gap-3 px-4 pt-3.5 sm:px-5">
        <h2 id="home-live-crews" className="rr-eyebrow">
          Live crews · {totals.running} agent{totals.running === 1 ? '' : 's'} running
          {idle > 0 ? ` · ${idle} stopped or paused` : ''}
        </h2>
        <a
          href={crewsHref()}
          className="inline-flex items-center gap-1 text-sm font-semibold text-ink underline decoration-signal decoration-2 underline-offset-4"
        >
          Site board <ArrowRight className="h-3.5 w-3.5" aria-hidden="true" />
        </a>
      </div>
      <ul className="grid gap-x-4 gap-y-1 px-4 pb-3 pt-2 sm:grid-cols-2 sm:px-5 xl:grid-cols-3">
        {items.map((item) => {
          const project = item.crew.project_id;
          const sessions = item.live_sessions;
          return (
            <li
              key={item.crew.id}
              className="flex min-w-0 items-center gap-2.5 border-t border-dashed border-rule py-2 font-mono text-[12px]"
            >
              <a href={crewHref(project)} className="min-w-0 truncate font-semibold uppercase text-ink hover:underline">
                {item.crew.name || project}
              </a>
              <span
                className="flex shrink-0 gap-[3px]"
                aria-label={sessions.map((s) => `${s.callsign} ${presenceText(s)}`).join(', ') || 'no agents running'}
                role="img"
              >
                {sessions.slice(0, 8).map((s) => (
                  <span key={s.id} className={clsx('h-2.5 w-2.5', squareClass(s))} title={`${s.callsign} · ${presenceText(s)}`} />
                ))}
                {sessions.length === 0 && <span className="h-2.5 w-2.5 bg-rule" />}
              </span>
              <span className={clsx('ml-auto shrink-0', liveSplit(item).running > 0 ? 'text-ink-2' : 'text-ink-3')}>
                {liveSplitText(liveSplit(item))}
              </span>
              {item.needs_you > 0 && (
                <a href={inboxHref('needs-you', project)} className="shrink-0 font-bold text-signal-ink">
                  ⚑ {item.needs_you}
                </a>
              )}
            </li>
          );
        })}
      </ul>
    </section>
  );
}
