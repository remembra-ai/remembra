// The Marshal desk: a pane docked at the bottom of the content column. It
// folds down to a 32px bar with the rules-only status line, rises to 44vh
// (resizable) and pushes the page up rather than covering it. Under 640px the
// bar floats above the tab bar and the open desk is a full-height sheet that
// holds focus until it is folded or closed.
//
// Read-only: it shows what the server read and said. The only thing Enter
// does is ask the question in the prompt.

import { useCallback, useEffect, useId, useRef, useSyncExternalStore, type KeyboardEvent, type Ref } from 'react';
import clsx from 'clsx';
import { useMarshalDesk, useMarshalDeskState } from '../../hooks/marshalDesk';
import {
  DESK_COPY,
  blocksAsking,
  clampHeight,
  defaultHeight,
  heightBounds,
  trapFocus,
  type DeskState,
  type HeightBounds,
  type OpenRequest,
} from '../../lib/marshalDesk';
import { BoardView } from './BoardView';
import { DeskBar } from './DeskBar';
import { DeskHandle } from './DeskHandle';
import { DeskNotice } from './DeskNotice';
import { PromptLine } from './PromptLine';
import { Transcript } from './Transcript';

const SHEET_QUERY = '(max-width: 639px)';
const FOCUSABLE = 'a[href], button:not([disabled]), input:not([disabled]), [tabindex]:not([tabindex="-1"])';

// One subscription for the life of the desk (a new function each render would resubscribe on every streamed read).
function subscribeSheet(onChange: () => void) {
  const mq = typeof window !== 'undefined' ? window.matchMedia?.(SHEET_QUERY) : undefined;
  mq?.addEventListener('change', onChange);
  return () => mq?.removeEventListener('change', onChange);
}

/** Phone width: the open desk is a sheet. False on the server and without matchMedia. */
function useSheet(): boolean {
  return useSyncExternalStore(
    subscribeSheet,
    () => typeof window !== 'undefined' && !!window.matchMedia?.(SHEET_QUERY).matches,
    () => false,
  );
}

function subscribeResize(onChange: () => void) {
  window.addEventListener('resize', onChange);
  return () => window.removeEventListener('resize', onChange);
}

function useViewportHeight(): number {
  return useSyncExternalStore(
    subscribeResize,
    () => window.innerHeight,
    () => 800,
  );
}

export interface DeskViewProps {
  state: DeskState;
  sheet: boolean;
  height: number;
  bounds: HeightBounds;
  bodyId: string;
  promptId: string;
  onToggle: () => void;
  onClose: () => void;
  onResize: (height: number, commit: boolean) => void;
  onDraft: (text: string) => void;
  onSubmit: () => void;
  onAsk: (request: OpenRequest) => void;
  onNewConversation: () => void;
  onKeyDown?: (event: KeyboardEvent<HTMLElement>) => void;
  regionRef?: Ref<HTMLElement>;
  toggleRef?: Ref<HTMLButtonElement>;
  inputRef?: Ref<HTMLInputElement>;
  scrollRef?: Ref<HTMLDivElement>;
}

/** The desk from its state: the bar, and when open the handle, the board or transcript, the notice and the prompt. */
export function DeskView({
  state,
  sheet,
  height,
  bounds,
  bodyId,
  promptId,
  onToggle,
  onClose,
  onResize,
  onDraft,
  onSubmit,
  onAsk,
  onNewConversation,
  onKeyDown,
  regionRef,
  toggleRef,
  inputRef,
  scrollRef,
}: DeskViewProps) {
  const open = state.mode === 'open';
  const blocked = blocksAsking(state.notice);
  const last = state.entries[state.entries.length - 1];
  // A refusal already printed under its question needn't be said twice.
  const notice = state.notice && blocked && last?.error?.message !== state.notice.message ? state.notice : null;
  const status = state.board?.status_line ?? (state.boardError ? '' : DESK_COPY.reading);
  return (
    <section
      ref={regionRef}
      role="region"
      aria-label="Marshal"
      onKeyDown={onKeyDown}
      data-desk={sheet ? (open ? 'sheet' : 'bar') : 'dock'}
      className={clsx(
        'rr-desk flex flex-col bg-panel text-ink',
        sheet && open && 'fixed inset-0 z-[70] pb-[env(safe-area-inset-bottom)] pt-[env(safe-area-inset-top)]',
        sheet && !open && 'fixed inset-x-4 bottom-[calc(76px+env(safe-area-inset-bottom))] z-[45] border-2 border-ink shadow-[4px_4px_0_var(--ink)]',
        !sheet && 'shrink-0 border-t-2 border-ink sm:mb-[calc(64px+env(safe-area-inset-bottom))] md:mb-0',
      )}
      style={!sheet && open ? { height } : undefined}
    >
      <DeskBar status={status} open={open} bodyId={bodyId} onToggle={onToggle} onClose={onClose} toggleRef={toggleRef} />
      {open && !sheet && <DeskHandle height={height} bounds={bounds} onResize={onResize} />}
      {open && (
        <div id={bodyId} className="flex min-h-0 flex-1 flex-col">
          <div ref={scrollRef} className="min-h-0 flex-1 overflow-y-auto overscroll-contain">
            <div className="mx-auto w-full max-w-3xl px-4 py-3 md:px-6">
              {state.entries.length === 0 ? (
                <BoardView board={state.board} boardError={state.boardError} blocked={blocked} onAsk={onAsk} />
              ) : (
                <Transcript entries={state.entries} asking={state.asking} blocked={blocked} onAsk={onAsk} onNewConversation={onNewConversation} />
              )}
            </div>
          </div>
          {notice && <DeskNotice notice={notice} />}
          <PromptLine
            id={promptId}
            draft={state.draft}
            asking={state.asking}
            blocked={blocked}
            onDraft={onDraft}
            onSubmit={onSubmit}
            inputRef={inputRef}
          />
        </div>
      )}
    </section>
  );
}

/** The mounted desk: wires DeskView to the provider, focus, Esc, resize and the sheet's focus trap. */
export function MarshalDesk() {
  const desk = useMarshalDesk();
  const { expand, collapse, close, newConversation, setHeight, invoker } = desk;
  const { state, dispatch } = useMarshalDeskState();
  const sheet = useSheet();
  const viewport = useViewportHeight();
  const bodyId = useId();
  const promptId = useId();
  const regionRef = useRef<HTMLElement>(null);
  const toggleRef = useRef<HTMLButtonElement>(null);
  const inputRef = useRef<HTMLInputElement>(null);
  const scrollRef = useRef<HTMLDivElement>(null);
  const shown = desk.available && !desk.optedOut && state.mode !== 'hidden';
  const mode = shown ? state.mode : 'hidden';
  const previous = useRef(mode);

  const bounds = heightBounds(viewport);
  const height = clampHeight(state.height ?? defaultHeight(viewport), bounds);

  // Opening puts the cursor in the prompt (a prefilled question is one Enter away).
  // Folding or closing returns focus to whatever opened the desk, if focus was inside it.
  useEffect(() => {
    const from = previous.current;
    previous.current = mode;
    if (from === mode) return;
    if (mode === 'open') {
      const input = inputRef.current;
      if (input) {
        input.focus();
        const end = input.value.length;
        input.setSelectionRange(end, end);
      }
      return;
    }
    // Only when focus was in the desk (or went with it): a click elsewhere keeps its own focus.
    const active = document.activeElement;
    const lost = !active || active === document.body || !active.isConnected || !!regionRef.current?.contains(active);
    if (!lost || active === toggleRef.current) return;
    const back = invoker();
    if (back) back.focus();
    else if (mode === 'collapsed') toggleRef.current?.focus();
    else document.getElementById('main')?.focus();
  }, [mode, invoker]);

  // The newest line in view as the answer streams in.
  const last = state.entries[state.entries.length - 1];
  const progress = last ? `${last.id}:${last.reads.length}:${last.answer ? 1 : 0}:${last.error ? 1 : 0}:${last.usage ? 1 : 0}` : '';
  useEffect(() => {
    const el = scrollRef.current;
    if (el && mode === 'open') el.scrollTop = el.scrollHeight;
  }, [progress, mode]);

  const onKeyDown = useCallback(
    (event: KeyboardEvent<HTMLElement>) => {
      if (event.key === 'Escape' && mode === 'open') {
        event.stopPropagation();
        collapse();
        return;
      }
      if (event.key !== 'Tab' || !sheet || mode !== 'open' || !regionRef.current) return;
      const items = Array.from(regionRef.current.querySelectorAll<HTMLElement>(FOCUSABLE));
      const next = trapFocus(items, items.indexOf(document.activeElement as HTMLElement), event.shiftKey);
      if (next < 0) return;
      event.preventDefault();
      items[next].focus();
    },
    [mode, sheet, collapse],
  );

  if (mode === 'hidden') return null;
  return (
    <DeskView
      state={state}
      sheet={sheet}
      height={height}
      bounds={bounds}
      bodyId={bodyId}
      promptId={promptId}
      onToggle={mode === 'open' ? collapse : expand}
      onClose={close}
      onResize={setHeight}
      onDraft={(text) => dispatch({ type: 'draft', text })}
      onSubmit={() => dispatch({ type: 'submit' })}
      onAsk={(request) => {
        // Asked from inside the desk: whatever opened it stays the place focus goes back to.
        dispatch({ type: 'open', ...request });
        inputRef.current?.focus();
      }}
      onNewConversation={() => {
        newConversation();
        inputRef.current?.focus();
      }}
      onKeyDown={onKeyDown}
      regionRef={regionRef}
      toggleRef={toggleRef}
      inputRef={inputRef}
      scrollRef={scrollRef}
    />
  );
}
