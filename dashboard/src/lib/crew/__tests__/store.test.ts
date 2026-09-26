import { describe, expect, it } from 'vitest';
import { CrewApiError, type CrewResponse } from '../api';
import { CrewSocket } from '../socket';
import { CrewStore } from '../store';
import type { CrewEvent, CrewEventsPage, CrewSnapshot } from '../types';
import { ManualTimers, SocketFactory, connectLast, flush } from './fakes';
import { reducerVectors } from './vectors';

const BASE: CrewSnapshot = reducerVectors().find((v) => v.name === 'snapshot_init')!.snapshot;
const CREW = BASE.crew.id;

function snapshotAt(seq: number, patch: Partial<CrewSnapshot> = {}): CrewSnapshot {
  const snap: CrewSnapshot = JSON.parse(JSON.stringify(BASE));
  snap.as_of_seq = seq;
  snap.crew.last_seq = seq;
  snap.etag = `"${seq}"`;
  return { ...snap, ...patch };
}

function event(seq: number, to: 'idle' | 'active' | 'quiet' = 'idle', session = 'cs_a'): CrewEvent {
  return {
    seq,
    id: `evt_${seq}`,
    crew_id: CREW,
    project_id: 'yaadbooks',
    ts: '2026-09-25T20:10:00.000Z',
    type: 'session.state_changed',
    v: 1,
    origin: 'server',
    actor: { kind: 'system', id: 'server', verified: true },
    refs: { session_id: session },
    severity: 'info',
    moment: false,
    summary: `${session} is ${to}`,
    payload: { from: 'active', to, reason: 'test' },
  };
}

class FakeApi {
  snapshots: (CrewSnapshot | CrewApiError)[] = [];
  snapshotCalls = 0;
  eventCalls: { since: number; etag: string | null }[] = [];
  eventPages: (CrewResponse<CrewEventsPage> | CrewApiError)[] = [];
  private pendingSnapshot: ((value: CrewResponse<CrewSnapshot>) => void) | null = null;
  holdSnapshots = false;

  snapshot = async (crewId: string): Promise<CrewResponse<CrewSnapshot>> => {
    expect(crewId).toBe(CREW);
    this.snapshotCalls += 1;
    if (this.holdSnapshots) {
      return new Promise((resolve) => {
        this.pendingSnapshot = resolve;
      });
    }
    const next = this.snapshots.shift();
    if (!next) throw new Error('no snapshot queued');
    if (next instanceof CrewApiError) throw next;
    return { status: 200, data: next, etag: next.etag };
  };

  releaseSnapshot(snap: CrewSnapshot): void {
    this.pendingSnapshot?.({ status: 200, data: snap, etag: snap.etag });
    this.pendingSnapshot = null;
  }

  events = async (crewId: string, sinceSeq: number, options: { etag?: string | null } = {}): Promise<CrewResponse<CrewEventsPage>> => {
    expect(crewId).toBe(CREW);
    this.eventCalls.push({ since: sinceSeq, etag: options.etag ?? null });
    const next = this.eventPages.shift();
    if (!next) return { status: 304, data: null, etag: options.etag ?? null };
    if (next instanceof CrewApiError) throw next;
    return next;
  };
}

function page(events: CrewEvent[], lastSeq: number, hasMore = false): CrewResponse<CrewEventsPage> {
  return { status: 200, data: { crew_id: CREW, events, last_seq: lastSeq, has_more: hasMore }, etag: `"${lastSeq}"` };
}

function setup(options: { withSocket?: boolean } = {}) {
  const timers = new ManualTimers();
  const factory = new SocketFactory();
  const socket =
    options.withSocket === false
      ? null
      : new CrewSocket({ url: 'ws://x/ws', credentials: () => ({ jwt: 't' }), createSocket: factory.create, timers, random: () => 1 });
  const api = new FakeApi();
  const store = new CrewStore(CREW, { api, socket, timers, pollMs: 5000, retryInitialMs: 1000, retryMaxMs: 8000 });
  let notifications = 0;
  store.subscribe(() => {
    notifications += 1;
  });
  return { timers, factory, socket, api, store, notified: () => notifications };
}

function subscribedFrame(since: number, replayed = 0) {
  return { type: 'crew.subscribed', crew_id: CREW, since_seq: since, replayed };
}

describe('CrewStore', () => {
  it('loads the snapshot, subscribes from as_of_seq, then applies replay, live events and presence', async () => {
    const { api, store, factory, notified } = setup();
    api.snapshots.push(snapshotAt(10));
    expect(store.getView().status).toBe('loading');
    store.start();
    await flush();
    expect(store.getView().state?.last_seq).toBe(10);
    expect(store.getView().status).toBe('polling'); // until the server confirms the subscription
    const ws = connectLast(factory);
    expect(ws.subscribes()).toEqual([{ type: 'subscribe', channel: 'crew', crew_id: CREW, topics: ['crew'], since_seq: 10 }]);
    ws.receive(subscribedFrame(10, 1));
    expect(store.getView().status).toBe('live');
    ws.receive({ type: 'crew.event', crew_id: CREW, data: event(11, 'idle') });
    ws.receive({ type: 'crew.event', crew_id: CREW, data: event(11, 'quiet') }); // duplicate: ignored
    ws.receive({ type: 'crew.event', crew_id: CREW, data: event(12, 'active', 'cs_b') });
    const view = store.getView();
    expect(view.state?.last_seq).toBe(12);
    expect(view.state?.sessions.cs_a.state).toBe('idle');
    expect(view.lastFrameAt).toBeGreaterThan(0);
    ws.receive({
      type: 'presence',
      crew_id: CREW,
      lanes: [{ session_id: 'cs_a', state: 'active', stuck: false, last_action: { tool: 'Edit', path_rel: 'src/app/pos/a.ts', age_s: 2 }, calls_since_checkpoint: 3 }],
    });
    expect(store.getView().state?.sessions.cs_a.presence?.last_action?.path_rel).toBe('src/app/pos/a.ts');
    expect(store.getView().state?.sessions.cs_a.state).toBe('idle'); // presence never changes state
    expect(notified()).toBeGreaterThan(3);
  });

  it('keeps the same view object while nothing changes', async () => {
    const { api, store, factory } = setup();
    api.snapshots.push(snapshotAt(10));
    store.start();
    await flush();
    const ws = connectLast(factory);
    ws.receive(subscribedFrame(10));
    const before = store.getView();
    ws.receive({ type: 'crew.event', crew_id: CREW, data: event(9) }); // old seq
    ws.receive({ type: 'crew.subscribed', crew_id: CREW, since_seq: 10, replayed: 0 });
    expect(store.getView()).toBe(before);
  });

  it('records snapshot extras and the server clock offset', async () => {
    const { api, store, timers } = setup();
    const snap = snapshotAt(10, { server_time: new Date(timers.now() + 2500).toISOString(), ignore: ['docs/**'] });
    api.snapshots.push(snap);
    store.start();
    await flush();
    const meta = store.getView().meta!;
    expect(meta.clock_offset_ms).toBe(2500);
    expect(meta.ignore).toEqual(['docs/**']);
    expect(meta.etag).toBe('"10"');
    expect(meta.fetched_at).toBe(timers.now());
  });

  it('refetches the snapshot on a gap and re-subscribes from the new as_of_seq', async () => {
    const { api, store, factory } = setup();
    api.snapshots.push(snapshotAt(10));
    store.start();
    await flush();
    const ws = connectLast(factory);
    ws.receive(subscribedFrame(10));
    api.snapshots.push(snapshotAt(20));
    ws.receive({ type: 'crew.event', crew_id: CREW, data: event(15) }); // gap: 11..14 missing
    expect(store.getView().status).toBe('resyncing');
    await flush();
    expect(api.snapshotCalls).toBe(2);
    expect(store.getView().state?.last_seq).toBe(20);
    expect(store.getView().state?.needs_resync).toBe(false);
    expect(ws.subscribes().map((m) => m.since_seq)).toEqual([10, 20]);
    expect(store.getView().status).toBe('polling');
    ws.receive(subscribedFrame(20));
    expect(store.getView().status).toBe('live');
    ws.receive({ type: 'crew.event', crew_id: CREW, data: event(21) });
    expect(store.getView().state?.last_seq).toBe(21);
  });

  it('refetches when the server asks for a resync (overflow, gap too large)', async () => {
    const { api, store, factory } = setup();
    api.snapshots.push(snapshotAt(10));
    store.start();
    await flush();
    const ws = connectLast(factory);
    ws.receive(subscribedFrame(10));
    api.snapshots.push(snapshotAt(700));
    ws.receive({ type: 'resync_required', crew_id: CREW, reason: 'overflow', last_seq: 700 });
    await flush();
    expect(api.snapshotCalls).toBe(2);
    expect(store.getView().state?.last_seq).toBe(700);
    expect(ws.subscribes().at(-1)?.since_seq).toBe(700);
  });

  it('events that race a resync are applied once, strictly by seq', async () => {
    const { api, store, factory } = setup();
    api.snapshots.push(snapshotAt(10));
    store.start();
    await flush();
    const ws = connectLast(factory);
    ws.receive(subscribedFrame(10));
    api.holdSnapshots = true;
    ws.receive({ type: 'crew.event', crew_id: CREW, data: event(13) }); // gap
    ws.receive({ type: 'crew.event', crew_id: CREW, data: event(14) }); // ignored while resyncing
    api.releaseSnapshot(snapshotAt(14));
    await flush();
    ws.receive({ type: 'crew.event', crew_id: CREW, data: event(14, 'quiet') }); // replay overlap
    ws.receive({ type: 'crew.event', crew_id: CREW, data: event(15, 'active') });
    expect(store.getView().state?.last_seq).toBe(15);
    expect(store.getView().state?.sessions.cs_a.state).toBe('active');
  });

  it('polls the events endpoint while the socket is down, and stops once live', async () => {
    const { api, store, factory, timers } = setup();
    api.snapshots.push(snapshotAt(10));
    store.start();
    await flush();
    expect(store.getView().status).toBe('polling');
    api.eventPages.push(page([event(11), event(12)], 13, true), page([event(13, 'active')], 13));
    await timers.advance(5000);
    // has_more: the second page was fetched at once, without an ETag
    expect(api.eventCalls).toEqual([
      { since: 10, etag: null },
      { since: 12, etag: null },
    ]);
    expect(store.getView().state?.last_seq).toBe(13);
    await timers.advance(5000);
    // caught up: the ETag (the crew's last_seq) is sent back and answered 304
    expect(api.eventCalls.at(-1)).toEqual({ since: 13, etag: '"13"' });
    expect(store.getView().state?.last_seq).toBe(13);
    // the socket comes up: live events take over and polling stops
    const ws = connectLast(factory);
    expect(ws.subscribes().at(-1)?.since_seq).toBe(13);
    ws.receive(subscribedFrame(13));
    expect(store.getView().status).toBe('live');
    const calls = api.eventCalls.length;
    await timers.advance(20000);
    expect(api.eventCalls.length).toBe(calls);
    // the socket drops: back to polling
    ws.serverClose(1006);
    expect(store.getView().status).toBe('polling');
    await timers.advance(5000);
    expect(api.eventCalls.length).toBeGreaterThan(calls);
  });

  it('works with no socket at all (polling only)', async () => {
    const { api, store, timers } = setup({ withSocket: false });
    api.snapshots.push(snapshotAt(10));
    store.start();
    await flush();
    api.eventPages.push(page([event(11)], 11));
    await timers.advance(5000);
    expect(store.getView().status).toBe('polling');
    expect(store.getView().state?.last_seq).toBe(11);
  });

  it('a polled page with a gap (pruned events) triggers a resync', async () => {
    const { api, store, timers } = setup({ withSocket: false });
    api.snapshots.push(snapshotAt(10), snapshotAt(30));
    store.start();
    await flush();
    api.eventPages.push(page([event(25)], 30));
    await timers.advance(5000);
    expect(api.snapshotCalls).toBe(2);
    expect(store.getView().state?.last_seq).toBe(30);
  });

  it('a 404 means not found: no stream, no polling', async () => {
    const { api, store, factory, timers } = setup();
    api.snapshots.push(new CrewApiError('Not found.', 404, 'not_found'));
    store.start();
    await flush();
    expect(store.getView().status).toBe('not_found');
    expect(store.getView().error?.status).toBe(404);
    expect(factory.sockets).toHaveLength(0);
    await timers.advance(60000);
    expect(api.snapshotCalls).toBe(1);
    expect(api.eventCalls).toHaveLength(0);
  });

  it('retries a failed first load with backoff, keeping the error visible', async () => {
    const { api, store, timers } = setup();
    api.snapshots.push(new CrewApiError('boom', 500, 'http_500'), new CrewApiError('boom', 503, 'http_503'), snapshotAt(10));
    store.start();
    await flush();
    expect(store.getView().status).toBe('error');
    expect(store.getView().error?.status).toBe(500);
    await timers.advance(999);
    expect(api.snapshotCalls).toBe(1);
    await timers.advance(1);
    expect(api.snapshotCalls).toBe(2);
    await timers.advance(2000);
    expect(api.snapshotCalls).toBe(3);
    expect(store.getView().state?.last_seq).toBe(10);
    expect(store.getView().error).toBeNull();
  });

  it('re-subscribes after a rate-limited subscribe', async () => {
    const { api, store, factory, timers } = setup();
    api.snapshots.push(snapshotAt(10));
    store.start();
    await flush();
    const ws = connectLast(factory);
    ws.receive({ type: 'error', data: { channel: 'crew', crew_id: CREW, code: 'rate_limited', message: 'too many', retry_after_s: 2 } });
    expect(store.getView().error?.code).toBe('rate_limited');
    expect(store.getView().status).toBe('polling');
    await timers.advance(2000);
    expect(ws.subscribes()).toHaveLength(2);
  });

  it('stop() unsubscribes, cancels timers and ignores late responses', async () => {
    const { api, store, factory, timers } = setup();
    api.snapshots.push(snapshotAt(10));
    store.start();
    await flush();
    const ws = connectLast(factory);
    ws.receive(subscribedFrame(10));
    store.stop();
    expect(store.getView().status).toBe('stopped');
    expect(ws.json().at(-1)).toEqual({ type: 'unsubscribe', channel: 'crew', crew_id: CREW });
    ws.receive({ type: 'crew.event', crew_id: CREW, data: event(11) });
    expect(store.getView().state?.last_seq).toBe(10);
    await timers.advance(60000);
    expect(api.eventCalls).toHaveLength(0);
  });

  it('refresh() refetches the snapshot now and coalesces concurrent calls', async () => {
    const { api, store } = setup({ withSocket: false });
    api.snapshots.push(snapshotAt(10), snapshotAt(12));
    store.start();
    await flush();
    const a = store.refresh();
    const b = store.refresh();
    expect(a).toBe(b);
    await a;
    expect(api.snapshotCalls).toBe(2);
    expect(store.getView().state?.last_seq).toBe(12);
  });
});

