// Crew routes (spec §9.1). Everything lives in the URL hash so reloads, the
// back button and deep links from email or webhook notifications open the
// exact screen:
//
//   #/crews                                      Site Board
//   #/crew?project=X                             Mission Control (view=track)
//   #/crew?project=X&view=zones[&zone=pos]       Zone Map
//   #/crew?project=X&view=board[&task=T-14]      Task Board
//   #/crew?project=X&view=channel[&thread=…]     Crew Channel
//   #/crew?project=X&view=feed[&type=…&session=…&zone=…&task=…&moments=1]
//   #/crew?project=X&view=report&report=…        Report receipt
//   #/crew?project=X&view=policy                 Policy
//   #/inbox?scope=needs-you|crew[&project=X]     Inboxes
//   #/agents?agent=…[&session=…]                 Agent page

import { useMemo } from 'react';
import { hrefFor, navigate, useRoute } from '../nav';

export const CREW_SCREENS = ['track', 'zones', 'board', 'channel', 'feed', 'report', 'policy'] as const;
export type CrewScreen = (typeof CREW_SCREENS)[number];

export const INBOX_SCOPES = ['needs-you', 'crew'] as const;
export type InboxScope = (typeof INBOX_SCOPES)[number];

export interface FeedFilters {
  /** Event type prefixes, e.g. ["claim.", "baton."] (URL: comma separated). */
  types: string[];
  session: string | null;
  zone: string | null;
  task: string | null;
  /** Moments only. */
  moments: boolean;
}

export interface CrewRoute {
  project: string | null;
  screen: CrewScreen;
  zone: string | null;
  task: string | null;
  thread: string | null;
  report: string | null;
  feed: FeedFilters;
}

function isScreen(value: string | null): value is CrewScreen {
  return value !== null && (CREW_SCREENS as readonly string[]).includes(value);
}

function clean(value: string | null): string | null {
  const v = value?.trim();
  return v ? v : null;
}

/** Parse `#/crew?…` parameters. Unknown views fall back to the track; a report view without an id too. */
export function parseCrewRoute(params: URLSearchParams): CrewRoute {
  const view = params.get('view');
  let screen: CrewScreen = isScreen(view) ? view : 'track';
  const report = clean(params.get('report'));
  if (screen === 'report' && !report) screen = 'track';
  const types = (params.get('type') ?? '')
    .split(',')
    .map((t) => t.trim())
    .filter(Boolean);
  return {
    project: clean(params.get('project')),
    screen,
    zone: clean(params.get('zone')),
    task: clean(params.get('task')),
    thread: clean(params.get('thread')),
    report,
    feed: {
      types,
      session: clean(params.get('session')),
      zone: clean(params.get('zone')),
      task: clean(params.get('task')),
      moments: params.get('moments') === '1',
    },
  };
}

export interface CrewLinkOptions {
  zone?: string | null;
  task?: string | null;
  thread?: string | null;
  report?: string | null;
  feed?: Partial<FeedFilters>;
}

function crewParams(project: string, screen: CrewScreen, options: CrewLinkOptions): Record<string, string | null> {
  const params: Record<string, string | null> = { project, view: screen === 'track' ? null : screen };
  if (screen === 'zones') params.zone = options.zone ?? null;
  if (screen === 'board') params.task = options.task ?? null;
  if (screen === 'channel') params.thread = options.thread ?? null;
  if (screen === 'report') params.report = options.report ?? null;
  if (screen === 'feed' && options.feed) {
    const f = options.feed;
    params.type = f.types && f.types.length ? f.types.join(',') : null;
    params.session = f.session ?? null;
    params.zone = f.zone ?? null;
    params.task = f.task ?? null;
    params.moments = f.moments ? '1' : null;
  }
  return params;
}

export function crewsHref(): string {
  return hrefFor('crews');
}

export function crewHref(project: string, screen: CrewScreen = 'track', options: CrewLinkOptions = {}): string {
  return hrefFor('crew', crewParams(project, screen, options));
}

export function goToCrews(): void {
  navigate('crews');
}

export function goToCrew(project: string, screen: CrewScreen = 'track', options: CrewLinkOptions = {}, replace = false): void {
  navigate('crew', crewParams(project, screen, options), replace);
}

export function inboxHref(scope: InboxScope, project?: string | null): string {
  return hrefFor('inbox', { scope, project: project ?? null });
}

export function parseInboxScope(params: URLSearchParams): InboxScope | null {
  const scope = params.get('scope');
  return scope !== null && (INBOX_SCOPES as readonly string[]).includes(scope) ? (scope as InboxScope) : null;
}

export function agentHref(agent: string, session?: string | null): string {
  return hrefFor('agents', { agent, session: session ?? null });
}

/** The crew route of the current hash (null when the current tab is not `crew`). */
export function useCrewRoute(): CrewRoute | null {
  const route = useRoute();
  return useMemo(() => (route.tab === 'crew' ? parseCrewRoute(route.params) : null), [route]);
}
