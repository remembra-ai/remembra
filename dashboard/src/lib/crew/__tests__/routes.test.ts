import { describe, expect, it } from 'vitest';
import { SECTIONS, parseHash, sectionOf, TABS } from '../../nav';
import { agentHref, crewHref, crewsHref, inboxHref, parseCrewRoute, parseInboxScope } from '../routes';

function route(href: string) {
  const parsed = parseHash(href);
  return { tab: parsed.tab, crew: parseCrewRoute(parsed.params), params: parsed.params };
}

describe('crew routes (§9.1)', () => {
  it('are tabs of the Crews section', () => {
    expect(TABS.crews.label).toBe('Crews');
    expect(sectionOf('crews').id).toBe('crews');
    expect(sectionOf('crew').id).toBe('crews');
    expect(parseHash(crewsHref()).tab).toBe('crews');
  });

  it('default to the track and round-trip every screen', () => {
    expect(crewHref('yaadbooks')).toBe('#/crew?project=yaadbooks');
    expect(route(crewHref('yaadbooks')).crew).toMatchObject({ project: 'yaadbooks', screen: 'track' });
    expect(route(crewHref('yaadbooks', 'zones', { zone: 'pos' })).crew).toMatchObject({ screen: 'zones', zone: 'pos' });
    expect(route(crewHref('yaadbooks', 'board', { task: 'T-14' })).crew).toMatchObject({ screen: 'board', task: 'T-14' });
    expect(route(crewHref('yaadbooks', 'channel', { thread: 'msg_1' })).crew).toMatchObject({ screen: 'channel', thread: 'msg_1' });
    expect(route(crewHref('yaadbooks', 'report', { report: 'rpt_1' })).crew).toMatchObject({ screen: 'report', report: 'rpt_1' });
    expect(route(crewHref('yaadbooks', 'policy')).crew.screen).toBe('policy');
  });

  it('only keep the parameters that belong to the screen', () => {
    expect(crewHref('p', 'track', { zone: 'pos', task: 'T-1' })).toBe('#/crew?project=p');
    expect(crewHref('p', 'zones', { task: 'T-1', zone: 'pos' })).toBe('#/crew?project=p&view=zones&zone=pos');
  });

  it('carry feed filters in the URL', () => {
    const href = crewHref('p', 'feed', { feed: { types: ['claim.', 'baton.'], session: 'cs_a', moments: true } });
    expect(href).toBe('#/crew?project=p&view=feed&type=claim.%2Cbaton.&session=cs_a&moments=1');
    expect(route(href).crew.feed).toEqual({ types: ['claim.', 'baton.'], session: 'cs_a', zone: null, task: null, moments: true });
  });

  it('fall back safely on unknown views, a report without an id and odd projects', () => {
    expect(route('#/crew?project=p&view=nope').crew.screen).toBe('track');
    expect(route('#/crew?project=p&view=report').crew.screen).toBe('track');
    expect(route('#/crew?view=zones').crew.project).toBeNull();
    expect(route('#/crew?project=%20%20').crew.project).toBeNull();
    const odd = crewHref('a b&c=d');
    expect(route(odd).crew.project).toBe('a b&c=d');
  });

  it('link inboxes by scope and agents by id', () => {
    expect(inboxHref('needs-you', 'yaadbooks')).toBe('#/inbox?scope=needs-you&project=yaadbooks');
    expect(inboxHref('crew')).toBe('#/inbox?scope=crew');
    expect(parseInboxScope(parseHash(inboxHref('crew')).params)).toBe('crew');
    expect(parseInboxScope(new URLSearchParams('scope=all'))).toBeNull();
    expect(agentHref('claude-code', 'cs_1')).toBe('#/agents?agent=claude-code&session=cs_1');
  });
});

describe('crew section', () => {
  it('has no sub-navigation bar (the crew page carries its own view tabs)', () => {
    expect(SECTIONS.find((s) => s.id === 'crews')).toMatchObject({ tabs: ['crews', 'crew'], subnav: false });
    expect(SECTIONS.filter((s) => s.subnav === false).map((s) => s.id)).toEqual(['crews']);
  });
});
