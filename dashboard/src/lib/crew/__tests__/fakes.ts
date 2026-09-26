// Test doubles for the crew data layer: a manual clock and an in-memory
// WebSocket that records what the client sends and lets a test play the
// server's frames.

import type { SocketLike, Timers } from '../socket';

export class ManualTimers implements Timers {
  private time = 1_000_000;
  private seq = 0;
  private readonly pending = new Map<number, { at: number; fn: () => void }>();

  now = (): number => this.time;

  setTimeout = (fn: () => void, ms: number): unknown => {
    const id = ++this.seq;
    this.pending.set(id, { at: this.time + Math.max(0, ms), fn });
    return id;
  };

  clearTimeout = (handle: unknown): void => {
    this.pending.delete(handle as number);
  };

  /** Advance the clock, running due timers in order (timers they schedule run too if due). */
  async advance(ms: number): Promise<void> {
    const end = this.time + ms;
    for (;;) {
      let next: [number, { at: number; fn: () => void }] | null = null;
      for (const entry of this.pending) if (entry[1].at <= end && (!next || entry[1].at < next[1].at)) next = entry;
      if (!next) break;
      this.pending.delete(next[0]);
      this.time = next[1].at;
      next[1].fn();
      await flush();
    }
    this.time = end;
    await flush();
  }

  get count(): number {
    return this.pending.size;
  }

  /** Delays of the pending timers, relative to now. */
  delays(): number[] {
    return [...this.pending.values()].map((t) => t.at - this.time).sort((a, b) => a - b);
  }
}

/** Let pending promise callbacks run. */
export async function flush(times = 5): Promise<void> {
  for (let i = 0; i < times; i++) await Promise.resolve();
}

export class FakeSocket implements SocketLike {
  readyState = 0;
  readonly sent: string[] = [];
  closedWith: { code?: number; reason?: string } | null = null;
  onopen: SocketLike['onopen'] = null;
  onclose: SocketLike['onclose'] = null;
  onmessage: SocketLike['onmessage'] = null;
  onerror: SocketLike['onerror'] = null;

  readonly url: string;

  constructor(url: string) {
    this.url = url;
  }

  send(data: string): void {
    if (this.readyState !== 1) throw new Error('send on a socket that is not open');
    this.sent.push(data);
  }

  close(code?: number, reason?: string): void {
    this.closedWith = { code, reason };
    this.readyState = 3;
  }

  // -- server side --------------------------------------------------------------------------------

  open(): void {
    this.readyState = 1;
    this.onopen?.(new Event('open'));
  }

  /** Server frame (objects are JSON-encoded; strings such as "ping" are sent as is). */
  receive(frame: unknown): void {
    this.onmessage?.({ data: typeof frame === 'string' ? frame : JSON.stringify(frame) } as MessageEvent);
  }

  serverClose(code: number, reason = ''): void {
    this.readyState = 3;
    this.onclose?.({ code, reason } as CloseEvent);
  }

  /** JSON messages the client sent (text frames such as "pong" are skipped). */
  json(): Record<string, unknown>[] {
    return this.sent.filter((s) => s.startsWith('{')).map((s) => JSON.parse(s));
  }

  subscribes(): Record<string, unknown>[] {
    return this.json().filter((m) => m.type === 'subscribe');
  }
}

export class SocketFactory {
  readonly sockets: FakeSocket[] = [];
  create = (url: string): FakeSocket => {
    const socket = new FakeSocket(url);
    this.sockets.push(socket);
    return socket;
  };
  get last(): FakeSocket {
    const s = this.sockets.at(-1);
    if (!s) throw new Error('no socket created');
    return s;
  }
}

/** Open the latest socket and complete the auth handshake. */
export function connectLast(factory: SocketFactory): FakeSocket {
  const socket = factory.last;
  socket.open();
  socket.receive({ type: 'connected', data: { message: 'ok' } });
  return socket;
}
