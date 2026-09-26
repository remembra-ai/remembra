// The two visible inbox tabs (§9.9) with their meaning in one line, the
// project filter, and a live strip over a dithered bank. "Details" (agent
// inbox, session queues) sits below the list, closed by default.

import clsx from 'clsx';
import { useCrewConnection } from '../../../lib/crew/hooks';
import type { InboxScope } from '../../../lib/crew/routes';
import type { CrewListItem } from '../../../lib/crew/types';
import { hrefFor } from '../../../lib/nav';
import { DitherBank, PixelGlyph, StatusStrip } from '../channel/pixels';
import { TAB_COPY, inboxParams, type InboxRoute } from './model';

export function InboxTabs({ route, crews, safetyCount }: { route: InboxRoute; crews: CrewListItem[]; safetyCount: number }) {
  const connection = useCrewConnection();
  const scoped = route.project ? crews.filter((c) => c.crew.project_id === route.project) : crews;
  const counts: Record<InboxScope, number> = {
    'needs-you': scoped.reduce((n, c) => n + c.needs_you, 0),
    crew: scoped.reduce((n, c) => n + c.crew_inbox, 0),
  };
  const live = connection === 'open';
  const moving = crews.filter((c) => c.live > 0).length;
  const strip = live
    ? `live · ${crews.length} crew${crews.length === 1 ? '' : 's'} · ${counts['needs-you']} need${counts['needs-you'] === 1 ? 's' : ''} you${safetyCount ? ` · ${safetyCount} safety` : ''}`
    : `${connection === 'connecting' ? 'connecting' : 'updating every 30 s'} · ${counts['needs-you']} need${counts['needs-you'] === 1 ? 's' : ''} you`;
  const link = (next: Partial<InboxRoute>) => hrefFor('inbox', inboxParams({ ...route, details: null, alerts: false, ...next }));

  return (
    <div className="rr-card relative overflow-hidden rounded-[3px]">
      <DitherBank live={live} shape="corner" seed={7} />
      <div className="relative px-4 pb-0 pt-4 sm:px-5">
        <div className="flex min-w-0 flex-wrap items-center gap-3">
          <p className="rr-eyebrow">Inbox{route.project ? ` · ${route.project}` : ' · every project'}</p>
          <StatusStrip live={live} packets={Math.min(3, moving)} className="min-w-0 sm:ml-auto">
            {strip}
          </StatusStrip>
        </div>
        <div role="tablist" aria-label="Inbox" className="mt-3 flex gap-1">
          {(['needs-you', 'crew'] as const).map((scope) => {
            const on = route.scope === scope;
            return (
              <a
                key={scope}
                role="tab"
                aria-selected={on}
                href={link({ scope })}
                className={clsx(
                  'relative inline-flex items-center gap-2 px-3 pb-3 pt-1.5 font-display text-lg font-bold tracking-[-0.01em]',
                  on ? 'text-ink' : 'text-ink-3 hover:text-ink',
                )}
              >
                <PixelGlyph name={scope === 'needs-you' ? 'bell' : 'crew'} size={16} />
                {TAB_COPY[scope].label}
                {counts[scope] > 0 && (
                  <span className={clsx('min-w-[1.4rem] px-1 text-center font-mono text-[11px] leading-5', scope === 'needs-you' ? 'bg-signal text-on-signal' : 'bg-ink text-paper')}>
                    {counts[scope]}
                  </span>
                )}
                {on && <span aria-hidden="true" className="absolute inset-x-2 bottom-0 h-[3px] bg-signal" />}
              </a>
            );
          })}
        </div>
      </div>
      <div className="relative flex flex-wrap items-center gap-x-4 gap-y-2 border-t border-rule bg-panel/80 px-4 py-2.5 sm:px-5">
        <p className="text-sm text-ink-2">{TAB_COPY[route.scope].meaning}</p>
        {crews.length > 1 && (
          <nav aria-label="Project" className="flex flex-wrap gap-1.5 sm:ml-auto">
            <a
              href={link({ project: null })}
              aria-current={route.project === null ? 'true' : undefined}
              className={clsx('rounded-[2px] border px-2 py-0.5 font-mono text-[11px]', route.project === null ? 'border-ink bg-ink text-paper' : 'border-rule text-ink-2 hover:border-ink')}
            >
              all
            </a>
            {crews.map((c) => {
              const n = route.scope === 'crew' ? c.crew_inbox : c.needs_you;
              const on = route.project === c.crew.project_id;
              return (
                <a
                  key={c.crew.id}
                  href={link({ project: c.crew.project_id })}
                  aria-current={on ? 'true' : undefined}
                  className={clsx('rounded-[2px] border px-2 py-0.5 font-mono text-[11px]', on ? 'border-ink bg-ink text-paper' : 'border-rule text-ink-2 hover:border-ink')}
                >
                  {c.crew.project_id}
                  {n > 0 && <span className={clsx('ml-1.5 tabular', !on && 'text-signal-ink')}>{n}</span>}
                </a>
              );
            })}
          </nav>
        )}
      </div>
    </div>
  );
}
