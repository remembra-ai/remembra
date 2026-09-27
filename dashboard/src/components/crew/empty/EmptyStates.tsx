// Empty states that teach setup (spec §9.14). Each says what to do next in
// one or two lines, with a hand-drawn pixel vignette and the dithered cloud
// behind it. Copy is the spec's, word for word. Other crew screens import
// these: Site Board (NoCrewsEmpty), Mission Control (OneAgentEmpty), Zone Map
// (NoZonesEmpty), Task Board (EmptyBoard), Needs-you inbox (NothingNeedsYou);
// the Event Feed uses EmptyFeed.

import { useRef, type ReactNode } from 'react';
import clsx from 'clsx';
import { crewHref } from '../../../lib/crew/routes';
import { CopyCommand } from '../../relay/ui';
import { DitherCloud } from './DitherCloud';
import { PIXEL_FILL, VIGNETTES, pixelRuns, type VignetteId } from './pixels';

export const CREW_INSTALL_COMMAND = 'pipx install remembra && remembra-crew connect --crew';

export function PixelVignette({ id, scale = 5, className }: { id: VignetteId; scale?: number; className?: string }) {
  const { width, height, runs } = pixelRuns(VIGNETTES[id]);
  return (
    <svg
      aria-hidden="true"
      viewBox={`0 0 ${width} ${height}`}
      width={width * scale}
      height={height * scale}
      shapeRendering="crispEdges"
      className={clsx('block max-w-full', className)}
      data-vignette={id}
    >
      {runs.map((r) => (
        <rect key={`${r.x}-${r.y}`} x={r.x} y={r.y} width={r.w} height={1} fill={PIXEL_FILL[r.pixel]} />
      ))}
    </svg>
  );
}

export function CrewEmptyState({
  art,
  eyebrow,
  title,
  children,
  action,
  compact = false,
  className,
  headingLevel = 2,
}: {
  art: VignetteId;
  eyebrow: string;
  title: ReactNode;
  children?: ReactNode;
  action?: ReactNode;
  /** Inside a card or a column (no panel of its own, smaller art). */
  compact?: boolean;
  className?: string;
  headingLevel?: 2 | 3;
}) {
  const textRef = useRef<HTMLDivElement>(null);
  const Heading = headingLevel === 2 ? 'h2' : 'h3';
  return (
    <div
      className={clsx(
        'relative isolate overflow-hidden',
        compact ? 'px-4 py-5 sm:px-5' : 'rr-card rounded-[3px] px-4 py-6 sm:px-6 sm:py-8',
        className,
      )}
      data-empty={art}
    >
      <DitherCloud shape={compact ? 'strip' : 'right'} avoidRef={textRef} className="-z-10 opacity-90" />
      <div className="flex flex-col gap-5 sm:flex-row sm:items-center sm:justify-between">
        <div ref={textRef} className="min-w-0 max-w-xl">
          <p className="rr-eyebrow">{eyebrow}</p>
          <Heading className={clsx('font-display mt-2 font-bold leading-tight text-ink', compact ? 'text-base' : 'text-xl')}>{title}</Heading>
          {children && <div className="mt-2 text-sm text-ink-2">{children}</div>}
          {action && <div className="mt-4 flex flex-wrap items-center gap-2">{action}</div>}
        </div>
        <PixelVignette id={art} scale={compact ? 4 : 6} className="shrink-0 self-start sm:self-center" />
      </div>
    </div>
  );
}

/** No crews yet: the three setup steps (§9.14). */
export function NoCrewsEmpty({ project }: { project?: string | null }) {
  return (
    <CrewEmptyState art="trail" eyebrow={project ? `No crew for ${project} yet` : 'No crews yet'} title="Three steps and your agents work as a crew.">
      <ol className="mt-1 space-y-3">
        <li className="flex gap-3">
          <span aria-hidden="true" className="font-mono text-xs font-bold text-signal-ink">1</span>
          <div className="min-w-0 flex-1">
            <CopyCommand command={CREW_INSTALL_COMMAND} label="Crew install command" />
            <p className="mt-1.5 text-xs text-ink-3">You will see every change before it is written.</p>
          </div>
        </li>
        <li className="flex gap-3">
          <span aria-hidden="true" className="font-mono text-xs font-bold text-ink-3">2</span>
          <p>Open the repo in any connected agent; it joins automatically.</p>
        </li>
        <li className="flex gap-3">
          <span aria-hidden="true" className="font-mono text-xs font-bold text-ink-3">3</span>
          <p>Name your zones so agents know what not to touch.</p>
        </li>
      </ol>
    </CrewEmptyState>
  );
}

/** The server runs without Crew mode (`GET /crews` is a 404): say so, instead of "update Remembra". */
export function CrewModeOff() {
  return (
    <CrewEmptyState art="quiet" eyebrow="Crew mode is off" title="This server runs without Crew mode.">
      <p>
        Memory, Relay handoffs and the agent inbox work as before. Whoever runs the server turns crews on with{' '}
        <code className="font-mono text-[12px]">REMEMBRA_CREW_MODE=true</code> and a restart.
      </p>
    </CrewEmptyState>
  );
}

/** One agent on the crew (Mission Control). `holds` are zone slugs the agent holds. */
export function OneAgentEmpty({ callsign, holds = [], compact = true }: { callsign?: string | null; holds?: string[]; compact?: boolean }) {
  return (
    <CrewEmptyState art="runner" eyebrow="One agent" title="Start another agent here; it gets the brief plus the zones this one holds." compact={compact} headingLevel={3}>
      {callsign && (
        <p className="font-mono text-[12px] text-ink-3">
          {callsign}
          {holds.length ? ` holds ${holds.join(', ')}` : ' holds no zones yet'}
        </p>
      )}
    </CrewEmptyState>
  );
}

/** Temporary zones after the no-zone bootstrap (Zone Map setup mode). */
export function NoZonesEmpty({ project, onKeep, compact = false }: { project: string; onKeep?: () => void; compact?: boolean }) {
  const action = onKeep ? (
    <button type="button" onClick={onKeep} className="rr-btn-primary px-3 py-2 text-sm">
      Keep and name them
    </button>
  ) : (
    <a href={crewHref(project, 'zones')} className="rr-btn-primary inline-block px-3 py-2 text-sm">
      Keep and name them
    </a>
  );
  return (
    <CrewEmptyState art="zones" eyebrow="Temporary zones" title="Temporary zones were made from your folders when a second agent joined." action={action} compact={compact} />
  );
}

/** Empty Task Board. */
export function EmptyBoard({ compact = false }: { compact?: boolean }) {
  return <CrewEmptyState art="receipt" eyebrow="No tasks yet" title="Tasks only close with a report." compact={compact} />;
}

/** Empty Needs-you inbox. */
export function NothingNeedsYou({ compact = false }: { compact?: boolean }) {
  return <CrewEmptyState art="calm" eyebrow="Needs you" title="Nothing needs you. The crew is running." compact={compact} />;
}

/** Empty Event Feed, or no events for the current filters. */
export function EmptyFeed({ filtered, onClear }: { filtered: boolean; onClear?: () => void }) {
  if (filtered) {
    return (
      <CrewEmptyState
        art="quiet"
        eyebrow="Filtered"
        title="No events match these filters."
        compact
        headingLevel={3}
        action={
          onClear && (
            <button type="button" onClick={onClear} className="rr-btn-ghost px-3 py-1.5 text-sm">
              Clear filters
            </button>
          )
        }
      />
    );
  }
  return (
    <CrewEmptyState art="quiet" eyebrow="No events yet" title="Events land here the moment an agent joins, claims a zone or checks in." compact headingLevel={3}>
      <p>Every event is kept in order, so what you see here is what happened.</p>
    </CrewEmptyState>
  );
}
