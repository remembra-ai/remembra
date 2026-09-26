// Header of a crew screen (Zone Map, Policy): the live pixel band, the title,
// the view links and the live status strip. The strip is the text version of
// everything the band animates.

import type { ReactNode } from 'react';
import { crewHref, type CrewScreen } from '../../../lib/crew/routes';
import { DitherField } from './DitherField';

const VIEWS: { screen: CrewScreen; label: string }[] = [
  { screen: 'track', label: 'Track' },
  { screen: 'zones', label: 'Zones' },
  { screen: 'board', label: 'Board' },
  { screen: 'channel', label: 'Channel' },
  { screen: 'feed', label: 'Feed' },
  { screen: 'policy', label: 'Policy' },
];

export function LiveStrip({ live, parts }: { live: boolean; parts: string[] }) {
  return (
    <p className="cz-strip" role="status" aria-live="off">
      <i data-live={live ? 'true' : 'false'} aria-hidden="true" />
      <span>{parts.filter(Boolean).join(' · ')}</span>
    </p>
  );
}

export function ScreenHeader({
  project,
  screen,
  title,
  lede,
  seq,
  live,
  strip,
  aside,
}: {
  project: string;
  screen: CrewScreen;
  title: string;
  lede: ReactNode;
  seq: number;
  live: boolean;
  strip: string[];
  aside?: ReactNode;
}) {
  return (
    <header className="cz-band px-4 pb-4 pt-4 sm:px-6 sm:pb-5 sm:pt-5">
      <DitherField seq={seq} />
      <div className="flex flex-wrap items-start justify-between gap-x-6 gap-y-3">
        <div className="min-w-0 max-w-[40rem]">
          <p className="rr-eyebrow">
            Crew · <span className="normal-case tracking-normal">{project}</span>
          </p>
          <h2 className="cz-title mt-2 text-[2rem] text-ink sm:text-[2.6rem]">{title}</h2>
          <div className="mt-2 text-sm text-ink-2">{lede}</div>
        </div>
        <nav aria-label="Crew views" className="cz-tabs flex flex-wrap gap-x-4 gap-y-1">
          {VIEWS.map((v) => (
            <a key={v.screen} href={crewHref(project, v.screen)} aria-current={v.screen === screen ? 'page' : undefined}>
              {v.label}
            </a>
          ))}
        </nav>
      </div>
      <div className="mt-4 flex flex-wrap items-center justify-between gap-3">
        <LiveStrip live={live} parts={strip} />
        {aside}
      </div>
    </header>
  );
}
