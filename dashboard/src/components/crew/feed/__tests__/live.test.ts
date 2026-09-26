/// <reference types="node" />
// Live check of the Event Feed's data path against a real Remembra server
// (real FastAPI app, real crew.db with a seeded event log, real /ws, real
// JWT). Driven by tests/crew/test_dashboard_feed_live.py, which starts the
// server, seeds a crew with a few hundred contract events and sets
// FEED_LIVE_URL / FEED_LIVE_JWT / FEED_LIVE_CREW. Skipped otherwise.
//
// It runs what the feed runs in the browser: the crew runtime (shared
// socket, store), FeedLog tapping that socket, the events endpoint for the
// first window, gap fills and older pages, and the feed model over the result.

import { writeFileSync } from 'node:fs';
import { describe, expect, it } from 'vitest';
import { createCrewApi, type CrewApi } from '../../../../lib/crew/api';
import { CrewRuntime } from '../../../../lib/crew/runtime';
import { crewSocketUrl, type SocketLike } from '../../../../lib/crew/socket';
import type { CrewErrorFrame, CrewFrame } from '../../../../lib/crew/types';
import { FeedLog } from '../feedLog';
import { NO_FILTERS, buildRows, matchesFilters, rowTarget } from '../model';

const URL_ = process.env.FEED_LIVE_URL ?? '';
const JWT = process.env.FEED_LIVE_JWT ?? '';
const CREW = process.env.FEED_LIVE_CREW ?? '';
const OUT = process.env.FEED_LIVE_OUT ?? '';

async function waitFor(what: string, check: () => boolean, timeoutMs = 10000, detail?: () => unknown): Promise<void> {
  const end = Date.now() + timeoutMs;
  while (!check()) {
    if (Date.now() > end) throw new Error(`timed out waiting for ${what}${detail ? `: ${JSON.stringify(detail())}` : ''}`);
    await new Promise((r) => setTimeout(r, 25));
  }
}

function contiguous(seqs: number[]): boolean {
  return seqs.every((s, i) => i === 0 || s === seqs[i - 1] + 1);
}

describe.skipIf(!URL_)('live event feed', () => {
  it('loads the newest window, follows live over the real socket, fills gaps and pages to seq 1', async () => {
    const api = createCrewApi({ baseUrl: URL_, credentials: () => ({ jwt: JWT }), fetch: (u, i) => fetch(u, i) });
    // each feed gets its own counted view of the events endpoint
    const counted = () => {
      const calls: number[] = [];
      const wrapped: Pick<CrewApi, 'events'> = {
        events: (crewId, since, options) => {
          calls.push(since);
          return api.events(crewId, since, options);
        },
      };
      return { api: wrapped, calls };
    };
    const runtime = new CrewRuntime({
      api,
      socketUrl: crewSocketUrl(URL_),
      credentials: () => ({ jwt: JWT }),
      createSocket: (url) => new WebSocket(url) as unknown as SocketLike,
      lingerMs: 100,
    });
    const report: Record<string, unknown> = {};
    const logs: FeedLog[] = [];
    try {
      // the store first (as on screen): it owns the replay cursor of the server subscription
      const release = runtime.leaseCrew(CREW);
      const store = runtime.storeFor(CREW);
      await waitFor('store live', () => store.getView().status === 'live');
      const head0 = store.getView().state!.last_seq;
      expect(head0).toBeGreaterThan(300);

      // -- the feed as the page wires it -----------------------------------------------------
      const mainApi = counted();
      const feed = new FeedLog(CREW, { api: mainApi.api, tap: (id, h) => runtime.socket.subscribeCrew(id, h, () => null), window: 120 });
      logs.push(feed);
      feed.start(head0);
      await waitFor('feed ready', () => feed.getView().status === 'ready');
      const v0 = feed.getView();
      expect(v0.events).toHaveLength(120);
      expect(v0.newestSeq).toBe(head0);
      expect(v0.floorSeq).toBe(head0 - 119);
      expect(v0.hasOlder).toBe(true);
      expect(contiguous(v0.events.map((e) => e.seq))).toBe(true);
      const callsAfterLoad = mainApi.calls.length;

      // a second feed whose tap drops every other live frame: it must fill the holes itself
      const lossyApi = counted();
      const lossy = new FeedLog(CREW, {
        api: lossyApi.api,
        tap: (id, h) => {
          let n = 0;
          return runtime.socket.subscribeCrew(
            id,
            (frame: CrewFrame | CrewErrorFrame) => {
              if (frame.type === 'crew.event' && n++ % 2 === 0) return;
              h(frame);
            },
            () => null,
          );
        },
        window: 20,
      });
      logs.push(lossy);
      lossy.start(head0);
      await waitFor('lossy ready', () => lossy.getView().status === 'ready');

      // -- real human actions; their events reach the feed over the socket ----------------------
      await api.freezeZone('zn_pos', 'Mani is editing POS himself');
      await api.postMessage(CREW, { body: '<b>Hold POS</b> until T-1 lands' });
      await api.pauseSession('cs_b', 'wrong branch');
      await api.resumeSession('cs_b', 'back on track');
      await api.unfreezeZone('zn_pos', 'done');
      await waitFor('store saw the unfreeze', () => store.getView().state!.last_seq >= head0 + 5 && !store.getView().state!.zones.zn_pos.frozen_by);
      // let follow-on server events (inbox items) settle: the head holds still for 400 ms
      let settled = store.getView().state!.last_seq;
      let stableSince = Date.now();
      await waitFor('head settled', () => {
        const cur = store.getView().state!.last_seq;
        if (cur !== settled) {
          settled = cur;
          stableSince = Date.now();
        }
        return Date.now() - stableSince > 400;
      });
      const head1 = settled;
      await waitFor('feed caught up live', () => feed.getView().newestSeq === head1);
      const v1 = feed.getView();
      expect(v1.arrivals).toBe(head1 - head0);
      expect(contiguous(v1.events.map((e) => e.seq))).toBe(true);
      const liveRestCalls = mainApi.calls.length - callsAfterLoad;
      expect(liveRestCalls).toBe(0); // every live event came over the socket
      const liveTypes = v1.events.filter((e) => e.seq > head0).map((e) => e.type);
      expect(liveTypes, liveTypes.join(',')).toEqual(expect.arrayContaining(['zone.frozen', 'message.posted', 'session.paused', 'session.resumed', 'zone.unfrozen']));

      await waitFor(
        'lossy filled its holes',
        () => lossy.getView().newestSeq === head1 && contiguous(lossy.getView().events.map((e) => e.seq)),
        10000,
        () => ({ head1, seqs: lossy.getView().events.map((e) => e.seq).slice(-14), error: lossy.getView().error?.message, calls: lossyApi.calls }),
      );
      expect(lossy.getView().events.filter((e) => e.seq > head0)).toHaveLength(head1 - head0);
      expect(lossyApi.calls.length).toBeGreaterThan(1); // the holes were fetched

      // the untrusted message is carried as data (the row renders it as text)
      const posted = v1.events.find((e) => e.type === 'message.posted' && e.seq > head0)!;
      expect((posted.payload.message as { body: string }).body).toBe('<b>Hold POS</b> until T-1 lands');
      expect(rowTarget(posted, store.getView().state)).toMatchObject({ screen: 'channel' });

      // -- older pages all the way down ------------------------------------------------------
      let guard = 0;
      while (feed.getView().hasOlder && guard++ < 20) await feed.loadOlder();
      const all = feed.getView().events.map((e) => e.seq);
      expect(all[0]).toBe(1);
      expect(all[all.length - 1]).toBe(head1);
      expect(all).toHaveLength(head1);
      expect(contiguous(all)).toBe(true);

      // -- the model over the real log ---------------------------------------------------------
      const rows = buildRows(feed.getView().events, NO_FILTERS, store.getView().state);
      expect(rows[0].event.seq).toBe(head1);
      const moments = feed.getView().events.filter((e) => matchesFilters(e, { ...NO_FILTERS, moments: true }, null));
      expect(moments.length).toBeGreaterThan(0);
      const posZone = feed.getView().events.filter((e) => matchesFilters(e, { ...NO_FILTERS, zone: 'pos' }, store.getView().state));
      expect(posZone.map((e) => e.type)).toEqual(expect.arrayContaining(['zone.frozen', 'zone.unfrozen']));

      // -- no socket at all: the feed follows the store's head through the endpoint ------------
      const blind = new FeedLog(CREW, { api, tap: null, window: 10, headGraceMs: 50 });
      logs.push(blind);
      blind.start(null); // probes the head itself
      await waitFor('blind ready', () => blind.getView().status === 'ready');
      expect(blind.getView().newestSeq).toBe(head1);
      await api.postMessage(CREW, { body: 'second note' });
      await waitFor('store saw it', () => store.getView().state!.last_seq > head1);
      blind.notifyHead(store.getView().state!.last_seq);
      await waitFor('blind followed the head', () => blind.getView().newestSeq === store.getView().state!.last_seq);

      report.head = store.getView().state!.last_seq;
      report.window = v0.events.length;
      report.live_arrivals = v1.arrivals;
      report.total_loaded = all.length;
      report.rows = rows.length;
      report.live_rest_calls = liveRestCalls;
      report.lossy_contiguous = contiguous(lossy.getView().events.map((e) => e.seq));
      release();
    } finally {
      for (const l of logs) l.stop();
      runtime.dispose();
      if (OUT) writeFileSync(OUT, JSON.stringify(report));
    }
  }, 60000);
});
