import { createContext, useContext } from 'react';
import { initialDeskState, type DeskAction, type DeskState, type OpenRequest } from '../lib/marshalDesk';

/** Where a desk open came from, so closing it can put focus back. */
export type DeskOpen = OpenRequest & { invoker?: HTMLElement | null };

/**
 * The Marshal desk as the rest of the dashboard sees it: whether it is there
 * and the ways in. The palette, the why? slips, Settings and the layout read
 * this; it changes only when the desk appears, goes or is closed, never while
 * someone types in the prompt or an answer streams in.
 */
export interface MarshalDeskApi {
  /** GET /marshal/settings answered 200: the desk exists for this account (a dashboard login on the allow-list). */
  available: boolean;
  /** The account turned the desk off in Settings > Diagnostics: nothing mounts. */
  optedOut: boolean;
  /** The bar is on screen (mounted and not closed with ×). */
  barVisible: boolean;
  /** Open the desk, with a question to ask now (`ask`) or leave in the prompt. */
  open: (request: DeskOpen) => void;
  expand: () => void;
  /** Esc: fold down to the bar; a running ask keeps streaming. */
  collapse: () => void;
  /** ×: hide the bar for this session and stop a running ask. */
  close: () => void;
  /** Drop the transcript and start a new `conv` (nothing was stored server-side). */
  newConversation: () => void;
  /** Settings saved `desk`: mount or unmount at once. */
  setDesk: (desk: boolean) => void;
  /** Store a height the user dragged or keyed to. */
  setHeight: (height: number, persist: boolean) => void;
  /** The element that opened the desk, if it is still on the page. */
  invoker: () => HTMLElement | null;
}

/** The desk's own state (the board, the transcript, the prompt): only the desk itself reads it. */
export interface MarshalDeskStore {
  state: DeskState;
  dispatch: (action: DeskAction) => void;
}

const noop = () => {};

/** Outside the provider (tests, API-key logins) the desk is simply not there. */
export const MARSHAL_DESK_OFF: MarshalDeskApi = {
  available: false,
  optedOut: false,
  barVisible: false,
  open: noop,
  expand: noop,
  collapse: noop,
  close: noop,
  newConversation: noop,
  setDesk: noop,
  setHeight: noop,
  invoker: () => null,
};

export const MARSHAL_DESK_EMPTY: MarshalDeskStore = {
  state: initialDeskState({ closed: true }),
  dispatch: noop,
};

export const MarshalDeskContext = createContext<MarshalDeskApi>(MARSHAL_DESK_OFF);
export const MarshalDeskStateContext = createContext<MarshalDeskStore>(MARSHAL_DESK_EMPTY);

export function useMarshalDesk(): MarshalDeskApi {
  return useContext(MarshalDeskContext);
}

export function useMarshalDeskState(): MarshalDeskStore {
  return useContext(MarshalDeskStateContext);
}
