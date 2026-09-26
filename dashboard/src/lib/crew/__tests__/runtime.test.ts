import { describe, expect, it } from 'vitest';
import { createCrewApi } from '../api';
import { CrewRuntime } from '../runtime';
import type { CrewListResponse, CrewSnapshot } from '../types';
import { ManualTimers, SocketFactory, connectLast, flush } from './fakes';
import { reducerVectors } from './vectors';

const SNAP: CrewSnapshot = reducerVectors().find((v) => v.name === 'snapshot_init')!.snapshot;

function setup() {
  const timers = new ManualTimers();
  const factory = new SocketFactory();
  const requests: string[] = [];
  const api = createCrewApi({
    baseUrl: '',
    credentials: () => ({ jwt: 't' }),
    fetch: async (url) => {
      requests.push(url);
      const body: CrewSnapshot | CrewListResponse = url.includes('/snapshot') ? SNAP : { crews: [], count: 0 };
      return new Response(JSON.stringify(body), { status: 200, headers: { 'Content-Type': 'application/json' } });
    },
  });
  const runtime = new CrewRuntime({
    api,
    socketUrl: 'ws://x/ws',
    credentials: () => ({ jwt: 't' }),
    createSocket: factory.create,
    timers,
    lingerMs: 15000,
  });
  return { timers, factory, runtime, requests };
}

describe('CrewRuntime', () => {
  it('shares one live store per crew between leases and one socket between crews', async () => {
    const { runtime, factory, requests } = setup();
    const release1 = runtime.leaseCrew(SNAP.crew.id);
    const release2 = runtime.leaseCrew(SNAP.crew.id);
    const releaseList = runtime.leaseCrewList();
    await flush(20);
    expect(requests.filter((u) => u.includes('/snapshot'))).toHaveLength(1);
    expect(factory.sockets).toHaveLength(1);
    const ws = connectLast(factory);
    expect(ws.subscribes().map((m) => m.crew_id)).toEqual([SNAP.crew.id, '*']);
    expect(runtime.storeFor(SNAP.crew.id).getView().state?.last_seq).toBe(SNAP.as_of_seq);
    expect(runtime.connectionStatus()).toBe('open');
    release1();
    release1(); // double release is a no-op
    release2();
    releaseList();
  });

  it('keeps a released store for the linger time, then stops it', async () => {
    const { runtime, factory, timers } = setup();
    const release = runtime.leaseCrew(SNAP.crew.id);
    await flush(20);
    const ws = connectLast(factory);
    const store = runtime.storeFor(SNAP.crew.id);
    release();
    await timers.advance(10000);
    const again = runtime.leaseCrew(SNAP.crew.id); // remount within the linger: same store, still live
    expect(runtime.storeFor(SNAP.crew.id)).toBe(store);
    again();
    await timers.advance(15000);
    expect(store.getView().status).toBe('stopped');
    expect(ws.json().at(-1)).toEqual({ type: 'unsubscribe', channel: 'crew', crew_id: SNAP.crew.id });
    expect(runtime.storeFor(SNAP.crew.id)).not.toBe(store); // a later lease starts fresh
  });

  it('dispose() closes the socket; a later lease starts over on a new socket', async () => {
    const { runtime, factory } = setup();
    const statuses: string[] = [];
    runtime.onConnectionStatus(() => statuses.push(runtime.connectionStatus()));
    runtime.leaseCrew(SNAP.crew.id);
    await flush(20);
    const first = connectLast(factory);
    const store = runtime.storeFor(SNAP.crew.id);
    runtime.dispose();
    expect(first.closedWith?.code).toBe(1000);
    expect(store.getView().status).toBe('stopped');
    expect(runtime.connectionStatus()).toBe('idle');
    runtime.leaseCrew(SNAP.crew.id);
    await flush(20);
    expect(factory.sockets).toHaveLength(2);
    expect(runtime.storeFor(SNAP.crew.id).getView().state?.last_seq).toBe(SNAP.as_of_seq);
    expect(statuses).toContain('open');
  });
});
