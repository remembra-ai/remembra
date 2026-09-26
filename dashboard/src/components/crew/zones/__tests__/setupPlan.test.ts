import { describe, expect, it } from 'vitest';
import {
  dropZone,
  initialPlan,
  mergeZones,
  newFilePatch,
  planErrors,
  planFromSuggestions,
  planToYaml,
  renameZone,
  slugify,
  splitBlocker,
  splitZone,
} from '../setupPlan';
import type { ZoneDetail } from '../zoneModel';
import { SNAPSHOT, TREE } from './fixtures';

const suggested: ZoneDetail[] = [
  { ...SNAPSHOT.zones[1], id: 'zn_s1', slug: 'app', title: 'app', include_globs: ['src/app/**'], source: 'suggested', parent_id: null },
  { ...SNAPSHOT.zones[1], id: 'zn_s2', slug: 'billing', title: 'billing', include_globs: ['src/billing/**'], source: 'suggested', parent_id: null },
  { ...SNAPSHOT.zones[1], id: 'zn_s3', slug: 'lib', title: 'lib', include_globs: ['src/lib/**'], source: 'suggested', parent_id: null },
  // not temporary: a repo zone and the built-in one are never part of the setup draft
  { ...SNAPSHOT.zones[1], id: 'zn_r', slug: 'pos', source: 'repo' },
  { ...SNAPSHOT.zones[6] },
];

describe('setup plan', () => {
  it('starts from the temporary zones only', () => {
    const plan = initialPlan(suggested);
    expect(plan.zones.map((z) => z.slug)).toEqual(['app', 'billing', 'lib']);
    expect(plan.log).toEqual([]);
  });

  it('renames, merges, drops, and keeps a trail of what happened', () => {
    let plan = initialPlan(suggested);
    plan = renameZone(plan, 'billing', 'money', 'Billing and GCT');
    plan = mergeZones(plan, 'lib', 'billing'); // keys stay stable across renames
    expect(plan.zones.map((z) => [z.slug, z.title, z.include, z.from])).toEqual([
      ['app', 'app', ['src/app/**'], ['app']],
      ['money', 'Billing and GCT', ['src/billing/**', 'src/lib/**'], ['billing', 'lib']],
    ]);
    plan = dropZone(plan, 'app');
    expect(plan.zones.map((z) => z.slug)).toEqual(['money']);
    expect(plan.log).toEqual(['renamed billing to money', 'merged lib into money', 'dropped app']);
    // no-ops leave the plan untouched
    expect(mergeZones(plan, 'billing', 'billing')).toBe(plan);
    expect(dropZone(plan, 'nope')).toBe(plan);
    expect(renameZone(plan, 'nope', 'x', 'y')).toBe(plan);
  });

  it('splits a folder zone into one zone per subfolder, in place', () => {
    const plan = initialPlan(suggested);
    const app = plan.zones[0];
    expect(splitBlocker(app, TREE)).toBeNull();
    const split = splitZone(plan, 'app', TREE);
    expect(split.zones.map((z) => [z.slug, z.include[0]])).toEqual([
      ['invoices', 'src/app/invoices/**'],
      ['pos', 'src/app/pos/**'],
      ['reports', 'src/app/reports/**'],
      ['settings', 'src/app/settings/**'],
      ['billing', 'src/billing/**'],
      ['lib', 'src/lib/**'],
    ]);
    expect(split.log).toEqual(['split app into invoices, pos, reports, settings']);
  });

  it('refuses a split that cannot be done, with the reason', () => {
    const plan = initialPlan(suggested);
    const billing = plan.zones[1];
    expect(splitBlocker(billing, TREE)).toBe('This folder has fewer than two subfolders.');
    expect(splitBlocker(billing, null)).toBe('The repository tree has not been uploaded yet.');
    expect(splitBlocker({ ...billing, include: ['a/**', 'b/**'] }, TREE)).toBe('Only a zone with one folder glob can be split.');
    expect(splitBlocker({ ...billing, include: ['src/*.ts'] }, TREE)).toBe('Only a folder zone (folder/**) can be split.');
    expect(splitZone(plan, 'billing', TREE)).toBe(plan);
  });

  it('makes unique slugs when a split collides with an existing zone', () => {
    const plan = planFromSuggestions([
      { slug: 'app', title: 'app', include: ['src/app/**'] },
      { slug: 'pos', title: 'pos', include: ['legacy/pos/**'] },
    ]);
    const split = splitZone(plan, 'app', TREE);
    expect(split.zones.map((z) => z.slug)).toEqual(['invoices', 'pos-2', 'reports', 'settings', 'pos']);
    expect(planErrors(split)).toEqual([]);
  });

  it('reports every problem the server would refuse', () => {
    let plan = initialPlan(suggested);
    plan = renameZone(plan, 'app', 'Bad Slug!', '');
    plan = renameZone(plan, 'lib', 'billing', 'x'.repeat(121));
    plan = { ...plan, zones: [...plan.zones, { key: 'k', slug: 'crew-policy', title: 't', include: [], from: [] }] };
    expect(planErrors(plan)).toEqual([
      'Bad Slug!: a slug is 1–48 of a-z, 0-9, - and _, starting with a letter or digit',
      'Bad Slug!: a title is 1–120 characters',
      'billing: used twice',
      'billing: a title is 1–120 characters',
      'crew-policy: reserved for the built-in policy zone',
      'crew-policy: needs at least one folder glob',
    ]);
  });

  it('slugifies folder names like the server does', () => {
    expect(slugify('Point Of Sale')).toBe('point-of-sale');
    expect(slugify('__x__')).toBe('x');
    expect(slugify('!!!')).toBe('zone');
  });

  it('writes zones.yml with every string quoted, and a git patch that creates it', () => {
    const plan = renameZone(initialPlan(suggested), 'app', 'true', 'App: "main" #1');
    const yaml = planToYaml(plan);
    expect(yaml).toBe(
      [
        '# Remembra Crew zones. Loosening changes need a human approval in the dashboard.',
        '# Saved from the dashboard setup; commit it on the default branch.',
        'version: 1',
        'zones:',
        '  "true":',
        '    title: "App: \\"main\\" #1"',
        '    include:',
        '      - "src/app/**"',
        '  "billing":',
        '    title: "billing"',
        '    include:',
        '      - "src/billing/**"',
        '  "lib":',
        '    title: "lib"',
        '    include:',
        '      - "src/lib/**"',
        '',
      ].join('\n'),
    );
    expect(planToYaml({ zones: [], log: [] })).toContain('zones: {}\n');
    const patch = newFilePatch('a: 1\nb: 2\n');
    expect(patch).toBe(
      ['diff --git a/.remembra/zones.yml b/.remembra/zones.yml', 'new file mode 100644', '--- /dev/null', '+++ b/.remembra/zones.yml', '@@ -0,0 +1,2 @@', '+a: 1', '+b: 2', ''].join('\n'),
    );
  });
});
