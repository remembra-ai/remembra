import { describe, expect, it } from 'vitest';
import { CrewSocket, crewSocketUrl, CLOSE_FORBIDDEN, CLOSE_UNAUTHORIZED } from '../socket';
import { ManualTimers, SocketFactory, connectLast } from './fakes';

function setup(creds: { jwt?: string | null; apiKey?: string | null } = { jwt: 'jwt-1' }) {
  const timers = new ManualTimers();
  const factory = new SocketFactory();
  const statuses: string[] = [];
  const socket = new CrewSocket({
    url: 'wss://api.example/ws',
    credentials: () => creds,
    createSocket: factory.create,
    timers,
    random: () => 1, // no jitter: delay = base
    initialBackoffMs: 1000,
    maxBackoffMs: 30000,
    silenceMs: 150000,
    authTimeoutMs: 15000,
  });
  socket.onStatus((s) => statuses.push(s));
  return { timers, factory, socket, statuses };
}

describe('crewSocketUrl', () => {
  it('derives ws(s)://…/ws from the API origin or the page', () => {
    expect(crewSocketUrl('https://api.remembra.dev')).toBe('wss://api.remembra.dev/ws');
    expect(crewSocketUrl('http://localhost:8787/')).toBe('ws://localhost:8787/ws');
    expect(crewSocketUrl('', { protocol: 'https:', host: 'app.remembra.dev' })).toBe('wss://app.remembra.dev/ws');
    expect(crewSocketUrl('', { protocol: 'http:', host: 'localhost:5173' })).toBe('ws://localhost:5173/ws');
  });
});

describe('CrewSocket', () => {
  it('connects lazily, authenticates with the first message, never in the URL', () => {
    const { factory, socket } = setup();
    expect(factory.sockets).toHaveLength(0);
    socket.subscribeCrew('crw_a', () => {}, () => 7);
    expect(factory.sockets).toHaveLength(1);
    expect(factory.last.url).toBe('wss://api.example/ws');
    expect(socket.status).toBe('connecting');
    factory.last.open();
    expect(factory.last.json()[0]).toEqual({ type: 'auth', token: 'jwt-1' });
    // nothing else is sent before the server confirms the auth
    expect(factory.last.json()).toHaveLength(1);
  });

  it('uses the API key when there is no dashboard login', () => {
    const { factory, socket } = setup({ jwt: null, apiKey: 'rem_key' });
    socket.subscribeSummary(() => {});
    factory.last.open();
    expect(factory.last.json()[0]).toEqual({ type: 'auth', api_key: 'rem_key' });
  });

  it('subscribes every crew and the summary once connected, with the current since_seq', () => {
    const { factory, socket, statuses } = setup();
    let since = 7;
    socket.subscribeCrew('crw_a', () => {}, () => since);
    socket.subscribeCrew('crw_b', () => {}, () => null);
    socket.subscribeSummary(() => {});
    since = 9;
    const ws = connectLast(factory);
    expect(socket.status).toBe('open');
    expect(statuses).toEqual(['connecting', 'open']);
    expect(ws.subscribes()).toEqual([
      { type: 'subscribe', channel: 'crew', crew_id: 'crw_a', topics: ['crew'], since_seq: 9 },
      { type: 'subscribe', channel: 'crew', crew_id: 'crw_b', topics: ['crew'] },
      { type: 'subscribe', channel: 'crew', crew_id: '*', topics: ['crew.summary'] },
    ]);
  });

  it('routes frames by crew id, summary frames to summary subscribers, errors to their crew', () => {
    const { factory, socket } = setup();
    const a: unknown[] = [];
    const b: unknown[] = [];
    const summaries: unknown[] = [];
    socket.subscribeCrew('crw_a', (f) => a.push(f), () => 0);
    socket.subscribeCrew('crw_b', (f) => b.push(f), () => 0);
    socket.subscribeSummary((c) => summaries.push(c));
    const ws = connectLast(factory);
    ws.receive({ type: 'crew.subscribed', crew_id: 'crw_a', since_seq: 0, replayed: 0 });
    ws.receive({ type: 'crew.event', crew_id: 'crw_a', data: { seq: 1 } });
    ws.receive({ type: 'presence', crew_id: 'crw_b', lanes: [] });
    ws.receive({ type: 'resync_required', crew_id: 'crw_b', reason: 'overflow', last_seq: 3 });
    ws.receive({ type: 'crew.subscribed', crew_id: '*', since_seq: 0, replayed: 0 });
    ws.receive({ type: 'crew.summary', crews: [{ crew_id: 'crw_a', live: 2 }] });
    ws.receive({ type: 'error', data: { channel: 'crew', crew_id: 'crw_b', code: 'not_found', message: 'crew not found' } });
    ws.receive({ type: 'crew.event', crew_id: 'crw_zzz', data: { seq: 1 } });
    ws.receive({ type: 'memory.created', data: {} });
    ws.receive('not json');
    expect(a.map((f) => (f as { type: string }).type)).toEqual(['crew.subscribed', 'crew.event']);
    expect(b.map((f) => (f as { type: string }).type)).toEqual(['presence', 'resync_required', 'error']);
    expect(summaries).toEqual([[{ crew_id: 'crw_a', live: 2 }]]);
  });

  it('answers the server ping with pong', () => {
    const { factory, socket } = setup();
    socket.subscribeCrew('crw_a', () => {}, () => 0);
    const ws = connectLast(factory);
    ws.receive('ping');
    expect(ws.sent.at(-1)).toBe('pong');
  });

  it('reconnects with exponential backoff and replays from the latest seq', async () => {
    const { factory, socket, timers } = setup();
    let since = 5;
    socket.subscribeCrew('crw_a', () => {}, () => since);
    connectLast(factory).serverClose(1006);
    expect(socket.status).toBe('reconnecting');
    expect(timers.delays()).toEqual([1000]);
    await timers.advance(1000);
    expect(factory.sockets).toHaveLength(2);
    factory.last.serverClose(1006); // never opened
    expect(timers.delays()).toEqual([2000]);
    await timers.advance(2000);
    factory.last.serverClose(1006);
    expect(timers.delays()).toEqual([4000]);
    await timers.advance(4000);
    since = 12;
    const ws = connectLast(factory);
    expect(ws.subscribes()).toEqual([{ type: 'subscribe', channel: 'crew', crew_id: 'crw_a', topics: ['crew'], since_seq: 12 }]);
    // a successful connection resets the backoff
    ws.serverClose(1011);
    expect(timers.delays()).toEqual([1000]);
  });

  it('caps the backoff and applies jitter', async () => {
    const timers = new ManualTimers();
    const factory = new SocketFactory();
    const socket = new CrewSocket({
      url: 'ws://x/ws',
      credentials: () => ({ jwt: 't' }),
      createSocket: factory.create,
      timers,
      random: () => 0, // lowest jitter: half the base delay
      initialBackoffMs: 1000,
      maxBackoffMs: 8000,
    });
    socket.subscribeCrew('crw_a', () => {}, () => 0);
    const delays: number[] = [];
    for (let i = 0; i < 6; i++) {
      factory.last.serverClose(1006);
      delays.push(timers.delays()[0]);
      await timers.advance(delays.at(-1)!);
    }
    expect(delays).toEqual([500, 1000, 2000, 4000, 4000, 4000]);
  });

  it('stops on 4001 until reconnect() is called', async () => {
    const { factory, socket, timers } = setup();
    socket.subscribeCrew('crw_a', () => {}, () => 0);
    factory.last.open();
    factory.last.serverClose(CLOSE_UNAUTHORIZED, 'Authentication required');
    expect(socket.status).toBe('unauthorized');
    expect(timers.count).toBe(0);
    await timers.advance(60000);
    expect(factory.sockets).toHaveLength(1);
    socket.subscribeCrew('crw_b', () => {}, () => 0); // a new subscriber does not hammer the server either
    expect(factory.sockets).toHaveLength(1);
    socket.reconnect();
    expect(factory.sockets).toHaveLength(2);
    const ws = connectLast(factory);
    expect(ws.subscribes().map((m) => m.crew_id)).toEqual(['crw_a', 'crw_b']);
  });

  it('retries slowly after 4003 (access revoked)', () => {
    const { factory, socket, timers } = setup();
    socket.subscribeCrew('crw_a', () => {}, () => 0);
    connectLast(factory).serverClose(CLOSE_FORBIDDEN, 'crew access revoked');
    expect(socket.status).toBe('forbidden');
    expect(timers.delays()).toEqual([30000]);
  });

  it('replaces a socket that went silent, and one that never confirmed the auth', async () => {
    const { factory, socket, timers } = setup();
    socket.subscribeCrew('crw_a', () => {}, () => 0);
    const ws = connectLast(factory);
    await timers.advance(149000);
    ws.receive('ping'); // any frame proves the connection is alive
    await timers.advance(149000);
    expect(factory.sockets).toHaveLength(1);
    await timers.advance(2000);
    expect(ws.closedWith?.code).toBe(1000);
    expect(socket.status).toBe('reconnecting');
    await timers.advance(1000);
    expect(factory.sockets).toHaveLength(2);
    factory.last.open(); // opened, auth sent, but no "connected" ever comes
    await timers.advance(15000);
    expect(factory.last.closedWith?.code).toBe(1000);
    await timers.advance(2000);
    expect(factory.sockets).toHaveLength(3);
  });

  it('unsubscribes on the server and closes the socket when the last subscriber leaves', () => {
    const { factory, socket, statuses } = setup();
    const offA = socket.subscribeCrew('crw_a', () => {}, () => 0);
    const offSummary = socket.subscribeSummary(() => {});
    const ws = connectLast(factory);
    offA();
    expect(ws.json().at(-1)).toEqual({ type: 'unsubscribe', channel: 'crew', crew_id: 'crw_a' });
    expect(ws.closedWith).toBeNull();
    offSummary();
    expect(ws.json().at(-1)).toEqual({ type: 'unsubscribe', channel: 'crew', crew_id: '*' });
    expect(ws.closedWith?.code).toBe(1000);
    expect(socket.status).toBe('idle');
    // our own close event does not trigger a reconnect
    ws.serverClose(1000);
    expect(statuses.at(-1)).toBe('idle');
  });

  it('resubscribe() re-sends one crew with its new cursor', () => {
    const { factory, socket } = setup();
    let since = 3;
    socket.subscribeCrew('crw_a', () => {}, () => since);
    const ws = connectLast(factory);
    since = 40;
    socket.resubscribe('crw_a');
    socket.resubscribe('crw_unknown');
    expect(ws.subscribes().map((m) => m.since_seq)).toEqual([3, 40]);
  });

  it('two local subscribers to one crew share one server subscription from the lowest cursor', () => {
    const { factory, socket } = setup();
    socket.subscribeCrew('crw_a', () => {}, () => 10);
    socket.subscribeCrew('crw_a', () => {}, () => 4);
    const ws = connectLast(factory);
    expect(ws.subscribes()).toEqual([{ type: 'subscribe', channel: 'crew', crew_id: 'crw_a', topics: ['crew'], since_seq: 4 }]);
  });

  it('stop() closes for good', async () => {
    const { factory, socket, timers } = setup();
    socket.subscribeCrew('crw_a', () => {}, () => 0);
    const ws = connectLast(factory);
    socket.stop();
    expect(ws.closedWith?.code).toBe(1000);
    expect(socket.status).toBe('closed');
    ws.serverClose(1006);
    await timers.advance(60000);
    expect(factory.sockets).toHaveLength(1);
  });
});
