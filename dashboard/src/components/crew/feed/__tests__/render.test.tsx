import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it } from 'vitest';
import type { CrewStreamStatus } from '../../../../lib/crew/store';
import { EventRow } from '../EventRow';
import { FeedFilters } from '../FeedFilters';
import { liveWords, recentCount } from '../live';
import { LiveStrip } from '../LiveStrip';
import { NO_FILTERS, buildRows, rowLook, type FeedRow } from '../model';
import { ev, sample, samples } from './fixtures';

const NOW = Date.parse('2026-09-25T20:10:00Z');

function row(r: FeedRow, extra: Partial<Parameters<typeof EventRow>[0]> = {}) {
  return renderToStaticMarkup(
    <EventRow
      row={r}
      state={null}
      project="yaadbooks"
      nowMs={NOW}
      index={0}
      total={1}
      selected={false}
      runKey={null}
      fresh={false}
      onSelect={() => {}}
      onToggleRun={() => {}}
      onOpen={() => {}}
      {...extra}
    />,
  ).replace(/<!-- -->/g, '');
}

describe('EventRow', () => {
  it('renders every L0 event type as a feed article with its status word', () => {
    for (const r of buildRows(samples(), NO_FILTERS, null)) {
      const html = row(r);
      expect(html, r.event.type).toContain('<article');
      expect(html).toContain('aria-posinset="1"');
      expect(html).toContain('aria-setsize="1"');
      expect(html).toContain(rowLook(r).label.replace(/&/g, '&amp;'));
      expect(html).toContain(`data-kind="${r.kind}"`);
    }
  });

  it('shows agent text as text with the trust label, never as markup', () => {
    const e = sample('message.posted');
    (e.payload.message as { body: string }).body = '<img src=x onerror=alert(1)><a href="javascript:alert(1)">x</a>';
    const html = row({ key: 'e:1', kind: 'event', event: e, events: [e], collapsed: false });
    expect(html).not.toContain('<img');
    expect(html).not.toContain('href="javascript');
    expect(html).toContain('&lt;img src=x onerror=alert(1)&gt;');
    expect(html).toContain('(key-verified)');
  });

  it('links a row to the exact item and makes only the selected row tabbable', () => {
    const e = sample('task.done');
    const html = row({ key: 'e:1', kind: 'completion', event: e, events: [e], collapsed: false }, { selected: true });
    expect(html).toContain('href="#/crew?project=yaadbooks&amp;view=report&amp;report=rpt_1"');
    expect(html).toContain('aria-label="Open the report receipt"');
    expect(html).toMatch(/<article[^>]*tabindex="0"/);
    expect(row({ key: 'e:1', kind: 'completion', event: e, events: [e], collapsed: false })).toMatch(/<article[^>]*tabindex="-1"/);
  });

  it('draws the baton pass with both callsigns and the dashed orange path', () => {
    const e = sample('baton.passed');
    const html = row({ key: 'e:1', kind: 'baton', event: e, events: [e], collapsed: false });
    expect(html).toContain('cs_a');
    expect(html).toContain('passed the baton to');
    expect(html).toContain('stroke-dasharray="3 3"');
    expect(html).toContain('rr-baton');
  });

  it('offers to expand a collapsed checkpoint run and to collapse an expanded one', () => {
    const cps = [1, 2, 3].map((s) =>
      ev(s, {
        type: 'checkpoint.created',
        payload: { checkpoint: { id: `c${s}`, session_id: 'cs_a', task_id: null, trigger: 'commit', headline: `h${s}`, facts_source: 'relay-cli' } },
      }),
    );
    const [collapsed] = buildRows(cps, NO_FILTERS, null);
    const html = row(collapsed);
    expect(html).toContain('3 checkpoints');
    expect(html).toContain('show 3');
    expect(html).toContain('aria-expanded="false"');
    const open = buildRows(cps, NO_FILTERS, null, new Set([collapsed.key]));
    expect(row(open[1], { runKey: collapsed.key })).toContain('collapse');
  });
});

describe('LiveStrip', () => {
  it('says the connection state in words, the head seq and the newest event', () => {
    const events = [ev(1, { ts: '2026-09-25T19:00:00Z' }), ev(2, { ts: '2026-09-25T20:09:50Z', summary: 'cc-1 claimed zone pos' })];
    const html = renderToStaticMarkup(<LiveStrip status="live" connection="open" events={events} newestSeq={2} arrivals={0} nowMs={NOW} />).replace(/<!-- -->/g, '');
    expect(html).toContain('live');
    expect(html).toContain('seq 2');
    expect(html).toContain('1 in the last 10 min');
    expect(html).toContain('cc-1 claimed zone pos');
    expect(html).toContain('aria-live="off"');
  });

  it('maps every stream status to words', () => {
    const statuses: CrewStreamStatus[] = ['loading', 'live', 'polling', 'resyncing', 'not_found', 'error', 'stopped'];
    for (const s of statuses) expect(liveWords(s, 'open').text.length).toBeGreaterThan(2);
    expect(liveWords('polling', 'closed')).toEqual({ text: 'updating every few seconds', live: false });
    expect(liveWords('error', 'unauthorized').text).toBe('signed out');
    expect(recentCount([], NOW)).toBe(0);
  });
});

describe('FeedFilters', () => {
  it('renders the chips as toggle buttons with pressed state and keeps URL values selectable', () => {
    const html = renderToStaticMarkup(
      <FeedFilters filters={{ ...NO_FILTERS, types: ['baton.', 'handoff.'], session: 'cc-9' }} state={null} onChange={() => {}} />,
    );
    expect(html).toContain('aria-pressed="true"');
    expect(html).toContain('aria-pressed="false"');
    expect(html).toContain('<option value="cc-9" selected="">cc-9</option>');
    expect(html).toContain('Filters');
    expect(html).toContain('Clear');
  });
});
