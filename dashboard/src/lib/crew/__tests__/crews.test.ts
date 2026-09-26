import { describe, expect, it } from 'vitest';
import { CrewApiError } from '../api';
import { CrewListStore, applySummary, sortCrews } from '../crews';
import { CrewSocket } from '../socket';
import type { CrewListItem, CrewListResponse } from '../types';
import { ManualTimers, SocketFactory, connectLast, flush } from './fakes';

function item(id: string, project: string, counts: Partial<Pick<CrewListItem, 'live' | 'needs_you' | 'moments_24h'>> = {}): CrewListItem {
  return {
    crew: { id, project_id: project, name: project, mode: 'solo', enforcement: 'enforce', settings_version: 1, last_seq: 0 },
    role: 'owner',
    live: 0,
    needs_you: 0,
    crew_inbox: 0,
    moments_24h: 0,
    last_event_at: null,
    tasks_by_status: {},
    phases: [],
    live_sessions: [],
    live_sessions_truncated: false,
    ...counts,
  };
}

describe('sortCrews / applySummary', () => {
  it('orders needs-you first, then live, then idle, then by project', () => {
    const sorted = sortCrews([
      item('crw_c', 'charlie'),
      item('crw_b', 'bravo', { live: 2 }),
      item('crw_a', 'alpha', { live: 1, needs_you: 1 }),
      item('crw_d', 'delta', { live: 1 }),
      item('crw_e', 'echo', { needs_you: 3 }),
    ]);
    expect(sorted.map((c) => c.crew.project_id)).toEqual(['alpha', 'echo', 'bravo', 'delta', 'charlie']);
  });

  it('overlays summary counts and mode, keeping untouched items by reference', () => {
    const a = item('crw_a', 'alpha');
    const b = item('crw_b', 'bravo');
    const out = applySummary([a, b], {
      crw_a: { crew_id: 'crw_a', project_id: 'alpha', mode: 'multi', live: 2, moments: 4, needs_you: 1 },
      crw_zzz: { crew_id: 'crw_zzz', project_id: 'zzz', mode: 'solo', live: 9, moments: 0, needs_you: 0 },
    });
    expect(out[0]).toMatchObject({ live: 2, needs_you: 1, moments_24h: 4, crew: { mode: 'multi' } });
    expect(out[1]).toBe(b);
    expect(a.live).toBe(0); // inputs untouched
  });
});

class FakeListApi {
  responses: (CrewListResponse | CrewApiError)[] = [];
  calls = 0;
  private held: ((value: CrewListResponse) => void) | null = null;
  hold = false;
  listCrews = async (): Promise<CrewListResponse> => {
    this.calls += 1;
    if (this.hold) return new Promise((resolve) => (this.held = resolve));
    const next = this.responses.shift();
    if (!next) throw new Error('no response queued');
    if (next instanceof CrewApiError) throw next;
    return next;
  };
  release(value: CrewListResponse): void {
    this.held?.(value);
    this.held = null;
  }
}

function setup() {
  const timers = new ManualTimers();
  const factory = new SocketFactory();
  const socket = new CrewSocket({ url: 'ws://x/ws', credentials: () => ({ jwt: 't' }), createSocket: factory.create, timers });
  const api = new FakeListApi();
  const store = new CrewListStore({ api, socket, timers, pollMs: 60000, refetchThrottleMs: 10000 });
  return { timers, factory, api, store };
}

describe('CrewListStore', () => {
  it('loads the list, subscribes to the summary and applies counts live', async () => {
    const { api, store, factory } = setup();
    api.responses.push({ crews: [item('crw_a', 'alpha', { live: 1 }), item('crw_b', 'bravo')], count: 2 });
    store.start();
    await flush();
    expect(store.getView()).toMatchObject({ status: 'ready', needsYou: 0 });
    const ws = connectLast(factory);
    expect(ws.subscribes()).toEqual([{ type: 'subscribe', channel: 'crew', crew_id: '*', topics: ['crew.summary'] }]);
    ws.receive({ type: 'crew.summary', crews: [{ crew_id: 'crw_b', project_id: 'bravo', mode: 'multi', live: 2, moments: 1, needs_you: 2 }] });
    const view = store.getView();
    expect(view.items[0].crew.id).toBe('crw_b'); // needs-you first now
    expect(view.needsYou).toBe(2);
  });

  it('refetches the list (throttled) after summary changes, and at once for an unknown crew', async () => {
    const { api, store, factory, timers } = setup();
    api.responses.push({ crews: [item('crw_a', 'alpha')], count: 1 });
    store.start();
    await flush();
    const ws = connectLast(factory);
    api.responses.push({ crews: [item('crw_a', 'alpha', { live: 1 })], count: 1 });
    ws.receive({ type: 'crew.summary', crews: [{ crew_id: 'crw_a', project_id: 'alpha', mode: 'solo', live: 1, moments: 0, needs_you: 0 }] });
    ws.receive({ type: 'crew.summary', crews: [{ crew_id: 'crw_a', project_id: 'alpha', mode: 'solo', live: 1, moments: 0, needs_you: 0 }] });
    await timers.advance(9999);
    expect(api.calls).toBe(1);
    await timers.advance(1);
    expect(api.calls).toBe(2);
    api.responses.push({ crews: [item('crw_a', 'alpha', { live: 1 }), item('crw_new', 'new')], count: 2 });
    ws.receive({ type: 'crew.summary', crews: [{ crew_id: 'crw_new', project_id: 'new', mode: 'solo', live: 1, moments: 0, needs_you: 0 }] });
    await timers.advance(0);
    expect(api.calls).toBe(3);
    expect(store.getView().items.map((c) => c.crew.id)).toContain('crw_new');
  });

  it('keeps summary frames that arrive while a refetch is in flight', async () => {
    const { api, store, factory } = setup();
    api.responses.push({ crews: [item('crw_a', 'alpha')], count: 1 });
    store.start();
    await flush();
    const ws = connectLast(factory);
    api.hold = true;
    const refreshing = store.refresh();
    ws.receive({ type: 'crew.summary', crews: [{ crew_id: 'crw_a', project_id: 'alpha', mode: 'solo', live: 3, moments: 0, needs_you: 1 }] });
    api.release({ crews: [item('crw_a', 'alpha', { live: 0 })], count: 1 }); // stale counts from before the frame
    await refreshing;
    expect(store.getView().items[0]).toMatchObject({ live: 3, needs_you: 1 });
  });

  it('keeps the last list on a failed refresh and reports the error; errors with no data', async () => {
    const { api, store } = setup();
    api.responses.push({ crews: [item('crw_a', 'alpha')], count: 1 }, new CrewApiError('down', 503, 'http_503'));
    store.start();
    await flush();
    await store.refresh();
    expect(store.getView()).toMatchObject({ status: 'ready', error: { status: 503 } });
    expect(store.getView().items).toHaveLength(1);

    const second = setup();
    second.api.responses.push(new CrewApiError('Crew mode is not enabled', 404, 'not_found'));
    second.store.start();
    await flush();
    expect(second.store.getView()).toMatchObject({ status: 'error', items: [] });
  });

  it('polls slowly and stops cleanly', async () => {
    const { api, store, factory, timers } = setup();
    api.responses.push({ crews: [], count: 0 }, { crews: [], count: 0 });
    store.start();
    await flush();
    await timers.advance(60000);
    expect(api.calls).toBe(2);
    const ws = connectLast(factory);
    store.stop();
    expect(ws.json().at(-1)).toEqual({ type: 'unsubscribe', channel: 'crew', crew_id: '*' });
    await timers.advance(600000);
    expect(api.calls).toBe(2);
    expect(store.getView().status).toBe('stopped');
  });
});

describe('CrewListStore permanent failures', () => {
  it('stops polling after a 404 (crew mode off) until refresh()', async () => {
    const { api, store, timers } = setup();
    api.responses.push(new CrewApiError('Not Found', 404, 'http_404'));
    store.start();
    await flush();
    await timers.advance(600000);
    expect(api.calls).toBe(1);
    api.responses.push({ crews: [item('crw_a', 'alpha')], count: 1 });
    await store.refresh();
    expect(store.getView()).toMatchObject({ status: 'ready', error: null });
  });
});
