// The Marshal desk's state for the whole signed-in dashboard: whether the desk
// exists for this account (GET /marshal/settings), the board, the transcript
// and the one ask in flight. The ask lives here, not in the dock, so folding
// the desk down (Esc) keeps it streaming; closing it (×), signing out or the
// opt-out stops it. Nothing is stored anywhere but this page's memory: a reload
// starts a new conversation, as the server expects.

import { useCallback, useEffect, useMemo, useReducer, useRef, useState, type ReactNode } from 'react';
import { MarshalDeskContext, MarshalDeskStateContext, type DeskOpen, type MarshalDeskApi, type MarshalDeskStore } from '../../hooks/marshalDesk';
import { api } from '../../lib/api';
import {
  BOARD_STALE_MS,
  askRequest,
  deskClosedThisSession,
  deskReducer,
  getBoard,
  initialDeskState,
  isAbortError,
  loadDeskHeight,
  loadDeskSettings,
  newConversationId,
  noticeFor,
  pendingEntry,
  rememberDeskClosed,
  runAsk,
  saveDeskHeight,
  type AskRun,
  type SettingsResult,
} from '../../lib/marshalDesk';

/** GET /marshal/settings: still asking, or its answer (the desk is not there, or it is: on or opted out). */
type SettingsStatus = { status: 'loading' } | SettingsResult;

export function MarshalDeskProvider({ children }: { children: ReactNode }) {
  // Only a dashboard login can use the desk; an API-key session never asks.
  const [settings, setSettings] = useState<SettingsStatus>(() =>
    api.getAuthMode() === 'jwt' ? { status: 'loading' } : { status: 'off' },
  );
  const [state, dispatch] = useReducer(deskReducer, undefined, () =>
    initialDeskState({ closed: deskClosedThisSession(), height: loadDeskHeight() }),
  );
  const [conv, setConv] = useState(newConversationId);
  const invokerRef = useRef<HTMLElement | null>(null);
  const askRef = useRef<AskRun | null>(null);
  const boardRef = useRef<AbortController | null>(null);
  const stateRef = useRef(state);
  useEffect(() => {
    stateRef.current = state;
  });

  const available = settings.status === 'ready';
  const optedOut = settings.status === 'ready' && !settings.desk;
  const mounted = available && !optedOut;

  // Does the desk exist for this account? 404/403 are final; an unreachable server is asked again.
  useEffect(() => {
    if (settings.status !== 'loading') return;
    return loadDeskSettings(setSettings);
  }, [settings.status]);

  const loadBoard = useCallback(() => {
    boardRef.current?.abort();
    const ctrl = new AbortController();
    boardRef.current = ctrl;
    getBoard(ctrl.signal).then(
      (board) => {
        if (!ctrl.signal.aborted) dispatch({ type: 'board', board, at: Date.now() });
      },
      (err: unknown) => {
        if (!isAbortError(err)) dispatch({ type: 'boardFailed', notice: noticeFor(err) });
      },
    );
  }, []);

  // The board: read once the bar shows, and again on expand when it is older than a minute.
  useEffect(() => {
    if (!mounted || state.mode === 'hidden') return;
    const stale = state.boardAt === null || (state.mode === 'open' && Date.now() - state.boardAt > BOARD_STALE_MS);
    if (stale) loadBoard();
  }, [mounted, state.mode, state.boardAt, loadBoard]);

  // Keep a collapsed status line current without polling a hidden tab or hidden desk.
  useEffect(() => {
    if (!mounted || state.mode === 'hidden') return;
    const refresh = () => {
      const current = stateRef.current;
      if (document.visibilityState === 'visible' &&
          (current.boardAt === null || Date.now() - current.boardAt >= BOARD_STALE_MS)) loadBoard();
    };
    const timer = window.setInterval(refresh, 120_000);
    document.addEventListener('visibilitychange', refresh);
    return () => {
      window.clearInterval(timer);
      document.removeEventListener('visibilitychange', refresh);
    };
  }, [mounted, state.mode, loadBoard]);

  // The ask in flight: one stream at a time, stopped on close, on unmount and after a minute of silence.
  const pendingId = pendingEntry(state)?.id ?? null;
  useEffect(() => {
    if (pendingId === null) return;
    const current = stateRef.current;
    const entry = current.entries.find((e) => e.id === pendingId);
    if (!entry) return;
    const run = runAsk({ request: askRequest(current, entry, conv), id: pendingId, dispatch });
    let detached = false;
    askRef.current = run;
    void run.finished.then(() => {
      if (!detached && askRef.current === run) {
        askRef.current = null;
        loadBoard();
      }
    });
    // Cleanup (the question ended, a new conversation, unmount): nothing from this run is shown after it.
    return () => {
      detached = true;
      run.stop(true);
    };
  }, [pendingId, conv, loadBoard]);

  // Signing out (or switching account) unmounts the provider: nothing keeps running.
  useEffect(
    () => () => {
      askRef.current?.stop(true);
      boardRef.current?.abort();
    },
    [],
  );

  /** Stop the ask in flight; its question shows as stopped. */
  const stopAsk = useCallback(() => {
    const run = askRef.current;
    askRef.current = null;
    run?.stop();
  }, []);

  const open = useCallback(({ invoker, ...request }: DeskOpen) => {
    const active = typeof document !== 'undefined' ? document.activeElement : null;
    invokerRef.current = invoker ?? (active instanceof HTMLElement ? active : null);
    rememberDeskClosed(false);
    dispatch({ type: 'open', ...request });
  }, []);

  const close = useCallback(() => {
    stopAsk();
    rememberDeskClosed(true);
    dispatch({ type: 'close' });
  }, [stopAsk]);

  const newConversation = useCallback(() => {
    stopAsk();
    dispatch({ type: 'reset' });
    setConv(newConversationId());
  }, [stopAsk]);

  const setDesk = useCallback(
    (desk: boolean) => {
      if (!desk) stopAsk();
      setSettings((prev) => (prev.status === 'ready' ? { status: 'ready', desk } : prev));
    },
    [stopAsk],
  );

  const setHeight = useCallback((height: number, persist: boolean) => {
    dispatch({ type: 'height', height });
    if (persist) saveDeskHeight(height);
  }, []);

  const expand = useCallback(() => dispatch({ type: 'expand' }), []);
  const collapse = useCallback(() => dispatch({ type: 'collapse' }), []);
  const invoker = useCallback(() => {
    const el = invokerRef.current;
    return el && el.isConnected ? el : null;
  }, []);

  // Two contexts: the ways in change only when the desk appears or goes, so a
  // keystroke in the prompt or a streamed read re-renders the desk alone, not
  // the layout, the palette and every why? slip.
  const barVisible = mounted && state.mode !== 'hidden';
  const ways = useMemo<MarshalDeskApi>(
    () => ({ available, optedOut, barVisible, open, expand, collapse, close, newConversation, setDesk, setHeight, invoker }),
    [available, optedOut, barVisible, open, expand, collapse, close, newConversation, setDesk, setHeight, invoker],
  );
  const store = useMemo<MarshalDeskStore>(() => ({ state, dispatch }), [state]);

  return (
    <MarshalDeskContext.Provider value={ways}>
      <MarshalDeskStateContext.Provider value={store}>{children}</MarshalDeskStateContext.Provider>
    </MarshalDeskContext.Provider>
  );
}
