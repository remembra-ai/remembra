/// <reference types="node" />
// The dashboard side of the 3-agent E2E (spec §13.3 step 8; WP-15).
//
// Driven by tests/crew/e2e/test_dashboard_e2e.py, which runs the same scenario as the model-free E2E
// and starts this file under the dashboard's vitest (`vitest run --dir ../tests/crew/e2e/dashboard`)
// as soon as the crew exists. It runs what the browser runs: the crew API client, the shared
// WebSocket, the per-crew store and reducer, and the Mission Control / Site Board view models
// (pickup slots, lanes, build tree, baton text, pass plan). While the agents work it records when each
// milestone becomes visible in the store; when the harness writes `done` it reloads from scratch
// (a new runtime: snapshot + replay) and compares. Skipped without CREW_E2E_URL.

import { existsSync, writeFileSync } from 'node:fs';
import { join } from 'node:path';
import { describe, expect, it } from 'vitest';
import { createCrewApi } from '../../../../dashboard/src/lib/crew/api';
import { CrewRuntime } from '../../../../dashboard/src/lib/crew/runtime';
import { crewSocketUrl, type SocketLike } from '../../../../dashboard/src/lib/crew/socket';
import type { CrewEvent, CrewState } from '../../../../dashboard/src/lib/crew/types';
import { pickupSlots, slotSentence } from '../../../../dashboard/src/components/crew/lane/pickup';
import { presenceView, sessionClaims } from '../../../../dashboard/src/components/crew/lane/model';
import { batonText, planPass } from '../../../../dashboard/src/components/crew/lane/transit';
import { buildTree } from '../../../../dashboard/src/pages/crew/buildTree';
import { needsYouCount } from '../../../../dashboard/src/lib/crew/selectors';

const URL_ = process.env.CREW_E2E_URL ?? '';
const JWT = process.env.CREW_E2E_JWT ?? '';
const CREW = process.env.CREW_E2E_CREW ?? '';
const DIR = process.env.CREW_E2E_DIR ?? '';
const LIVE = ['requested', 'queued', 'active', 'offered', 'reserved'];

type Milestone = { at: number; seq: number };

async function waitFor(what: string, check: () => boolean, timeoutMs: number): Promise<void> {
  const end = Date.now() + timeoutMs;
  while (!check()) {
    if (Date.now() > end) throw new Error(`timed out waiting for ${what}`);
    await new Promise((r) => setTimeout(r, 50));
  }
}

// What a reload must reproduce (docs/crew/reducer.md). Two differences are by contract and left out:
// a snapshot carries open tasks only (the live state keeps done ones), and no event updates a session's
// current_task_id (only a snapshot does: KNOWN GAP reported by WP-15, a lane shows a newly started
// task only after the next snapshot).
function projection(s: CrewState) {
  return {
    last_seq: s.last_seq,
    sessions: Object.values(s.sessions)
      .filter((x) => x.state !== 'ended')
      .map((x) => `${x.id}:${x.callsign}:${x.state}`)
      .sort(),
    claims: Object.values(s.claims)
      .filter((c) => LIVE.includes(c.state))
      .map((c) => `${c.id}:${c.state}:${c.holder_session_id ?? '-'}:${c.epoch}`)
      .sort(),
    tasks: Object.values(s.tasks)
      .filter((t) => !['done', 'cancelled'].includes(t.status))
      .map((t) => `T-${t.number}:${t.status}:${t.owner_session_id ?? '-'}`)
      .sort(),
    zones: Object.values(s.zones)
      .map((z) => `${z.slug}:${z.frozen_by ? 'frozen' : 'open'}`)
      .sort(),
    needs_you: needsYouCount(s),
  };
}

describe.skipIf(!URL_)('crew e2e: the dashboard follows the three agents live', () => {
  it('shows every step live and reloads to the same state', async () => {
    const api = createCrewApi({ baseUrl: URL_, credentials: () => ({ jwt: JWT }), fetch: (u, i) => fetch(u, i) });
    const runtime = new CrewRuntime({
      api,
      socketUrl: crewSocketUrl(URL_),
      credentials: () => ({ jwt: JWT }),
      createSocket: (url) => new WebSocket(url) as unknown as SocketLike,
      lingerMs: 100,
    });
    const report: Record<string, unknown> = {};
    const milestones: Record<string, Milestone> = {};
    const seen: CrewEvent[] = [];
    const release = runtime.leaseCrew(CREW);
    const store = runtime.storeFor(CREW);
    const tapOff = runtime.socket.subscribeCrew(
      CREW,
      (frame) => {
        if (frame.type === 'crew.event') seen.push((frame as { data: CrewEvent }).data);
      },
      () => null,
    );
    const mark = (name: string, ok: boolean, s: CrewState) => {
      if (ok && !milestones[name]) milestones[name] = { at: Date.now(), seq: s.last_seq };
    };
    let slotView: Record<string, unknown> | null = null;
    let siteBoardStep1: Record<string, unknown> | null = null;
    let laneA: Record<string, unknown> | null = null;
    const unsub = store.subscribe(() => {
      const s = store.getView().state;
      if (!s) return;
      const tasks = Object.values(s.tasks);
      const t1 = tasks.find((t) => t.number === 1);
      const pos = Object.values(s.zones).find((z) => z.slug === 'pos');
      const posClaims = Object.values(s.claims).filter((c) => pos && c.zone_id === pos.id && LIVE.includes(c.state));
      const held = posClaims.find((c) => c.state === 'active');
      const cc1 = Object.values(s.sessions).find((x) => x.callsign === 'cc-1');
      mark('t1_in_progress_by_a', !!(t1 && cc1 && t1.status === 'in_progress' && held?.holder_session_id === cc1.id), s);
      if (milestones.t1_in_progress_by_a && !laneA && cc1) {
        const pv = presenceView(cc1, sessionClaims(s, cc1.id), Date.now());
        laneA = { label: pv.label, claims: sessionClaims(s, cc1.id).map((c) => `${s.zones[c.zone_id ?? '']?.slug}:${c.state}`) };
        const item = { crew: s.crew!, role: 'owner', live: 1, needs_you: 0, crew_inbox: 0, moments_24h: 0, last_event_at: null, tasks_by_status: {}, live_sessions: [], phases: [] };
        void api.snapshot(CREW).then((res) => {
          const tree = buildTree(item as never, res.data, Date.now());
          siteBoardStep1 = { loose: tree.loose.map((l) => (l.kind === 'session' ? `${l.session.callsign}:${l.taskRef}:${l.right}` : `baton:${l.slot.taskRef}`)) };
        });
      }
      mark('blocked_b', Object.values(s.guard_blocks).some((n) => n > 0), s);
      mark('tamper', Object.values(s.tamper_blocks).some((n) => n > 0), s);
      const slots = pickupSlots(s, seen);
      const slot = slots.find((x) => x.taskRef === 'T-1');
      mark('pickup_slot', !!slot, s);
      if (slot && !slotView) {
        const sentence = slotSentence(slot, Date.now());
        slotView = { ...sentence, from: slot.fromCallsign, reason: slot.reason, command: slot.pickupCommand, ref: slot.batonRef };
      }
      if (slot && slotView && slot.offeredTo.length) slotView.offeredTo = slot.offeredTo;
      const batonItem = Object.values(s.inbox).some((i) => i.kind === 'baton_available');
      mark('needs_you_open', batonItem && needsYouCount(s) > 0, s);
      mark('baton_passed', s.batons.length > 0, s);
      mark('t1_done', t1?.status === 'done', s);
      mark('needs_you_resolved', !!milestones.needs_you_open && !slot && !batonItem, s);
    });
    try {
      await waitFor('store live', () => store.getView().status === 'live', 20000);
      writeFileSync(join(DIR, 'ready.json'), JSON.stringify({ at: Date.now(), last_seq: store.getView().state!.last_seq }));
      await waitFor('the harness to finish the scenario', () => existsSync(join(DIR, 'done')), 540000);
      const head = Number(JSON.parse((await import('node:fs')).readFileSync(join(DIR, 'done'), 'utf8')).last_seq);
      await waitFor('store at the head', () => store.getView().state!.last_seq >= head, 20000);
      const live = store.getView().state!;

      // Baton pass as Mission Control plays it: animate with motion, instant (toast) with reduced motion.
      const pass = live.batons[live.batons.length - 1];
      const callsign = (id: string | null | undefined) => (id ? live.sessions[id]?.callsign ?? id : '?');
      report.baton_text = batonText(pass, callsign);
      report.pass_plan_motion = planPass({ dialogOpen: false, waitedMs: 0, maxWaitMs: 2000, grant: { ms: 600 }, canDraw: true });
      report.pass_plan_reduced = planPass({ dialogOpen: false, waitedMs: 0, maxWaitMs: 2000, grant: { ms: 0 }, canDraw: true });

      // Reload: a brand-new runtime (snapshot + replay) reaches the same state.
      const fresh = new CrewRuntime({
        api,
        socketUrl: crewSocketUrl(URL_),
        credentials: () => ({ jwt: JWT }),
        createSocket: (url) => new WebSocket(url) as unknown as SocketLike,
        lingerMs: 100,
      });
      const releaseFresh = fresh.leaseCrew(CREW);
      const reloaded = fresh.storeFor(CREW);
      await waitFor('reloaded store live at the head', () => reloaded.getView().status === 'live' && (reloaded.getView().state?.last_seq ?? 0) >= head, 20000);
      report.inbox = Object.values(live.inbox).map((i) => `${i.kind}:${i.state}:${i.audience}`);
      report.live = projection(live);
      report.reloaded = projection(reloaded.getView().state!);
      report.inbox_reloaded = Object.values(reloaded.getView().state!.inbox).map((i) => `${i.kind}:${i.state}:${i.audience}`);
      releaseFresh();
      fresh.dispose();
      expect(report.reloaded).toEqual(report.live);
      report.milestones = milestones;
      report.slot = slotView;
      report.lane_a = laneA;
      report.site_board_step1 = siteBoardStep1;
      report.event_ts = Object.fromEntries(seen.map((e) => [e.seq, e.ts]));
      report.head = head;
    } finally {
      unsub();
      tapOff();
      release();
      runtime.dispose();
      writeFileSync(join(DIR, 'report.json'), JSON.stringify(report));
    }
  }, 600000);
});
