// Phone "check-in" (spec §9.12): the bottom bar on a phone is
//   Crews · Needs you · Feed · Inbox · More
// so a glance from the phone answers "is the crew fine, and does anything
// need me?". Pure: which tabs, where they go, which is current, the badges.

import { SECTIONS, hrefFor, type Route, type SectionId } from '../../../lib/nav';
import { crewHref, crewsHref, inboxHref, parseCrewRoute, parseInboxScope } from '../../../lib/crew/routes';
import type { CrewListItem } from '../../../lib/crew/types';

export type CheckInId = 'crews' | 'needs-you' | 'feed' | 'inbox';

export interface CheckInItem {
  id: CheckInId;
  label: string;
  href: string;
  active: boolean;
  badge: number;
  /** Screen-reader words for the badge ("2 need you"). */
  badgeText: string;
}

/**
 * The crew whose feed the Feed tab opens: the crew on screen, else the one
 * last opened in this tab, else the crew with the most recent event.
 */
export function feedProject(route: Route, crews: readonly CrewListItem[], lastProject: string | null): string | null {
  if (route.tab === 'crew') {
    const p = parseCrewRoute(route.params).project;
    if (p) return p;
  }
  if (lastProject && (crews.length === 0 || crews.some((c) => c.crew.project_id === lastProject))) return lastProject;
  let best: CrewListItem | null = null;
  for (const c of crews) {
    if (!best) best = c;
    else if ((c.last_event_at ?? '') > (best.last_event_at ?? '')) best = c;
  }
  return best?.crew.project_id ?? null;
}

export function needsYouTotal(crews: readonly CrewListItem[]): number {
  return crews.reduce((n, c) => n + Math.max(0, c.needs_you || 0), 0);
}

export function checkInItems(route: Route, crews: readonly CrewListItem[], agentInboxUnread: number, lastProject: string | null): CheckInItem[] {
  const project = feedProject(route, crews, lastProject);
  const crewScreen = route.tab === 'crew' ? parseCrewRoute(route.params).screen : null;
  const scope = route.tab === 'inbox' ? parseInboxScope(route.params) : null;
  const needs = needsYouTotal(crews);
  return [
    {
      id: 'crews',
      label: 'Crews',
      href: crewsHref(),
      active: route.tab === 'crews' || (route.tab === 'crew' && crewScreen !== 'feed'),
      badge: 0,
      badgeText: '',
    },
    {
      id: 'needs-you',
      label: 'Needs you',
      href: inboxHref('needs-you'),
      active: route.tab === 'inbox' && scope === 'needs-you',
      badge: needs,
      badgeText: needs ? `${needs} need${needs === 1 ? 's' : ''} you` : '',
    },
    {
      id: 'feed',
      label: 'Feed',
      href: project ? crewHref(project, 'feed') : crewsHref(),
      active: route.tab === 'crew' && crewScreen === 'feed',
      badge: 0,
      badgeText: '',
    },
    {
      id: 'inbox',
      label: 'Inbox',
      href: hrefFor('inbox'),
      active: route.tab === 'inbox' && scope !== 'needs-you',
      badge: Math.max(0, agentInboxUnread),
      badgeText: agentInboxUnread > 0 ? `${agentInboxUnread} unread for you` : '',
    },
  ];
}

/** What the More sheet holds on a phone: everything the bar does not. */
export function moreSections(isAdmin: boolean): SectionId[] {
  return SECTIONS.filter((s) => (!s.adminOnly || isAdmin) && s.id !== 'crews' && s.id !== 'inbox').map((s) => s.id);
}

/** Last crew project opened in this tab (a per-tab convenience, not state that must persist). */
let lastCrewProject: string | null = null;

export function rememberCrewProject(route: Route): void {
  if (route.tab === 'crew') {
    const p = parseCrewRoute(route.params).project;
    if (p) lastCrewProject = p;
  }
}

export function lastCrewProjectSeen(): string | null {
  return lastCrewProject;
}
