/// <reference types="node" />
// Live check of the whole data layer against a real Remembra server (real
// FastAPI app, real crew.db, real /ws, real JWT auth). Driven by
// tests/crew/test_dashboard_live.py, which starts the server, seeds a crew
// and sets CREW_LIVE_URL / CREW_LIVE_JWT / CREW_LIVE_CREW. Skipped otherwise.
//
// It runs exactly what the browser runs: the crew API client, the shared
// socket (first-message auth, subscribe, replay, reconnect), the per-crew
// store and reducer, the crew list with summary counts, the polling
// fallback, and the command-palette actions.

import { writeFileSync } from 'node:fs';
import { describe, expect, it } from 'vitest';
import { createCrewApi } from '../api';
import { answer, runFlow, startFlow, type CrewFlow, type FlowContext } from '../commands';
import { fromSnapshot } from '../reducer';
import { CrewRuntime } from '../runtime';
import { crewSocketUrl, type SocketLike } from '../socket';
import { CrewStore } from '../store';
import type { CrewState } from '../types';

const URL_ = process.env.CREW_LIVE_URL ?? '';
const JWT = process.env.CREW_LIVE_JWT ?? '';
const CREW = process.env.CREW_LIVE_CREW ?? '';
const OUT = process.env.CREW_LIVE_OUT ?? '';

async function waitFor(what: string, check: () => boolean, timeoutMs = 10000): Promise<void> {
  const end = Date.now() + timeoutMs;
  while (!check()) {
    if (Date.now() > end) throw new Error(`timed out waiting for ${what}`);
    await new Promise((r) => setTimeout(r, 25));
  }
}

// Known contract gap (reported to WP-0a/WP-4): `session.paused` carries only `reason`, and the
// reducer spec sets `state = paused` without touching `state_reason`, while the server's session
// view says `state_reason: "paused_by_human"`. Both reducers (Python reference and this one)
// follow the spec, so a paused session's state_reason is left out of the comparison.
function comparable(state: CrewState) {
  return {
    crew: state.crew,
    mode: state.mode,
    last_seq: state.last_seq,
    sessions: Object.fromEntries(
      Object.entries(state.sessions).map(([id, s]) => [
        id,
        { ...s, presence: null, state_reason: s.state === 'paused' ? null : s.state_reason },
      ]),
    ),
    claims: state.claims,
    zones: state.zones,
    tasks: state.tasks,
    collisions: state.collisions,
    decisions: state.decisions,
    offers: state.offers,
    inbox_counts: state.inbox_counts,
    pending_zone_changes: Object.keys(state.pending_zone_changes).sort(),
  };
}

describe.skipIf(!URL_)('live crew server', () => {
  it('follows a crew over the real WebSocket, runs palette actions and matches the server snapshot', async () => {
    const api = createCrewApi({ baseUrl: URL_, credentials: () => ({ jwt: JWT }), fetch: (u, i) => fetch(u, i) });
    const sockets: WebSocket[] = [];
    const runtime = new CrewRuntime({
      api,
      socketUrl: crewSocketUrl(URL_),
      credentials: () => ({ jwt: JWT }),
      createSocket: (url) => {
        const ws = new WebSocket(url);
        sockets.push(ws);
        return ws as unknown as SocketLike;
      },
      lingerMs: 100,
    });
    const report: Record<string, unknown> = {};
    try {
      // -- crew list + summary --------------------------------------------------------------
      const releaseList = runtime.leaseCrewList();
      const list = runtime.crewList();
      await waitFor('crew list', () => list.getView().status === 'ready' && list.getView().items.length === 1);
      expect(list.getView().items[0].crew.id).toBe(CREW);
      expect(list.getView().items[0].live).toBe(2);

      // -- one crew, live ---------------------------------------------------------------------
      const releaseCrew = runtime.leaseCrew(CREW);
      const store = runtime.storeFor(CREW);
      await waitFor('live stream', () => store.getView().status === 'live');
      expect(runtime.connectionStatus()).toBe('open');
      expect(sockets).toHaveLength(1); // the list and the crew share one socket
      expect(sockets[0].url).toBe(crewSocketUrl(URL_)); // no credentials in the URL
      const state0 = store.getView().state!;
      expect(Object.keys(state0.sessions).sort()).toEqual(['cs_a', 'cs_b']);
      expect(state0.zones.zn_pos.slug).toBe('pos');

      // -- palette actions against the real API; each lands in the store over the socket ------
      const ctx: FlowContext = {
        crews: list.getView().items,
        stateOf: (id) => runtime.storeFor(id).getView().state,
      };
      const run = async (flow: CrewFlow, answers: string[]) => {
        let f = flow;
        for (const a of answers) f = answer(f, ctx, a);
        return runFlow(f, ctx, api);
      };
      const current = { crewId: CREW, project: 'yaadbooks' };

      const who = await run(startFlow('who-holds', current), ['zn_pos']);
      expect(who.message).toBe('pos: held EXCLUSIVELY by cc-1 for T-1 · active');

      const froze = await run(startFlow('freeze', current), ['zn_pos', 'Mani is editing POS himself']);
      expect(froze.message).toMatch(/^Froze zone pos/);
      await waitFor('zone.frozen', () => !!store.getView().state!.zones.zn_pos.frozen_by);

      await run(startFlow('pause', current), ['cs_b', 'wrong branch']);
      await waitFor('session.paused', () => store.getView().state!.sessions.cs_b.state === 'paused');

      const seqBefore = store.getView().state!.last_seq;
      await run(startFlow('checkpoint', current), ['cs_a', 'before lunch']);
      await waitFor('checkpoint request event', () => store.getView().state!.last_seq > seqBefore);

      await run(startFlow('post'), [CREW, 'Hold POS until T-1 lands']);
      await waitFor('message.posted', () => store.getView().state!.messages.some((m) => m.body === 'Hold POS until T-1 lands'));
      const msg = store.getView().state!.messages.find((m) => m.body === 'Hold POS until T-1 lands')!;
      expect(msg).toMatchObject({ author_kind: 'human', kind: 'chat' });

      const bypass = await run(startFlow('bypass', current), ['cs_a', 'push', '5']);
      expect(bypass.copy).toMatch(/^RCB-[0-9A-HJKMNP-TV-Z]{5}-[0-9A-HJKMNP-TV-Z]{5}$/);

      // human-only refusals come back as readable errors (bypass minutes over the cap)
      const refused = await api.issueBypassCode(CREW, { session_id: 'cs_a', scope: 'push', minutes: 99 }).catch((e) => e);
      expect(refused).toMatchObject({ status: 422 });

      // -- the socket drops; an action happens meanwhile; the reconnect replays it ------------
      const seqAtDrop = store.getView().state!.last_seq;
      sockets[0].close();
      await waitFor('reconnecting', () => runtime.connectionStatus() !== 'open');
      await api.unfreezeZone('zn_pos', 'done editing');
      await waitFor('reconnected with replay', () => sockets.length === 2 && store.getView().status === 'live', 15000);
      await waitFor('replayed unfreeze', () => !store.getView().state!.zones.zn_pos.frozen_by);
      expect(store.getView().state!.last_seq).toBeGreaterThan(seqAtDrop);

      // -- the reduced state equals a fresh server snapshot --------------------------------------
      const fresh = (await api.snapshot(CREW)).data!;
      await waitFor('caught up to the snapshot', () => store.getView().state!.last_seq >= fresh.as_of_seq);
      expect(store.getView().state!.last_seq).toBe(fresh.as_of_seq);
      expect(comparable(store.getView().state!)).toEqual(comparable(fromSnapshot(fresh)));
      expect(store.getView().state!.needs_resync).toBe(false);

      // -- polling fallback: no socket at all ---------------------------------------------------
      const poller = new CrewStore(CREW, { api, socket: null, pollMs: 200 });
      poller.start();
      await waitFor('poller snapshot', () => poller.getView().state !== null);
      expect(poller.getView().status).toBe('polling');
      await api.resumeSession('cs_b', 'back on track');
      await waitFor('polled session.resumed', () => poller.getView().state!.sessions.cs_b.state !== 'paused');
      await waitFor('socket saw it too', () => store.getView().state!.sessions.cs_b.state !== 'paused');
      expect(poller.getView().state!.last_seq).toBe(store.getView().state!.last_seq);
      poller.stop();

      // -- a crew this login cannot see is a 404, never a leak ------------------------------------
      const ghost = new CrewStore('crw_ffffffffffffffff', { api, socket: runtime.socket });
      ghost.start();
      await waitFor('ghost not found', () => ghost.getView().status === 'not_found');
      ghost.stop();

      report.last_seq = store.getView().state!.last_seq;
      report.moments = store.getView().state!.moments.map((m) => m.type);
      report.messages = store.getView().state!.messages.length;
      report.sockets = sockets.length;
      releaseCrew();
      releaseList();
    } finally {
      runtime.dispose();
      if (OUT) writeFileSync(OUT, JSON.stringify(report));
    }
  }, 60000);
});
