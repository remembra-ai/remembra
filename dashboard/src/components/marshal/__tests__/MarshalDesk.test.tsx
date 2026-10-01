// Server-render the real desk (no DOM needed) in every state it has: the bar,
// the board, reading, example A answered, example B's validation fallback,
// the error ending and each notice; the sheet and the dock; and walk the
// prompt's own element tree to prove Enter asks and does nothing else.

import { readFileSync, readdirSync } from 'node:fs';
import { isValidElement, type ReactElement, type ReactNode } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it, vi } from 'vitest';
import {
  MARSHAL_DESK_EMPTY,
  MARSHAL_DESK_OFF,
  MarshalDeskContext,
  MarshalDeskStateContext,
  useMarshalDesk,
  useMarshalDeskState,
  type MarshalDeskApi,
} from '../../../hooks/marshalDesk';
import {
  DESK_COPY,
  SERVER_COPY,
  dailyLimitText,
  deskReducer,
  heightBounds,
  initialDeskState,
  type AnswerEvent,
  type DeskAction,
  type DeskBoard,
  type DeskState,
  type DeskStreamEvent,
} from '../../../lib/marshalDesk';
import { DeskHandle } from '../DeskHandle';
import { MarshalDeskProvider } from '../DeskProvider';
import { DeskView, MarshalDesk, type DeskViewProps } from '../MarshalDesk';
import { PromptLine } from '../PromptLine';

interface ContractEvent {
  event: string;
  data: Record<string, unknown>;
}

const CONTRACT = JSON.parse(
  readFileSync(new URL('../../../lib/__tests__/fixtures/marshal_desk_contract.json', import.meta.url), 'utf8'),
) as {
  board: DeskBoard;
  stream_a: { events: ContractEvent[]; footer_rendered: string };
  stream_b: { events: ContractEvent[] };
  error_tail: { events: ContractEvent[] };
  sse_error_messages: Record<string, string>;
};

const events = (list: ContractEvent[]) => list.map((e) => ({ type: e.event, data: e.data }) as unknown as DeskStreamEvent);
const run = (state: DeskState, ...actions: DeskAction[]) => actions.reduce(deskReducer, state);
const noop = () => {};

function view(state: DeskState, over: Partial<DeskViewProps> = {}): string {
  return renderToStaticMarkup(
    <DeskView
      state={state}
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
      {...over}
    />,
  );
}

/** The prompt is off: read-only and announced as disabled (never `disabled`, which would drop focus). */
const PROMPT_OFF = /<input[^>]*readOnly=""[^>]*aria-label="Ask Marshal"[^>]*aria-disabled="true"/;

/** Visible text (tags stripped, entities decoded, whitespace collapsed). */
function text(html: string): string {
  return html
    .replace(/<[^>]+>/g, ' ')
    .replace(/&#x27;/g, "'")
    .replace(/&quot;/g, '"')
    .replace(/&gt;/g, '>')
    .replace(/&lt;/g, '<')
    .replace(/&amp;/g, '&')
    .replace(/\s+/g, ' ')
    .trim();
}

const withBoard = run(initialDeskState(), { type: 'board', board: CONTRACT.board, at: 1 });
const opened = run(withBoard, { type: 'expand' });
const askA = (state: DeskState) => run(state, { type: 'open', question: 'why is codex waiting', agentId: 'codex', ask: true, source: 'why_slip' });
const answeredA = run(askA(opened), ...events(CONTRACT.stream_a.events));
const answeredB = run(opened, { type: 'open', question: 'how much is solo a month', ask: true, source: 'palette' }, ...events(CONTRACT.stream_b.events));

describe('the bar', () => {
  it('is a 32px rr-win-bar with the signal square, the status line and the toggle', () => {
    const html = view(withBoard);
    expect(html).toMatch(/<div class="rr-win-bar h-8 /);
    expect(html).toContain('<i aria-hidden="true"></i>');
    expect(text(html)).toContain(`marshal ${CONTRACT.board.status_line}`);
    expect(html).toContain('aria-expanded="false" aria-controls="desk-body"');
    expect(html).toContain('aria-label="Close Marshal"');
    // Folded: no body, no prompt.
    expect(html).not.toContain('id="desk-body"');
    expect(html).not.toContain('Ask Marshal');
  });

  it('says reading… until the board is in, and nothing when the board could not be read', () => {
    expect(text(view(initialDeskState()))).toContain(`marshal ${DESK_COPY.reading}`);
    const failed = run(initialDeskState(), { type: 'boardFailed', notice: { state: 'unreachable', message: DESK_COPY.unreachable } });
    expect(text(view(failed))).not.toContain(DESK_COPY.reading);
  });

  it('opens as a region the transcript lives in, with the handle to resize it', () => {
    const html = view(opened);
    expect(html).toContain('role="region" aria-label="Marshal"');
    expect(html).toContain('aria-expanded="true" aria-controls="desk-body"');
    expect(html).toContain('id="desk-body"');
    expect(html).toContain('style="height:440px"');
    expect(html).toContain('role="separator" aria-orientation="horizontal" aria-label="Resize Marshal" aria-valuemin="160" aria-valuemax="700" aria-valuenow="440"');
    expect(html).toContain('data-desk="dock"');
  });
});

describe('the board (what the desk opens on)', () => {
  it('shows the rules calls with see why, the questions, and the footer; no greeting', () => {
    const html = view(opened);
    const t = text(html);
    expect(t).toContain('codex The key works, but nothing from Codex has reached Remembra. [??] (inferred, not proven) see why');
    expect(t).toContain('why is codex waiting what did claude-code hand off last');
    expect(t).toContain(CONTRACT.board.footer);
    expect(t).not.toMatch(/\bhello\b|\bhi\b|welcome|how can/i);
    expect(html).not.toContain('role="log"');
  });

  it('with no calls it shows the status line; a proven call is [!!]', () => {
    const quiet = run(initialDeskState(), { type: 'board', board: { ...CONTRACT.board, calls: [], status_line: 'every agent handing off' }, at: 1 }, { type: 'expand' });
    expect(text(view(quiet))).toContain('● every agent handing off');
    const proven = run(
      initialDeskState(),
      { type: 'board', board: { ...CONTRACT.board, calls: [{ agent_id: null, code: 'KEY_MISSING', proven: true, text: 'No API key active on your account.', ask: "why can't my agents reach Remembra" }] }, at: 1 },
      { type: 'expand' },
    );
    expect(text(view(proven))).toContain('account No API key active on your account. [!!] (shown by your data) see why');
  });

  it('reads the board first: › reading…', () => {
    expect(text(view(run(initialDeskState(), { type: 'expand' })))).toContain(`› ${DESK_COPY.reading}`);
  });
});

describe('the transcript', () => {
  it('while reading: the question, each read with its ms, then › reading…', () => {
    const reading = run(askA(opened), events(CONTRACT.stream_a.events)[0]);
    const html = view(reading);
    const t = text(html);
    expect(html).toContain('role="log" aria-live="polite" aria-relevant="additions"');
    expect(t).toContain('? Asked: why is codex waiting');
    expect(t).toContain('› trail/diagnosis · codex · CODEX_TRUST_MISSING · inferred · 41ms');
    expect(t).toContain(`› ${DESK_COPY.reading}`);
    expect(html).toContain('rr-win-line');
    // The prompt is read-only while a question is out (focus stays; Enter does nothing).
    expect(html).toMatch(PROMPT_OFF);
  });

  it('example A: reads in order, the call as text, chips [1] [2], the commands captioned, > then $, and the footer', () => {
    const html = view(answeredA);
    const t = text(html);
    const reads = ['› trail/diagnosis · codex · CODEX_TRUST_MISSING · inferred · 41ms', '› trail/summary · 1 agent · claude-code 9 handoffs · newest 2h ago · 7d · 38ms'];
    expect(reads.map((r) => t.indexOf(r)).every((i, n, all) => i >= 0 && (n === 0 || i > all[n - 1]))).toBe(true);
    expect(t).not.toContain(DESK_COPY.reading);
    expect(t).toContain(CONTRACT.stream_a.events[2].data.text as string);
    expect(t).toContain('[1] trail/diagnosis · codex · CODEX_TRUST_MISSING · inferred');
    expect(t).toContain('[2] trail/summary · 1 agent · claude-code 9 handoffs · newest 2h ago · 7d');
    expect(html).toContain('data-anchor="agent:codex"'); // chip [1] can outline the Codex row
    expect(t).toContain(DESK_COPY.caption);
    const prompts = [...html.matchAll(/<span aria-hidden="true" class="select-none text-signal">([^<]*)<\/span>/g)].map((m) => m[1]);
    expect(prompts).toEqual(['&gt;', '$']);
    const copied = [...html.matchAll(/<span class="whitespace-pre">([^<]*)<\/span>/g)].map((m) => m[1]);
    expect(copied).toEqual(['/hooks', 'remembra-relay doctor --agent codex']);
    expect(t).toContain(CONTRACT.stream_a.footer_rendered);
    expect(t).toContain('new conversation');
    expect(t).not.toContain(DESK_COPY.redacted);
    // No filled signal button anywhere: nothing on the desk sends or confirms.
    expect(html).not.toContain('rr-btn-primary');
  });

  it('example B: the read lines, "That\'s all I can confirm." and a person to ask', () => {
    const html = view(answeredB);
    const t = text(html);
    expect(t).toContain('cloud/plan · free · keys 2 of 3 · create_key allowed');
    expect(t).toContain("That's all I can confirm.");
    expect(t).toContain(DESK_COPY.fallbackDoc);
    expect(html).toContain('href="https://remembra.dev/contact"');
    expect(t).toContain('gpt-4o-mini · 1 read · <$0.001 · not billed to your credits');
    expect(t).not.toContain('$9');
  });

  it('a quoted price: the quote, then "From remembra.dev pages; the page governs." with its page', () => {
    const doc = 'https://docs.remembra.dev/reference/plans-and-credits/#plans';
    const answer = { ...(CONTRACT.stream_a.events[2].data as unknown as AnswerEvent), text: 'Solo: "$12 / month or $120 / year".', commands: [], doc };
    const tail = [{ event: 'answer', data: answer as unknown as Record<string, unknown> }, ...CONTRACT.stream_a.events.slice(3)];
    const html = view(run(askA(opened), ...events([...CONTRACT.stream_a.events.slice(0, 2), ...tail])));
    const t = text(html);
    expect(t).toContain(`${DESK_COPY.pagesGovern} · docs.remembra.dev/reference/plans-and-credits/#plans`);
    expect(html).toContain(`href="${doc}"`);
    expect(t).not.toContain(DESK_COPY.fallbackDoc);
    // A page that isn't Remembra's is never linked, and example A (no page) has no such line.
    const foreign = { ...answer, doc: 'https://evil.example/pricing' };
    const shown = view(run(askA(opened), ...events([...CONTRACT.stream_a.events.slice(0, 2), { event: 'answer', data: foreign as unknown as Record<string, unknown> }, ...CONTRACT.stream_a.events.slice(3)])));
    expect(shown).not.toContain('evil.example');
    expect(text(view(answeredA))).not.toContain(DESK_COPY.pagesGovern);
  });

  it('the error ending: the server sentence, its usage, and ask again', () => {
    const html = view(run(askA(opened), ...events([CONTRACT.stream_a.events[0], ...CONTRACT.error_tail.events])));
    const t = text(html);
    expect(t).toContain(CONTRACT.sse_error_messages.model_unavailable);
    expect(t).toContain('gpt-4o-mini · 1 read · $0.000 · not billed to your credits');
    expect(t).toContain('ask again');
  });

  it('a key held in the question is said to be removed', () => {
    const usage = { ...(CONTRACT.stream_a.events[3].data as Record<string, unknown>), input_redactions: 1 };
    const html = view(run(askA(opened), ...events([...CONTRACT.stream_a.events.slice(0, 3), { event: 'usage', data: usage }, CONTRACT.stream_a.events[4]])));
    expect(text(html)).toContain(`› ${DESK_COPY.redacted}`);
  });

  it('a question closed mid-answer is shown as stopped', () => {
    const html = view(run(askA(opened), events(CONTRACT.stream_a.events)[0], { type: 'aborted', id: 1 }));
    expect(text(html)).toContain('› stopped before the answer');
  });

  it('model text is text: markup, script and foreign links never render as HTML', () => {
    const hostile = run(askA(opened), events(CONTRACT.stream_a.events)[0], {
      type: 'answer',
      data: {
        ...(CONTRACT.stream_a.events[2].data as unknown as AnswerEvent),
        text: '<script>alert(1)</script> <img src=x onerror=alert(1)> [docs](javascript:alert(1)) ![p](https://evil.example/p.png) see https://docs.remembra.dev/guides/relay/',
      },
    });
    const html = view(hostile);
    expect(html).not.toMatch(/<script|<img|href="javascript:|evil\.example\/p\.png/);
    expect(html).toContain('&lt;script&gt;alert(1)&lt;/script&gt;');
    expect(html).toContain('[image removed: evil.example]');
    expect(html).toContain('href="https://docs.remembra.dev/guides/relay/" target="_blank" rel="noopener noreferrer"');
  });
});

describe('notices', () => {
  const asked = askA(opened);

  it('offline (from the board): the board stays, the notice says so, the prompt is off', () => {
    const offline = run(opened, { type: 'board', board: { ...CONTRACT.board, model: { ...CONTRACT.board.model, state: 'offline', reason: 'breaker_open' } }, at: 2 });
    const html = view(offline);
    expect(text(html)).toContain(CONTRACT.board.footer);
    expect(html).toContain('role="status" data-notice="offline"');
    expect(text(html)).toContain(SERVER_COPY.offlineToday);
    expect(html).toMatch(PROMPT_OFF);
    expect(html).not.toMatch(/<input[^>]*\sdisabled=""/);
    expect(html).toMatch(/<button type="button" disabled="" class="rr-btn-ghost[^"]*">see why<\/button>/);
  });

  it('limited_day: the day\'s limit, the prompt off', () => {
    const limited = run(asked, { type: 'failed', id: 1, notice: { state: 'limited_day', message: dailyLimitText(40) } });
    const html = view(limited);
    expect(text(html)).toContain('40 questions today is the limit. Rules-only checks still work.');
    expect(html).toMatch(PROMPT_OFF);
    // Said once, under the question it refused.
    expect(text(html).split('40 questions today').length - 1).toBe(1);
    const fromBoard = view(run(opened, { type: 'board', board: { ...CONTRACT.board, model: { ...CONTRACT.board.model, state: 'limited', reason: 'daily_asks' } }, at: 3 }));
    expect(fromBoard).toContain('data-notice="limited_day"');
  });

  it('limited_minute: the per-minute sentence, and the prompt stays usable', () => {
    const html = view(run(asked, { type: 'failed', id: 1, notice: { state: 'limited_minute', message: DESK_COPY.limitedMinute } }));
    expect(text(html)).toContain(DESK_COPY.limitedMinute);
    expect(html).toMatch(/<button type="button" class="rr-btn-ghost[^"]*"><svg[^]*?<\/svg> ask again<\/button>/);
    expect(html).not.toMatch(PROMPT_OFF);
  });

  it('expired: sign in again, the prompt off', () => {
    const html = view(run(asked, { type: 'failed', id: 1, notice: { state: 'expired', message: DESK_COPY.expired } }));
    expect(text(html)).toContain(DESK_COPY.expired);
    expect(html).toMatch(PROMPT_OFF);
  });

  it('unreachable: the sentence under the question', () => {
    const html = view(run(asked, { type: 'failed', id: 1, notice: { state: 'unreachable', message: DESK_COPY.unreachable } }));
    expect(text(html)).toContain(DESK_COPY.unreachable);
  });

  it('while the model is off, an earlier error\'s ask again is off too', () => {
    const failed = run(asked, ...events([CONTRACT.stream_a.events[0], ...CONTRACT.error_tail.events]));
    const html = view(run(failed, { type: 'board', board: { ...CONTRACT.board, model: { ...CONTRACT.board.model, state: 'offline', reason: 'no_key' } }, at: 9 }));
    expect(html).toMatch(/<button type="button" disabled="" class="rr-btn-ghost[^"]*"><svg[^]*?<\/svg> ask again<\/button>/);
    expect(html).toMatch(PROMPT_OFF);
  });

  it('opted out (turned off in another tab): the server sentence, the prompt off', () => {
    const html = view(run(opened, { type: 'boardFailed', notice: { state: 'opted_out', message: SERVER_COPY.optedOut } }));
    expect(text(html)).toContain(SERVER_COPY.optedOut);
    expect(html).toContain('data-notice="opted_out"');
    expect(html).toMatch(PROMPT_OFF);
  });
});

describe('phone width', () => {
  it('folded, the bar floats above the tab bar with a 16px gutter', () => {
    const html = view(withBoard, { sheet: true });
    expect(html).toContain('data-desk="bar"');
    expect(html).toContain('fixed inset-x-4 bottom-[calc(76px+env(safe-area-inset-bottom))]');
  });

  it('open, it is a full-height sheet with no resize handle', () => {
    const html = view(opened, { sheet: true });
    expect(html).toContain('data-desk="sheet"');
    expect(html).toContain('fixed inset-0 z-[70]');
    expect(html).not.toContain('role="separator"');
    expect(html).not.toContain('style="height');
  });
});

describe('mounting', () => {
  const desk = (over: Partial<MarshalDeskApi>, state?: DeskState) =>
    renderToStaticMarkup(
      <MarshalDeskContext.Provider value={{ ...MARSHAL_DESK_OFF, ...over }}>
        <MarshalDeskStateContext.Provider value={{ ...MARSHAL_DESK_EMPTY, ...(state ? { state } : {}) }}>
          <MarshalDesk />
        </MarshalDeskStateContext.Provider>
      </MarshalDeskContext.Provider>,
    );

  it('renders nothing when the desk is not there, opted out, or closed', () => {
    expect(desk({})).toBe('');
    expect(desk({ available: true, optedOut: true }, withBoard)).toBe('');
    expect(desk({ available: true }, { ...withBoard, mode: 'hidden' })).toBe('');
    // Outside the provider there is no desk at all.
    expect(renderToStaticMarkup(<MarshalDesk />)).toBe('');
  });

  it('renders the bar for an account that has it', () => {
    const html = desk({ available: true }, withBoard);
    expect(html).toContain('role="region" aria-label="Marshal"');
    expect(text(html)).toContain(CONTRACT.board.status_line);
  });

  it('the ways in carry no desk state, so typing or a streamed read re-renders the desk alone', () => {
    function Probe() {
      const ways = useMarshalDesk();
      const { state } = useMarshalDeskState();
      return <pre>{JSON.stringify({ keys: Object.keys(ways).sort(), available: ways.available, state })}</pre>;
    }
    // The API client reads its session from localStorage: an empty one here (no login).
    vi.stubGlobal('localStorage', { getItem: () => null, setItem: () => {}, removeItem: () => {} });
    let html = '';
    try {
      html = renderToStaticMarkup(
        <MarshalDeskProvider>
          <Probe />
        </MarshalDeskProvider>,
      );
    } finally {
      vi.unstubAllGlobals();
    }
    const seen = JSON.parse(text(html)) as { keys: string[]; available: boolean; state: DeskState };
    expect(seen.keys).toEqual(Object.keys(MARSHAL_DESK_OFF).sort());
    expect(seen.keys).not.toContain('state');
    expect(seen.keys).not.toContain('dispatch');
    // No dashboard login in this test: the desk isn't there, and nothing was asked.
    expect(seen.available).toBe(false);
    expect(seen.state).toEqual(initialDeskState());
    // Only the desk itself reads its state; the layout, the palette, the slips and Settings read the ways in.
    const src = new URL('../../../', import.meta.url);
    for (const file of ['components/AppLayout.tsx', 'components/CommandPalette.tsx', 'components/relay/WhySlip.tsx', 'components/marshal/MarshalDeskSetting.tsx']) {
      const code = readFileSync(new URL(file, src), 'utf8');
      expect(code, file).toContain('useMarshalDesk()');
      expect(code, file).not.toContain('useMarshalDeskState');
    }
  });
});

// ---------------------------------------------------------------------------
// Handlers, straight from the element tree
// ---------------------------------------------------------------------------

type AnyProps = Record<string, unknown> & { children?: ReactNode };

/** Every host element in a tree that has not been rendered (function components are not expanded). */
function elements(node: ReactNode): ReactElement<AnyProps>[] {
  if (Array.isArray(node)) return node.flatMap(elements);
  if (!isValidElement<AnyProps>(node)) return [];
  return [node, ...elements(node.props.children)];
}

describe('Enter never confirms anything', () => {
  function prompt(over: Partial<Parameters<typeof PromptLine>[0]> = {}) {
    const calls: string[] = [];
    const tree = PromptLine({
      id: 'p',
      draft: 'why is codex waiting',
      asking: false,
      blocked: false,
      onDraft: (t) => calls.push(`draft:${t}`),
      onSubmit: () => calls.push('submit'),
      ...over,
    });
    const all = elements(tree);
    const input = all.find((el) => el.type === 'input') as ReactElement<AnyProps>;
    const form = all.find((el) => el.type === 'form') as ReactElement<AnyProps>;
    const key = (k: string, extra: Record<string, unknown> = {}) => {
      let prevented = false;
      (input.props.onKeyDown as (e: unknown) => void)({
        key: k,
        keyCode: k === 'Enter' ? 13 : 0,
        nativeEvent: { isComposing: false },
        preventDefault: () => (prevented = true),
        ...extra,
      });
      return prevented;
    };
    return { calls, all, input, form, key };
  }

  it('Enter asks the question, once, and nothing else', () => {
    const p = prompt();
    expect(p.key('Enter')).toBe(true); // no implicit form submit on top of it
    expect(p.calls).toEqual(['submit']);
  });

  it('not while composing, not while a question is out, not while the model is off, not past 1,000', () => {
    const composing = prompt();
    composing.key('Enter', { nativeEvent: { isComposing: true } });
    expect(composing.calls).toEqual([]);
    for (const over of [{ asking: true }, { blocked: true }, { draft: 'x'.repeat(1001) }, { draft: '   ' }]) {
      const p = prompt(over);
      p.key('Enter');
      expect(p.calls, JSON.stringify(over).slice(0, 40)).toEqual([]);
    }
  });

  it('other keys do nothing; the ask button submits the same one question', () => {
    const p = prompt();
    expect(p.key('a')).toBe(false);
    expect(p.key(' ')).toBe(false);
    expect(p.calls).toEqual([]);
    (p.form.props.onSubmit as (e: unknown) => void)({ preventDefault: () => {} });
    expect(p.calls).toEqual(['submit']);
  });

  it('the prompt holds no control that sends, confirms or runs anything', () => {
    const p = prompt();
    const buttons = p.all.filter((el) => el.type === 'button');
    expect(buttons).toHaveLength(1);
    expect(buttons[0].props.type).toBe('submit');
    expect(buttons[0].props.children).toBe('ask');
    // And the reducer's `submit` only ever adds a question.
    const next = deskReducer({ ...initialDeskState(), draft: 'why' }, { type: 'submit' });
    expect(next.entries).toHaveLength(1);
    expect(Object.keys(next).sort()).toEqual(Object.keys(initialDeskState()).sort());
  });

  it('counts past 800 code points and flags past 1,000', () => {
    const html = renderToStaticMarkup(<PromptLine id="p" draft={'y'.repeat(1001)} asking={false} blocked={false} onDraft={noop} onSubmit={noop} />);
    expect(html).toContain('1001/1000');
    expect(html).toContain('aria-invalid="true"');
    expect(html).toMatch(/<button type="submit" disabled=""/);
    const empty = renderToStaticMarkup(<PromptLine id="p" draft="" asking={false} blocked={false} onDraft={noop} onSubmit={noop} />);
    expect(empty).toContain('rr-desk-cursor');
    expect(empty).toContain('caret-transparent');
    expect(empty).not.toContain('/1000');
  });
});

describe('the resize handle', () => {
  it('keys move it 24px and Home/End go to the ends, each stored', () => {
    const resized: [number, boolean][] = [];
    const bounds = heightBounds(1000);
    const handle = DeskHandleTree(440, bounds, (h, commit) => resized.push([h, commit]));
    const press = (key: string) => {
      let prevented = false;
      (handle.props.onKeyDown as (e: unknown) => void)({ key, preventDefault: () => (prevented = true) });
      return prevented;
    };
    expect(press('ArrowUp')).toBe(true);
    expect(press('ArrowDown')).toBe(true);
    expect(press('Home')).toBe(true);
    expect(press('End')).toBe(true);
    expect(press('Tab')).toBe(false);
    expect(resized).toEqual([
      [464, true],
      [416, true],
      [160, true],
      [700, true],
    ]);
  });
});

/** The handle's element (its only hook is useRef, which needs a render: the markup covers it; keys need no ref). */
function DeskHandleTree(height: number, bounds: ReturnType<typeof heightBounds>, onResize: (h: number, commit: boolean) => void) {
  let captured: ReactElement<AnyProps> | null = null;
  function Probe() {
    captured = DeskHandle({ height, bounds, onResize }) as ReactElement<AnyProps>;
    return captured;
  }
  renderToStaticMarkup(<Probe />);
  return captured as unknown as ReactElement<AnyProps>;
}

describe('source rules', () => {
  it('no file of the desk injects HTML', () => {
    const dir = new URL('../', import.meta.url);
    const files = readdirSync(dir).filter((f) => /\.tsx?$/.test(f));
    expect(files.length).toBeGreaterThan(10);
    for (const file of files) expect(readFileSync(new URL(file, dir), 'utf8'), file).not.toContain('dangerouslySetInnerHTML');
    expect(readFileSync(new URL('../../../lib/marshalDesk.ts', import.meta.url), 'utf8')).not.toContain('dangerouslySetInnerHTML');
  });
});
