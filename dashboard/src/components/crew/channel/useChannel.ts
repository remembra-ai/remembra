// Data for the Crew Channel: the message window from REST (full bodies,
// provenance labels, timestamps), kept live from the crew stream. Every
// `message.*` event in reducer state is merged at once (so a new message shows
// immediately) and then refetched from REST for its full row.

import { useCallback, useEffect, useRef, useState } from 'react';
import { api } from '../../../lib/api';
import { CrewApiError, crewApi } from '../../../lib/crew/api';
import type { CrewDetail, CrewState } from '../../../lib/crew/types';
import { useResource } from '../../../hooks/useResource';
import { mergeMessages, type ChannelMessage } from './model';

const WINDOW = 200;
const MAX_SEQ = Number.MAX_SAFE_INTEGER;

export interface ChannelData {
  messages: ChannelMessage[];
  loading: boolean;
  error: unknown;
  /** More messages exist before the oldest loaded one. */
  hasOlder: boolean;
  loadingOlder: boolean;
  loadOlder: () => void;
  /** Id of the newest message that arrived live (drives the packet drop). */
  arrivedId: string | null;
  refresh: () => void;
  /** Merge a message the screen just posted or changed (before its event arrives). */
  upsert: (message: ChannelMessage) => void;
  /** Load a whole thread (its root may be older than the window). */
  loadThread: (rootId: string) => void;
}

export function useChannelMessages(crewId: string, state: CrewState | null): ChannelData {
  const [messages, setMessages] = useState<ChannelMessage[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<unknown>(null);
  const [hasOlder, setHasOlder] = useState(false);
  const [loadingOlder, setLoadingOlder] = useState(false);
  const [arrivedId, setArrivedId] = useState<string | null>(null);
  const [nonce, setNonce] = useState(0);
  const maxSeq = useRef(0);
  const loaded = useRef(false);
  const threadsLoaded = useRef(new Set<string>());

  const take = useCallback((rows: ChannelMessage[]) => {
    setMessages((prev) => {
      const next = mergeMessages(prev, rows);
      maxSeq.current = next.length ? next[next.length - 1].seq : maxSeq.current;
      return next;
    });
  }, []);

  // Initial window: the newest 200 messages.
  useEffect(() => {
    let cancelled = false;
    loaded.current = false;
    threadsLoaded.current = new Set();
    setLoading(true);
    crewApi
      .messages(crewId, { before: MAX_SEQ, limit: WINDOW })
      .then((res) => {
        if (cancelled) return;
        const rows = res.items as ChannelMessage[];
        setMessages(mergeMessages([], rows));
        maxSeq.current = rows.length ? rows[rows.length - 1].seq : 0;
        setHasOlder(rows.length >= WINDOW);
        setError(null);
        loaded.current = true;
      })
      .catch((err: unknown) => {
        if (!cancelled) setError(err);
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [crewId, nonce]);

  // Live: merge message events at once, then fetch full rows after the newest seq we hold.
  const live = state?.messages;
  const lastLive = useRef<typeof live>(undefined);
  useEffect(() => {
    if (!live || live === lastLive.current) return;
    const first = lastLive.current === undefined;
    lastLive.current = live;
    if (!live.length) return;
    const known = new Set(messages.map((m) => m.id));
    const fresh = live.filter((m) => !known.has(m.id));
    take(live as ChannelMessage[]);
    if (first || !loaded.current) return;
    if (fresh.length) setArrivedId(fresh[fresh.length - 1].id);
    const since = Math.max(0, Math.min(maxSeq.current, ...fresh.map((m) => m.seq - 1)));
    crewApi
      .messages(crewId, { since_seq: since, limit: WINDOW })
      .then((res) => take(res.items as ChannelMessage[]))
      .catch(() => undefined); // the event already shows the message; the next change retries
    // Edits, pins and redactions of messages already shown: refetch those threads' rows.
    const changed = live.filter((m) => known.has(m.id));
    for (const root of new Set(changed.map((m) => m.thread_root_id || m.id))) {
      crewApi
        .messages(crewId, { thread: root, limit: WINDOW })
        .then((res) => take(res.items as ChannelMessage[]))
        .catch(() => undefined);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps -- runs per reducer change of the message list only
  }, [live, crewId, take]);

  const loadOlder = useCallback(() => {
    const oldest = messages.length ? messages[0].seq : MAX_SEQ;
    setLoadingOlder(true);
    crewApi
      .messages(crewId, { before: oldest, limit: 100 })
      .then((res) => {
        take(res.items as ChannelMessage[]);
        setHasOlder(res.items.length >= 100);
      })
      .catch((err: unknown) => setError(err))
      .finally(() => setLoadingOlder(false));
  }, [crewId, messages, take]);

  const loadThread = useCallback(
    (rootId: string) => {
      if (threadsLoaded.current.has(rootId)) return;
      threadsLoaded.current.add(rootId);
      crewApi
        .messages(crewId, { thread: rootId, limit: WINDOW })
        .then((res) => take(res.items as ChannelMessage[]))
        .catch(() => threadsLoaded.current.delete(rootId));
    },
    [crewId, take],
  );

  const refresh = useCallback(() => setNonce((n) => n + 1), []);
  const upsert = useCallback((m: ChannelMessage) => take([m]), [take]);

  return { messages, loading, error, hasOlder, loadingOlder, loadOlder, arrivedId, refresh, upsert, loadThread };
}

/** The caller's role in this crew and whether it is a human principal (dashboard login, D27). */
export function useCrewAccess(crewId: string | null): { detail: CrewDetail | undefined; human: boolean | null; error: unknown } {
  const res = useResource(crewId ? `crew-detail:${crewId}` : null, () => crewApi.getCrew(crewId as string), { pollMs: 120000 });
  return { detail: res.data, human: res.data ? res.data.human : null, error: res.error };
}

export function currentUserId(): string | null {
  return api.getUserId();
}

/** Plain words for a failed crew action: what went wrong and what to do. */
export function actionError(err: unknown): string {
  if (err instanceof CrewApiError) {
    if (err.humanOnly) return 'Only a dashboard login can do this. API keys (and agents) never can.';
    if (err.stepUpRequired) return 'This needs a fresh login (within 15 minutes). Sign out, sign back in, and try again.';
    if (err.status === 0) return 'Could not reach the Remembra server. Check your connection and try again.';
    if (err.status === 429) return `Too many requests. Try again in ${err.retryAfterS ?? 'a few'} seconds.`;
    if (err.status === 404) return 'That item no longer exists or is not in this crew.';
    return err.message || `Request failed (${err.status}).`;
  }
  return err instanceof Error ? err.message : 'Something went wrong.';
}
