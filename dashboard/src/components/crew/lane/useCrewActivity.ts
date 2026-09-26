// The crew's events of the last hour, for the lane activity strips, the
// compact feed, the status strip and pickup-slot times (spec §9.3).
//
// The reducer keeps state, not history, so this hook keeps its own window:
//   1. backfill: the last ≤600 events through `GET /crews/{id}/events`;
//   2. live: the crew's frames on the shared socket (one server subscription
//      is shared with the crew store; this listener never moves its cursor);
//   3. catch-up: when the store's `last_seq` runs ahead of the window (polling
//      mode, a dropped frame), fetch what is missing.
// Events older than the window are dropped.

import { useEffect, useRef, useState } from 'react';
import { useCrewRuntime } from '../../../lib/crew/context';
import type { CrewEvent, CrewFrame } from '../../../lib/crew/types';
import { mergeEvents, WINDOW_MIN } from './activity';

export const BACKFILL_EVENTS = 600;
const PAGE = 200;
const MAX_PAGES = 4;
const CATCH_UP_MS = 1500;

interface Window {
  crewId: string | null;
  events: CrewEvent[];
  loaded: boolean;
}

/**
 * `lastSeq` is the crew store's applied seq (null until its snapshot loads;
 * nothing is fetched or subscribed before that).
 */
export function useCrewActivity(crewId: string | null, lastSeq: number | null): { events: CrewEvent[]; loaded: boolean } {
  const runtime = useCrewRuntime();
  const [win, setWin] = useState<Window>({ crewId: null, events: [], loaded: false });
  const maxSeq = useRef(0);
  const ready = crewId !== null && lastSeq !== null;
  const lastSeqRef = useRef(lastSeq);
  useEffect(() => {
    lastSeqRef.current = lastSeq;
  });

  // Backfill and live frames, once per crew.
  useEffect(() => {
    if (!ready || crewId === null) return;
    let cancelled = false;
    maxSeq.current = 0;
    const add = (events: CrewEvent[]) => {
      if (cancelled || !events.length) return;
      for (const e of events) maxSeq.current = Math.max(maxSeq.current, e.seq);
      setWin((prev) => {
        const base = prev.crewId === crewId ? prev.events : [];
        return { crewId, events: mergeEvents(base, events, Date.now(), WINDOW_MIN), loaded: prev.crewId === crewId ? prev.loaded : false };
      });
    };
    const unsubscribe = runtime.socket.subscribeCrew(
      crewId,
      (frame) => {
        const f = frame as CrewFrame;
        if (f.type === 'crew.event') add([f.data]);
      },
      () => null,
    );
    (async () => {
      let since = Math.max(0, (lastSeqRef.current ?? 0) - BACKFILL_EVENTS);
      try {
        for (let page = 0; page < MAX_PAGES && !cancelled; page += 1) {
          const res = await runtime.api.events(crewId, since, { limit: PAGE });
          const body = res.data;
          if (!body || !body.events.length) break;
          add(body.events);
          since = body.events[body.events.length - 1].seq;
          if (!body.has_more) break;
        }
      } catch {
        // the strip shows what the live stream brings; the lanes still work without history
      }
      if (!cancelled) setWin((prev) => ({ crewId, events: prev.crewId === crewId ? prev.events : [], loaded: true }));
    })();
    return () => {
      cancelled = true;
      unsubscribe();
    };
  }, [runtime, crewId, ready]);

  // Catch up when the store is ahead of the window.
  useEffect(() => {
    if (!ready || crewId === null || lastSeq === null) return;
    const timer = window.setTimeout(async () => {
      if (maxSeq.current === 0 || lastSeq <= maxSeq.current) return;
      try {
        const res = await runtime.api.events(crewId, maxSeq.current, { limit: PAGE });
        const events = res.data?.events ?? [];
        if (!events.length) return;
        for (const e of events) maxSeq.current = Math.max(maxSeq.current, e.seq);
        setWin((prev) => (prev.crewId === crewId ? { ...prev, events: mergeEvents(prev.events, events, Date.now(), WINDOW_MIN) } : prev));
      } catch {
        // next change retries
      }
    }, CATCH_UP_MS);
    return () => window.clearTimeout(timer);
  }, [runtime, crewId, lastSeq, ready]);

  if (win.crewId !== crewId) return { events: [], loaded: false };
  return { events: win.events, loaded: win.loaded };
}
