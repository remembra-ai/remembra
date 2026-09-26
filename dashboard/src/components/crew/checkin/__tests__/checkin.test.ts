import { describe, expect, it } from 'vitest';
import { parseHash } from '../../../../lib/nav';
import type { CrewListItem } from '../../../../lib/crew/types';
import { checkInItems, feedProject, lastCrewProjectSeen, moreSections, needsYouTotal, rememberCrewProject } from '../checkin';

function crew(project: string, needs: number, last: string | null): CrewListItem {
  return {
    crew: { id: `crw_${project}`, project_id: project, name: project, mode: 'multi', enforcement: 'enforce', settings_version: 1, last_seq: 1 },
    role: 'owner',
    live: 1,
    needs_you: needs,
    crew_inbox: 0,
    moments_24h: 0,
    last_event_at: last,
    tasks_by_status: {},
    phases: [],
    live_sessions: [],
    live_sessions_truncated: false,
  };
}

const crews = [crew('yaadbooks', 2, '2026-09-25T20:00:00Z'), crew('trademind', 1, '2026-09-25T21:00:00Z')];

describe('phone check-in bar (§9.12)', () => {
  it('is Crews · Needs you · Feed · Inbox, with the needs-you total across crews', () => {
    const items = checkInItems(parseHash('#/home'), crews, 3, null);
    expect(items.map((i) => i.label)).toEqual(['Crews', 'Needs you', 'Feed', 'Inbox']);
    expect(items[1]).toMatchObject({ href: '#/inbox?scope=needs-you', badge: 3, badgeText: '3 need you' });
    expect(items[3]).toMatchObject({ href: '#/inbox', badge: 3, badgeText: '3 unread for you' });
    expect(needsYouTotal([crew('a', 1, null)])).toBe(1);
    expect(checkInItems(parseHash('#/home'), [crew('a', 1, null)], 0, null)[1].badgeText).toBe('1 needs you');
  });

  it('opens the feed of the crew on screen, else the last one opened, else the busiest', () => {
    expect(feedProject(parseHash('#/crew?project=yaadbooks&view=zones'), crews, 'trademind')).toBe('yaadbooks');
    expect(feedProject(parseHash('#/home'), crews, 'yaadbooks')).toBe('yaadbooks');
    expect(feedProject(parseHash('#/home'), crews, null)).toBe('trademind'); // most recent event
    expect(feedProject(parseHash('#/home'), crews, 'gone')).toBe('trademind');
    expect(feedProject(parseHash('#/home'), [], null)).toBeNull();
    const items = checkInItems(parseHash('#/home'), crews, 0, null);
    expect(items[2].href).toBe('#/crew?project=trademind&view=feed');
    expect(checkInItems(parseHash('#/home'), [], 0, null)[2].href).toBe('#/crews');
  });

  it('marks exactly the current tab', () => {
    const active = (hash: string) => checkInItems(parseHash(hash), crews, 0, null).filter((i) => i.active).map((i) => i.id);
    expect(active('#/crews')).toEqual(['crews']);
    expect(active('#/crew?project=yaadbooks')).toEqual(['crews']);
    expect(active('#/crew?project=yaadbooks&view=feed')).toEqual(['feed']);
    expect(active('#/inbox?scope=needs-you')).toEqual(['needs-you']);
    expect(active('#/inbox')).toEqual(['inbox']);
    expect(active('#/inbox?scope=crew')).toEqual(['inbox']);
    expect(active('#/trail')).toEqual([]);
  });

  it('puts every other section under More (admin only for admins)', () => {
    expect(moreSections(false)).toEqual(['home', 'trail', 'agents', 'memory', 'graph', 'settings']);
    expect(moreSections(true)).toContain('admin');
  });

  it('remembers the last crew opened in this tab', () => {
    rememberCrewProject(parseHash('#/crew?project=yaadbooks'));
    rememberCrewProject(parseHash('#/trail'));
    expect(lastCrewProjectSeen()).toBe('yaadbooks');
  });
});
