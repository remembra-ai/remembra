/// <reference types="node" />
// WP-13a live check against a real Remembra server (real FastAPI app, real
// crew.db, real JWT and API-key auth). Driven by
// tests/crew/test_wp13a_dashboard_live.py, which starts the server, plays the
// WP-13a scenario over HTTP (three agents, zones, tasks, checkpoints, a guard
// deny, then cc-1 out of credits) and sets WP13A_LIVE_* for this file.
// Skipped otherwise.
//
// It reads exactly what Mission Control and the Site Board read (crew list,
// snapshot, event window) and checks the view models and rendered lanes and
// pickup slot against it; then it runs the human lane actions for real: hand
// the baton to cc-2 (the pass arrives as baton.passed and the slot empties),
// pause and resume codex-1, and the same pause with an API key is refused.

import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it } from 'vitest';
import { createCrewApi, CrewApiError } from '../../../../lib/crew/api';
import { fromSnapshot } from '../../../../lib/crew/reducer';
import type { CrewEvent } from '../../../../lib/crew/types';
import { buildTree } from '../../../../pages/crew/buildTree';
import { liveStatus, quotaSources } from '../../../../pages/crew/trackModel';
import { runLaneAction } from '../actions';
import { buildStrip } from '../activity';
import { CrewLane } from '../CrewLane';
import { presenceView, sessionClaims, zoneChips } from '../model';
import { PickupSlot } from '../PickupSlot';
import { pickupSlots } from '../pickup';
import { batonText } from '../transit';

const URL_ = process.env.WP13A_LIVE_URL ?? '';
const JWT = process.env.WP13A_LIVE_JWT ?? '';
const KEY = process.env.WP13A_LIVE_KEY ?? '';
const CREW = process.env.WP13A_LIVE_CREW ?? '';

async function allEvents(api: ReturnType<typeof createCrewApi>, since = 0): Promise<CrewEvent[]> {
  const out: CrewEvent[] = [];
  let cursor = since;
  for (let i = 0; i < 20; i += 1) {
    const page = (await api.events(CREW, cursor, { limit: 200 })).data;
    if (!page || !page.events.length) break;
    out.push(...page.events);
    cursor = page.events[page.events.length - 1].seq;
    if (!page.has_more) break;
  }
  return out;
}

async function waitFor<T>(what: string, check: () => Promise<T | null>, timeoutMs = 10000): Promise<T> {
  const end = Date.now() + timeoutMs;
  for (;;) {
    const value = await check();
    if (value !== null) return value;
    if (Date.now() > end) throw new Error(`timed out waiting for ${what}`);
    await new Promise((r) => setTimeout(r, 50));
  }
}

describe.skipIf(!URL_)('WP-13a views against a live crew', () => {
  it('builds the track, the pickup slot and the site tree from real data, then passes the baton for real', async () => {
    const api = createCrewApi({ baseUrl: URL_, credentials: () => ({ jwt: JWT }), fetch: (u, i) => fetch(u, i) });
    const now = Date.now();

    // -- what Mission Control reads -----------------------------------------------------
    const access = await api.getCrew(CREW);
    expect(access.human).toBe(true);
    const snap = (await api.snapshot(CREW)).data!;
    const state = fromSnapshot(snap);
    const events = await allEvents(api);
    expect(events.length).toBeGreaterThan(10);
    const bySign = Object.fromEntries(Object.values(state.sessions).map((s) => [s.callsign, s]));
    const cc1 = bySign['cc-1'];
    const codex = bySign['codex-1'];
    const cc2 = bySign['cc-2'];
    expect([cc1.state, codex.state, cc2.state]).toEqual(['quota_blocked', 'active', 'active']);

    // cc-1 ran out of credits: a dropped baton, with the StopFailure error and its source
    const sources = quotaSources(events);
    const p1 = presenceView(cc1, sessionClaims(state, cc1.id), now, sources.get(cc1.id) ?? null);
    expect(p1).toMatchObject({ kind: 'dropped', label: 'credits ran out', detail: 'billing_error · reported', settled: true });

    // POS waits in one pickup slot with the saved work counted from baton.ref_created
    const slots = pickupSlots(state, events);
    expect(slots).toHaveLength(1);
    const slot = slots[0];
    expect(slot).toMatchObject({
      taskRef: 'T-1',
      reason: 'quota',
      reasonText: 'credits ran out',
      fromCallsign: 'cc-1',
      savedFiles: 3,
      unpushed: 2,
      batonRef: 'refs/remembra/baton/T-1/1',
      pickupCommand: 'remembra-crew adopt T-1',
    });
    expect(slot.zones.map((z) => [z.slug, z.title])).toEqual([['pos', 'POS section']]);
    expect(slot.sinceMs).not.toBeNull();
    const slotHtml = renderToStaticMarkup(<PickupSlot slot={slot} state={state} nowMs={now} canAct onRequest={() => {}} />);
    expect(slotHtml).toMatch(/Waiting for the next runner: POS section, handed off by cc-1 \d+s ago \(credits ran out\)\./);
    expect(slotHtml).toContain('3 uncommitted files saved. 2 commits not pushed yet.');

    // strips: cc-1 checkpointed twice (plus the quota checkpoint), cc-2 was blocked by the guard
    const strip1 = buildStrip(events, cc1.id, now);
    expect(strip1.buckets[59].marks).toContain('checkpoint');
    expect(strip1.checkpointTimes.length).toBeGreaterThanOrEqual(2);
    const strip3 = buildStrip(events, cc2.id, now);
    expect(strip3.buckets.some((b) => b.marks.includes('guard'))).toBe(true);

    // codex-1's lane: T-2 with the REPORTS chip, a read-only fence before write (hook client, advisory adapter), self-declared
    const codexHtml = renderToStaticMarkup(
      <CrewLane
        state={state}
        session={codex}
        project="yaadbooks"
        strip={buildStrip(events, codex.id, now)}
        nowMs={now}
        canAct
        quotaSource={null}
        onRequest={() => {}}
      />,
    );
    expect(codexHtml).toContain('Reports export');
    expect(codexHtml).toContain('zone reports, exclusive');
    expect(codexHtml).toContain('before write: read-only fence · commit ✓ · push ✓');
    expect(codexHtml).toContain('self-declared');
    expect(liveStatus(state, events[events.length - 1], now).text.length).toBeGreaterThan(0);

    // -- what the Site Board reads ------------------------------------------------------
    const item = (await api.listCrews()).crews.find((c) => c.crew.id === CREW)!;
    const tree = buildTree(item, snap, now);
    const pos = tree.phases.find((p) => p.label === 'Phase 2 · POS')!;
    expect(pos.glyph).toBe('◉');
    expect(pos.children.map((l) => (l.kind === 'session' ? `${l.glyph} ${l.session.callsign}` : `${l.glyph} baton`))).toEqual([
      '⚠ cc-1',
      '✦ baton',
    ]);
    const reports = tree.phases.find((p) => p.label === 'Phase 3 · Reports')!;
    expect(reports.children[0]).toMatchObject({ kind: 'session', glyph: '◉', right: 'REPORTS ▨ excl · advisory' });
    expect(tree.loose.map((l) => l.kind === 'session' && l.session.callsign)).toEqual(['cc-2']);

    // -- human actions, for real ----------------------------------------------------------
    const lastSeq = state.last_seq;
    const handed = await runLaneAction(api, {
      action: 'hand-baton',
      reason: 'cc-1 is out of credits',
      taskId: slot.task!.id,
      claimIds: slot.claims.map((c) => c.id),
      to: cc2.id,
      names: { to: 'cc-2' },
    });
    expect(handed).toBe('Handed the baton to cc-2: the task and its zones are now its.');
    const pass = await waitFor('baton.passed', async () => (await allEvents(api, lastSeq)).find((e) => e.type === 'baton.passed') ?? null);
    const names = (sid: string | null | undefined) => Object.values(state.sessions).find((s) => s.id === sid)?.callsign ?? 'unknown';
    expect(batonText({ seq: pass.seq, ts: pass.ts, ...pass.payload }, names)).toBe('Baton passed cc-1 → cc-2 (handed over by you)');
    const after = fromSnapshot((await api.snapshot(CREW)).data!);
    expect(pickupSlots(after)).toHaveLength(0);
    const chips = zoneChips(after, sessionClaims(after, cc2.id));
    expect(chips.map((c) => [c.label, c.inherited])).toEqual([['pos', true]]);

    await runLaneAction(api, { action: 'pause', reason: 'hold while I look', sessionId: codex.id, names: { session: 'codex-1' } });
    expect(fromSnapshot((await api.snapshot(CREW)).data!).sessions[codex.id].state).toBe('paused');
    await runLaneAction(api, { action: 'resume', reason: 'carry on', sessionId: codex.id });
    expect(fromSnapshot((await api.snapshot(CREW)).data!).sessions[codex.id].state).not.toBe('paused');

    // an agent's API key is never a human principal (D27)
    const agentApi = createCrewApi({ baseUrl: URL_, credentials: () => ({ apiKey: KEY }), fetch: (u, i) => fetch(u, i) });
    expect((await agentApi.getCrew(CREW)).human).toBe(false);
    const refused = await runLaneAction(agentApi, { action: 'pause', reason: 'x', sessionId: codex.id }).catch((e) => e);
    expect(refused).toBeInstanceOf(CrewApiError);
    expect((refused as CrewApiError).status).toBe(403);
  }, 60000);
});
