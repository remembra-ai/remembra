/// <reference types="node" />
// Live check of the Marshal desk against a real Remembra server (the real
// FastAPI app, SQLite ledger, JWT auth and the desk's model pointed at a local
// OpenAI-compatible stub, never the real API). Driven by
// tests/test_marshal_desk_dashboard_live.py, which starts the server and the
// stub, seeds an account and sets the MARSHAL_LIVE_* variables (and
// VITE_API_URL, so the client's API_V1 is the live server). Skipped otherwise.
//
// It runs exactly what the browser runs: loadDeskSettings, getBoard, the why?
// slip's prefilled question, the reducer, runAsk over the real SSE stream,
// the follow-up with its history, and the desk rendered from the state it
// reached. One step per run (MARSHAL_LIVE_STEP), because the driver changes
// the ledger between them:
//   ask      settings, board, a streamed answer and a follow-up, the opt-out
//            and back; the other account and an API key are refused
//   offline  the platform's day is spent: the board says so and an ask is
//            refused before any model call
//   limit    asks until the account's daily limit refuses one

import { writeFileSync } from 'node:fs';
import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it, vi } from 'vitest';
import { DeskView } from '../../components/marshal/MarshalDesk';
import { API_V1 } from '../../config';
import { api } from '../api';
import {
  DESK_COPY,
  DeskError,
  SERVER_COPY,
  askMarshal,
  askRefusal,
  askRequest,
  dailyLimitText,
  deskErrorState,
  deskReducer,
  deskToggle,
  footerText,
  getBoard,
  heightBounds,
  initialDeskState,
  loadDeskSettings,
  newConversationId,
  noticeFor,
  pendingEntry,
  runAsk,
  setDeskSettings,
  slipAsk,
  type DeskAction,
  type DeskState,
  type SettingsResult,
} from '../marshalDesk';

const URL_ = process.env.MARSHAL_LIVE_URL ?? '';
const JWT = process.env.MARSHAL_LIVE_JWT ?? '';
const JWT_OTHER = process.env.MARSHAL_LIVE_JWT_OTHER ?? '';
const API_KEY = process.env.MARSHAL_LIVE_API_KEY ?? '';
const STEP = process.env.MARSHAL_LIVE_STEP ?? '';
const OUT = process.env.MARSHAL_LIVE_OUT ?? '';
/** How long the slowest model call took on purpose (the stub's delay): the first read must beat the answer by most of it. */
const MODEL_DELAY_MS = Number(process.env.MARSHAL_LIVE_MODEL_DELAY_MS ?? '0');

const noop = () => {};

function signIn(jwt: string) {
  const store = new Map<string, string>();
  vi.stubGlobal('localStorage', {
    getItem: (k: string) => store.get(k) ?? null,
    setItem: (k: string, v: string) => void store.set(k, v),
    removeItem: (k: string) => void store.delete(k),
  });
  api.clearAll();
  api.setJwtToken(jwt);
}

function settingsOf(): Promise<SettingsResult> {
  return new Promise((resolve) => loadDeskSettings(resolve, { retries: 0 }));
}

/** The provider's reducer and ask effect, without React: every action is applied and logged with its arrival time. */
class Desk {
  state: DeskState = initialDeskState();
  log: { type: string; at: number }[] = [];
  readonly conv = newConversationId();

  dispatch = (action: DeskAction) => {
    this.log.push({ type: action.type, at: Date.now() });
    this.state = deskReducer(this.state, action);
  };

  /** What the provider's effect does once a question is in flight. */
  async runPending(): Promise<void> {
    const entry = pendingEntry(this.state);
    if (!entry) throw new Error('nothing in flight');
    const run = runAsk({ request: askRequest(this.state, entry, this.conv), id: entry.id, dispatch: this.dispatch });
    await run.finished;
  }

  async ask(question: string): Promise<void> {
    this.dispatch({ type: 'open', question, ask: true, source: 'prompt' });
    await this.runPending();
  }

  html(): string {
    return renderToStaticMarkup(
      <DeskView
        state={this.state}
        sheet={false}
        height={440}
        bounds={heightBounds(1000)}
        bodyId="desk-body"
        promptId="desk-prompt"
        onToggle={noop}
        onClose={noop}
        onResize={noop}
        onDraft={noop}
        onSubmit={noop}
        onAsk={noop}
        onNewConversation={noop}
      />,
    );
  }
}

/** Visible text: tags stripped, entities decoded, whitespace collapsed. */
function visible(html: string): string {
  return html
    .replace(/<[^>]+>/g, ' ')
    .replace(/&#x27;|&#39;/g, "'")
    .replace(/&quot;/g, '"')
    .replace(/&lt;/g, '<')
    .replace(/&gt;/g, '>')
    .replace(/&amp;/g, '&')
    .replace(/\s+/g, ' ')
    .trim();
}

async function refusal(question: string): Promise<DeskError> {
  try {
    await askMarshal({ question, conv: newConversationId(), history: [], context: null, source: 'prompt' }, { onEvent: noop });
  } catch (err) {
    if (err instanceof DeskError) return err;
    throw err;
  }
  throw new Error('the ask was not refused');
}

function report(data: Record<string, unknown>) {
  if (OUT) writeFileSync(OUT, JSON.stringify(data, null, 2));
}

describe.skipIf(!URL_)('live Marshal desk', () => {
  it.runIf(STEP === 'ask')(
    'streams a why? answer with its evidence and cost, answers a follow-up, and refuses everyone else',
    async () => {
      expect(API_V1).toBe(`${URL_}/api/v1`); // the client under test talks to the live server
      signIn(JWT);
      expect(api.getAuthMode()).toBe('jwt');
      expect(await settingsOf()).toEqual({ status: 'ready', desk: true });

      const desk = new Desk();
      desk.dispatch({ type: 'expand' });
      const board = await getBoard();
      desk.dispatch({ type: 'board', board, at: Date.now() });
      expect(board.model.state).toBe('ready');
      const codexCall = board.calls.find((c) => c.agent_id === 'codex');
      expect(codexCall).toBeDefined();

      // The why? slip: prefilled, scoped to codex, sent by Enter (submit).
      desk.dispatch({ type: 'open', ...slipAsk('codex') });
      expect(desk.state.entries).toHaveLength(0);
      expect(desk.state.draft).toBe('why is codex waiting');
      desk.dispatch({ type: 'submit' });
      const started = Date.now();
      await desk.runPending();

      const first = desk.state.entries[0];
      expect(first.done).toBe(true);
      expect(first.error).toBeUndefined();
      expect(first.reads.length).toBeGreaterThanOrEqual(1);
      expect(first.reads.every((r) => r.ok)).toBe(true);
      expect(first.answer?.fallback).toBe(false);
      expect(first.answer?.evidence.length).toBeGreaterThanOrEqual(1);
      expect(first.usage?.billed_to_credits).toBe(false);
      expect(first.usage?.reads).toBe(first.reads.length);
      const order = desk.log.filter((l) => l.at >= started).map((l) => l.type);
      expect(order.slice(-3)).toEqual(['answer', 'usage', 'done']);
      expect(order.indexOf('read')).toBeLessThan(order.indexOf('answer'));
      const firstRead = desk.log.find((l) => l.type === 'read' && l.at >= started)!;
      const answered = desk.log.find((l) => l.type === 'answer' && l.at >= started)!;
      // Each read line reached the page as it happened, not with the answer.
      if (MODEL_DELAY_MS > 0) expect(answered.at - firstRead.at).toBeGreaterThanOrEqual(MODEL_DELAY_MS * 0.8);

      const html = desk.html();
      const text = visible(html);
      for (const read of first.reads) expect(text).toContain(`${read.label} · ${read.summary}`);
      expect(text).toContain(first.answer!.text);
      for (const [i, ref] of first.answer!.evidence.entries()) expect(text).toContain(`[${i + 1}] ${ref.label}`);
      expect(text).toContain(DESK_COPY.caption);
      for (const command of first.answer!.commands) expect(text).toContain(command.text);
      expect(text).toContain(footerText(first.usage!));
      expect(text).toContain(DESK_COPY.notBilled);

      // A follow-up carries the answered turn as history; nothing is stored on the server for it.
      const followUp = askRequest(desk.state, { ...first, id: 99 }, desk.conv);
      expect(followUp.history).toEqual([{ question: 'why is codex waiting', answer: first.answer!.text }]);
      await desk.ask('what did claude-code hand off last');
      const second = desk.state.entries[1];
      expect(second.done).toBe(true);
      expect(second.error).toBeUndefined();
      expect(second.answer).toBeDefined();
      expect(second.usage?.asks_today).toBe((first.usage?.asks_today ?? 0) + 1);

      // The opt-out (Settings > Diagnostics), the one thing the desk writes: saved first, then applied.
      let applied: boolean | null = null;
      const toggle = deskToggle(setDeskSettings, (desk) => {
        applied = desk;
      });
      expect(await toggle(false)).toEqual({ desk: false });
      expect(applied).toBe(false);
      expect(await settingsOf()).toEqual({ status: 'ready', desk: false });
      const optedOut = await getBoard().catch((e: unknown) => e);
      expect(noticeFor(optedOut)).toEqual({ state: 'opted_out', message: SERVER_COPY.optedOut });
      const optedOutAsk = await refusal('why is codex waiting');
      expect([optedOutAsk.status, optedOutAsk.code]).toEqual([403, 'marshal_opted_out']);
      expect(await toggle(true)).toEqual({ desk: true });
      await expect(getBoard()).resolves.toMatchObject({ footer: board.footer }); // on again

      // Another account (not on the allow-list): the desk isn't there, and its routes say so.
      signIn(JWT_OTHER);
      expect(await settingsOf()).toEqual({ status: 'off' });
      const hidden = await getBoard().catch((e: unknown) => e);
      expect(hidden).toBeInstanceOf(DeskError);
      expect([(hidden as DeskError).status, (hidden as DeskError).code, deskErrorState(hidden)]).toEqual([404, 'marshal_unavailable', 'unavailable']);
      const otherAsk = await refusal('why is codex waiting');
      expect([otherAsk.status, otherAsk.code]).toEqual([404, 'marshal_unavailable']);

      // An API key: the dashboard never sends one to the desk, and the server refuses one sent anyway.
      api.clearAll();
      api.setApiKey(API_KEY);
      expect(api.getAuthMode()).toBe('api_key');
      const keyed = await fetch(`${API_V1}/marshal/ask`, {
        method: 'POST',
        headers: { 'X-API-Key': API_KEY, 'Content-Type': 'application/json', Accept: 'text/event-stream' },
        body: JSON.stringify({ question: 'why is codex waiting', conv: newConversationId(), history: [], context: null, source: 'prompt' }),
      });
      const keyedBody = (await keyed.json()) as { detail: { error: string } };
      const keyedBoard = await fetch(`${API_V1}/marshal/board`, { headers: { 'X-API-Key': API_KEY } });

      report({
        board: { status_line: board.status_line, calls: board.calls, model: board.model, asks: board.asks },
        first: {
          reads: first.reads.map((r) => ({ tool: r.tool, label: r.label, summary: r.summary, ok: r.ok })),
          answer: first.answer,
          usage: first.usage,
          read_to_answer_ms: answered.at - firstRead.at,
          events: order,
        },
        second: { history_sent: followUp.history.length, answer: second.answer?.text, usage: second.usage },
        rendered_footer: footerText(first.usage!),
        opted_out: { board: (optedOut as DeskError).status, ask: optedOutAsk.status },
        other_account: { settings: 'off', board: (hidden as DeskError).status, ask: otherAsk.status },
        api_key: { ask: keyed.status, ask_error: keyedBody.detail.error, board: keyedBoard.status },
      });
      expect([keyed.status, keyedBody.detail.error]).toEqual([403, 'marshal_login_required']);
      expect(keyedBoard.status).toBe(403);
      vi.unstubAllGlobals();
    },
    60_000,
  );

  it.runIf(STEP === 'offline')(
    "shows the model off for today and refuses an ask when the platform's day is spent",
    async () => {
      signIn(JWT);
      const desk = new Desk();
      const board = await getBoard();
      desk.dispatch({ type: 'board', board, at: Date.now() });
      expect(board.model).toMatchObject({ state: 'offline', reason: 'daily_budget' });
      expect(desk.state.notice?.state).toBe('offline');
      // The prompt is blocked: Enter asks nothing.
      expect(askRefusal(desk.state, 'why is codex waiting')).toBe('blocked');
      desk.dispatch({ type: 'open', question: 'why is codex waiting', ask: true, source: 'prompt' });
      expect(desk.state.entries).toHaveLength(0);
      expect(visible(desk.html())).toContain("Marshal's model is off for today. Rules-only checks still work.");
      // And the server refuses one sent anyway, before the stream (and before any model call).
      const refused = await refusal('why is codex waiting');
      expect([refused.status, refused.code, refused.data?.reason]).toEqual([503, 'marshal_offline', 'daily_budget']);
      expect(noticeFor(refused)).toEqual({ state: 'offline', message: "Marshal's model is off for today. Rules-only checks still work." });
      report({ board_model: board.model, refused: { status: refused.status, code: refused.code, data: refused.data } });
      vi.unstubAllGlobals();
    },
    60_000,
  );

  it.runIf(STEP === 'limit')(
    "asks until the account's daily limit, then the desk blocks and the server refuses the next",
    async () => {
      signIn(JWT);
      const desk = new Desk();
      let answered = 0;
      let minuteWaits = 0;
      for (let n = 0; n < 60; n += 1) {
        if (askRefusal(desk.state, 'why is codex waiting') === 'blocked') break;
        desk.dispatch({ type: 'reset' }); // a fresh conversation each time: the history stays empty
        await desk.ask('why is codex waiting');
        const entry = desk.state.entries[desk.state.entries.length - 1];
        if (entry.error && desk.state.notice?.state === 'limited_minute') {
          // The route's 20 a minute (slowapi): wait the minute out and ask again.
          minuteWaits += 1;
          await new Promise((r) => setTimeout(r, 61_000));
          continue;
        }
        expect(entry.error).toBeUndefined();
        answered += 1;
      }
      const last = desk.state.entries[desk.state.entries.length - 1];
      expect(last.usage?.asks_today).toBe(last.usage?.asks_limit);
      // The last answer's usage says the day is used up: the desk blocks the prompt itself.
      expect(desk.state.notice).toEqual({ state: 'limited_day', message: dailyLimitText(last.usage!.asks_limit) });
      // The server refuses the next one sent anyway.
      const refused = await refusal('why is codex waiting');
      expect([refused.status, refused.code, refused.data?.limit, refused.data?.used]).toEqual([
        429,
        'marshal_daily_limit',
        last.usage!.asks_limit,
        last.usage!.asks_limit,
      ]);
      expect(refused.message).toBe(dailyLimitText(last.usage!.asks_limit));
      expect(deskErrorState(refused)).toBe('limited_day');
      report({ answered, minute_waits: minuteWaits, asks_today: last.usage!.asks_today, refused: { status: refused.status, code: refused.code, data: refused.data, message: refused.message } });
      vi.unstubAllGlobals();
    },
    300_000,
  );
});
