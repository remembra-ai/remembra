import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it } from 'vitest';
import { ManualTimers } from '../../../../lib/crew/__tests__/fakes';
import { Announcer, coalesce, momentAnnouncements } from '../announcer';
import { CrewLiveRegions } from '../CrewA11y';
import { CREW_GO_KEYS, crewGoTarget, isTypingTarget } from '../keys';

describe('moment announcements', () => {
  const moments = [
    { seq: 3, type: 'baton.passed', summary: 'cc-2 took the baton for T-14', ts: null },
    { seq: 5, type: 'guard.tamper_blocked', summary: 'codex-1 tried to edit crew policy. Blocked', ts: null },
    { seq: 4, type: 'task.done', summary: 'T-14 done', ts: null },
  ];

  it('announces only moments newer than the last seen seq, oldest first', () => {
    expect(momentAnnouncements(moments, 3)).toEqual([
      { text: 'T-14 done', level: 'polite' },
      { text: 'codex-1 tried to edit crew policy. Blocked', level: 'assertive' },
    ]);
    expect(momentAnnouncements(moments, 5)).toEqual([]);
  });

  it('is assertive only for safety moments', () => {
    for (const type of ['guard.tamper_blocked', 'gate.tampered', 'guard.bypass_used', 'collision.detected']) {
      expect(momentAnnouncements([{ seq: 1, type, summary: 'x', ts: null }], 0)[0].level).toBe('assertive');
    }
    for (const type of ['baton.passed', 'session.lost', 'session.quota_blocked', 'task.done', 'decision.confirmed']) {
      expect(momentAnnouncements([{ seq: 1, type, summary: 'x', ts: null }], 0)[0].level).toBe('polite');
    }
  });
});

describe('Announcer', () => {
  it('coalesces a burst into one polite announcement', async () => {
    const timers = new ManualTimers();
    const a = new Announcer({ timers, politeMs: 1000 });
    let changes = 0;
    a.subscribe(() => (changes += 1));
    a.announce('cc-1 joined');
    a.announce('codex-1 joined.');
    a.announce('crew assembled');
    a.announce('T-14 done');
    expect(a.getText().polite).toBe('');
    await timers.advance(1000);
    expect(a.getText().polite).toBe('4 updates. codex-1 joined. crew assembled. T-14 done. And 1 more.');
    expect(changes).toBe(1);
  });

  it('keeps assertive separate and fast, and repeats identical text', async () => {
    const timers = new ManualTimers();
    const a = new Announcer({ timers, politeMs: 1000, assertiveMs: 200 });
    a.announce('Bypass code used by cc-1', 'assertive');
    await timers.advance(200);
    expect(a.getText()).toEqual({ polite: '', assertive: 'Bypass code used by cc-1' });
    a.announce('Bypass code used by cc-1', 'assertive');
    await timers.advance(200);
    expect(a.getText().assertive).toBe('Bypass code used by cc-1\u200b');
    a.announce('   ');
    await timers.advance(2000);
    expect(a.getText().polite).toBe('');
    a.reset();
    expect(a.getText()).toEqual({ polite: '', assertive: '' });
  });

  it('coalesce keeps a single message as is', () => {
    expect(coalesce(['one'])).toBe('one');
    expect(coalesce([])).toBe('');
    expect(coalesce(['a', 'b'])).toBe('2 updates. a. b.');
  });

  it('renders exactly one polite and one assertive region', () => {
    const a = new Announcer();
    const html = renderToStaticMarkup(<CrewLiveRegions announcer={a} />);
    expect(html.match(/aria-live="polite"/g)).toHaveLength(1);
    expect(html.match(/aria-live="assertive"/g)).toHaveLength(1);
  });
});

describe('crew go-keys', () => {
  it('maps g c/z/b/h/f to the crew views on a crew, and g c to Crews elsewhere', () => {
    expect(Object.keys(CREW_GO_KEYS).sort()).toEqual(['b', 'c', 'f', 'h', 'z']);
    expect(crewGoTarget('f', 'yaadbooks')).toEqual({ kind: 'crew', screen: 'feed' });
    expect(crewGoTarget('H', 'yaadbooks')).toEqual({ kind: 'crew', screen: 'track' });
    expect(crewGoTarget('z', 'yaadbooks')).toEqual({ kind: 'crew', screen: 'zones' });
    expect(crewGoTarget('c', null)).toEqual({ kind: 'crews' });
    expect(crewGoTarget('h', null)).toBeNull(); // the global g h (Home) applies
    expect(crewGoTarget('x', 'yaadbooks')).toBeNull();
  });

  it('never fires while typing', () => {
    expect(isTypingTarget({ tagName: 'INPUT' } as unknown as EventTarget)).toBe(true);
    expect(isTypingTarget({ tagName: 'TEXTAREA' } as unknown as EventTarget)).toBe(true);
    expect(isTypingTarget({ tagName: 'DIV', isContentEditable: true } as unknown as EventTarget)).toBe(true);
    expect(isTypingTarget({ tagName: 'DIV', isContentEditable: false } as unknown as EventTarget)).toBe(false);
    expect(isTypingTarget(null)).toBe(false);
  });
});
