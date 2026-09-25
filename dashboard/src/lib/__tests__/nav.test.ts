import { describe, expect, it } from 'vitest';
import { SECTIONS, TABS, hrefFor, parseHash, sectionOf } from '../nav';

describe('hash routes', () => {
  it('parses the tab and its parameters', () => {
    const route = parseHash('#/trail?agent=codex&project=widget');
    expect(route.tab).toBe('trail');
    expect(route.params.get('agent')).toBe('codex');
    expect(route.params.get('project')).toBe('widget');
  });

  it('falls back to home for empty or unknown hashes', () => {
    expect(parseHash('').tab).toBe('home');
    expect(parseHash('#/nope').tab).toBe('home');
    expect(parseHash('#/constructor').tab).toBe('home');
  });

  it('builds hrefs without empty parameters and round-trips them', () => {
    expect(hrefFor('inbox', { compose: '1', to: 'codex', agent: null })).toBe('#/inbox?compose=1&to=codex');
    expect(hrefFor('home')).toBe('#/home');
    const route = parseHash(hrefFor('trail', { project: 'a b&c' }));
    expect(route.params.get('project')).toBe('a b&c');
  });
});

describe('sections', () => {
  it('put every tab in exactly one section', () => {
    const tabs = SECTIONS.flatMap((s) => s.tabs);
    expect(new Set(tabs).size).toBe(tabs.length);
    expect(new Set(tabs)).toEqual(new Set(Object.keys(TABS)));
  });

  it('maps sub-pages to their section', () => {
    expect(sectionOf('keys').id).toBe('settings');
    expect(sectionOf('entities').id).toBe('graph');
    expect(sectionOf('timeline').id).toBe('memory');
    expect(sectionOf('admin').adminOnly).toBe(true);
  });
});
