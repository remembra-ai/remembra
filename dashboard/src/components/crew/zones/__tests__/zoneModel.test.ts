import { describe, expect, it } from 'vitest';
import {
  anchorPaths,
  buildZoneTree,
  defaultExpanded,
  enforcementLayers,
  expandBraces,
  flattenTree,
  globAnchors,
  holderLabel,
  isFrozen,
  leaseText,
  pendingTargets,
  rowHolderText,
  toneOf,
  zoneStatus,
  type TreeRow,
} from '../zoneModel';
import { NOW, PENDING, TREE, crewState } from './fixtures';

function find(root: TreeRow, path: string): TreeRow | undefined {
  if (root.path === path) return root;
  for (const c of root.children) {
    const hit = find(c, path);
    if (hit) return hit;
  }
  return undefined;
}

describe('glob anchors', () => {
  it('expands braces, nested too', () => {
    expect(expandBraces('src/{payroll,hr}/**')).toEqual(['src/payroll/**', 'src/hr/**']);
    expect(expandBraces('a/{b,{c,d}}/x')).toEqual(['a/b/x', 'a/c/x', 'a/d/x']);
    expect(expandBraces('no/braces/**')).toEqual(['no/braces/**']);
    expect(expandBraces('broken/{a,b')).toEqual(['broken/{a,b']);
  });

  it('finds the literal folder a glob starts from and whether it covers subfolders', () => {
    expect(globAnchors('src/app/pos/**')).toEqual([{ dir: 'src/app/pos', deep: true, file: false }]);
    expect(globAnchors('src/app/pos/*.ts')).toEqual([{ dir: 'src/app/pos', deep: false, file: false }]);
    expect(globAnchors('package.json')).toEqual([{ dir: '', deep: false, file: true }]);
    expect(globAnchors('src/app/')).toEqual([{ dir: 'src/app', deep: true, file: false }]);
    expect(globAnchors('**/migrations/**')).toEqual([{ dir: '', deep: true, file: false }]);
    expect(globAnchors('./lib/**')).toEqual([{ dir: 'lib', deep: true, file: false }]);
    expect(globAnchors('src/{payroll,hr}/**').map((a) => a.dir)).toEqual(['src/payroll', 'src/hr']);
  });
});

describe('the overlay tree', () => {
  const state = crewState();

  it('lays zones over the snapshot folders and adds folders known only from globs', () => {
    const root = buildZoneTree(Object.values(state.zones), TREE);
    expect(root.children.map((c) => c.name)).toEqual(['docs', 'src']);
    const pos = find(root, 'src/app/pos')!;
    expect(pos.zoneIds).toEqual(['zn_pos']);
    expect(pos.files).toBe(12);
    expect(pos.synthetic).toBe(false);
    expect(pos.inheritedIds).toEqual(['zn_app']); // src/app/** covers it from above
    expect(find(root, 'src/app')!.zoneIds).toEqual(['zn_app']);
    expect(find(root, 'src/app/settings')!.zoneIds).toEqual([]);
    expect(find(root, 'src/app/settings')!.inheritedIds).toEqual(['zn_app']);
    // payroll/hr are not in the snapshot: synthetic rows from the brace glob
    expect(find(root, 'src/payroll')!.synthetic).toBe(true);
    expect(find(root, 'src/hr')!.zoneIds).toEqual(['zn_payroll']);
    // the built-in policy zone is pinned separately, never in the tree
    expect(find(root, '.remembra')).toBeUndefined();
    expect(find(root, 'docs')!.zonesBelow).toBe(0);
    expect(find(root, 'src')!.zonesBelow).toBe(7); // app, pos, invoices, reports, billing, payroll(2 folders)
  });

  it('still builds a tree from the globs when no snapshot was uploaded', () => {
    const root = buildZoneTree(Object.values(state.zones), null);
    expect(find(root, 'src/app/pos')!.synthetic).toBe(true);
    expect(find(root, 'docs')).toBeUndefined();
  });

  it('ignores malformed snapshot nodes instead of crashing', () => {
    const bad = { name: '.', children: [{ name: '' }, { name: 'a/b' }, null, { name: 'ok', files: 2 }] } as never;
    const root = buildZoneTree([], bad);
    expect(root.children.map((c) => c.name)).toEqual(['ok']);
  });

  it('opens the folders that lead to zones and flattens with box guides', () => {
    const root = buildZoneTree(Object.values(state.zones), TREE);
    const open = defaultExpanded(root);
    expect([...open].sort()).toEqual(['src', 'src/app']);
    const flat = flattenTree(root, open);
    expect(flat.map((f) => `${f.guide}${f.row.name}`)).toEqual([
      '├─ docs',
      '└─ src',
      '   ├─ app',
      '   │  ├─ invoices',
      '   │  ├─ pos',
      '   │  ├─ reports',
      '   │  └─ settings',
      '   ├─ billing',
      '   ├─ hr',
      '   ├─ lib',
      '   └─ payroll',
    ]);
    expect(flat.find((f) => f.row.name === 'pos')!.parentPath).toBe('src/app');
    expect(flattenTree(root, new Set()).map((f) => f.row.name)).toEqual(['docs', 'src']);
  });

  it('knows where to reveal a zone', () => {
    expect(anchorPaths(state.zones.zn_payroll)).toEqual(['src/payroll', 'src/hr']);
  });
});

describe('zone status', () => {
  const state = crewState();
  const pending = pendingTargets([PENDING]);

  it('collects pending targets', () => {
    expect([...pending.slugs].sort()).toEqual(['payroll', 'pos']);
    expect(pending.enforcement).toBe(false);
    expect(pendingTargets([{ ...PENDING, items: [{ op: 'change', target: 'enforcement', field: null, loosening: true, reason: 'enforce->observe' }] }]).enforcement).toBe(true);
    expect(pendingTargets([{ ...PENDING, state: 'applied' }]).slugs.size).toBe(0);
  });

  it('gives each zone one primary state, in priority order', () => {
    const st = (id: string) => zoneStatus(state, state.zones[id], pending.slugs, NOW);
    expect(st('zn_pos').primary).toBe('contested'); // held by cc-1, cc-2 waiting
    expect(st('zn_pos').pending).toBe(true);
    expect(st('zn_pos').waiters.map((c) => c.id)).toEqual(['clm_q']);
    expect(st('zn_invoices').primary).toBe('reserved');
    expect(st('zn_reports').primary).toBe('shared');
    expect(st('zn_billing').primary).toBe('breach'); // an open breach outranks the freeze
    expect(st('zn_billing').frozen).toBe(true);
    expect(st('zn_payroll').primary).toBe('free');
    expect(st('zn_app').primary).toBe('free');
  });

  it('writes the row text from ids, slugs and callsigns', () => {
    const line = (id: string) => rowHolderText(state, state.zones[id], zoneStatus(state, state.zones[id], pending.slugs, NOW));
    expect(line('zn_pos')).toMatch(/^held by cc-1 \(enforced\) since \d{1,2}:\d{2}( [AP]M)? · T-14 · 1 waiting$/);
    expect(line('zn_invoices')).toBe('reserved for the next pickup of T-12 (quota) · held until picked up');
    expect(line('zn_reports')).toMatch(/^shared by cc-2 \(enforced\) since /);
    expect(line('zn_billing')).toBe('frozen by a human · breach: exclusive breach by cc-2');
    expect(line('zn_payroll')).toBe('free · protected: needs a human grant');
    expect(line('zn_app')).toBe('free');
  });

  it('treats a freeze that has run out as lifted', () => {
    expect(isFrozen({ frozen_by: 'u', frozen_until: '2026-09-26T14:00:00Z' }, NOW)).toBe(false);
    expect(isFrozen({ frozen_by: 'u', frozen_until: '2026-09-26T15:00:00Z' }, NOW)).toBe(true);
    expect(isFrozen({ frozen_by: 'u', frozen_until: null }, NOW)).toBe(true);
    expect(isFrozen({ frozen_by: null, frozen_until: null }, NOW)).toBe(false);
  });

  it('marks the tone that needs a person', () => {
    const st = (id: string) => zoneStatus(state, state.zones[id], pending.slugs, NOW);
    expect(toneOf(st('zn_billing'))).toBe('fail');
    expect(toneOf(st('zn_invoices'))).toBe('signal');
    expect(toneOf(st('zn_payroll'))).toBe('signal'); // a pending change touches it
    expect(toneOf(st('zn_app'))).toBe('dim');
    expect(toneOf(undefined)).toBe('dim');
  });

  it('labels holders, leases and enforcement layers', () => {
    expect(holderLabel(state, state.claims.clm_pos)).toBe('cc-1 (enforced)');
    expect(holderLabel(state, { ...state.claims.clm_pos, holder_kind: 'human' })).toBe('a human');
    expect(leaseText(state.claims.clm_pos, NOW)).toBe('8m left');
    expect(leaseText({ lease_expires_at: '2026-09-26T14:09:20Z' }, NOW)).toBe('expired 40s ago');
    expect(leaseText({ lease_expires_at: null }, NOW)).toBeNull();
    expect(enforcementLayers(state.sessions.cs_a)).toEqual({ beforeWrite: 'enforced', commit: 'enforced', push: 'enforced' });
    expect(enforcementLayers(state.sessions.cs_b)).toEqual({ beforeWrite: 'read-only fence', commit: 'missing', push: 'missing' });
    expect(enforcementLayers({ ...state.sessions.cs_b, client_kind: 'mcp', githook_state: null }).beforeWrite).toBe('advisory (crew_guard)');
  });
});
