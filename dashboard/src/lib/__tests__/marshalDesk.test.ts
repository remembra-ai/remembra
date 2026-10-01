// The Marshal desk client against the contract (desk-contract.json v1): the
// stream parser byte for byte, askMarshal and the board/settings routes over
// a mocked fetch that replays the contract's example streams and error
// bodies, and the pure state machine the desk renders from.

import { readFileSync } from 'node:fs';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { api } from '../api';
import {
  CLOSED_KEY,
  DESK_COPY,
  DeskError,
  FALLBACK_TEXT,
  HEIGHT_KEY,
  SERVER_COPY,
  askMarshal,
  askRefusal,
  askRequest,
  blocksAsking,
  boardNotice,
  clampHeight,
  countChars,
  createSseParser,
  dailyLimitText,
  defaultHeight,
  deskClosedThisSession,
  deskErrorState,
  deskReducer,
  deskToggle,
  footerText,
  formatUsd,
  getBoard,
  getDeskSettings,
  heightBounds,
  initialDeskState,
  inlineParts,
  isAbortError,
  isSubmitKey,
  loadDeskHeight,
  loadDeskSettings,
  marshalPaletteItem,
  newConversationId,
  nextHeight,
  noticeFor,
  pendingEntry,
  rememberDeskClosed,
  runAsk,
  saveDeskHeight,
  setDeskSettings,
  slipAsk,
  toDeskEvent,
  trapFocus,
  trimHistory,
  type AnswerEvent,
  type DeskAction,
  type DeskBoard,
  type DeskEntry,
  type DeskState,
  type DeskStreamEvent,
  type UsageEvent,
} from '../marshalDesk';

interface ContractEvent {
  event: string;
  data: Record<string, unknown>;
}

const CONTRACT = JSON.parse(readFileSync(new URL('./fixtures/marshal_desk_contract.json', import.meta.url), 'utf8')) as {
  errors: Record<string, { status: number; message?: string; messages?: Record<string, string> }>;
  error_body_exceptions: { '401': unknown; '422': unknown; '429_slowapi': unknown };
  board: DeskBoard;
  dashboard_copy: Record<string, string>;
  sse_error_messages: Record<string, string>;
  stream_a: { request: Record<string, unknown>; raw: string; events: ContractEvent[]; footer_rendered: string };
  stream_b: { request: Record<string, unknown>; raw: string; events: ContractEvent[] };
  error_tail: { events: ContractEvent[] };
};

const A = CONTRACT.stream_a;
const B = CONTRACT.stream_b;
const enc = new TextEncoder();

/** What the parser hands on, as [event, parsed data]. */
function collect(feed: (parser: ReturnType<typeof createSseParser>) => void): ContractEvent[] {
  const out: ContractEvent[] = [];
  const parser = createSseParser((event, data) => out.push({ event, data: JSON.parse(data) }));
  feed(parser);
  return out;
}

describe('createSseParser', () => {
  it('reads example A and example B exactly as the contract lists their events', () => {
    for (const example of [A, B]) {
      expect(collect((p) => (p.push(enc.encode(example.raw)), p.end()))).toEqual(example.events);
    }
  });

  it('gives the same events however the bytes are split (every byte boundary, a · and … split included)', () => {
    for (const example of [A, B]) {
      const bytes = enc.encode(example.raw);
      for (let cut = 0; cut <= bytes.length; cut += 1) {
        const got = collect((p) => {
          p.push(bytes.slice(0, cut));
          p.push(bytes.slice(cut));
          p.end();
        });
        expect(got, `cut at byte ${cut}`).toEqual(example.events);
      }
      expect(collect((p) => (bytes.forEach((b) => p.push(Uint8Array.of(b))), p.end()))).toEqual(example.events);
    }
  });

  it('accepts CRLF and CR line ends, a CRLF split across chunks, and keepalive comments between events', () => {
    const crlf = A.raw.replace(/\n/g, '\r\n');
    const cr = A.raw.replace(/\n/g, '\r');
    expect(collect((p) => (p.push(crlf), p.end()))).toEqual(A.events);
    expect(collect((p) => (p.push(cr), p.end()))).toEqual(A.events);
    const bytes = enc.encode(crlf);
    for (let cut = 0; cut <= bytes.length; cut += 97) {
      expect(collect((p) => (p.push(bytes.slice(0, cut)), p.push(bytes.slice(cut)), p.end()))).toEqual(A.events);
    }
    const withKeepalives = `: keepalive\n\n${A.raw.replace(/\n\nevent: /g, '\n\n: keepalive\n\nevent: ')}: keepalive\n\n`;
    expect(collect((p) => (p.push(withKeepalives), p.end()))).toEqual(A.events);
  });

  it('joins several data lines with a newline, ignores id/retry and hands on unknown events by name', () => {
    const seen: [string, string][] = [];
    const p = createSseParser((event, data) => seen.push([event, data]));
    p.push('﻿event: proposal\ndata: {"a":\ndata: 1}\nid: 7\nretry: 10\n\ndata: x\n\nevent: read\n\n');
    p.push('event: done\ndata: {"ok":true}');
    p.end(); // no closing blank line: dropped, as the stream rules say
    expect(seen).toEqual([
      ['proposal', '{"a":\n1}'],
      ['message', 'x'],
    ]);
    expect(toDeskEvent('proposal', '{"a":1}')).toBeNull(); // not a desk event: ignored
  });

  it('refuses a known event whose data is not the contract shape', () => {
    expect(() => toDeskEvent('read', '{not json')).toThrow(DeskError);
    expect(() => toDeskEvent('answer', JSON.stringify({ text: 'x' }))).toThrow(DeskError);
    expect(() =>
      toDeskEvent('answer', JSON.stringify({ ...A.events[2].data, commands: [{ text: 'rm -rf ~', kind: 'shell', prompt: '#' }] })),
    ).toThrow(DeskError);
    expect(() => toDeskEvent('usage', JSON.stringify({ ...A.events[3].data, billed_to_credits: true }))).toThrow(DeskError);
    expect(toDeskEvent('done', '{"ok":false}')).toEqual({ type: 'done', data: { ok: false } });
  });
});

// ---------------------------------------------------------------------------
// The client over a mocked fetch
// ---------------------------------------------------------------------------

interface Call {
  url: string;
  method: string;
  headers: Record<string, string>;
  body: string | null;
}

const store = new Map<string, string>();
let calls: Call[] = [];

function headersOf(init: RequestInit): Record<string, string> {
  const out: Record<string, string> = {};
  new Headers(init.headers).forEach((value, key) => (out[key] = value));
  return out;
}

/** A text/event-stream body that sends `chunks` one read at a time and errors when `signal` aborts. */
function streamBody(chunks: Uint8Array[], signal?: AbortSignal | null, hold = false): ReadableStream<Uint8Array> {
  let i = 0;
  return new ReadableStream<Uint8Array>({
    start(controller) {
      signal?.addEventListener('abort', () => controller.error(new DOMException('The operation was aborted.', 'AbortError')));
    },
    async pull(controller) {
      if (i < chunks.length) {
        controller.enqueue(chunks[i]);
        i += 1;
        return;
      }
      if (hold) return new Promise<void>(() => {}); // the server went quiet
      controller.close();
    },
  });
}

function mockFetch(respond: (call: Call, init: RequestInit) => Response | Promise<Response>) {
  vi.stubGlobal('fetch', async (url: string, init: RequestInit = {}) => {
    const call: Call = { url, method: init.method ?? 'GET', headers: headersOf(init), body: typeof init.body === 'string' ? init.body : null };
    calls.push(call);
    return respond(call, init);
  });
}

const json = (status: number, body: unknown) =>
  new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } });

const sse = (chunks: Uint8Array[], init: RequestInit, hold = false) =>
  new Response(streamBody(chunks, init.signal, hold), { status: 200, headers: { 'content-type': 'text/event-stream; charset=utf-8' } });

/** The raw stream cut into uneven pieces (a multi-byte character split on the way). */
function pieces(raw: string, size = 37): Uint8Array[] {
  const bytes = enc.encode(raw);
  const out: Uint8Array[] = [];
  for (let at = 0; at < bytes.length; at += size) out.push(bytes.slice(at, at + size));
  return out;
}

async function replay(raw: string, request: Record<string, unknown>) {
  mockFetch((_call, init) => sse(pieces(raw), init));
  const events: DeskStreamEvent[] = [];
  await askMarshal(request as never, { onEvent: (e) => events.push(e) });
  return { events };
}

describe('askMarshal and the desk routes', () => {
  beforeEach(() => {
    store.clear();
    calls = [];
    vi.stubGlobal('localStorage', {
      getItem: (k: string) => store.get(k) ?? null,
      setItem: (k: string, v: string) => void store.set(k, v),
      removeItem: (k: string) => void store.delete(k),
    });
    api.clearAll();
    api.setJwtToken('desk-session');
    api.setApiKey('rem_not_for_marshal');
  });

  afterEach(() => {
    api.clearAll();
    vi.unstubAllGlobals();
  });

  it('replays example A: one POST with the JWT (never the API key), the events in order', async () => {
    const { events } = await replay(A.raw, A.request);
    expect(events.map((e) => ({ event: e.type, data: e.data }))).toEqual(A.events);
    expect(calls).toHaveLength(1);
    const [call] = calls;
    expect(call.url).toBe('/api/v1/marshal/ask');
    expect(call.method).toBe('POST');
    expect(call.headers.authorization).toBe('Bearer desk-session');
    expect(call.headers.accept).toBe('text/event-stream');
    expect(call.headers['content-type']).toBe('application/json');
    expect(call.headers['x-api-key']).toBeUndefined();
    expect(JSON.parse(call.body ?? '')).toEqual(A.request);
  });

  it('replays example B, the validation fallback', async () => {
    const { events } = await replay(B.raw, B.request);
    expect(events.map((e) => ({ event: e.type, data: e.data }))).toEqual(B.events);
    const answer = events.find((e) => e.type === 'answer')?.data as AnswerEvent;
    expect(answer.fallback).toBe(true);
    expect(answer.text).toBe(FALLBACK_TEXT);
  });

  it('replays the error ending: error, usage, done {ok:false}', async () => {
    const raw = [A.events[0], ...CONTRACT.error_tail.events].map((e) => `event: ${e.event}\ndata: ${JSON.stringify(e.data)}\n\n`).join('');
    const { events } = await replay(raw, A.request);
    expect(events.map((e) => e.type)).toEqual(['read', 'error', 'usage', 'done']);
    expect(events[1].data).toEqual({ error: 'model_unavailable', message: CONTRACT.sse_error_messages.model_unavailable, retryable: true });
    expect(events[3].data).toEqual({ ok: false });
  });

  it('stops reading at done: bytes after it are never handed on', async () => {
    const raw = `${A.raw}event: read\ndata: ${JSON.stringify(A.events[0].data)}\n\n`;
    const { events } = await replay(raw, A.request);
    expect(events.map((e) => e.type)).toEqual(['read', 'read', 'answer', 'usage', 'done']);
  });

  it('a stream that ends before done is a cut connection (no reconnect)', async () => {
    const cut = A.raw.slice(0, A.raw.indexOf('event: answer'));
    mockFetch((_c, init) => sse(pieces(cut), init));
    const events: DeskStreamEvent[] = [];
    const err = await askMarshal(A.request as never, { onEvent: (e) => events.push(e) }).catch((e: unknown) => e);
    expect(err).toBeInstanceOf(DeskError);
    expect((err as DeskError).code).toBe('stream_cut');
    expect(deskErrorState(err)).toBe('unreachable');
    expect(events.map((e) => e.type)).toEqual(['read', 'read']);
    expect(calls).toHaveLength(1);
  });

  it('abort mid-stream rejects with the AbortError and hands nothing more on', async () => {
    const ctrl = new AbortController();
    // The first read, then the rest of example A a chunk at a time: the abort lands after the first event.
    mockFetch((_c, init) => sse(pieces(A.raw, 64), init, true));
    const events: DeskStreamEvent[] = [];
    const running = askMarshal(
      A.request as never,
      {
        onEvent: (e) => {
          events.push(e);
          ctrl.abort();
        },
      },
      ctrl.signal,
    );
    const err = await running.catch((e: unknown) => e);
    expect(isAbortError(err)).toBe(true);
    expect(events.map((e) => e.type)).toEqual(['read']);
  });

  it('a malformed event fails the ask', async () => {
    mockFetch((_c, init) => sse([enc.encode('event: read\ndata: {"id":"r1"}\n\n')], init));
    const err = await askMarshal(A.request as never, { onEvent: () => {} }).catch((e: unknown) => e);
    expect((err as DeskError).code).toBe('bad_response');
  });

  it('a 200 that is not an event stream is refused', async () => {
    mockFetch(() => json(200, { ok: true }));
    const err = await askMarshal(A.request as never, { onEvent: () => {} }).catch((e: unknown) => e);
    expect((err as DeskError).code).toBe('bad_response');
    expect(deskErrorState(err)).toBe('unreachable');
  });

  const refusal = (code: string, extra: Record<string, unknown> = {}) => {
    const spec = CONTRACT.errors[code];
    const message = spec.message ?? spec.messages?.no_key ?? '';
    return { status: spec.status, body: { detail: { error: code, message, ...extra } }, message };
  };

  it.each([
    ['delegated_principal_refused', 'login_required'],
    ['marshal_unavailable', 'unavailable'],
    ['marshal_login_required', 'login_required'],
    ['marshal_opted_out', 'opted_out'],
    ['marshal_offline', 'offline'],
    ['marshal_daily_limit', 'limited_day'],
  ] as const)('maps %s before the stream to the %s notice, with the server sentence', async (code, state) => {
    const { status, body, message } = refusal(code, code === 'marshal_offline' ? { reason: 'no_key' } : {});
    mockFetch(() => json(status, body));
    const err = await askMarshal(A.request as never, { onEvent: () => {} }).catch((e: unknown) => e);
    expect(err).toBeInstanceOf(DeskError);
    expect((err as DeskError).status).toBe(status);
    expect((err as DeskError).code).toBe(code);
    expect(deskErrorState(err)).toBe(state);
    expect(noticeFor(err)).toEqual({ state, message });
  });

  it('keeps the offline reason and the monthly sentence the server sends', async () => {
    const monthly = CONTRACT.errors.marshal_offline.messages?.monthly_budget ?? '';
    mockFetch(() => json(503, { detail: { error: 'marshal_offline', message: monthly, reason: 'monthly_budget', resets_at: '2026-11-01T00:00:00Z' } }));
    const err = (await askMarshal(A.request as never, { onEvent: () => {} }).catch((e: unknown) => e)) as DeskError;
    expect(err.data?.reason).toBe('monthly_budget');
    expect(noticeFor(err)).toEqual({ state: 'offline', message: monthly });
  });

  it('a body without its message falls back to the no_key sentence', async () => {
    mockFetch(() => json(503, { detail: { error: 'marshal_offline' } }));
    const err = await askMarshal(A.request as never, { onEvent: () => {} }).catch((e: unknown) => e);
    expect(noticeFor(err)).toEqual({ state: 'offline', message: CONTRACT.errors.marshal_offline.messages?.no_key });
  });

  it('slowapi 429 (no detail) is the per-minute limit; 401 is an expired session; 422 is invalid', async () => {
    mockFetch(() => json(429, CONTRACT.error_body_exceptions['429_slowapi']));
    let err = await askMarshal(A.request as never, { onEvent: () => {} }).catch((e: unknown) => e);
    expect(deskErrorState(err)).toBe('limited_minute');
    expect(noticeFor(err).message).toBe(DESK_COPY.limitedMinute);

    mockFetch(() => json(401, CONTRACT.error_body_exceptions['401']));
    err = await askMarshal(A.request as never, { onEvent: () => {} }).catch((e: unknown) => e);
    expect(deskErrorState(err)).toBe('expired');
    expect(noticeFor(err).message).toBe(DESK_COPY.expired);

    mockFetch(() => json(422, CONTRACT.error_body_exceptions['422']));
    err = await askMarshal(A.request as never, { onEvent: () => {} }).catch((e: unknown) => e);
    expect(deskErrorState(err)).toBe('invalid');
    expect(noticeFor(err).message).toBe(SERVER_COPY.internal);
  });

  it('a network failure is unreachable; a 502 from a proxy is too, never "offline"', async () => {
    vi.stubGlobal('fetch', async () => {
      throw new TypeError('Failed to fetch');
    });
    let err = await askMarshal(A.request as never, { onEvent: () => {} }).catch((e: unknown) => e);
    expect((err as DeskError).status).toBe(0);
    expect(noticeFor(err)).toEqual({ state: 'unreachable', message: DESK_COPY.unreachable });
    mockFetch(() => new Response('<html>bad gateway</html>', { status: 502, headers: { 'content-type': 'text/html' } }));
    err = await askMarshal(A.request as never, { onEvent: () => {} }).catch((e: unknown) => e);
    expect(noticeFor(err)).toEqual({ state: 'unreachable', message: DESK_COPY.unreachable });
  });

  it('without a dashboard session nothing is sent (an API key never reaches the desk)', async () => {
    api.clearJwtToken();
    mockFetch(() => json(200, {}));
    const err = await askMarshal(A.request as never, { onEvent: () => {} }).catch((e: unknown) => e);
    expect(deskErrorState(err)).toBe('expired');
    expect(await getBoard().catch((e: unknown) => deskErrorState(e))).toBe('expired');
    expect(calls).toEqual([]);
  });

  it('GET board and settings, PUT settings: JWT only, the contract bodies', async () => {
    mockFetch((call) => {
      if (call.url.endsWith('/marshal/board')) return json(200, CONTRACT.board);
      if (call.method === 'PUT') return json(200, JSON.parse(call.body ?? '{}'));
      return json(200, { desk: true });
    });
    expect(await getBoard()).toEqual(CONTRACT.board);
    expect(await getDeskSettings()).toEqual({ desk: true });
    expect(await setDeskSettings(false)).toEqual({ desk: false });
    expect(calls.map((c) => `${c.method} ${c.url}`)).toEqual([
      'GET /api/v1/marshal/board',
      'GET /api/v1/marshal/settings',
      'PUT /api/v1/marshal/settings',
    ]);
    expect(calls[2].body).toBe('{"desk":false}');
    for (const call of calls) {
      expect(call.headers.authorization).toBe('Bearer desk-session');
      expect(call.headers['x-api-key']).toBeUndefined();
    }
  });

  it('settings 404 means the desk is not there; a board of the wrong shape is refused', async () => {
    mockFetch(() => json(404, { detail: { error: 'marshal_unavailable', message: CONTRACT.errors.marshal_unavailable.message } }));
    expect(await getDeskSettings().catch((e: unknown) => deskErrorState(e))).toBe('unavailable');
    mockFetch(() => json(200, { status_line: 'x' }));
    expect(await getBoard().catch((e: unknown) => (e as DeskError).code)).toBe('bad_response');
  });
});

// ---------------------------------------------------------------------------
// The runners the provider uses: one ask to its end, the settings read
// ---------------------------------------------------------------------------

describe('runAsk', () => {
  beforeEach(() => {
    store.clear();
    calls = [];
    vi.stubGlobal('localStorage', {
      getItem: (k: string) => store.get(k) ?? null,
      setItem: (k: string, v: string) => void store.set(k, v),
      removeItem: (k: string) => void store.delete(k),
    });
    api.clearAll();
    api.setJwtToken('desk-session');
  });

  afterEach(() => {
    vi.useRealTimers();
    api.clearAll();
    vi.unstubAllGlobals();
  });

  /** Drive the real reducer with whatever the run dispatches. */
  function desk() {
    let state = deskReducer(initialDeskState(), { type: 'open', question: 'why is codex waiting', agentId: 'codex', ask: true, source: 'why_slip' });
    const seen: DeskAction[] = [];
    const dispatch = (action: DeskAction) => {
      seen.push(action);
      state = deskReducer(state, action);
    };
    const entry = pendingEntry(state) as DeskEntry;
    return { dispatch, seen, entry, get state() { return state; }, request: askRequest(state, entry, '5b1f2c7e-9d0a-4c1e-8f7b-2a3d4e5f6a7b') };
  }

  it('runs example A to its end: the contract request, every event into the desk, the ask over', async () => {
    mockFetch((_c, init) => sse(pieces(A.raw), init));
    const d = desk();
    expect(d.request).toEqual(A.request);
    await runAsk({ request: d.request, id: d.entry.id, dispatch: d.dispatch }).finished;
    expect(d.seen.map((a) => a.type)).toEqual(['read', 'read', 'answer', 'usage', 'done']);
    expect(d.state.asking).toBe(false);
    expect(d.state.entries[0].answer?.text).toBe(A.events[2].data.text);
    expect(calls).toHaveLength(1);
  });

  it('a refusal before the stream marks the question and sets the notice', async () => {
    mockFetch(() => json(503, { detail: { error: 'marshal_offline', message: SERVER_COPY.offlineToday, reason: 'breaker_open', retry_after_seconds: 30 } }));
    const d = desk();
    await runAsk({ request: d.request, id: d.entry.id, dispatch: d.dispatch }).finished;
    expect(d.state.notice).toEqual({ state: 'offline', message: SERVER_COPY.offlineToday });
    expect(d.state.entries[0]).toMatchObject({ done: true, error: { message: SERVER_COPY.offlineToday, retryable: false } });
    expect(d.state.asking).toBe(false);
  });

  it('stop() (×) aborts the stream and marks the question stopped; nothing after it is shown', async () => {
    const first = A.raw.slice(0, A.raw.indexOf('event: read', 10));
    mockFetch((_c, init) => sse([enc.encode(first)], init, true));
    const d = desk();
    const run = runAsk({ request: d.request, id: d.entry.id, dispatch: d.dispatch });
    await vi.waitFor(() => expect(d.seen.map((a) => a.type)).toEqual(['read']));
    run.stop();
    await run.finished;
    expect(d.seen.map((a) => a.type)).toEqual(['read', 'aborted']);
    expect(d.state.entries[0]).toMatchObject({ done: true, stopped: true });
    expect(d.state.asking).toBe(false);
    run.stop(); // twice is once
    expect(d.seen).toHaveLength(2);
  });

  it('stop(true) (unmount, a new conversation) dispatches nothing at all', async () => {
    mockFetch((_c, init) => sse([], init, true));
    const d = desk();
    const run = runAsk({ request: d.request, id: d.entry.id, dispatch: d.dispatch });
    run.stop(true);
    await run.finished;
    expect(d.seen).toEqual([]);
  });

  it('a minute without an event is a lost ask: aborted, shown as unreachable; keepalives alone never hold it open', async () => {
    vi.useFakeTimers();
    let push: ((bytes: Uint8Array) => void) | null = null;
    mockFetch((_c, init) => {
      const body = new ReadableStream<Uint8Array>({
        start(controller) {
          push = (bytes) => controller.enqueue(bytes);
          init.signal?.addEventListener('abort', () => controller.error(new DOMException('aborted', 'AbortError')));
        },
      });
      return new Response(body, { status: 200, headers: { 'content-type': 'text/event-stream' } });
    });
    const send = (text: string) => (push as unknown as (bytes: Uint8Array) => void)(enc.encode(text));
    const d = desk();
    const run = runAsk({ request: d.request, id: d.entry.id, dispatch: d.dispatch });
    // A read at 50 s starts the minute again.
    await vi.advanceTimersByTimeAsync(50_000);
    send(`event: read\ndata: ${JSON.stringify(A.events[0].data)}\n\n`);
    await vi.advanceTimersByTimeAsync(0);
    expect(d.seen.map((a) => a.type)).toEqual(['read']);
    // Then only keepalives, every 10 s, as a server that stopped working would send.
    for (let i = 0; i < 5; i += 1) {
      await vi.advanceTimersByTimeAsync(10_000);
      send(': keepalive\n\n');
    }
    await vi.advanceTimersByTimeAsync(9_000); // 59 s after the read: still waiting
    expect(d.seen.map((a) => a.type)).toEqual(['read']);
    await vi.advanceTimersByTimeAsync(1_001);
    await run.finished;
    expect(d.seen.slice(1)).toEqual([{ type: 'failed', id: d.entry.id, notice: { state: 'unreachable', message: DESK_COPY.unreachable } }]);
    expect(d.state.entries[0].reads).toHaveLength(1);
    expect(d.state.entries[0].error).toEqual({ message: DESK_COPY.unreachable, retryable: true });
    expect(d.state.asking).toBe(false);
  });

  it('a connection that never answers at all is lost after a minute too', async () => {
    vi.useFakeTimers();
    mockFetch((_c, init) => sse([], init, true));
    const d = desk();
    const run = runAsk({ request: d.request, id: d.entry.id, dispatch: d.dispatch });
    await vi.advanceTimersByTimeAsync(59_999);
    expect(d.seen).toEqual([]);
    await vi.advanceTimersByTimeAsync(1);
    await run.finished;
    expect(d.seen.map((a) => a.type)).toEqual(['failed']);
  });

  it('a stream cut before done is shown as unreachable, never retried', async () => {
    mockFetch((_c, init) => sse(pieces(A.raw.slice(0, A.raw.indexOf('event: answer'))), init));
    const d = desk();
    await runAsk({ request: d.request, id: d.entry.id, dispatch: d.dispatch }).finished;
    expect(d.seen.map((a) => a.type)).toEqual(['read', 'read', 'failed']);
    expect(d.state.entries[0].reads).toHaveLength(2);
    expect(d.state.entries[0].error?.message).toBe(DESK_COPY.unreachable);
    expect(calls).toHaveLength(1);
  });
});

describe('loadDeskSettings', () => {
  beforeEach(() => {
    store.clear();
    calls = [];
    vi.stubGlobal('localStorage', {
      getItem: (k: string) => store.get(k) ?? null,
      setItem: (k: string, v: string) => void store.set(k, v),
      removeItem: (k: string) => void store.delete(k),
    });
    api.clearAll();
    api.setJwtToken('desk-session');
    vi.useFakeTimers();
  });

  afterEach(() => {
    vi.useRealTimers();
    api.clearAll();
    vi.unstubAllGlobals();
  });

  it('200 is the answer, opted out included', async () => {
    mockFetch(() => json(200, { desk: false }));
    const results: unknown[] = [];
    loadDeskSettings((r) => results.push(r));
    await vi.advanceTimersByTimeAsync(0);
    expect(results).toEqual([{ status: 'ready', desk: false }]);
  });

  it('404 (not on the allow-list, or turned off) and 403 are final: no retry', async () => {
    for (const [status, code] of [
      [404, 'marshal_unavailable'],
      [403, 'marshal_login_required'],
    ] as const) {
      calls = [];
      mockFetch(() => json(status, { detail: { error: code, message: 'x' } }));
      const results: unknown[] = [];
      loadDeskSettings((r) => results.push(r));
      await vi.advanceTimersByTimeAsync(120_000);
      expect(results).toEqual([{ status: 'off' }]);
      expect(calls).toHaveLength(1);
    }
  });

  it('an unreachable server is asked again twice, 30 s apart, then the desk stays off', async () => {
    let answers = 0;
    vi.stubGlobal('fetch', async () => {
      answers += 1;
      throw new TypeError('Failed to fetch');
    });
    const results: unknown[] = [];
    loadDeskSettings((r) => results.push(r));
    await vi.advanceTimersByTimeAsync(29_000);
    expect(answers).toBe(1);
    await vi.advanceTimersByTimeAsync(1_000);
    expect(answers).toBe(2);
    await vi.advanceTimersByTimeAsync(30_000);
    expect(answers).toBe(3);
    expect(results).toEqual([{ status: 'off' }]);
  });

  it('a retry that reaches the server gives its answer; cancel stops everything', async () => {
    let answers = 0;
    vi.stubGlobal('fetch', async () => {
      answers += 1;
      if (answers === 1) throw new TypeError('Failed to fetch');
      return json(200, { desk: true });
    });
    const results: unknown[] = [];
    loadDeskSettings((r) => results.push(r));
    await vi.advanceTimersByTimeAsync(30_000);
    expect(results).toEqual([{ status: 'ready', desk: true }]);

    answers = 0;
    vi.stubGlobal('fetch', async () => {
      answers += 1;
      throw new TypeError('Failed to fetch');
    });
    const later: unknown[] = [];
    const cancel = loadDeskSettings((r) => later.push(r));
    await vi.advanceTimersByTimeAsync(0);
    cancel();
    await vi.advanceTimersByTimeAsync(120_000);
    expect(answers).toBe(1);
    expect(later).toEqual([]);
  });
});

// ---------------------------------------------------------------------------
// Pure helpers
// ---------------------------------------------------------------------------

describe('text helpers', () => {
  it('counts code points as the server does (an emoji is one)', () => {
    expect(countChars('why')).toBe(3);
    expect(countChars('\u{1F600}')).toBe(1);
    expect('\u{1F600}'.length).toBe(2);
  });

  it('formats cost: <$0.001 below a tenth of a cent, else three places', () => {
    expect(formatUsd(0.000682)).toBe('<$0.001');
    expect(formatUsd(0.0021)).toBe('$0.002');
    expect(formatUsd(0.02)).toBe('$0.020');
    expect(formatUsd(0)).toBe('$0.000');
  });

  it("renders the footer as the contract's example", () => {
    expect(footerText(A.events[3].data as unknown as UsageEvent)).toBe(A.footer_rendered);
    expect(footerText({ ...(A.events[3].data as unknown as UsageEvent), reads: 1 })).toBe(
      'gpt-4o-mini · 1 read · <$0.001 · not billed to your credits',
    );
  });

  it('inlineParts: code, bold and Remembra links only; markup, javascript: and foreign links stay text', () => {
    expect(inlineParts('Run `/hooks` in **Codex**, see https://docs.remembra.dev/guides/relay/#codex-trust.')).toEqual([
      { kind: 'text', text: 'Run ' },
      { kind: 'code', text: '/hooks' },
      { kind: 'text', text: ' in ' },
      { kind: 'bold', text: 'Codex' },
      { kind: 'text', text: ', see ' },
      { kind: 'link', text: 'https://docs.remembra.dev/guides/relay/#codex-trust', href: 'https://docs.remembra.dev/guides/relay/#codex-trust' },
      { kind: 'text', text: '.' },
    ]);
    expect(inlineParts('[plans](https://remembra.dev/pricing)')).toEqual([{ kind: 'link', text: 'plans', href: 'https://remembra.dev/pricing' }]);
    for (const hostile of [
      '<script>alert(1)</script>',
      '[click](javascript:alert(1))',
      '[docs](https://evil.example/x)',
      'see https://remembra.dev.evil.example/x',
      '[x](http://remembra.dev/)',
      '[x](https://user:pw@remembra.dev/)',
      '<a href="https://evil.example">x</a>',
    ]) {
      const parts = inlineParts(hostile);
      expect(parts.every((p) => p.kind === 'text'), hostile).toBe(true);
      expect(parts.map((p) => p.text).join('')).toBe(hostile);
    }
    expect(inlineParts('look ![chart](https://evil.example/c.png?k=1)')).toEqual([{ kind: 'text', text: 'look [image removed: evil.example]' }]);
    expect(inlineParts('<img src="https://evil.example/p.gif">')).toEqual([{ kind: 'text', text: '[image removed: evil.example]' }]);
  });

  it('the palette ? mode asks a question, opens on a bare ?, and is off when the desk is', () => {
    const on = { available: true, optedOut: false };
    expect(marshalPaletteItem('?why is codex waiting', on)).toEqual({ label: 'Ask Marshal: "why is codex waiting"', question: 'why is codex waiting', ask: true });
    expect(marshalPaletteItem('  ?  ', on)).toEqual({ label: 'Ask Marshal', question: '', ask: false });
    expect(marshalPaletteItem('why', on)).toBeNull();
    expect(marshalPaletteItem('?why', { available: false, optedOut: false })).toBeNull();
    expect(marshalPaletteItem('?why', { available: true, optedOut: true })).toBeNull();
    expect(countChars(marshalPaletteItem(`?${'\u{1F600}'.repeat(1200)}`, on)?.question ?? '')).toBe(1000);
  });

  it('Enter asks, and only when no input method is composing', () => {
    expect(isSubmitKey({ key: 'Enter' })).toBe(true);
    expect(isSubmitKey({ key: 'Enter', isComposing: true })).toBe(false);
    expect(isSubmitKey({ key: 'Enter', keyCode: 229 })).toBe(false);
    expect(isSubmitKey({ key: ' ' })).toBe(false);
  });

  it('the slip asks about its own agent, prefilled and not sent', () => {
    expect(slipAsk('codex')).toEqual({ question: 'why is codex waiting', agentId: 'codex', ask: false, source: 'why_slip' });
    expect(slipAsk('Claude')).toEqual({ question: 'why is claude-code waiting', agentId: 'claude-code', ask: false, source: 'why_slip' });
  });

  it('a conversation id passes the server pattern', () => {
    expect(newConversationId()).toMatch(/^[A-Za-z0-9_-]{8,64}$/);
    expect(newConversationId()).not.toBe(newConversationId());
  });
});

describe('resize, focus and storage', () => {
  it('nextHeight moves 24px, clamps to [160, 70vh], and Home/End go to the ends', () => {
    const b = heightBounds(1000);
    expect(b).toEqual({ min: 160, max: 700 });
    expect(nextHeight('ArrowUp', 440, b)).toBe(464);
    expect(nextHeight('ArrowDown', 440, b)).toBe(416);
    expect(nextHeight('ArrowUp', 690, b)).toBe(700);
    expect(nextHeight('ArrowDown', 170, b)).toBe(160);
    expect(nextHeight('Home', 440, b)).toBe(160);
    expect(nextHeight('End', 440, b)).toBe(700);
    expect(nextHeight('Enter', 440, b)).toBeNull();
    expect(defaultHeight(1000)).toBe(440);
    expect(defaultHeight(300)).toBe(160);
    expect(clampHeight(5000, b)).toBe(700);
  });

  it('trapFocus wraps both ways and pulls focus back in from outside', () => {
    const items = ['bar', 'input', 'ask'];
    expect(trapFocus(items, 0, false)).toBe(1);
    expect(trapFocus(items, 2, false)).toBe(0);
    expect(trapFocus(items, 0, true)).toBe(2);
    expect(trapFocus(items, 1, true)).toBe(0);
    expect(trapFocus(items, -1, false)).toBe(0);
    expect(trapFocus(items, -1, true)).toBe(2);
    expect(trapFocus([], 0, false)).toBe(-1);
  });

  it('height and the closed flag survive storage that is missing or refuses', () => {
    const map = new Map<string, string>();
    const good = { getItem: (k: string) => map.get(k) ?? null, setItem: (k: string, v: string) => void map.set(k, v), removeItem: (k: string) => void map.delete(k) };
    const refusing = {
      getItem: () => {
        throw new Error('SecurityError');
      },
      setItem: () => {
        throw new Error('QuotaExceededError');
      },
      removeItem: () => {
        throw new Error('SecurityError');
      },
    };
    expect(loadDeskHeight(good)).toBeNull();
    saveDeskHeight(312.4, good);
    expect(map.get(HEIGHT_KEY)).toBe('312');
    expect(loadDeskHeight(good)).toBe(312);
    map.set(HEIGHT_KEY, 'tall');
    expect(loadDeskHeight(good)).toBeNull();
    expect(loadDeskHeight(refusing)).toBeNull();
    expect(() => saveDeskHeight(300, refusing)).not.toThrow();
    expect(loadDeskHeight(null)).toBeNull();

    rememberDeskClosed(true, good);
    expect(map.get(CLOSED_KEY)).toBe('1');
    expect(deskClosedThisSession(good)).toBe(true);
    rememberDeskClosed(false, good);
    expect(deskClosedThisSession(good)).toBe(false);
    expect(deskClosedThisSession(refusing)).toBe(false);
    expect(() => rememberDeskClosed(true, refusing)).not.toThrow();
  });

  it('the opt-out toggle saves first, then applies what the server stored', async () => {
    const order: string[] = [];
    const toggle = deskToggle(
      async (desk) => {
        order.push(`save ${desk}`);
        return { desk };
      },
      (desk) => order.push(`apply ${desk}`),
    );
    await toggle(false);
    expect(order).toEqual(['save false', 'apply false']);
    const failing = deskToggle(
      async () => {
        throw new DeskError(0, 'network', DESK_COPY.unreachable);
      },
      (desk) => order.push(`apply ${desk}`),
    );
    await expect(failing(true)).rejects.toBeInstanceOf(DeskError);
    expect(order).toEqual(['save false', 'apply false']); // nothing applied when the save failed
  });
});

// ---------------------------------------------------------------------------
// The state machine
// ---------------------------------------------------------------------------

const events = (list: ContractEvent[]): DeskStreamEvent[] => list.map((e) => ({ type: e.event, data: e.data }) as unknown as DeskStreamEvent);

function run(state: DeskState, ...actions: Parameters<typeof deskReducer>[1][]): DeskState {
  return actions.reduce(deskReducer, state);
}

const answered = (q: string, a: string, id: number): DeskEntry => ({
  id,
  question: q,
  context: null,
  source: 'prompt',
  reads: [],
  answer: { ...(A.events[2].data as unknown as AnswerEvent), text: a },
  done: true,
});

describe('deskReducer', () => {
  const start = initialDeskState();

  it('starts collapsed, or hidden once closed this session, at the stored height', () => {
    expect(start.mode).toBe('collapsed');
    expect(initialDeskState({ closed: true }).mode).toBe('hidden');
    expect(initialDeskState({ height: 300 }).height).toBe(300);
  });

  it('open with ask starts an entry scoped to the agent; without it the question waits in the prompt', () => {
    const asked = run(start, { type: 'open', question: 'why is codex waiting', agentId: 'codex', ask: true, source: 'palette' });
    expect(asked.mode).toBe('open');
    expect(asked.asking).toBe(true);
    expect(asked.entries).toHaveLength(1);
    expect(asked.entries[0]).toMatchObject({ question: 'why is codex waiting', context: { agent_id: 'codex' }, source: 'palette', done: false });
    expect(pendingEntry(asked)?.id).toBe(asked.entries[0].id);

    const drafted = run(start, { type: 'open', ...slipAsk('codex') });
    expect(drafted.mode).toBe('open');
    expect(drafted.asking).toBe(false);
    expect(drafted.entries).toEqual([]);
    expect(drafted.draft).toBe('why is codex waiting');
    expect(drafted.draftContext).toEqual({ agent_id: 'codex' });

    // Enter sends the prefilled question, with the slip's agent and source.
    const sent = run(drafted, { type: 'submit' });
    expect(sent.entries[0]).toMatchObject({ question: 'why is codex waiting', context: { agent_id: 'codex' }, source: 'why_slip' });
    expect(sent.draft).toBe('');
    expect(askRequest(sent, sent.entries[0], 'conv-12345678')).toEqual({
      question: 'why is codex waiting',
      conv: 'conv-12345678',
      history: [],
      context: { agent_id: 'codex' },
      source: 'why_slip',
    });
  });

  it('a bare open keeps what the prompt holds; clearing a prefilled question drops its agent', () => {
    const typed = run(start, { type: 'draft', text: 'what did claude-code hand off' });
    expect(run(typed, { type: 'open', source: 'palette' }).draft).toBe('what did claude-code hand off');
    const drafted = run(start, { type: 'open', ...slipAsk('codex') }, { type: 'draft', text: '' });
    expect(drafted.draftContext).toBeNull();
    expect(drafted.draftSource).toBe('prompt');
  });

  it('collapse keeps a running ask streaming; close hides the desk; expand reopens', () => {
    const asked = run(start, { type: 'open', question: 'q', ask: true, source: 'prompt' });
    const collapsed = run(asked, { type: 'collapse' });
    expect(collapsed.mode).toBe('collapsed');
    expect(collapsed.asking).toBe(true);
    const read = run(collapsed, events(A.events)[0]);
    expect(read.entries[0].reads).toHaveLength(1);
    expect(run(collapsed, { type: 'close' }).mode).toBe('hidden');
    expect(run(collapsed, { type: 'expand' }).mode).toBe('open');
    expect(run(start, { type: 'collapse' }).mode).toBe('collapsed');
  });

  it('submit is refused while asking, when blank, and past 1,000 code points', () => {
    const asking = run(start, { type: 'open', question: 'first', ask: true, source: 'prompt' }, { type: 'draft', text: 'second' });
    expect(run(asking, { type: 'submit' })).toBe(asking);
    expect(askRefusal(asking, 'second')).toBe('asking');
    const blank = run(start, { type: 'draft', text: '   ' });
    expect(run(blank, { type: 'submit' })).toBe(blank);
    const long = run(start, { type: 'draft', text: 'x'.repeat(1001) });
    expect(run(long, { type: 'submit' })).toBe(long);
    expect(askRefusal(start, '\u{1F600}'.repeat(1000))).toBeNull();
    expect(askRefusal(start, '\u{1F600}'.repeat(1001))).toBe('too_long');
    // An open with ask while a question is out waits in the prompt instead of starting a second stream.
    const queued = run(asking, { type: 'open', question: 'third', ask: true, source: 'board' });
    expect(queued.entries).toHaveLength(1);
    expect(queued.draft).toBe('third');
  });

  it('files example A into the entry and ends the ask on done', () => {
    const final = run(start, { type: 'open', question: 'why is codex waiting', agentId: 'codex', ask: true, source: 'why_slip' }, ...events(A.events));
    const [entry] = final.entries;
    expect(final.asking).toBe(false);
    expect(entry.done).toBe(true);
    expect(entry.reads.map((r) => r.id)).toEqual(['r1', 'r2']);
    expect(entry.answer?.text).toBe(A.events[2].data.text);
    expect(entry.usage?.usd).toBe(0.000682);
    expect(entry.error).toBeUndefined();
    // Events after done are not filed anywhere.
    expect(run(final, events(A.events)[0])).toBe(final);
  });

  it('files the error ending as the entry error, with its usage', () => {
    const final = run(start, { type: 'open', question: 'q', ask: true, source: 'prompt' }, ...events(CONTRACT.error_tail.events));
    expect(final.entries[0].error).toEqual({ message: CONTRACT.sse_error_messages.model_unavailable, retryable: true });
    expect(final.entries[0].usage?.model_calls).toBe(1);
    expect(final.entries[0].done).toBe(true);
    expect(final.asking).toBe(false);
  });

  it('failed before the stream: offline and limited_day block the prompt; unreachable only marks the entry', () => {
    const asked = run(start, { type: 'open', question: 'q', ask: true, source: 'prompt' });
    const offline = run(asked, { type: 'failed', notice: { state: 'offline', message: SERVER_COPY.offlineToday } });
    expect(offline.entries[0]).toMatchObject({ done: true, error: { message: SERVER_COPY.offlineToday, retryable: false } });
    expect(offline.asking).toBe(false);
    expect(blocksAsking(offline.notice)).toBe(true);
    expect(run(offline, { type: 'draft', text: 'again' }, { type: 'submit' }).entries).toHaveLength(1);

    const limited = run(asked, { type: 'failed', notice: { state: 'limited_day', message: dailyLimitText(40) } });
    expect(blocksAsking(limited.notice)).toBe(true);

    const cut = run(asked, { type: 'failed', notice: { state: 'unreachable', message: DESK_COPY.unreachable } });
    expect(cut.entries[0].error).toEqual({ message: DESK_COPY.unreachable, retryable: true });
    expect(blocksAsking(cut.notice)).toBe(false);
    const again = run(cut, { type: 'draft', text: 'again' }, { type: 'submit' });
    expect(again.entries).toHaveLength(2);
    expect(again.notice).toBeNull(); // the old line goes once a new question is out
  });

  it('aborted (closed mid-ask) stops the entry without an error', () => {
    const stopped = run(start, { type: 'open', question: 'q', ask: true, source: 'prompt' }, events(A.events)[0], { type: 'aborted', id: 1 });
    expect(stopped.entries[0]).toMatchObject({ done: true, stopped: true });
    expect(stopped.entries[0].error).toBeUndefined();
    expect(stopped.asking).toBe(false);
  });

  it('a late failure or abort of an older stream never touches the question now in flight', () => {
    const first = run(start, { type: 'open', question: 'first', ask: true, source: 'prompt' }, { type: 'reset' });
    const second = run(first, { type: 'open', question: 'second', ask: true, source: 'prompt' });
    const [entry] = second.entries;
    expect(entry.id).toBe(2);
    expect(run(second, { type: 'aborted', id: 1 })).toBe(second);
    expect(run(second, { type: 'failed', id: 1, notice: { state: 'unreachable', message: DESK_COPY.unreachable } })).toBe(second);
    expect(run(second, { type: 'aborted', id: 2 }).asking).toBe(false);
    // Nothing in flight: a failure changes nothing (no notice, no asking flip).
    const idle = run(start, { type: 'failed', notice: { state: 'unreachable', message: DESK_COPY.unreachable } });
    expect(idle).toBe(start);
  });

  it('the board: stored, and its model state becomes the notice', () => {
    const ready = run(start, { type: 'board', board: CONTRACT.board, at: 1000 });
    expect(ready.board).toEqual(CONTRACT.board);
    expect(ready.boardAt).toBe(1000);
    expect(ready.notice).toBeNull();

    const offlineBoard: DeskBoard = { ...CONTRACT.board, model: { ...CONTRACT.board.model, state: 'offline', reason: 'no_key' } };
    expect(run(start, { type: 'board', board: offlineBoard, at: 1 }).notice).toEqual({ state: 'offline', message: SERVER_COPY.offlineToday });
    const monthly: DeskBoard = { ...offlineBoard, model: { ...offlineBoard.model, reason: 'monthly_budget' } };
    expect(boardNotice(monthly)).toEqual({ state: 'offline', message: SERVER_COPY.offlineMonth });
    const limitedBoard: DeskBoard = { ...CONTRACT.board, model: { ...CONTRACT.board.model, state: 'limited', reason: 'daily_asks' }, asks: { ...CONTRACT.board.asks, used: 40 } };
    const limited = run(start, { type: 'board', board: limitedBoard, at: 1 });
    expect(limited.notice).toEqual({ state: 'limited_day', message: '40 questions today is the limit. Rules-only checks still work.' });
    expect(limited.notice?.message).toBe(CONTRACT.errors.marshal_daily_limit.message);
    // A fresh ready board clears an offline notice; an opted-out one stands.
    expect(run(limited, { type: 'board', board: CONTRACT.board, at: 2 }).notice).toBeNull();
    const optedOut = run(start, { type: 'boardFailed', notice: { state: 'opted_out', message: SERVER_COPY.optedOut } });
    expect(optedOut.boardError?.state).toBe('opted_out');
    expect(run(optedOut, { type: 'board', board: CONTRACT.board, at: 3 }).notice?.state).toBe('opted_out');
    // A board read that just failed shows its line, and the prompt stays usable.
    const cut = run(start, { type: 'boardFailed', notice: { state: 'unreachable', message: DESK_COPY.unreachable } });
    expect(cut.notice).toBeNull();
    expect(cut.boardError?.message).toBe(DESK_COPY.unreachable);
  });

  it('usage keeps the ask count; the last ask of the day closes the prompt', () => {
    const withBoard = run(start, { type: 'board', board: CONTRACT.board, at: 1 });
    const usage = { ...(A.events[3].data as unknown as UsageEvent) };
    const counted = run(withBoard, { type: 'open', question: 'q', ask: true, source: 'prompt' }, { type: 'usage', data: usage });
    expect(counted.board?.asks.used).toBe(4);
    const last = run(withBoard, { type: 'open', question: 'q', ask: true, source: 'prompt' }, { type: 'usage', data: { ...usage, asks_today: 40 } }, { type: 'done', data: { ok: true } });
    expect(last.notice).toEqual({ state: 'limited_day', message: dailyLimitText(40) });
    expect(blocksAsking(last.notice)).toBe(true);
  });

  it('height and reset', () => {
    expect(run(start, { type: 'height', height: 333.6 }).height).toBe(334);
    const busy = run(start, { type: 'open', question: 'q', ask: true, source: 'prompt' }, { type: 'draft', text: 'next' });
    const reset = run(busy, { type: 'reset' });
    expect(reset.entries).toEqual([]);
    expect(reset.asking).toBe(false);
    expect(reset.draft).toBe('');
  });
});

describe('trimHistory', () => {
  it('sends the last six answered turns, oldest first, clipped to 1,000 and 600 code points', () => {
    const entries: DeskEntry[] = Array.from({ length: 8 }, (_, i) => answered(`q${i}`, `a${i}`, i + 1));
    entries.push({ id: 9, question: 'failed', context: null, source: 'prompt', reads: [], error: { message: 'x', retryable: true }, done: true });
    entries.push({ id: 10, question: 'stopped', context: null, source: 'prompt', reads: [], done: true, stopped: true });
    const turns = trimHistory(entries);
    expect(turns.map((t) => t.question)).toEqual(['q2', 'q3', 'q4', 'q5', 'q6', 'q7']);
    const long = trimHistory([answered('\u{1F600}'.repeat(1200), 'b'.repeat(700), 1)])[0];
    expect(countChars(long.question)).toBe(1000);
    expect(long.answer).toHaveLength(600);
  });

  it('the request carries the turns before the question in flight, not the question itself', () => {
    const state = run(
      { ...initialDeskState(), entries: [answered('what did claude-code hand off last', 'Claude Code handed off 2h ago.', 1)], nextId: 2 },
      { type: 'open', question: 'why is codex waiting', ask: true, source: 'prompt' },
    );
    const entry = pendingEntry(state) as DeskEntry;
    expect(askRequest(state, entry, 'c0nversation').history).toEqual([
      { question: 'what did claude-code hand off last', answer: 'Claude Code handed off 2h ago.' },
    ]);
  });
});

describe('the copy is the contract copy', () => {
  it('DESK_COPY is exactly dashboard_copy (its format notes aside)', () => {
    const copy = Object.fromEntries(Object.entries(CONTRACT.dashboard_copy).filter(([key]) => !key.endsWith('_format')));
    expect(DESK_COPY).toEqual(copy);
  });

  it('SERVER_COPY mirrors the server sentences word for word', () => {
    expect(SERVER_COPY.offlineToday).toBe(CONTRACT.errors.marshal_offline.messages?.no_key);
    expect(SERVER_COPY.offlineMonth).toBe(CONTRACT.errors.marshal_offline.messages?.monthly_budget);
    expect(SERVER_COPY.optedOut).toBe(CONTRACT.errors.marshal_opted_out.message);
    expect(SERVER_COPY.unavailable).toBe(CONTRACT.errors.marshal_unavailable.message);
    expect(SERVER_COPY.loginRequired).toBe(CONTRACT.errors.marshal_login_required.message);
    expect(SERVER_COPY.internal).toBe(CONTRACT.sse_error_messages.internal);
    expect(dailyLimitText(40)).toBe(CONTRACT.errors.marshal_daily_limit.message);
  });
});


describe('edited questions and redacted history', () => {
  it('drops a prefilled agent scope when the user types a different question', () => {
    const scoped = deskReducer(initialDeskState(), { type: 'open', ...slipAsk('codex') });
    const edited = deskReducer(scoped, { type: 'draft', text: 'what did claude-code hand off' });
    expect(edited.draftContext).toBeNull();
    expect(edited.draftSource).toBe('prompt');
  });

  it('does not resend a question that previously held a credential', () => {
    const privateEntry = answered('synthetic-private-token', 'Removed the key.', 1);
    privateEntry.usage = { ...(A.events[3].data as unknown as UsageEvent), input_redactions: 1 };
    expect(trimHistory([privateEntry, answered('safe question', 'safe answer', 2)])).toEqual([
      { question: 'safe question', answer: 'safe answer' },
    ]);
  });
});
