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

function runtimeAnswering(status: number) {
  const requests: string[] = [];
  const api = createCrewApi({
    baseUrl: '',
    credentials: () => ({ jwt: 't' }),
    fetch: async (url) => {
      requests.push(url);
      const body = status === 200 ? { crews: [], count: 0 } : { detail: 'Not Found' };
      return new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } });
    },
  });
  const runtime = new CrewRuntime({
    api,
    socketUrl: 'ws://x/ws',
    credentials: () => ({ jwt: 't' }),
    createSocket: new SocketFactory().create,
    timers: new ManualTimers(),
    lingerMs: 15000,
  });
  return { runtime, requests };
}

describe('crew mode of the server (REMEMBRA_CREW_MODE)', () => {
  it('is off when GET /crews answers 404, from one probe, and tells its listeners', async () => {
    const { runtime, requests } = runtimeAnswering(404);
    expect(runtime.crewMode()).toBe('unknown');
    let told = 0;
    runtime.onCrewMode(() => (told += 1));
    runtime.probeCrewMode();
    runtime.probeCrewMode(); // one request in flight at a time
    await flush(20);
    expect(runtime.crewMode()).toBe('off');
    expect(told).toBe(1);
    runtime.probeCrewMode(); // known: no more requests
    await flush(20);
    expect(requests.filter((u) => u.endsWith('/crews'))).toHaveLength(1);
  });

  it('is on when GET /crews answers, and the crew list keeps it current', async () => {
    const { runtime } = runtimeAnswering(200);
    runtime.probeCrewMode();
    await flush(20);
    expect(runtime.crewMode()).toBe('on');
    const off = runtimeAnswering(404).runtime;
    const release = off.leaseCrewList();
    await flush(20);
    expect(off.crewMode()).toBe('off'); // the list's own 404 says so too
    release();
  });

  it('stays unknown on other failures (signed out, server error), so the crew screens still show', async () => {
    for (const status of [401, 500]) {
      const { runtime } = runtimeAnswering(status);
      runtime.probeCrewMode();
      await flush(20);
      expect(runtime.crewMode()).toBe('unknown');
    }
  });
});

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
