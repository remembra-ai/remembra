// Server-side renders of the Zone Map and Policy components with real reducer
// state: structure, accessible names, the words each state shows, and what a
// principal without rights sees. (Interaction and the live API are covered by
// the live test against a real server, tests/crew/test_wp13b_dashboard_live.py.)

import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it } from 'vitest';
import { createCrewApi } from '../../../../lib/crew/api';
import type { CrewDetail } from '../../../../lib/crew/types';
import { GitHookStatus } from '../../policy/GitHookStatus';
import { PolicyPanel } from '../../policy/PolicyPanel';
import { checkoutRows } from '../../policy/policyModel';
import type { HumanActionRunner } from '../../policy/useHumanAction';
import { PendingZoneChanges } from '../PendingZoneChanges';
import { ZoneSetup } from '../ZoneSetup';
import { ZoneTree } from '../ZoneTree';
import { zonesApi } from '../zonesApi';
import { buildZoneTree, defaultExpanded, pendingTargets, zoneStatus, type ZoneDetail, type ZoneStatus } from '../zoneModel';
import { NOW, PENDING, SNAPSHOT, TREE, crewState } from './fixtures';

const api = createCrewApi({ baseUrl: '', credentials: () => ({ jwt: 'x' }), fetch: async () => new Response('{}') });
const runner: HumanActionRunner = { run: (_w, a) => a(), prompt: null };

function statuses() {
  const state = crewState();
  const pending = pendingTargets([PENDING]);
  const map = new Map<string, ZoneStatus>();
  for (const z of Object.values(state.zones)) map.set(z.id, zoneStatus(state, z, pending.slugs, NOW));
  return { state, map };
}

describe('ZoneTree', () => {
  it('renders an accessible tree of folders with zones, states and holders', () => {
    const { state, map } = statuses();
    const root = buildZoneTree(Object.values(state.zones), TREE);
    const html = renderToStaticMarkup(
      <ZoneTree
        state={state}
        root={root}
        statuses={map}
        expanded={defaultExpanded(root)}
        onToggle={() => {}}
        selectedZoneId="zn_pos"
        onOpenZone={() => {}}
        flashZoneIds={new Set(['zn_pos'])}
      />,
    );
    expect(html).toContain('role="tree"');
    expect(html.match(/role="treeitem"/g)).toHaveLength(11);
    // roving focus: exactly one row is in the tab order
    expect(html.match(/tabindex="0"/g)).toHaveLength(1);
    expect(html).toMatch(/aria-label="src\/app\/pos\/\. zone pos, contested, policy change pending: held by cc-1 \(enforced\) since [^"]+ · T-14 · 1 waiting"/);
    expect(html).toContain('aria-selected="true"');
    expect(html).toContain('cz-row cz-flash');
    expect(html).toContain('reserved for the next pickup of T-12 (quota) · held until picked up');
    expect(html).toContain('frozen by a human · breach: exclusive breach by cc-2');
    expect(html).toContain('free · protected: needs a human grant');
    expect(html).toContain('>12 files<');
    expect(html).toContain('from glob');
    expect(html).toContain('├─ ');
    // untrusted titles are text, not markup
    expect(html).toContain('>POS<');
  });

  it('escapes hostile zone titles', () => {
    const { state, map } = statuses();
    state.zones.zn_pos = { ...state.zones.zn_pos, title: '<img src=x onerror=alert(1)>' };
    const root = buildZoneTree(Object.values(state.zones), TREE);
    const html = renderToStaticMarkup(
      <ZoneTree state={state} root={root} statuses={map} expanded={defaultExpanded(root)} onToggle={() => {}} selectedZoneId={null} onOpenZone={() => {}} flashZoneIds={new Set()} />,
    );
    expect(html).not.toContain('<img');
    expect(html).toContain('&lt;img src=x onerror=alert(1)&gt;');
  });
});

describe('PendingZoneChanges', () => {
  it('shows the diff, what is held back, who uploaded it, and the decision buttons', () => {
    const state = crewState();
    const html = renderToStaticMarkup(
      <PendingZoneChanges state={state} changes={[PENDING]} now={new Date(NOW)} canAct why={null} crewApi={api} runner={runner} onChanged={() => {}} />,
    );
    expect(html).toContain('1 zone change waiting for you');
    expect(html).toContain('remove zone payroll');
    expect(html).toContain('(held back)');
    expect(html).toContain('zone pos: title');
    expect(html).toContain('(applied already)');
    expect(html).toContain('uploaded by codex-1');
    expect(html).toContain('loosens protection');
    expect(html).toContain('>Approve<');
    expect(html).toContain('>Reject<');
  });

  it('explains instead of offering buttons to someone who cannot decide', () => {
    const html = renderToStaticMarkup(
      <PendingZoneChanges state={crewState()} changes={[PENDING]} now={new Date(NOW)} canAct={false} why="Only a dashboard login can do this; API keys never can." crewApi={api} runner={runner} onChanged={() => {}} />,
    );
    expect(html).not.toContain('>Approve<');
    expect(html).toContain('API keys never can');
  });

  it('renders nothing without pending changes', () => {
    const html = renderToStaticMarkup(
      <PendingZoneChanges state={crewState()} changes={[{ ...PENDING, state: 'applied' }]} now={new Date(NOW)} canAct why={null} crewApi={api} runner={runner} onChanged={() => {}} />,
    );
    expect(html).toBe('');
  });
});

describe('ZoneSetup', () => {
  it('lists the temporary zones with Keep / Rename / Merge / Split / Drop and Undo', () => {
    const zones: ZoneDetail[] = [
      { ...SNAPSHOT.zones[0], id: 'zn_s1', slug: 'app', include_globs: ['src/app/**'], source: 'suggested', is_leaf: true },
      { ...SNAPSHOT.zones[4], id: 'zn_s2', slug: 'billing', frozen_by: null, source: 'suggested' },
    ];
    const html = renderToStaticMarkup(
      <ZoneSetup crewId="crw_x" zones={zones} tree={TREE} bootstrap canAct why={null} api={zonesApi(api)} runner={runner} onChanged={() => {}} />,
    );
    expect(html).toContain('Temporary zones (auto)');
    for (const word of ['keep', 'Rename', 'Merge into…', 'Split', 'Drop', 'Undo temporary zones', 'Save as .remembra/zones.yml']) expect(html).toContain(word);
    expect(html).toContain('src/app/**');
    // billing has no subfolders: Split is disabled with the reason
    expect(html).toContain('title="This folder has fewer than two subfolders."');
  });

  it('offers a draft from folders when the crew has no zones', () => {
    const html = renderToStaticMarkup(
      <ZoneSetup crewId="crw_y" zones={[]} tree={TREE} bootstrap={false} canAct why={null} api={zonesApi(api)} runner={runner} onChanged={() => {}} />,
    );
    expect(html).toContain('No zones yet');
    expect(html).toContain('Draft zones from my folders');
  });
});

describe('Policy', () => {
  it('shows each checkout with its git gates and the fix for a missing one', () => {
    const html = renderToStaticMarkup(<GitHookStatus rows={checkoutRows(crewState())} />);
    expect(html).toContain('1 checkout has lost the commit gate');
    expect(html).toContain('commit gate: missing');
    expect(html).toContain('remembra-crew connect --git-hooks --apply');
    expect(html).toContain('cc-1 (enforced)');
    expect(renderToStaticMarkup(<GitHookStatus rows={[]} />)).toContain('No agents are running');
  });

  it('renders enforcement, the truth table with live agents, zone changes, codes and the log', () => {
    const state = crewState();
    const detail: CrewDetail = {
      crew: SNAPSHOT.crew,
      role: 'owner',
      permissions: ['crew:read', 'crew:write', 'crew:claim', 'crew:override', 'crew:admin'],
      human: true,
      settings: { enforcement: 'enforce' },
      settings_version: 3,
      members: 1,
      created_at: '2026-09-01T00:00:00Z',
    };
    const html = renderToStaticMarkup(
      <PolicyPanel
        crewId="crw_0000000000000a01"
        state={state}
        detail={detail}
        access={{ canAct: true, why: null }}
        canAdmin
        adminWhy={null}
        offsetMs={0}
        now={new Date(NOW)}
        pendingChanges={[PENDING]}
        recentChanges={[{ ...PENDING, id: 'zch_0', state: 'applied', decided_at: '2026-09-26T13:00:00Z', summary: 'added pos' }]}
        codes={[
          { id: 'byp_active000001', session_id: 'cs_b', scope: 'push', issued_by: 'u', expires_at: '2026-09-26T14:14:30Z', used_at: null, created_at: '2026-09-26T14:04:30Z', state: 'active' },
          { id: 'byp_used00000002', session_id: 'cs_a', scope: 'commit', issued_by: 'u', expires_at: '2026-09-26T14:00:00Z', used_at: '2026-09-26T13:55:00Z', created_at: '2026-09-26T13:50:00Z', state: 'used' },
        ]}
        codesError={null}
        events={[
          {
            seq: 39,
            id: 'e',
            crew_id: 'c',
            project_id: 'p',
            ts: '2026-09-26T14:09:00Z',
            type: 'guard.tamper_blocked',
            v: 1,
            origin: 'client',
            actor: { kind: 'session', id: 'cs_b', callsign: 'codex-1', verified: true },
            refs: {},
            severity: 'high',
            moment: true,
            summary: 'codex-1 tried git commit --no-verify: blocked',
            payload: {},
          },
        ]}
        api={api}
        runner={runner}
        onChanged={() => {}}
        onCodesChanged={() => {}}
      />,
    );
    expect(html).toContain('This crew is on enforce');
    expect(html).toContain('aria-pressed="true"');
    expect(html).toContain('live: cc-1'); // Claude Code row
    expect(html).toContain('live: codex-1'); // unverified own-worktree row
    expect(html).toContain('Local enforcement coordinates cooperative agents.');
    expect(html).toContain('1 change waiting for you');
    expect(html).toContain('approved · zones.yml @');
    expect(html).toContain('active · 4:30 left');
    expect(html).toContain('used ');
    expect(html).toContain('Issue a code…');
    expect(html).toContain('codex-1 tried git commit --no-verify: blocked');
  });

  it('never lists codes to someone who cannot act', () => {
    const state = crewState();
    const html = renderToStaticMarkup(
      <PolicyPanel
        crewId="c"
        state={state}
        detail={undefined}
        access={{ canAct: false, why: 'Only a dashboard login can do this; API keys never can.' }}
        canAdmin={false}
        adminWhy="Only a dashboard login can change enforcement; API keys never can."
        offsetMs={0}
        now={new Date(NOW)}
        pendingChanges={[]}
        recentChanges={[]}
        codes={undefined}
        codesError={null}
        events={[]}
        api={api}
        runner={runner}
        onChanged={() => {}}
        onCodesChanged={() => {}}
      />,
    );
    expect(html).not.toContain('Issue a code');
    expect(html).toContain('Only a dashboard login can change enforcement');
    expect(html.match(/disabled=""/g)?.length).toBeGreaterThanOrEqual(3); // the three enforcement plates
  });
});
