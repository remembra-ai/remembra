// Fix wave 3 (dashboard review findings): deep links reach the exact event, the now line keeps
// counting after an agent stops, delight moments go through one gate, and the crew screens keep
// to one polite and one assertive live region.

import { renderToStaticMarkup } from 'react-dom/server';
import { createRef } from 'react';
import { describe, expect, it } from 'vitest';
import { parseHash } from '../../../lib/nav';
import { crewHashFromLink, crewHref, parseCrewRoute } from '../../../lib/crew/routes';
import { crewDelight, holdNeedsYou } from '../../../lib/crew/delight';
import { DelightGate } from '../../../lib/motion';
import { BatonTransit } from '../BatonTransit';
import { CrewAssembled } from '../CrewAssembled';
import { NO_FILTERS, buildRows, locateSeq } from '../feed/model';
import { ev } from '../feed/__tests__/fixtures';
import { event, state } from '../lane/__tests__/fixture';
import { CrewLane } from '../lane/CrewLane';
import { buildStrip } from '../lane/activity';
import { PRESENCE_INTERVAL_MS, nowLine } from '../lane/model';
import { assembledEvent, planPass } from '../lane/transit';

describe('deep links (§9.12)', () => {
  it('the link notify.deep_link emits opens the crew route with the event (same literals as tests/crew/test_notify.py)', () => {
    const route = parseHash('#/crew?project=yaadbooks&view=feed&seq=1042');
    expect(route.tab).toBe('crew');
    expect(parseCrewRoute(route.params)).toMatchObject({ project: 'yaadbooks', screen: 'feed', seq: 1042 });
    const receipt = parseCrewRoute(parseHash('#/crew?project=yaadbooks&view=report&report=rpt_0123456789abcdef&seq=9').params);
    expect(receipt).toMatchObject({ screen: 'report', report: 'rpt_0123456789abcdef', seq: 9 });
    expect(parseCrewRoute(parseHash('#/crew?project=my+proj%2Fx&view=feed&seq=3').params).project).toBe('my proj/x');
  });

  it('ignores a malformed seq and round-trips one through crewHref', () => {
    for (const bad of ['0', '-3', '1e3', 'abc', '']) expect(parseCrewRoute(new URLSearchParams(`project=p&seq=${bad}`)).seq).toBeNull();
    const href = crewHref('yaadbooks', 'feed', { seq: 77 });
    expect(href).toBe('#/crew?project=yaadbooks&view=feed&seq=77');
    expect(parseCrewRoute(parseHash(href).params).seq).toBe(77);
  });

  it('takes only the hash of a server link, and only when it is a crew route', () => {
    expect(crewHashFromLink('https://app.remembra.dev/#/crew?project=yaadbooks&view=feed&seq=5')).toBe('#/crew?project=yaadbooks&view=feed&seq=5');
    expect(crewHashFromLink('https://app.remembra.dev/#/crews/crw_1/feed?seq=5')).toBeNull(); // not a route of this dashboard
    expect(crewHashFromLink('https://app.remembra.dev/#/crew?view=feed&seq=5')).toBeNull(); // no project
    expect(crewHashFromLink('')).toBeNull();
    expect(crewHashFromLink(undefined)).toBeNull();
  });

  it('the feed finds the linked event, inside a collapsed run too, and asks for older pages until it is loaded', () => {
    const cp = (seq: number) => ev(seq, { type: 'checkpoint.created', payload: { checkpoint: { id: `ckp_${seq}`, session_id: 'cs_a', trigger: 'commit', headline: 'h' } } });
    const rows = buildRows([ev(40), cp(41), cp(42), cp(43), ev(44)], NO_FILTERS, null);
    expect(locateSeq(rows, 44, { hasOlder: false, floorSeq: 40 })).toEqual({ index: 0, key: 'e:44' });
    expect(locateSeq(rows, 42, { hasOlder: false, floorSeq: 40 })).toEqual({ index: 1, key: 'cp:41' });
    expect(locateSeq(rows, 12, { hasOlder: true, floorSeq: 40 })).toBe('older');
    expect(locateSeq(rows, 12, { hasOlder: false, floorSeq: 1 })).toBe('missing');
    expect(locateSeq(rows, 999, { hasOlder: true, floorSeq: 40 })).toBe('missing');
  });
});

describe('now line age (the lane never freezes at "just now")', () => {
  it('counts from the frame receipt time and dims the action after two quiet presence intervals', () => {
    const s = state();
    const a = { ...s.sessions.cs_a, presence: { state: 'active' as const, stuck: false, calls_since_checkpoint: 1, last_action: { tool: 'Edit', path_rel: 'src/x.ts', age_s: 2 } } };
    expect(nowLine(s, a, 0).action).toMatchObject({ ageS: 2, stale: false });
    expect(nowLine(s, a, 90_000).action).toMatchObject({ ageS: 92, stale: true });
    expect(nowLine(s, a, 2 * PRESENCE_INTERVAL_MS).action?.stale).toBe(false);
    expect(nowLine(s, a, Number.NaN).action).toMatchObject({ ageS: 2, stale: false });
  });

  it('the rendered lane shows the counted age, not "just now"', () => {
    const s = state();
    const session = { ...s.sessions.cs_a, presence: { state: 'active' as const, stuck: false, calls_since_checkpoint: 1, last_action: { tool: 'Edit', path_rel: 'src/x.ts', age_s: 1 } } };
    const nowMs = Date.parse('2026-09-26T12:00:00Z');
    const render = (presenceAt: number | null) =>
      renderToStaticMarkup(
        <CrewLane state={s} session={session} project="yaadbooks" strip={buildStrip([], 'cs_a', nowMs)} nowMs={nowMs} canAct quotaSource={null} onRequest={() => {}} presenceAt={presenceAt} />,
      );
    expect(render(nowMs - 1000)).toContain('Edit</span><span> src/x.ts</span><span class="text-ink-3"> · just now');
    const idle = render(nowMs - 6 * 60_000);
    expect(idle).toContain(' · 6m ago');
    expect(idle).toContain('title="No new activity since"');
  });
});

describe('delight gate wiring (§9.13)', () => {
  it('grants report whether they may still play; opening a needs-you item stops a running one and tells listeners', () => {
    let now = 0;
    const g = new DelightGate({ now: () => now, reduced: () => false });
    const stopped: string[] = [];
    g.subscribe(() => stopped.push('stop'));
    const grant = g.request({ kind: 'baton_pass', ms: 600 });
    if (typeof grant === 'string') throw new Error('refused');
    expect(grant.active()).toBe(true);
    now = 300;
    const release = holdNeedsYou(g);
    expect(grant.active()).toBe(false);
    expect(stopped).toEqual(['stop']);
    expect(g.request({ kind: 'crew_assembled', ms: 600 })).toBe('needs_you_open');
    release();
    const again = g.request({ kind: 'crew_assembled', ms: 600 });
    expect(typeof again).toBe('object');
    if (typeof again === 'string') return;
    now = 1000;
    expect(again.active()).toBe(false); // over
    const reduced = new DelightGate({ reduced: () => true }).request({ kind: 'baton_pass', ms: 600 });
    expect(typeof reduced === 'object' && reduced.active()).toBe(false);
  });

  it('holds are counted: the gate reopens only when the last open needs-you item closes', () => {
    const g = new DelightGate({ reduced: () => false });
    const a = holdNeedsYou(g);
    const b = holdNeedsYou(g);
    a();
    a(); // releasing twice is a no-op
    expect(g.needsYou).toBe(true);
    b();
    expect(g.needsYou).toBe(false);
    expect(crewDelight).toBeInstanceOf(DelightGate);
  });

  it('plans each baton pass from the gate: wait for a dialog or a running animation, else instant or animate', () => {
    const base = { dialogOpen: false, waitedMs: 0, maxWaitMs: 3000, canDraw: true };
    expect(planPass({ ...base, dialogOpen: true, grant: null })).toBe('wait');
    expect(planPass({ ...base, dialogOpen: true, waitedMs: 3000, grant: { ms: 600 } })).toBe('instant');
    expect(planPass({ ...base, grant: 'busy' })).toBe('wait');
    expect(planPass({ ...base, waitedMs: 3000, grant: 'busy' })).toBe('instant');
    expect(planPass({ ...base, grant: 'needs_you_open' })).toBe('instant');
    expect(planPass({ ...base, grant: { ms: 0 } })).toBe('instant'); // reduced motion
    expect(planPass({ ...base, grant: { ms: 600 }, canDraw: false })).toBe('instant');
    expect(planPass({ ...base, grant: { ms: 600 } })).toBe('animate');
  });

  it('"Crew assembled" plays for a solo → multi change after the track opened, not for history', () => {
    const history = event('crew.mode_changed', 30, { seq: 5, payload: { from: 'solo', to: 'multi' } });
    const back = event('crew.mode_changed', 20, { seq: 8, payload: { from: 'multi', to: 'solo' } });
    const now = event('crew.mode_changed', 1, { seq: 12, payload: { from: 'solo', to: 'multi' } });
    expect(assembledEvent([history, back], 10)).toBeNull();
    expect(assembledEvent([history, back, now], 10)?.seq).toBe(12);
    expect(assembledEvent([history, back, now], 12)).toBeNull();
  });
});

describe('live regions (§9.16: one polite, one assertive, presence never announced)', () => {
  it('the baton pass and "Crew assembled" add no live region of their own', () => {
    const ref = createRef<HTMLDivElement>();
    const baton = renderToStaticMarkup(<BatonTransit batons={[{ seq: 3, ts: null, from_session: 'cs_a', to_session: 'cs_b' }]} containerRef={ref} callsignOf={() => 'cc-1'} />);
    expect(baton).not.toMatch(/role="status"|aria-live|role="alert"/);
    const assembled = renderToStaticMarkup(<CrewAssembled events={[]} sinceSeq={0} agents={2} containerRef={ref} />);
    expect(assembled).toBe('');
  });
});
