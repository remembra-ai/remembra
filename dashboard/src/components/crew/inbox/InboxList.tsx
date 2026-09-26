// The list under the active inbox tab: Needs-you or Crew items for one
// project or all of them, safety items first, each with its one action.

import { useNow } from '../../../hooks/useResource';
import type { InboxScope } from '../../../lib/crew/routes';
import { CopyCommand, ErrorNotice, StaleNotice, TrailSkeleton } from '../../relay/ui';
import { PixelGlyph } from '../channel/pixels';
import { TAB_COPY } from './model';
import { NeedsYouCard } from './NeedsYouCard';
import type { InboxData } from './useInbox';

const INSTALL = 'pipx install remembra && remembra-crew connect --crew';

export function InboxList({ scope, project, data }: { scope: InboxScope; project: string | null; data: InboxData }) {
  const now = useNow(30000);
  if (data.loading) return <div className="rr-card rounded-[3px]"><TrailSkeleton rows={3} /></div>;
  if (!data.crewsLoading && data.crews.length === 0) {
    return (
      <div className="rr-card rounded-[3px] px-4 py-6 sm:px-5">
        <p className="rr-eyebrow">No crews yet</p>
        <p className="mt-2 max-w-xl text-sm text-ink-2">
          Crew items appear once a connected agent joins a project. Connect this machine (you will see every change before it is written):
        </p>
        <CopyCommand className="mt-3 max-w-xl" command={INSTALL} label="Crew install command" />
      </div>
    );
  }
  if (project && !data.crewId && !data.crewsLoading) {
    return (
      <div className="rr-card rounded-[3px] px-4 py-6 text-sm text-ink-2 sm:px-5">
        There is no crew for <span className="font-mono font-bold text-ink">{project}</span> (or it is not visible to this login).
      </div>
    );
  }
  if (data.error && !data.items.length) {
    return (
      <div className="rr-card rounded-[3px]">
        <ErrorNotice error={data.error} what="the inbox" onRetry={data.refresh} />
      </div>
    );
  }
  if (!data.items.length) {
    return (
      <div className="rr-card crew-px rounded-[3px] px-4 py-10 text-center sm:px-5">
        <PixelGlyph name={scope === 'needs-you' ? 'check' : 'crew'} size={30} className="mx-auto text-ink-3" />
        <p className="font-display mt-3 text-xl font-bold text-ink">{TAB_COPY[scope].empty}</p>
      </div>
    );
  }
  return (
    <div className="rr-card rounded-[3px]">
      {data.error != null && <StaleNotice error={data.error} what="the inbox" />}
      <ul className="divide-y divide-rule" aria-label={TAB_COPY[scope].label}>
        {data.items.map((item) => (
          <NeedsYouCard
            key={item.id}
            item={item}
            state={data.state}
            sessions={data.sessions}
            now={now}
            showProject={!project}
            onChanged={data.refresh}
          />
        ))}
      </ul>
    </div>
  );
}
