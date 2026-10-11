// The Marshal desk: the dashboard half of POST /api/v1/marshal/ask and its
// board, settings and stream (desk-contract.json, contract version 1).
//
// Read-only by construction. The desk asks a question and shows what the
// server sends back: the reads it made (›), the call, its evidence and at most
// two commands for the user to run on their own machine. Nothing here writes
// anything except the user's own opt-out (PUT /marshal/settings), and there is
// no control that confirms or sends anything on the user's behalf.
//
// Everything the model wrote reaches the page as React text nodes only
// (inlineParts): no HTML is ever injected, links go to Remembra's three hosts
// only, and images are defanged the way the brief does.

import { API_V1 } from '../config';
import { api } from './api';
import { agentMeta, canonicalAgentId } from './agents';
import { defangImages } from './handoffTrust';

// ---------------------------------------------------------------------------
// Contract types
// ---------------------------------------------------------------------------

/** A board call's rule id (the port's VerdictCode, HANDED_OFF never reaches the board). */
export type DeskCallCode =
  | 'KEY_MISSING'
  | 'KEY_NEVER_USED'
  | 'PICKS_UP_NEVER_CLOSES'
  | 'CODEX_TRUST_MISSING'
  | 'HOOKS_NOT_FIRING'
  | 'STALE_CHECKPOINT'
  | 'NOTHING_WAITING';

export interface BoardCall {
  /** null for an account-level call (KEY_MISSING, KEY_NEVER_USED). */
  agent_id: string | null;
  code: DeskCallCode;
  proven: boolean;
  text: string;
  /** The question "see why" asks. */
  ask: string;
}

export type ModelState = 'ready' | 'offline' | 'limited';
export type ModelReason = 'no_key' | 'breaker_open' | 'daily_budget' | 'monthly_budget' | 'daily_asks';

/** GET /marshal/board: rules only, no model, nothing written. */
export interface DeskBoard {
  status_line: string;
  calls: BoardCall[];
  suggestions: string[];
  model: { state: ModelState; reason: ModelReason | null; name: string; retry_after_seconds: number | null };
  asks: { used: number; limit: number; resets_at: string };
  footer: string;
  generated_at: string;
}

/** GET/PUT /marshal/settings: the per-account opt-out. */
export interface DeskSettings {
  desk: boolean;
}

export type AskSource = 'palette' | 'why_slip' | 'board' | 'prompt';

export interface HistoryTurn {
  question: string;
  answer: string;
}

export interface AskContext {
  agent_id: string;
}

export interface AskRequest {
  question: string;
  conv: string;
  history: HistoryTurn[];
  context: AskContext | null;
  source: AskSource;
}

export type ToolName =
  | 'trail_summary'
  | 'trail'
  | 'brief_preview'
  | 'inbox_summary'
  | 'usage_summary'
  | 'usage_daily'
  | 'plan'
  | 'diagnose_agent'
  | 'docs_lookup';

export interface ReadEvent {
  id: string;
  tool: ToolName | string;
  /** The route family: trail/summary, trail, session/brief, … */
  label: string;
  args: Record<string, unknown>;
  ok: boolean;
  http_status: number | null;
  /** Deterministic, written by rules on the server. */
  summary: string;
  ms: number;
  /** `agent:<id>` or `entry:<id>`: the page row the read is about. */
  anchor: string | null;
}

export interface EvidenceRef {
  ref: string;
  /** Server-built: `<read.label> · <read.summary>`, at most 72 characters. */
  label: string;
  anchor: string | null;
}

export type CommandKind = 'terminal' | 'codex_ui' | 'agent';

export interface DeskCommand {
  text: string;
  kind: CommandKind;
  prompt: '$' | '>';
}

export type FallbackReason =
  | 'unparseable'
  | 'too_long'
  | 'no_evidence'
  | 'unknown_evidence'
  | 'failed_read_evidence'
  | 'command_not_allowed'
  | 'command_contradicts_verdict'
  | 'command_in_text'
  | 'link_not_allowed'
  | 'key_shaped'
  | 'unverified_quote'
  | 'banned_phrase'
  | 'voice_banned'
  | 'first_person'
  | 'exclamation'
  | 'emoji'
  | 'too_many_sentences'
  | 'sentence_too_long'
  | 'unsourced_figure'
  | 'pricing_unquoted'
  | 'model_budget'
  | 'tool_choice_ignored';

export interface AnswerEvent {
  text: string;
  evidence: EvidenceRef[];
  commands: DeskCommand[];
  /** true: the validator refused the model's answer and `text` is exactly "That's all I can confirm." */
  fallback: boolean;
  fallback_reason: FallbackReason | string | null;
  /** The deterministic read lines (fallback only). */
  summary: string[];
  /**
   * The page to read: a person to ask on a fallback; on an answer, the remembra.dev page it quotes (a price),
   * shown with "From remembra.dev pages; the page governs."
   */
  doc: string | null;
}

export interface UsageEvent {
  model: string;
  reads: number;
  model_calls: number;
  input_tokens: number;
  cached_tokens: number;
  output_tokens: number;
  usd: number;
  billed_to_credits: false;
  asks_today: number;
  asks_limit: number;
  input_redactions: number;
}

export interface ErrorEvent {
  error: 'model_unavailable' | 'timeout' | 'internal' | string;
  message: string;
  retryable: boolean;
}

export interface DoneEvent {
  ok: boolean;
}

/** One event of the stream: `read{0,4} (answer | error) usage done`. */
export type DeskStreamEvent =
  | { type: 'read'; data: ReadEvent }
  | { type: 'answer'; data: AnswerEvent }
  | { type: 'error'; data: ErrorEvent }
  | { type: 'usage'; data: UsageEvent }
  | { type: 'done'; data: DoneEvent };

/** The `detail.error` codes the desk routes send, plus the client's own for bodies without one. */
export type DeskErrorCode =
  | 'delegated_principal_refused'
  | 'marshal_unavailable'
  | 'marshal_login_required'
  | 'marshal_opted_out'
  | 'marshal_offline'
  | 'marshal_daily_limit'
  /** slowapi's 429 (`{"error": "Rate limit exceeded: …"}`, no detail). */
  | 'rate_limited'
  /** 401: no session, or an expired one. */
  | 'unauthorized'
  /** 422: the request failed validation. */
  | 'invalid'
  /** fetch failed: offline, DNS, CORS, a dropped connection. */
  | 'network'
  /** The stream ended before its `done` event. */
  | 'stream_cut'
  /** A 2xx that is not the contract's shape. */
  | 'bad_response'
  /** Any other status. */
  | 'http';

/** What the desk shows for a failure: one state per notice line. */
export type DeskErrorState =
  | 'unavailable'
  | 'login_required'
  | 'opted_out'
  | 'offline'
  | 'limited_day'
  | 'limited_minute'
  | 'invalid'
  | 'expired'
  | 'unreachable';

/** A failure of a desk route: the HTTP status (0 = never reached), the code and the sentence to show. */
export class DeskError extends Error {
  readonly status: number;
  readonly code: DeskErrorCode | string;
  /** The server's `detail` object, when it sent one (`reason`, `resets_at`, `limit`, …). */
  readonly data?: Record<string, unknown>;
  /** true when `message` is the server's own sentence (not a client fallback). */
  readonly fromServer: boolean;

  constructor(status: number, code: DeskErrorCode | string, message: string, data?: Record<string, unknown>, fromServer = false) {
    super(message);
    this.name = 'DeskError';
    this.status = status;
    this.code = code;
    this.data = data;
    this.fromServer = fromServer;
  }
}

// ---------------------------------------------------------------------------
// Copy (voice rules: spec 4.6; tests/voice.test.ts lints it)
// ---------------------------------------------------------------------------

/** The dashboard's own strings (desk-contract.json `dashboard_copy`). The server owns every other sentence. */
export const DESK_COPY = {
  reading: 'reading…',
  caption: "runs on your machine · Marshal can't run it",
  notBilled: 'not billed to your credits',
  fallbackDoc: 'ask a person · remembra.dev/contact',
  pagesGovern: 'From remembra.dev pages; the page governs.',
  limitedMinute: '20 questions a minute is the limit. Try again in a minute.',
  expired: 'Your session has expired: sign in again.',
  unreachable: "Couldn't reach Remembra. Ask again.",
  redacted: 'your question held a key-shaped string · removed before the model received it',
  settingLabel: 'Marshal desk',
  settingHelp: "Ask about your relay here. The transcript stays on this page; usage counts are stored. The desk cannot make changes.",
} as const;

/**
 * The server's own sentences (desk-contract.json `errors`), mirrored for the
 * two cases where the dashboard has to say one without a body: the board's
 * model state (the board carries a reason, not a message) and a body that
 * came back without its `message`.
 */
export const SERVER_COPY = {
  offlineToday: "Marshal's model is off for today. Rules-only checks still work.",
  offlineMonth: "Marshal's model is off until next month. Rules-only checks still work.",
  optedOut: 'Marshal desk is off for this account. Turn it on in Settings > Diagnostics.',
  unavailable: "Marshal isn't available on this account.",
  loginRequired: "Marshal answers in a dashboard login only. API keys and connected apps can't use it.",
  internal: 'Marshal hit an error. Ask again.',
} as const;

/** `40 questions today is the limit. …` (marshal_daily_limit, with the account's limit). */
export function dailyLimitText(limit: number): string {
  return `${limit} questions today is the limit. Rules-only checks still work.`;
}

/** The fallback answer's one sentence (the server sends exactly this). */
export const FALLBACK_TEXT = "That's all I can confirm.";
export const CONTACT_URL = 'https://remembra.dev/contact';

// ---------------------------------------------------------------------------
// Limits (the server enforces the same numbers)
// ---------------------------------------------------------------------------

export const MAX_QUESTION_CHARS = 1000;
export const MAX_HISTORY_TURNS = 6;
export const MAX_HISTORY_ANSWER_CHARS = 600;
/** The prompt shows its counter past this many code points. */
export const COUNTER_FROM = 800;
/**
 * No event for this long means the ask is lost. The server answers or gives
 * up within 45 s, so a minute of keepalives alone is a server that stopped
 * working, and a minute of nothing a dropped connection.
 */
export const STREAM_IDLE_MS = 60_000;
/** Expanding the desk re-reads a board older than this. */
export const BOARD_STALE_MS = 60_000;
export const ALLOWED_LINK_HOSTS = ['remembra.dev', 'docs.remembra.dev', 'app.remembra.dev'] as const;

/** Unicode code points, as the server counts them (an emoji is one). */
export function countChars(text: string): number {
  return Array.from(text).length;
}

/** The first `max` code points (never splits a surrogate pair). */
export function clipChars(text: string, max: number): string {
  const chars = Array.from(text);
  return chars.length <= max ? text : chars.slice(0, max).join('');
}

// ---------------------------------------------------------------------------
// text/event-stream
// ---------------------------------------------------------------------------

export interface SseParser {
  /** Feed bytes (decoded as UTF-8, a character split across chunks included) or text. */
  push(chunk: Uint8Array | string): void;
  /** The stream ended: flush the decoder. An event without its closing blank line is dropped. */
  end(): void;
}

/**
 * An incremental text/event-stream parser (the WHATWG rules the desk needs):
 * LF, CRLF or CR line ends, a CRLF split across chunks, several `data:` lines
 * joined with "\n", `:` comments (the keepalive) ignored, `id:` and `retry:`
 * ignored (there is no resume), a leading BOM dropped. It hands every event
 * on by name; which names matter is the caller's business.
 */
export function createSseParser(onEvent: (event: string, data: string) => void): SseParser {
  const decoder = new TextDecoder('utf-8');
  let started = false;
  let buffer = '';
  let afterCR = false;
  let eventName = '';
  let data: string[] = [];

  const dispatch = () => {
    const name = eventName || 'message';
    const payload = data;
    eventName = '';
    data = [];
    if (payload.length) onEvent(name, payload.join('\n'));
  };

  const line = (text: string) => {
    if (text === '') {
      dispatch();
      return;
    }
    if (text.charCodeAt(0) === 0x3a) return; // ":" comment
    const colon = text.indexOf(':');
    const field = colon === -1 ? text : text.slice(0, colon);
    let value = colon === -1 ? '' : text.slice(colon + 1);
    if (value.charCodeAt(0) === 0x20) value = value.slice(1);
    if (field === 'event') eventName = value;
    else if (field === 'data') data.push(value);
  };

  const feed = (text: string) => {
    let start = 0;
    for (let i = 0; i < text.length; i += 1) {
      const c = text.charCodeAt(i);
      if (afterCR) {
        afterCR = false;
        if (c === 0x0a) {
          start = i + 1;
          continue;
        }
      }
      if (c === 0x0a || c === 0x0d) {
        line(buffer + text.slice(start, i));
        buffer = '';
        afterCR = c === 0x0d;
        start = i + 1;
      }
    }
    buffer += text.slice(start);
  };

  return {
    push(chunk) {
      let text: string;
      if (typeof chunk === 'string') {
        text = chunk;
        // TextDecoder drops a leading BOM from bytes; text has to be checked here.
        if (!started && text.charCodeAt(0) === 0xfeff) text = text.slice(1);
      } else {
        text = decoder.decode(chunk, { stream: true });
      }
      if (text) started = true;
      feed(text);
    },
    end() {
      feed(decoder.decode());
      buffer = '';
      eventName = '';
      data = [];
    },
  };
}

const isObject = (v: unknown): v is Record<string, unknown> => typeof v === 'object' && v !== null && !Array.isArray(v);
const isString = (v: unknown): v is string => typeof v === 'string';
const isNumber = (v: unknown): v is number => typeof v === 'number' && Number.isFinite(v);
const isNullableString = (v: unknown): v is string | null => v === null || typeof v === 'string';
const isStringArray = (v: unknown): v is string[] => Array.isArray(v) && v.every(isString);

function isReadEvent(v: unknown): v is ReadEvent {
  return (
    isObject(v) &&
    isString(v.id) &&
    isString(v.tool) &&
    isString(v.label) &&
    isObject(v.args) &&
    typeof v.ok === 'boolean' &&
    (v.http_status === null || isNumber(v.http_status)) &&
    isString(v.summary) &&
    isNumber(v.ms) &&
    isNullableString(v.anchor)
  );
}

function isAnswerEvent(v: unknown): v is AnswerEvent {
  return (
    isObject(v) &&
    isString(v.text) &&
    Array.isArray(v.evidence) &&
    v.evidence.every((e) => isObject(e) && isString(e.ref) && isString(e.label) && isNullableString(e.anchor)) &&
    Array.isArray(v.commands) &&
    v.commands.every(
      (c) =>
        isObject(c) &&
        isString(c.text) &&
        (c.kind === 'terminal' || c.kind === 'codex_ui' || c.kind === 'agent') &&
        (c.prompt === '$' || c.prompt === '>'),
    ) &&
    typeof v.fallback === 'boolean' &&
    isNullableString(v.fallback_reason) &&
    isStringArray(v.summary) &&
    isNullableString(v.doc)
  );
}

function isUsageEvent(v: unknown): v is UsageEvent {
  return (
    isObject(v) &&
    isString(v.model) &&
    ['reads', 'model_calls', 'input_tokens', 'cached_tokens', 'output_tokens', 'usd', 'asks_today', 'asks_limit', 'input_redactions'].every(
      (k) => isNumber(v[k]),
    ) &&
    v.billed_to_credits === false
  );
}

function isErrorEvent(v: unknown): v is ErrorEvent {
  return isObject(v) && isString(v.error) && isString(v.message) && typeof v.retryable === 'boolean';
}

function isDoneEvent(v: unknown): v is DoneEvent {
  return isObject(v) && typeof v.ok === 'boolean';
}

const EVENT_CHECKS: Record<DeskStreamEvent['type'], (v: unknown) => boolean> = {
  read: isReadEvent,
  answer: isAnswerEvent,
  error: isErrorEvent,
  usage: isUsageEvent,
  done: isDoneEvent,
};

/**
 * One parsed SSE event as a desk event: null for a name the desk doesn't know
 * (ignored, so the server can add one), a DeskError for a known event whose
 * data isn't the contract's shape.
 */
export function toDeskEvent(name: string, data: string): DeskStreamEvent | null {
  if (!Object.prototype.hasOwnProperty.call(EVENT_CHECKS, name)) return null;
  const type = name as DeskStreamEvent['type'];
  let parsed: unknown;
  try {
    parsed = JSON.parse(data);
  } catch {
    throw new DeskError(200, 'bad_response', DESK_COPY.unreachable);
  }
  if (!EVENT_CHECKS[type](parsed)) throw new DeskError(200, 'bad_response', DESK_COPY.unreachable);
  return { type, data: parsed } as DeskStreamEvent;
}

// ---------------------------------------------------------------------------
// The client (dashboard JWT only; an API key never reaches these routes)
// ---------------------------------------------------------------------------

/** The rejection an aborted fetch or stream read gives (a DOMException named AbortError). */
export function isAbortError(err: unknown): boolean {
  return typeof err === 'object' && err !== null && (err as { name?: unknown }).name === 'AbortError';
}

/** The contract's error body, as a DeskError (the status says which shape it is). */
async function errorFrom(res: Response): Promise<DeskError> {
  let body: unknown = null;
  try {
    body = await res.json();
  } catch {
    body = null;
  }
  const detail = isObject(body) ? body.detail : undefined;
  if (isObject(detail) && isString(detail.error)) {
    const message = isString(detail.message) && detail.message ? detail.message : null;
    return new DeskError(res.status, detail.error, message ?? fallbackMessage(res.status, detail.error), detail, message !== null);
  }
  if (res.status === 401) return new DeskError(401, 'unauthorized', DESK_COPY.expired);
  if (res.status === 422) return new DeskError(422, 'invalid', SERVER_COPY.internal, Array.isArray(detail) ? { errors: detail } : undefined);
  if (res.status === 429) return new DeskError(429, 'rate_limited', DESK_COPY.limitedMinute);
  return new DeskError(res.status, 'http', res.status >= 500 ? DESK_COPY.unreachable : SERVER_COPY.internal);
}

/** The sentence for a code whose body came without its `message`. */
function fallbackMessage(status: number, code: string): string {
  switch (code) {
    case 'marshal_offline':
      return SERVER_COPY.offlineToday;
    case 'marshal_daily_limit':
      return SERVER_COPY.offlineToday;
    case 'marshal_opted_out':
      return SERVER_COPY.optedOut;
    case 'marshal_unavailable':
      return SERVER_COPY.unavailable;
    case 'marshal_login_required':
    case 'delegated_principal_refused':
      return SERVER_COPY.loginRequired;
    default:
      return status >= 500 ? DESK_COPY.unreachable : SERVER_COPY.internal;
  }
}

/** One desk request: the JWT (never an X-API-Key), errors as DeskError, an abort left as it is. */
async function deskFetch(path: string, init: RequestInit & { accept?: string } = {}): Promise<Response> {
  const token = api.getJwtToken();
  if (!token) throw new DeskError(401, 'unauthorized', DESK_COPY.expired);
  const { accept = 'application/json', headers, ...rest } = init;
  let res: Response;
  try {
    res = await fetch(`${API_V1}${path}`, {
      ...rest,
      cache: 'no-store',
      headers: {
        Authorization: `Bearer ${token}`,
        Accept: accept,
        ...(rest.body !== undefined ? { 'Content-Type': 'application/json' } : {}),
        ...headers,
      },
    });
  } catch (err) {
    if (isAbortError(err)) throw err;
    throw new DeskError(0, 'network', DESK_COPY.unreachable);
  }
  if (!res.ok) throw await errorFrom(res);
  return res;
}

async function deskJson<T>(path: string, check: (v: unknown) => v is T, init: RequestInit = {}): Promise<T> {
  const res = await deskFetch(path, init);
  let body: unknown;
  try {
    body = await res.json();
  } catch (err) {
    if (isAbortError(err)) throw err;
    throw new DeskError(res.status, 'bad_response', DESK_COPY.unreachable);
  }
  if (!check(body)) throw new DeskError(res.status, 'bad_response', DESK_COPY.unreachable);
  return body;
}

function isBoard(v: unknown): v is DeskBoard {
  return (
    isObject(v) &&
    isString(v.status_line) &&
    Array.isArray(v.calls) &&
    v.calls.every(
      (c) => isObject(c) && isNullableString(c.agent_id) && isString(c.code) && typeof c.proven === 'boolean' && isString(c.text) && isString(c.ask),
    ) &&
    isStringArray(v.suggestions) &&
    isObject(v.model) &&
    (v.model.state === 'ready' || v.model.state === 'offline' || v.model.state === 'limited') &&
    isNullableString(v.model.reason) &&
    isString(v.model.name) &&
    isObject(v.asks) &&
    isNumber(v.asks.used) &&
    isNumber(v.asks.limit) &&
    isString(v.footer)
  );
}

function isSettings(v: unknown): v is DeskSettings {
  return isObject(v) && typeof v.desk === 'boolean';
}

/** GET /marshal/board (rules only). */
export function getBoard(signal?: AbortSignal): Promise<DeskBoard> {
  return deskJson('/marshal/board', isBoard, { signal });
}

/** GET /marshal/settings: 200 means the desk exists for this account; `desk: false` means opted out. */
export function getDeskSettings(signal?: AbortSignal): Promise<DeskSettings> {
  return deskJson('/marshal/settings', isSettings, { signal });
}

/** PUT /marshal/settings: the user's own opt-out, the one thing the desk writes. */
export function setDeskSettings(desk: boolean): Promise<DeskSettings> {
  return deskJson('/marshal/settings', isSettings, { method: 'PUT', body: JSON.stringify({ desk }) });
}

export interface AskHandlers {
  /** Every contract event, in order. */
  onEvent: (event: DeskStreamEvent) => void;
}

/**
 * POST /marshal/ask and read its stream to the `done` event. Rejects with a
 * DeskError before the stream (the contract's statuses), when the connection
 * drops or ends before `done` (no reconnect: the answer is never resumed),
 * or when an event isn't the contract's shape; with the AbortError when
 * `signal` aborts.
 */
export async function askMarshal(req: AskRequest, handlers: AskHandlers, signal?: AbortSignal): Promise<void> {
  const res = await deskFetch('/marshal/ask', {
    method: 'POST',
    body: JSON.stringify(req),
    accept: 'text/event-stream',
    signal,
  });
  const type = res.headers.get('content-type') ?? '';
  if (!type.includes('text/event-stream') || !res.body) {
    throw new DeskError(res.status, 'bad_response', DESK_COPY.unreachable);
  }
  const reader = res.body.getReader();
  let finished = false;
  const parser = createSseParser((name, data) => {
    if (finished) return;
    const event = toDeskEvent(name, data);
    if (!event) return;
    if (event.type === 'done') finished = true;
    handlers.onEvent(event);
  });
  try {
    while (!finished) {
      let chunk: ReadableStreamReadResult<Uint8Array>;
      try {
        chunk = await reader.read();
      } catch (err) {
        if (isAbortError(err)) throw err;
        throw new DeskError(0, 'network', DESK_COPY.unreachable);
      }
      if (chunk.done) {
        parser.end();
        break;
      }
      parser.push(chunk.value);
    }
  } finally {
    // Done early (or failed): let the connection go.
    reader.cancel().catch(() => undefined);
  }
  if (!finished) throw new DeskError(0, 'stream_cut', DESK_COPY.unreachable);
}

/** Timers the runners use (the page's, or a test's). */
export interface DeskTimers {
  setTimeout: (fn: () => void, ms: number) => unknown;
  clearTimeout: (id: unknown) => void;
}

const PAGE_TIMERS: DeskTimers = {
  setTimeout: (fn, ms) => globalThis.setTimeout(fn, ms),
  clearTimeout: (id) => globalThis.clearTimeout(id as ReturnType<typeof setTimeout>),
};

export interface AskRun {
  /**
   * Abort the ask. By default its question is marked stopped; `silent` (the
   * provider unmounting, the effect re-running) dispatches nothing more at all.
   */
  stop: (silent?: boolean) => void;
  /** Settles when the run is over, whatever the outcome (it never rejects). */
  finished: Promise<void>;
}

/**
 * Run one question to its end: every stream event goes to `dispatch`; a
 * failure becomes `failed` (with its notice) and a stop becomes `aborted`,
 * both naming the question so they can never touch a newer one. A minute
 * with no event (keepalives don't count: the server has answered or given up
 * by 45 s) is a lost ask: it is aborted and shown as unreachable.
 */
export function runAsk({
  request,
  id,
  dispatch,
  idleMs = STREAM_IDLE_MS,
  timers = PAGE_TIMERS,
}: {
  request: AskRequest;
  id: number;
  dispatch: (action: DeskAction) => void;
  idleMs?: number;
  timers?: DeskTimers;
}): AskRun {
  const ctrl = new AbortController();
  // `live`: this run may still dispatch. It goes false on stop and once the run is over.
  let live = true;
  let timedOut = false;
  let idle: unknown;
  const quiet = () => {
    timers.clearTimeout(idle);
    idle = timers.setTimeout(() => {
      timedOut = true;
      ctrl.abort();
    }, idleMs);
  };
  quiet();
  const finished = askMarshal(
    request,
    {
      onEvent: (event) => {
        quiet();
        if (live) dispatch(event);
      },
    },
    ctrl.signal,
  )
    .catch((err: unknown) => {
      if (!live) return;
      if (timedOut) dispatch({ type: 'failed', id, notice: { state: 'unreachable', message: DESK_COPY.unreachable } });
      else if (isAbortError(err)) dispatch({ type: 'aborted', id });
      else dispatch({ type: 'failed', id, notice: noticeFor(err) });
    })
    .finally(() => {
      live = false;
      timers.clearTimeout(idle);
    });
  return {
    stop: (silent = false) => {
      if (!live) return;
      live = false;
      timers.clearTimeout(idle);
      ctrl.abort();
      if (!silent) dispatch({ type: 'aborted', id });
    },
    finished,
  };
}

export type SettingsResult = { status: 'ready'; desk: boolean } | { status: 'off' };

/**
 * Ask whether the desk exists for this account. 200 is the answer; 404 (not
 * on the allow-list, turned off) and 401/403 are final; an unreachable server
 * is asked again after `retryMs`, `retries` times. Returns a cancel function.
 */
export function loadDeskSettings(
  onResult: (result: SettingsResult) => void,
  { retryMs = 30_000, retries = 2, timers = PAGE_TIMERS }: { retryMs?: number; retries?: number; timers?: DeskTimers } = {},
): () => void {
  const ctrl = new AbortController();
  let retry: unknown;
  const attempt = (n: number) => {
    getDeskSettings(ctrl.signal).then(
      (stored) => {
        if (!ctrl.signal.aborted) onResult({ status: 'ready', desk: stored.desk });
      },
      (err: unknown) => {
        if (isAbortError(err) || ctrl.signal.aborted) return;
        if (deskErrorState(err) === 'unreachable' && n < retries) {
          retry = timers.setTimeout(() => attempt(n + 1), retryMs);
          return;
        }
        onResult({ status: 'off' });
      },
    );
  };
  attempt(0);
  return () => {
    ctrl.abort();
    timers.clearTimeout(retry);
  };
}

// ---------------------------------------------------------------------------
// Failures as notices
// ---------------------------------------------------------------------------

/** Which notice a failure is. Anything that isn't a DeskError counts as unreachable. */
export function deskErrorState(err: unknown): DeskErrorState {
  if (!(err instanceof DeskError)) return 'unreachable';
  switch (err.code) {
    case 'marshal_unavailable':
      return 'unavailable';
    case 'marshal_login_required':
    case 'delegated_principal_refused':
      return 'login_required';
    case 'marshal_opted_out':
      return 'opted_out';
    case 'marshal_offline':
      return 'offline';
    case 'marshal_daily_limit':
      return 'limited_day';
    case 'rate_limited':
      return 'limited_minute';
    case 'unauthorized':
      return 'expired';
    case 'invalid':
      return 'invalid';
    default:
      if (err.status === 401) return 'expired';
      if (err.status === 404) return 'unavailable';
      if (err.status === 429) return 'limited_minute';
      return 'unreachable';
  }
}

export interface DeskNotice {
  state: DeskErrorState;
  message: string;
}

/** The notice line for a failure: the server's sentence where the contract gives one, the desk's own otherwise. */
export function noticeFor(err: unknown): DeskNotice {
  const state = deskErrorState(err);
  const server = err instanceof DeskError && err.fromServer ? err.message : null;
  switch (state) {
    case 'offline':
      return { state, message: server ?? SERVER_COPY.offlineToday };
    case 'limited_day':
      return { state, message: server ?? SERVER_COPY.offlineToday };
    case 'opted_out':
      return { state, message: server ?? SERVER_COPY.optedOut };
    case 'unavailable':
      return { state, message: server ?? SERVER_COPY.unavailable };
    case 'login_required':
      return { state, message: server ?? SERVER_COPY.loginRequired };
    case 'limited_minute':
      return { state, message: DESK_COPY.limitedMinute };
    case 'expired':
      return { state, message: DESK_COPY.expired };
    case 'invalid':
      return { state, message: SERVER_COPY.internal };
    default:
      return { state: 'unreachable', message: DESK_COPY.unreachable };
  }
}

/** Notices that stop the prompt until something changes (a new board, a new session, the setting). */
const BLOCKING: ReadonlySet<DeskErrorState> = new Set(['offline', 'limited_day', 'opted_out', 'unavailable', 'login_required', 'expired']);

export function blocksAsking(notice: DeskNotice | null): boolean {
  return !!notice && BLOCKING.has(notice.state);
}

/** What the board's model state means for the prompt (null: it may ask). */
export function boardNotice(board: DeskBoard): DeskNotice | null {
  if (board.model.state === 'offline') {
    return { state: 'offline', message: board.model.reason === 'monthly_budget' ? SERVER_COPY.offlineMonth : SERVER_COPY.offlineToday };
  }
  if (board.model.state === 'limited') return { state: 'limited_day', message: dailyLimitText(board.asks.limit) };
  return null;
}

// ---------------------------------------------------------------------------
// Rendering helpers
// ---------------------------------------------------------------------------

/** `<$0.001` below a tenth of a cent, else dollars to three places. */
export function formatUsd(usd: number): string {
  if (!Number.isFinite(usd) || usd <= 0) return '$0.000';
  if (usd < 0.001) return '<$0.001';
  return `$${usd.toFixed(3)}`;
}

/** `gpt-4o-mini · 2 reads · <$0.001 · not billed to your credits` */
export function footerText(usage: UsageEvent): string {
  const reads = `${usage.reads} ${usage.reads === 1 ? 'read' : 'reads'}`;
  return `${usage.model} · ${reads} · ${formatUsd(usage.usd)} · ${DESK_COPY.notBilled}`;
}

export type InlinePart =
  | { kind: 'text'; text: string }
  | { kind: 'bold'; text: string }
  | { kind: 'code'; text: string }
  | { kind: 'link'; text: string; href: string };

/** An https link to one of Remembra's own hosts, normalised; null for anything else. */
export function allowedHref(raw: string): string | null {
  let url: URL;
  try {
    url = new URL(raw);
  } catch {
    return null;
  }
  if (url.protocol !== 'https:' || url.username || url.password || url.port) return null;
  if (!(ALLOWED_LINK_HOSTS as readonly string[]).includes(url.hostname)) return null;
  return url.href;
}

/** An allowed link as its reader sees it: host, path and anchor, no scheme (`docs.remembra.dev/…/#plans`). */
export function hrefLabel(href: string): string {
  const url = new URL(href);
  return `${url.hostname}${url.pathname === '/' ? '' : url.pathname}${url.search}${url.hash}`;
}

// `code`, **bold**, [label](https://…) and a bare https://… URL, in that order of precedence.
const INLINE = /`([^`\n]+)`|\*\*([^*\n]+?)\*\*|\[([^\]\n]{1,200})\]\((\S{1,2000}?)\)|(https:\/\/[^\s<>"'`)\]]+)/g;

/**
 * The desk's mini-markdown: `code`, **bold** and links to remembra.dev,
 * docs.remembra.dev and app.remembra.dev. Everything else stays text, markup
 * included (`<script>`, `javascript:` links, foreign hosts). Images are
 * replaced with `[image removed: <host>]` first, as briefs do. The parts are
 * rendered as React text nodes; nothing is ever parsed as HTML.
 */
export function inlineParts(source: string): InlinePart[] {
  const text = defangImages(source);
  const parts: InlinePart[] = [];
  const push = (part: InlinePart) => {
    const last = parts[parts.length - 1];
    if (part.kind === 'text' && last?.kind === 'text') last.text += part.text;
    else if (part.kind !== 'text' || part.text) parts.push(part);
  };
  let at = 0;
  for (const m of text.matchAll(INLINE)) {
    const index = m.index ?? 0;
    push({ kind: 'text', text: text.slice(at, index) });
    at = index + m[0].length;
    if (m[1] !== undefined) {
      push({ kind: 'code', text: m[1] });
    } else if (m[2] !== undefined) {
      push({ kind: 'bold', text: m[2] });
    } else if (m[3] !== undefined) {
      const href = allowedHref(m[4]);
      push(href ? { kind: 'link', text: m[3], href } : { kind: 'text', text: m[0] });
    } else {
      // A bare URL: sentence punctuation after it is not part of it.
      const raw = m[5].replace(/[.,;:!?]+$/, '');
      const href = allowedHref(raw);
      push(href ? { kind: 'link', text: raw, href } : { kind: 'text', text: raw });
      push({ kind: 'text', text: m[5].slice(raw.length) });
    }
  }
  push({ kind: 'text', text: text.slice(at) });
  return parts;
}

/** The label for a board call's agent, or `account` for an account-level call. */
export function callAgentLabel(agentId: string | null): string {
  if (!agentId) return 'account';
  return agentMeta(agentId).adapter ?? canonicalAgentId(agentId);
}

export interface OpenRequest {
  question?: string;
  agentId?: string | null;
  ask?: boolean;
  source: AskSource;
}

/** The "why?" slip's `ask Marshal about this`: prefilled, scoped to the agent, never sent until Enter. */
export function slipAsk(agentId: string): OpenRequest & { question: string; agentId: string; ask: false } {
  const canonical = canonicalAgentId(agentId);
  const adapter = agentMeta(agentId).adapter ?? canonical;
  return { question: `why is ${adapter} waiting`, agentId: canonical, ask: false, source: 'why_slip' };
}

export interface PaletteMarshalItem {
  label: string;
  question: string;
  ask: boolean;
}

/**
 * The palette's `?` mode: `?why is codex waiting` becomes one item that asks
 * it, a bare `?` one that opens the desk. Null when the query isn't a `?`
 * question or the desk isn't there for this account.
 */
export function marshalPaletteItem(query: string, desk: { available: boolean; optedOut: boolean }): PaletteMarshalItem | null {
  if (!desk.available || desk.optedOut) return null;
  const trimmed = query.trimStart();
  if (!trimmed.startsWith('?')) return null;
  const question = clipChars(trimmed.slice(1).trim(), MAX_QUESTION_CHARS);
  return question ? { label: `Ask Marshal: "${question}"`, question, ask: true } : { label: 'Ask Marshal', question: '', ask: false };
}

// ---------------------------------------------------------------------------
// Resize, focus and storage
// ---------------------------------------------------------------------------

export interface HeightBounds {
  min: number;
  max: number;
}

export const MIN_DESK_HEIGHT = 160;
export const HEIGHT_STEP = 24;

/** [160px, 70% of the viewport]. */
export function heightBounds(viewport: number): HeightBounds {
  return { min: MIN_DESK_HEIGHT, max: Math.max(MIN_DESK_HEIGHT, Math.floor(viewport * 0.7)) };
}

export function clampHeight(height: number, bounds: HeightBounds): number {
  return Math.min(bounds.max, Math.max(bounds.min, Math.round(height)));
}

/** 44% of the viewport, clamped. */
export function defaultHeight(viewport: number): number {
  return clampHeight(viewport * 0.44, heightBounds(viewport));
}

/** The resize handle's keys: ArrowUp/ArrowDown move it 24px, Home/End go to min/max. Null for any other key. */
export function nextHeight(key: string, height: number, bounds: HeightBounds): number | null {
  switch (key) {
    case 'ArrowUp':
      return clampHeight(height + HEIGHT_STEP, bounds);
    case 'ArrowDown':
      return clampHeight(height - HEIGHT_STEP, bounds);
    case 'Home':
      return bounds.min;
    case 'End':
      return bounds.max;
    default:
      return null;
  }
}

/**
 * Enter in the prompt asks the question, and that is all it ever does (the
 * desk has nothing to confirm). Not while an input method is composing: that
 * Enter picks a character.
 */
export function isSubmitKey(event: { key: string; isComposing?: boolean; keyCode?: number }): boolean {
  return event.key === 'Enter' && !event.isComposing && event.keyCode !== 229;
}

/** Tab inside the sheet: the next focusable index, wrapping at both ends (-1 = focus is outside). */
export function trapFocus(focusables: ArrayLike<unknown>, activeIndex: number, shift: boolean): number {
  const count = focusables.length;
  if (count === 0) return -1;
  if (activeIndex < 0 || activeIndex >= count) return shift ? count - 1 : 0;
  if (shift) return activeIndex === 0 ? count - 1 : activeIndex - 1;
  return activeIndex === count - 1 ? 0 : activeIndex + 1;
}

export const HEIGHT_KEY = 'marshal.desk.height';
export const CLOSED_KEY = 'marshal.desk.closed';

type KeyValueStore = Pick<Storage, 'getItem' | 'setItem' | 'removeItem'>;

function storage(kind: 'localStorage' | 'sessionStorage'): KeyValueStore | null {
  try {
    return typeof window === 'undefined' ? null : window[kind];
  } catch {
    return null;
  }
}

/** The height the user dragged to, or null (the default). Storage may be missing or refuse. */
export function loadDeskHeight(store: KeyValueStore | null = storage('localStorage')): number | null {
  try {
    const raw = store?.getItem(HEIGHT_KEY);
    const value = raw ? Number(raw) : NaN;
    return Number.isFinite(value) && value > 0 ? Math.round(value) : null;
  } catch {
    return null;
  }
}

export function saveDeskHeight(height: number, store: KeyValueStore | null = storage('localStorage')): void {
  try {
    store?.setItem(HEIGHT_KEY, String(Math.round(height)));
  } catch {
    // Storage unavailable: the height lasts for this page.
  }
}

/** true after × on the bar, for this browser tab's session. */
export function deskClosedThisSession(store: KeyValueStore | null = storage('sessionStorage')): boolean {
  try {
    return store?.getItem(CLOSED_KEY) === '1';
  } catch {
    return false;
  }
}

export function rememberDeskClosed(closed: boolean, store: KeyValueStore | null = storage('sessionStorage')): void {
  try {
    if (closed) store?.setItem(CLOSED_KEY, '1');
    else store?.removeItem(CLOSED_KEY);
  } catch {
    // Storage unavailable: the bar comes back on the next load.
  }
}

/** A conversation id for `conv` (the server only hashes it): a UUID, 36 characters of [0-9a-f-]. */
export function newConversationId(): string {
  const c = (globalThis as { crypto?: Crypto }).crypto;
  if (c?.randomUUID) return c.randomUUID();
  const bytes = new Uint8Array(16);
  if (c?.getRandomValues) c.getRandomValues(bytes);
  else for (let i = 0; i < 16; i += 1) bytes[i] = Math.floor(Math.random() * 256);
  bytes[6] = (bytes[6] & 0x0f) | 0x40;
  bytes[8] = (bytes[8] & 0x3f) | 0x80;
  const hex = Array.from(bytes, (b) => b.toString(16).padStart(2, '0')).join('');
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
}

/**
 * The opt-out toggle: save first, then apply what the server answered (so the
 * desk mounts or unmounts only once the setting is really stored).
 */
export function deskToggle(
  save: (desk: boolean) => Promise<DeskSettings>,
  apply: (desk: boolean) => void,
): (desk: boolean) => Promise<DeskSettings> {
  return async (desk) => {
    const stored = await save(desk);
    apply(stored.desk);
    return stored;
  };
}

// ---------------------------------------------------------------------------
// The desk's state (pure)
// ---------------------------------------------------------------------------

export type DeskMode = 'hidden' | 'collapsed' | 'open';

export interface DeskEntry {
  id: number;
  question: string;
  context: AskContext | null;
  source: AskSource;
  reads: ReadEvent[];
  answer?: AnswerEvent;
  /** The stream's `error` event, or a failure before or during it. */
  error?: { message: string; retryable: boolean };
  usage?: UsageEvent;
  /** The stream is over for this question (answered, failed or stopped). */
  done: boolean;
  /** Closed before the answer arrived. */
  stopped?: boolean;
}

export interface DeskState {
  mode: DeskMode;
  board: DeskBoard | null;
  /** When the board was read (ms since the epoch). */
  boardAt: number | null;
  /** Why the last board read failed, while there is no board to show. */
  boardError: DeskNotice | null;
  entries: DeskEntry[];
  draft: string;
  /** The agent a prefilled question is about; dropped when the user edits the question. */
  draftContext: AskContext | null;
  draftSource: AskSource;
  asking: boolean;
  notice: DeskNotice | null;
  /** The dragged height in px, or null for the default (44vh). */
  height: number | null;
  nextId: number;
}

export type DeskAction =
  | ({ type: 'open' } & OpenRequest)
  | { type: 'expand' }
  | { type: 'collapse' }
  | { type: 'close' }
  | { type: 'draft'; text: string }
  | { type: 'submit' }
  | DeskStreamEvent
  /** The ask failed. `id` names its question: a late failure of an older stream changes nothing. */
  | { type: 'failed'; id?: number; notice: DeskNotice }
  | { type: 'aborted'; id?: number }
  | { type: 'board'; board: DeskBoard; at: number }
  | { type: 'boardFailed'; notice: DeskNotice }
  | { type: 'height'; height: number }
  | { type: 'reset' };

export function initialDeskState({ closed = false, height = null }: { closed?: boolean; height?: number | null } = {}): DeskState {
  return {
    mode: closed ? 'hidden' : 'collapsed',
    board: null,
    boardAt: null,
    boardError: null,
    entries: [],
    draft: '',
    draftContext: null,
    draftSource: 'prompt',
    asking: false,
    notice: null,
    height,
    nextId: 1,
  };
}

/** Why a question can't be asked now (null: it can). */
export function askRefusal(state: Pick<DeskState, 'asking' | 'notice'>, question: string): 'asking' | 'blank' | 'too_long' | 'blocked' | null {
  if (state.asking) return 'asking';
  const q = question.trim();
  if (!q) return 'blank';
  if (countChars(q) > MAX_QUESTION_CHARS) return 'too_long';
  if (blocksAsking(state.notice)) return 'blocked';
  return null;
}

function startAsk(state: DeskState, question: string, context: AskContext | null, source: AskSource): DeskState {
  const entry: DeskEntry = { id: state.nextId, question: question.trim(), context, source, reads: [], done: false };
  return {
    ...state,
    entries: [...state.entries, entry],
    nextId: state.nextId + 1,
    asking: true,
    draft: '',
    draftContext: null,
    draftSource: 'prompt',
    // A notice from the last failure is stale once a new question goes out.
    notice: blocksAsking(state.notice) ? state.notice : null,
  };
}

/** Apply `update` to the question in flight (the one `id` names, when given); nothing when none is. */
function inFlight(state: DeskState, update: (entry: DeskEntry) => DeskEntry, extra: Partial<DeskState> = {}, id?: number): DeskState {
  const last = state.entries[state.entries.length - 1];
  if (!last || last.done || (id !== undefined && last.id !== id)) return state;
  return { ...state, ...extra, entries: [...state.entries.slice(0, -1), update(last)] };
}

/**
 * The whole desk, as a pure state machine (plan §1.5). Side effects (the
 * stream, storage, focus) belong to the provider; this only says what the
 * desk shows. There is no action that confirms or sends anything: the only
 * thing Enter does is `submit`, which asks a question.
 */
export function deskReducer(state: DeskState, action: DeskAction): DeskState {
  switch (action.type) {
    case 'open': {
      const opened: DeskState = { ...state, mode: 'open' };
      const context = action.agentId ? { agent_id: canonicalAgentId(action.agentId) } : null;
      // A bare open (the palette's `?`, the bar) keeps whatever the prompt holds.
      if (!action.question) return opened;
      if (action.ask && askRefusal(state, action.question) === null) return startAsk(opened, action.question, context, action.source);
      // Not asked (or it can't be yet): the question waits in the prompt, with its agent.
      return { ...opened, draft: action.question, draftContext: context, draftSource: action.source };
    }
    case 'expand':
      return state.mode === 'open' ? state : { ...state, mode: 'open' };
    case 'collapse':
      return state.mode === 'open' ? { ...state, mode: 'collapsed' } : state;
    case 'close':
      return { ...state, mode: 'hidden' };
    case 'draft':
      return action.text !== state.draft
        ? { ...state, draft: action.text, draftContext: null, draftSource: 'prompt' }
        : { ...state, draft: action.text };
    case 'submit':
      if (askRefusal(state, state.draft) !== null) return state;
      return startAsk(state, state.draft, state.draftContext, state.draftSource);
    case 'read':
      return inFlight(state, (e) => ({ ...e, reads: [...e.reads.filter((r) => r.id !== action.data.id), action.data] }));
    case 'answer':
      return inFlight(state, (e) => ({ ...e, answer: action.data }));
    case 'error':
      return inFlight(state, (e) => ({ ...e, error: { message: action.data.message, retryable: action.data.retryable } }));
    case 'usage': {
      const usage = action.data;
      const board = state.board ? { ...state.board, asks: { ...state.board.asks, used: usage.asks_today, limit: usage.asks_limit } } : null;
      const limited = usage.asks_today >= usage.asks_limit;
      return inFlight(state, (e) => ({ ...e, usage }), {
        board,
        notice: limited ? { state: 'limited_day', message: dailyLimitText(usage.asks_limit) } : state.notice,
      });
    }
    case 'done':
      return inFlight(state, (e) => ({ ...e, done: true }), { asking: false });
    case 'failed':
      return inFlight(
        state,
        (e) => ({ ...e, error: { message: action.notice.message, retryable: !blocksAsking(action.notice) }, done: true }),
        { asking: false, notice: action.notice },
        action.id,
      );
    case 'aborted':
      return inFlight(state, (e) => ({ ...e, done: true, stopped: true }), { asking: false }, action.id);
    case 'board': {
      const fromBoard = boardNotice(action.board);
      // The board is the fresher word on the model; other notices stand.
      const keep = state.notice && state.notice.state !== 'offline' && state.notice.state !== 'limited_day' ? state.notice : null;
      return { ...state, board: action.board, boardAt: action.at, boardError: null, notice: fromBoard ?? keep };
    }
    case 'boardFailed':
      return { ...state, boardError: action.notice, notice: blocksAsking(action.notice) ? action.notice : state.notice };
    case 'height':
      return { ...state, height: Math.round(action.height) };
    case 'reset':
      return { ...state, entries: [], draft: '', draftContext: null, draftSource: 'prompt', asking: false, notice: blocksAsking(state.notice) ? state.notice : null };
    default:
      return state;
  }
}

/**
 * The turns sent as `history`: the last six answered questions of this
 * conversation, oldest first, clipped to the server's limits (1,000 and 600
 * code points). A failed or stopped question isn't a turn.
 */
export function trimHistory(entries: readonly DeskEntry[]): HistoryTurn[] {
  return entries
    .filter((e): e is DeskEntry & { answer: AnswerEvent } =>
      e.done && !!e.answer && !e.error && !(e.usage && e.usage.input_redactions > 0),
    )
    .slice(-MAX_HISTORY_TURNS)
    .map((e) => ({ question: clipChars(e.question, MAX_QUESTION_CHARS), answer: clipChars(e.answer.text, MAX_HISTORY_ANSWER_CHARS) }));
}

/** The question in flight (asked, not done), if any. */
export function pendingEntry(state: Pick<DeskState, 'asking' | 'entries'>): DeskEntry | null {
  const last = state.entries[state.entries.length - 1];
  return state.asking && last && !last.done ? last : null;
}

/** The request for a question in flight: its entry, the conversation and the turns before it. */
export function askRequest(state: Pick<DeskState, 'entries'>, entry: DeskEntry, conv: string): AskRequest {
  return {
    question: entry.question,
    conv,
    history: trimHistory(state.entries.filter((e) => e.id !== entry.id)),
    context: entry.context,
    source: entry.source,
  };
}
