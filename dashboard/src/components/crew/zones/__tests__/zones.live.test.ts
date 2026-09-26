/// <reference types="node" />
// WP-13b live check: the Zone Map and Policy data paths against a real Remembra
// server (real FastAPI app, crew.db, WS, JWT auth), driven by
// tests/crew/test_wp13b_dashboard_live.py which seeds the scene and sets the
// CREW_LIVE_* variables. Skipped otherwise.
//
// It runs what the screens run: the zones listing with the tree snapshot, the
// overlay tree and zone states over the live store, the drawer's actions
// (freeze/unfreeze, grant, transfer, hold, revoke, edit → export patch), the
// pending-change approval with a stale login (step-up: 401, sign in again,
// retry), enforcement changes, bypass codes, the event tail, setup mode's
// zones.yml (parsed by the server's own parser afterwards) and Undo.

import { writeFileSync } from 'node:fs';
import { describe, expect, it } from 'vitest';
import { CrewApiError, createCrewApi, type CrewApi } from '../../../../lib/crew/api';
import { CrewRuntime } from '../../../../lib/crew/runtime';
import { crewSocketUrl, type SocketLike } from '../../../../lib/crew/socket';
import type { CrewState } from '../../../../lib/crew/types';
import { checkoutRows } from '../../policy/policyModel';
import { needsStepUp, reauthenticate } from '../../policy/stepUp';
import { EventTailStore, policyEvents, zoneClaimEvents } from '../eventTail';
import { initialPlan, mergeZones, planFromSuggestions, planToYaml, renameZone, splitZone } from '../setupPlan';
import { performZoneAction, undoTemporaryZones, type Run } from '../zoneActions';
import { zonesApi } from '../zonesApi';
import { buildZoneTree, pendingTargets, rowHolderText, zoneStatus, type TreeRow } from '../zoneModel';

const URL_ = process.env.CREW_LIVE_URL ?? '';
const JWT = process.env.CREW_LIVE_JWT ?? '';
const STALE = process.env.CREW_LIVE_STALE_JWT ?? '';
const CREW = process.env.CREW_LIVE_CREW ?? '';
const CREW2 = process.env.CREW_LIVE_CREW2 ?? '';
const EMAIL = process.env.CREW_LIVE_EMAIL ?? '';
const PASSWORD = process.env.CREW_LIVE_PASSWORD ?? '';
const OUT = process.env.CREW_LIVE_OUT ?? '';

async function waitFor(what: string, check: () => boolean, timeoutMs = 10000): Promise<void> {
  const end = Date.now() + timeoutMs;
  while (!check()) {
    if (Date.now() > end) throw new Error(`timed out waiting for ${what}`);
    await new Promise((r) => setTimeout(r, 25));
  }
}

function find(root: TreeRow, path: string): TreeRow | undefined {
  if (root.path === path) return root;
  for (const c of root.children) {
    const hit = find(c, path);
    if (hit) return hit;
  }
  return undefined;
}

describe.skipIf(!URL_)('zone map and policy against a live crew server', () => {
  it('shows, acts and follows live', async () => {
    let jwt = JWT;
    const api: CrewApi = createCrewApi({ baseUrl: URL_, credentials: () => ({ jwt }), fetch: (u, i) => fetch(u, i) });
    const zapi = zonesApi(api);
    const runtime = new CrewRuntime({
      api,
      socketUrl: crewSocketUrl(URL_),
      credentials: () => ({ jwt }),
      createSocket: (url) => new WebSocket(url) as unknown as SocketLike,
      lingerMs: 100,
    });
    const report: Record<string, unknown> = {};
    // The dashboard's step-up runner, minus the dialog: on 401 step_up_required sign in again, retry once.
    let reauths = 0;
    const run: Run = async (_what, action) => {
      try {
        return await action();
      } catch (err) {
        if (!needsStepUp(err)) throw err;
        jwt = await reauthenticate((u, i) => fetch(u, i), `${URL_}/api/v1`, EMAIL, PASSWORD);
        reauths += 1;
        return action();
      }
    };
    const release = runtime.leaseCrew(CREW);
    const store = runtime.storeFor(CREW);
    try {
      await waitFor('live stream', () => store.getView().status === 'live');
      const st = (): CrewState => store.getView().state!;
      const zoneId = (slug: string) => Object.values(st().zones).find((z) => z.slug === slug)!.id;

      // -- the listing and the overlay tree ---------------------------------------------------------
      const listing = await zapi.listZones(CREW);
      expect(listing.tree?.tree.name).toBe('.');
      expect(listing.bootstrap_zones).toBe(false);
      expect(listing.pending_zone_changes).toHaveLength(1);
      const root = buildZoneTree(Object.values(st().zones), listing.tree!.tree);
      expect(find(root, 'src/app/pos')!.zoneIds).toEqual([zoneId('pos')]);
      expect(find(root, 'src/app/pos')!.files).toBe(14);
      expect(find(root, 'supabase/migrations')!.zoneIds).toEqual([]);

      const changes = await zapi.zoneChanges(CREW, 'pending');
      expect(changes).toHaveLength(1);
      expect(changes[0].items.filter((i) => i.loosening).map((i) => i.target)).toEqual(['zone:payroll']);
      expect(changes[0].uploaded_by_session).toBe('cs_b');
      const pending = pendingTargets(changes);
      const status = (slug: string) => zoneStatus(st(), st().zones[zoneId(slug)], pending.slugs, Date.now());
      expect(status('pos').primary).toBe('contested'); // cc-1 holds, cc-2 waits
      expect(status('reports').primary).toBe('shared');
      expect(status('invoices').primary).toBe('held');
      expect(status('payroll').primary).toBe('free');
      expect(status('payroll').pending).toBe(true);
      expect(rowHolderText(st(), st().zones[zoneId('invoices')], status('invoices'))).toMatch(/^held by codex-1 \(advisory\) since /);

      // -- git gates per checkout -------------------------------------------------------------------
      expect(checkoutRows(st()).map((r) => [r.worktreeId, r.hook])).toEqual([
        ['wt-b', 'missing'],
        ['wt-a', 'ok'],
      ]);

      // -- freeze / unfreeze (unfreeze needs a fresh login) -----------------------------------------
      const payroll = st().zones[zoneId('payroll')];
      const drawer = (slug: string) => {
        const z = st().zones[zoneId(slug)];
        const s = status(slug);
        return { crewId: CREW, zone: z, target: s.holders[0] ?? s.reserved, crewApi: api, zonesApi: zapi, run };
      };
      await performZoneAction('freeze', drawer('payroll'), { reason: 'Mani is editing payroll himself', sessionId: null, until: null });
      await waitFor('zone.frozen', () => status('payroll').primary === 'frozen');
      expect(reauths).toBe(0); // freeze is human-only but not step-up
      jwt = STALE; // a login older than 15 minutes: step-up actions must ask again
      await performZoneAction('unfreeze', drawer('payroll'), { reason: 'done', sessionId: null, until: null });
      expect(reauths).toBe(1); // 401 step_up_required → signed in again → retried
      await waitFor('zone.unfrozen', () => status('payroll').primary === 'free');

      // -- grant the protected zone to codex-1 --------------------------------------------------------
      await performZoneAction('grant', drawer('payroll'), { reason: 'codex-1 does payroll today', sessionId: 'cs_b', until: null });
      await waitFor('payroll granted to cs_b', () => status('payroll').holders.some((c) => c.holder_session_id === 'cs_b' && c.state === 'active'));
      expect(status('payroll').holders[0].epoch).toBeGreaterThanOrEqual(2); // the hand-over bumps the fencing epoch
      expect(payroll.protected).toBe(true);

      // -- transfer, hold, revoke on pos ------------------------------------------------------------------
      await performZoneAction('transfer', drawer('pos'), { reason: 'cc-2 takes over the POS', sessionId: 'cs_c', until: null });
      await waitFor('pos transferred', () => status('pos').holders.some((c) => c.holder_session_id === 'cs_c'));
      await performZoneAction('hold', drawer('pos'), { reason: 'lunch', sessionId: null, until: null });
      await waitFor('pos on hold', () => status('pos').reserved?.reserve_reason === 'human_hold');
      const held = drawer('pos').target!.id;
      await performZoneAction('revoke', drawer('pos'), { reason: 'free it', sessionId: null, until: null });
      await waitFor('pos hold revoked', () => !st().claims[held] || !['active', 'offered', 'reserved'].includes(st().claims[held].state));

      // -- edit a repo zone: the server answers with the patch to commit (D9) -------------------------------
      const reportsZone = st().zones[zoneId('reports')];
      const edit = await run('edit', () => zapi.patchZone(reportsZone.id, { title: 'Reports and exports' }, reportsZone.version));
      expect(edit.applied).toBe(false);
      if (!edit.applied) {
        expect(edit.export_patch).toContain('+    title: Reports and exports');
        report.export_patch = edit.export_patch;
      }
      const exported = await zapi.exportZones(CREW);
      expect(exported.yaml).toContain('pos:');

      // -- approve the pending loosening change --------------------------------------------------------------
      const beforeReauth = reauths;
      jwt = STALE;
      await run('approve', () => api.approveZoneChange(changes[0].id));
      expect(reauths).toBe(beforeReauth + 1);
      await waitFor('pending change decided', () => Object.keys(st().pending_zone_changes).length === 0);
      await waitFor('payroll removed', () => !Object.values(st().zones).some((z) => z.slug === 'payroll'));
      expect(await zapi.zoneChanges(CREW, 'pending')).toEqual([]);

      // -- enforcement level --------------------------------------------------------------------------------
      const detail = await api.getCrew(CREW);
      expect(detail.human).toBe(true);
      await run('enforcement', () => api.patchSettings(CREW, { enforcement: 'observe' }, detail.settings_version));
      await waitFor('observe', () => st().crew?.enforcement === 'observe');
      const again = await api.getCrew(CREW);
      await run('enforcement', () => api.patchSettings(CREW, { enforcement: 'enforce' }, again.settings_version));
      await waitFor('enforce', () => st().crew?.enforcement === 'enforce');

      // -- bypass codes -------------------------------------------------------------------------------------------
      const issued = await run('bypass', () => api.issueBypassCode(CREW, { session_id: 'cs_b', scope: 'push', minutes: 5 }));
      expect(issued.code).toMatch(/^RCB-[0-9A-Z]{5}-[0-9A-Z]{5}$/);
      const codes = await zapi.bypassCodes(CREW);
      expect(codes.codes.map((c) => [c.id, c.state, c.scope])).toEqual([[issued.code_id, 'active', 'push']]);
      expect(JSON.stringify(codes)).not.toContain(issued.code);
      report.bypass_code_id = issued.code_id;

      // -- the event tail the drawer and the policy log read ---------------------------------------------------------
      const tail = new EventTailStore(zapi, CREW, 400);
      await tail.want(st().last_seq);
      const posEvents = zoneClaimEvents(tail.getView().events, zoneId('pos'));
      expect(posEvents.map((e) => e.type)).toEqual(expect.arrayContaining(['claim.revoked', 'claim.reserved', 'claim.transferred', 'claim.granted']));
      expect(policyEvents(tail.getView().events).map((e) => e.type)).toEqual(expect.arrayContaining(['zone.change_decided', 'crew.settings_changed']));
      expect(tail.getView().events.at(-1)!.seq).toBe(st().last_seq);

      // -- setup mode on the bootstrapped crew ------------------------------------------------------------------------
      const boot = await zapi.listZones(CREW2);
      expect(boot.bootstrap_zones).toBe(true);
      let plan = initialPlan(boot.zones);
      expect(plan.zones.map((z) => z.slug)).toEqual(['cart', 'catalog', 'checkout']);
      plan = renameZone(plan, 'checkout', 'till', 'Checkout till');
      plan = mergeZones(plan, 'cart', 'checkout');
      report.setup_yaml = planToYaml(plan);
      const suggested = await zapi.suggestZones(CREW2);
      report.suggest_yaml = planToYaml(splitZone(planFromSuggestions(suggested.zones), 'nope', boot.tree?.tree));
      const removed = await undoTemporaryZones(boot.zones, zapi, run);
      expect(removed).toBe(3);
      const after = await zapi.listZones(CREW2);
      expect(after.bootstrap_zones).toBe(false);
      expect(after.zones.filter((z) => !z.builtin)).toEqual([]);

      // -- an API key is never a human: no codes, no approvals ---------------------------------------------------------
      const keyApi = createCrewApi({ baseUrl: URL_, credentials: () => ({ apiKey: 'rem_not_a_real_key' }), fetch: (u, i) => fetch(u, i) });
      const denied = await zonesApi(keyApi).bypassCodes(CREW).catch((e: unknown) => e);
      expect(denied).toBeInstanceOf(CrewApiError);
      expect([401, 403]).toContain((denied as CrewApiError).status);

      report.last_seq = st().last_seq;
      report.reauths = reauths;
      if (OUT) writeFileSync(OUT, JSON.stringify(report));
    } finally {
      release();
      runtime.dispose();
    }
  }, 60000);
});
