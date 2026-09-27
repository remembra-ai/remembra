import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it } from 'vitest';
import { BAYER8, CELL, EMBER, cloudCells, mix, parseHex, tonesFrom } from '../dither';
import { CREW_INSTALL_COMMAND, EmptyBoard, EmptyFeed, NoCrewsEmpty, NoZonesEmpty, NothingNeedsYou, OneAgentEmpty } from '../EmptyStates';
import { VIGNETTES, pixelRuns, type VignetteId } from '../pixels';

describe('dither field', () => {
  it('uses a complete 8×8 Bayer matrix', () => {
    expect(BAYER8).toHaveLength(64);
    expect(new Set(BAYER8).size).toBe(64);
    expect(Math.min(...BAYER8)).toBeGreaterThan(0);
    expect(Math.max(...BAYER8)).toBeLessThan(1);
  });

  it('is deterministic, sized to the canvas in 6px cells, and levels stay in 0..4', () => {
    const a = cloudCells(300, 120, 0, 'right');
    const b = cloudCells(300, 120, 0, 'right');
    expect(a.cols).toBe(Math.ceil(300 / CELL));
    expect(a.rows).toBe(20);
    expect([...a.cells]).toEqual([...b.cells]);
    expect(Math.max(...a.cells)).toBeLessThanOrEqual(EMBER);
    expect([...a.cells].some((v) => v > 0)).toBe(true);
  });

  it('banks to the right for the "right" shape (left third nearly clear)', () => {
    const { cols, rows, cells } = cloudCells(600, 180, 0, 'right');
    let left = 0;
    let right = 0;
    for (let j = 0; j < rows; j += 1)
      for (let i = 0; i < cols; i += 1) {
        if (!cells[j * cols + i]) continue;
        if (i < cols / 3) left += 1;
        else if (i > (2 * cols) / 3) right += 1;
      }
    expect(right).toBeGreaterThan(left * 4);
  });

  it('keeps the text area clear', () => {
    const avoid = { l: 0, t: 0, r: 400, b: 180 };
    const { cols, rows, cells } = cloudCells(600, 180, 0, 'right', avoid);
    for (let j = 0; j < rows; j += 1) for (let i = 0; i < Math.floor(390 / CELL); i += 1) expect(cells[j * cols + i]).toBe(0);
  });

  it('drifts with time, and an ember burst paints a ring of signal cells', () => {
    const still = cloudCells(600, 180, 0, 'floor');
    const later = cloudCells(600, 180, 40, 'floor');
    expect([...still.cells]).not.toEqual([...later.cells]);
    const burst = cloudCells(600, 180, 0, 'floor', null, { x: 300, y: 90, rad: 60, wid: 16, a: 1 });
    const embers = [...burst.cells].filter((v) => v === EMBER).length;
    expect(embers).toBeGreaterThan(40);
  });

  it('derives dust tones from the theme tokens, light and dark', () => {
    expect(parseHex('#c9ccc5')).toEqual([201, 204, 197]);
    expect(parseHex('#fff')).toEqual([255, 255, 255]);
    expect(parseHex('rgb(1,2,3)')).toBeNull();
    expect(mix('#000000', '#ffffff', 0.5)).toBe('#808080');
    const light = tonesFrom({ paper2: '#e2e4de', rule: '#c9ccc5', signal: '#ff5b14', panel: '#fafaf7' });
    const dark = tonesFrom({ paper2: '#1a1c1f', rule: '#2e3135', signal: '#ff6b2b', panel: '#1b1d20' });
    expect(light.fills[3]).toBe('#c9ccc5');
    expect(dark.fills[3]).toBe('#2e3135');
    expect(light.fills[4]).not.toBe(light.fills[3]);
  });
});

describe('pixel vignettes', () => {
  it('are hand-drawn grids of known pixels with even rows', () => {
    for (const [id, rows] of Object.entries(VIGNETTES)) {
      const width = rows[0].length;
      for (const row of rows) {
        expect(row.length, `${id}: "${row}"`).toBe(width);
        expect(row, id).toMatch(/^[#+:o.]+$/);
      }
      expect(pixelRuns(rows).runs.length, id).toBeGreaterThan(3);
    }
  });

  it('merges runs of one colour into single rects', () => {
    const { width, height, runs } = pixelRuns(['##.oo', '#...:']);
    expect({ width, height }).toEqual({ width: 5, height: 2 });
    expect(runs).toEqual([
      { x: 0, y: 0, w: 2, pixel: '#' },
      { x: 3, y: 0, w: 2, pixel: 'o' },
      { x: 0, y: 1, w: 1, pixel: '#' },
      { x: 4, y: 1, w: 1, pixel: ':' },
    ]);
  });

  it('use orange only where the empty state asks for an action', () => {
    const withSignal = (id: VignetteId) => VIGNETTES[id].some((r) => r.includes('o'));
    expect(withSignal('trail')).toBe(true); // connect
    expect(withSignal('runner')).toBe(true); // start another agent
    expect(withSignal('zones')).toBe(true); // keep and name them
    expect(withSignal('receipt')).toBe(false);
    expect(withSignal('calm')).toBe(false);
    expect(withSignal('quiet')).toBe(false);
  });
});

describe('empty states render the spec copy (§9.14)', () => {
  const html = (node: React.ReactElement) => renderToStaticMarkup(node).replace(/<!-- -->/g, '');

  it('No crews: the three steps and the install command', () => {
    const out = html(<NoCrewsEmpty />);
    // connect is a dry run without --apply (it prints the diff and writes nothing); --crew does nothing
    expect(CREW_INSTALL_COMMAND).toBe('pipx install remembra && remembra-crew connect --apply');
    expect(out).toContain(CREW_INSTALL_COMMAND.replace(/&/g, '&amp;'));
    expect(out).toContain('you will see every change before it is written'.replace(/^y/, 'Y'));
    // a repo is a crew repo once it has .remembra/: the agent does not join a repo without it
    expect(out).toContain('Add <code');
    expect(out).toContain('.remembra/zones.yml</code> to the repo and commit it on the default branch');
    expect(out).toContain('Open the repo in Claude Code; it joins automatically.');
    expect(out).toContain('remembra-crew connect --include-unverified --apply');
    expect(out).toContain('remembra-crew verify --agent');
    expect(out).not.toContain('any connected agent');
    expect(out).toContain('data-vignette="trail"');
    expect(out).toContain('aria-hidden="true"');
    expect(html(<NoCrewsEmpty project="yaadbooks" />)).toContain('No crew for yaadbooks yet');
  });

  it('One agent, No zones, Empty board, Empty Needs-you', () => {
    expect(html(<OneAgentEmpty callsign="cc-1" holds={['pos']} />)).toContain('Start another agent here; it gets the brief plus the zones this one holds.');
    expect(html(<OneAgentEmpty callsign="cc-1" holds={['pos']} />)).toContain('cc-1 holds pos');
    const zones = html(<NoZonesEmpty project="yaadbooks" />);
    expect(zones).toContain('Temporary zones were made from your folders when a second agent joined.');
    expect(zones).toContain('href="#/crew?project=yaadbooks&amp;view=zones"');
    expect(zones).toContain('Keep and name them');
    expect(html(<NoZonesEmpty project="p" onKeep={() => {}} />)).toContain('<button type="button"');
    expect(html(<EmptyBoard />)).toContain('Tasks only close with a report.');
    expect(html(<NothingNeedsYou />)).toContain('Nothing needs you. The crew is running.');
  });

  it('Empty feed, plain and filtered', () => {
    expect(html(<EmptyFeed filtered={false} />)).toContain('Events land here the moment an agent joins');
    const filtered = html(<EmptyFeed filtered onClear={() => {}} />);
    expect(filtered).toContain('No events match these filters.');
    expect(filtered).toContain('Clear filters');
  });

  it('never puts the untrusted callsign anywhere but text', () => {
    const out = html(<OneAgentEmpty callsign={'<script>alert(1)</script>'} />);
    expect(out).not.toContain('<script>');
    expect(out).toContain('&lt;script&gt;');
  });
});
