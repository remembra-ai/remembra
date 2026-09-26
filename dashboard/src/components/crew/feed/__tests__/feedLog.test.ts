import { describe, expect, it } from 'vitest';
import { CrewApiError } from '../../../../lib/crew/api';
import { ManualTimers, flush } from '../../../../lib/crew/__tests__/fakes';
import { FeedLog } from '../feedLog';
import { CREW, FakeEventsApi, FakeTap, ev } from './fixtures';

function setup(count: number, opts: { window?: number; cap?: number; tap?: boolean } = {}) {
  const api = new FakeEventsApi(count);
  const tap = new FakeTap();
  const timers = new ManualTimers();
  const log = new FeedLog(CREW, { api, tap: opts.tap === false ? null : tap.tap, timers, window: opts.window ?? 50, cap: opts.cap });
  return { api, tap, timers, log };
}

const seqs = (log: FeedLog) => log.getView().events.map((e) => e.seq);

describe('FeedLog: first load', () => {
  it('loads the newest window from the head hint, ascending, and knows there is more below', async () => {
    const { api, log } = setup(120);
    log.start(120);
    await flush(20);
    const v = log.getView();
    expect(v.status).toBe('ready');
    expect(seqs(log)).toEqual(Array.from({ length: 50 }, (_, i) => 71 + i));
    expect(v.newestSeq).toBe(120);
    expect(v.floorSeq).toBe(71);
    expect(v.hasOlder).toBe(true);
    expect(api.calls[0]).toEqual({ since: 70, limit: 200 });
  });

  it('probes the head when no hint is given, and pages past 200 events', async () => {
    const { api, log } = setup(450, { window: 400 });
    log.start(null);
    await flush(30);
    expect(api.calls.map((c) => c.since)).toEqual([0, 50, 250]);
    expect(api.calls[0].limit).toBe(1);
    expect(seqs(log)).toHaveLength(400);
    expect(log.getView().newestSeq).toBe(450);
  });

  it('an empty crew is ready with nothing older', async () => {
    const { log } = setup(0);
    log.start(0);
    await flush(10);
    expect(log.getView()).toMatchObject({ status: 'ready', events: [], hasOlder: false, newestSeq: 0 });
  });

  it('reports errors (and 404 as not_found) and can retry', async () => {
    const { api, log } = setup(10);
    api.failNext = new CrewApiError('boom', 503, 'http_503');
    log.start(10);
    await flush(10);
    expect(log.getView().status).toBe('error');
    expect(log.getView().error?.status).toBe(503);
    log.retry();
    await flush(20);
    expect(log.getView().status).toBe('ready');
    expect(seqs(log)).toHaveLength(10);

    const other = setup(3);
    other.api.failNext = new CrewApiError('gone', 404, 'not_found');
    other.log.start(3);
    await flush(10);
    expect(other.log.getView().status).toBe('not_found');
  });
});

describe('FeedLog: live tap', () => {
  it('appends live events, counts arrivals and ignores duplicates and other crews', async () => {
    const { api, tap, timers, log } = setup(5);
    log.start(5);
    await flush(10);
    const [e6] = api.append();
    tap.event(e6);
    tap.event(e6);
    tap.event({ ...ev(7), crew_id: 'crw_other' });
    expect(seqs(log)).toEqual([1, 2, 3, 4, 5, 6]);
    expect(log.getView().arrivals).toBe(1);
    expect(log.getView().lastArrivalAt).toBe(timers.now());
    expect(api.calls).toHaveLength(1); // no fetch for a contiguous frame
  });

  it('fills a gap in seq from the events endpoint', async () => {
    const { api, tap, timers, log } = setup(5);
    log.start(5);
    await flush(10);
    const fresh = api.append(4); // 6..9; the socket only delivers 9
    tap.event(fresh[3]);
    await timers.advance(250);
    expect(seqs(log)).toEqual([1, 2, 3, 4, 5, 6, 7, 8, 9]);
    expect(log.getView().arrivals).toBe(4);
    expect(api.calls[api.calls.length - 1].since).toBe(5);
  });

  it('catches up after resync_required or a fresh subscription', async () => {
    const { api, tap, timers, log } = setup(3);
    log.start(3);
    await flush(10);
    api.append(3);
    tap.send({ type: 'resync_required', crew_id: CREW, reason: 'overflow', last_seq: 6 });
    await timers.advance(250);
    expect(log.getView().newestSeq).toBe(6);
    api.append(2);
    tap.send({ type: 'crew.subscribed', crew_id: CREW, since_seq: 6, replayed: 0 });
    await timers.advance(250);
    expect(log.getView().newestSeq).toBe(8);
  });

  it('keeps live events that arrive during the first load and fills holes after it', async () => {
    const { api, tap, timers, log } = setup(10);
    api.hold();
    log.start(10);
    await flush(5);
    const fresh = api.append(3); // 11..13 while the first page is in flight; socket delivers 13 only
    tap.event(fresh[2]);
    api.open();
    await flush(30);
    await timers.advance(250);
    expect(seqs(log)).toEqual(Array.from({ length: 13 }, (_, i) => i + 1));
  });

  it('follows the store head when no live frame comes (socket down), after a grace period', async () => {
    const { api, timers, log } = setup(4, { tap: false });
    log.start(4);
    await flush(10);
    api.append(2);
    log.notifyHead(6);
    log.notifyHead(6);
    await flush(5);
    expect(log.getView().newestSeq).toBe(4);
    await timers.advance(1500);
    await flush(20);
    expect(log.getView().newestSeq).toBe(6);
    expect(api.calls).toHaveLength(2);
  });

  it('does not fetch a head that a live frame already delivered', async () => {
    const { api, tap, timers, log } = setup(4);
    log.start(4);
    await flush(10);
    const [e5] = api.append();
    log.notifyHead(5);
    tap.event(e5);
    await timers.advance(2000);
    expect(api.calls).toHaveLength(1);
  });

  it('stop() unsubscribes and ignores late responses', async () => {
    const { api, tap, log } = setup(4);
    api.hold();
    log.start(4);
    log.stop();
    api.open();
    await flush(10);
    expect(tap.unsubscribed).toBe(1);
    expect(log.getView().status).toBe('stopped');
    expect(log.getView().events).toEqual([]);
  });
});

describe('FeedLog: rate limits and retries', () => {
  it('coalesces a burst of gaps into one request', async () => {
    const { api, tap, timers, log } = setup(5);
    log.start(5);
    await flush(10);
    const fresh = api.append(8); // 6..13; every other frame is lost
    for (const e of fresh) if (e.seq % 2 === 1) tap.event(e);
    await timers.advance(250);
    expect(seqs(log)).toEqual(Array.from({ length: 13 }, (_, i) => i + 1));
    expect(api.calls).toHaveLength(2); // the first load and one fill
  });

  it('waits for Retry-After on 429 and then fills', async () => {
    const { api, tap, timers, log } = setup(5);
    log.start(5);
    await flush(10);
    const fresh = api.append(3);
    api.failNext = new CrewApiError('slow down', 429, 'rate_limited', { retryAfterS: 7 });
    tap.event(fresh[2]);
    await timers.advance(250);
    expect(log.getView().status).toBe('ready');
    expect(log.getView().error?.status).toBe(429);
    expect(seqs(log)).toEqual([1, 2, 3, 4, 5, 8]);
    await timers.advance(6999);
    expect(api.calls).toHaveLength(2);
    await timers.advance(1);
    expect(seqs(log)).toEqual([1, 2, 3, 4, 5, 6, 7, 8]);
    expect(log.getView().error).toBeNull();
  });

  it('retries the first load on its own after a server error, but not after a 403', async () => {
    const { api, timers, log } = setup(4);
    api.failNext = new CrewApiError('down', 503, 'http_503');
    log.start(4);
    await flush(10);
    expect(log.getView().status).toBe('error');
    await timers.advance(2000);
    expect(log.getView().status).toBe('ready');

    const denied = setup(4);
    denied.api.failNext = new CrewApiError('no', 403, 'forbidden');
    denied.log.start(4);
    await flush(10);
    await denied.timers.advance(60000);
    expect(denied.log.getView().status).toBe('error');
    expect(denied.api.calls).toHaveLength(1);
  });

  it('stop() cancels pending fills and retries', async () => {
    const { api, tap, timers, log } = setup(5);
    log.start(5);
    await flush(10);
    tap.event(api.append(2)[1]);
    log.stop();
    await timers.advance(60000);
    expect(api.calls).toHaveLength(1);
    expect(timers.count).toBe(0);
  });
});

describe('FeedLog: older pages and the cap', () => {
  it('loads older pages down to seq 1', async () => {
    const { log } = setup(260, { window: 20 });
    log.start(260);
    await flush(10);
    await log.loadOlder();
    expect(log.getView().floorSeq).toBe(41);
    expect(seqs(log)[0]).toBe(41);
    await log.loadOlder();
    expect(log.getView()).toMatchObject({ floorSeq: 1, hasOlder: false });
    expect(seqs(log)).toHaveLength(260);
    await log.loadOlder(); // nothing below seq 1: no request
    expect(seqs(log)).toHaveLength(260);
  });

  it('steps over a stretch pruned by retention', async () => {
    const { api, log } = setup(700, { window: 50, cap: 5000 });
    api.prune(200, 640);
    log.start(700);
    await flush(10);
    expect(seqs(log)[0]).toBe(651);
    await log.loadOlder();
    // 641..650, the pruned 200..640 skipped, stops as soon as a page has events
    expect(seqs(log)[0]).toBe(641);
    await log.loadOlder();
    expect(seqs(log)[0]).toBeLessThan(200);
    expect(seqs(log)).not.toContain(300);
  });

  it('keeps at most `cap` events, dropping the oldest', async () => {
    const { api, tap, log } = setup(60, { window: 60, cap: 60 });
    log.start(60);
    await flush(10);
    for (const e of api.append(5)) tap.event(e);
    const v = log.getView();
    expect(v.events).toHaveLength(60);
    expect(v.events[0].seq).toBe(6);
    expect(v.floorSeq).toBe(6);
    expect(v.hasOlder).toBe(true);
  });
});
