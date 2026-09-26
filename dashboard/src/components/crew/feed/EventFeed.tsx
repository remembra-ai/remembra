// Event Feed (spec §9.6), `#/crew?project=X&view=feed[&type=…&session=…&zone=…&task=…&moments=1]`.
//
// Every event of the crew, newest first, live: the first window from the
// events endpoint, then the shared WebSocket, with holes filled and older
// pages on demand. Special rows for batons, checkpoint runs, completions,
// collisions, decisions, guard blocks, bypass codes and tamper blocks.
// Filters live in the URL. The list is virtualised (fixed-height rows) and
// follows the ARIA feed pattern: j / k move between items, Enter opens one.
// While the viewer reads older rows the list keeps its place and a pill
// counts what arrived above.

import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState, type UIEvent } from 'react';
import { useCrewSocket } from '../../../hooks/useCrewSocket';
import { useNow } from '../../../hooks/useResource';
import { crewHref, goToCrew, useCrewRoute, type FeedFilters as Filters } from '../../../lib/crew/routes';
import { prefersReducedMotion } from '../../../lib/motion';
import { ErrorNotice, TrailSkeleton } from '../../relay/ui';
import { CREW_KEY_HELP, isTypingTarget } from '../a11y/keys';
import { DitherCloud } from '../empty/DitherCloud';
import { EmptyFeed } from '../empty/EmptyStates';
import { EventRow, ROW_H } from './EventRow';
import { FeedFilters } from './FeedFilters';
import { LiveStrip } from './LiveStrip';
import { NO_FILTERS, buildRows, hasFilters, locateSeq, moveSelection, runKeyOf, scrollToReveal, windowRange, type FeedRow } from './model';
import { NewEventsPill } from './NewEventsPill';
import { useFeedLog } from './useFeedLog';

const TOP_SLACK = 8;

export function EventFeed({ crewId, project }: { crewId: string; project: string }) {
  const crew = useCrewSocket(crewId);
  const route = useCrewRoute();
  const filters: Filters = route?.feed ?? NO_FILTERS;
  const feed = useFeedLog(crewId, crew.state ? crew.state.last_seq : null);
  const now = useNow(5000);
  const nowMs = now.getTime();

  const [expanded, setExpanded] = useState<ReadonlySet<string>>(() => new Set());
  const rows = useMemo(() => buildRows(feed.events, filters, crew.state, expanded), [feed.events, filters, crew.state, expanded]);

  const scrollRef = useRef<HTMLDivElement>(null);
  const titleRef = useRef<HTMLDivElement>(null);
  const [scroll, setScroll] = useState({ top: 0, height: 560 });
  const [seenSeq, setSeenSeq] = useState(0);
  const [selected, setSelected] = useState<string | null>(null);
  const rowsRef = useRef<FeedRow[]>(rows);
  const newestRef = useRef(feed.newestSeq);
  const anchor = useRef<{ key: string; offset: number } | null>(null);
  const lastTop = useRef(0);
  const keyboardFocus = useRef(false);

  const loadOlder = feed.loadOlder;
  const loadOlderRef = useRef(loadOlder);
  useLayoutEffect(() => {
    loadOlderRef.current = loadOlder;
  });

  // A deep link (`&seq=N`, from an email or webhook notification, §9.12) selects that event until
  // the viewer picks another row; older pages load until it is in the window.
  const target = route?.seq ?? null;
  const located = useMemo(
    () => (target !== null && feed.status === 'ready' ? locateSeq(rows, target, feed) : null),
    [target, rows, feed],
  );
  const targetKey = located !== null && typeof located === 'object' ? located.key : null;
  const revealed = useRef<number | null>(null);
  useEffect(() => {
    if (target === null || located === null) return;
    if (located === 'older') {
      if (!feed.loadingOlder) loadOlderRef.current();
      return;
    }
    if (located === 'missing' || revealed.current === target) return;
    const el = scrollRef.current;
    if (!el) return;
    revealed.current = target;
    const reveal = scrollToReveal(located.index, el.scrollTop, el.clientHeight, ROW_H);
    if (reveal !== null) el.scrollTop = reveal;
  }, [target, located, feed.loadingOlder]);

  const selectedRef = useRef<string | null>(null);
  useLayoutEffect(() => {
    rowsRef.current = rows;
    newestRef.current = feed.newestSeq;
    selectedRef.current = selected ?? targetKey;
  });

  // Keep the viewer's place when rows arrive above it.
  useLayoutEffect(() => {
    const el = scrollRef.current;
    const a = anchor.current;
    if (!el || !a || el.scrollTop <= TOP_SLACK) return;
    const idx = rows.findIndex((r) => r.key === a.key);
    if (idx < 0) return;
    const want = idx * ROW_H + a.offset;
    if (Math.abs(el.scrollTop - want) > 1) el.scrollTop = want;
  }, [rows]);

  // Track the viewport size (the scroller exists once there are rows).
  const hasList = rows.length > 0 && feed.status === 'ready';
  useEffect(() => {
    const el = scrollRef.current;
    if (!el || typeof ResizeObserver === 'undefined') return undefined;
    const ro = new ResizeObserver(() => setScroll((s) => (s.height === el.clientHeight ? s : { ...s, height: el.clientHeight })));
    ro.observe(el);
    return () => ro.disconnect();
  }, [hasList]);

  const onScroll = useCallback(
    (e: UIEvent<HTMLDivElement>) => {
      const el = e.currentTarget;
      const top = el.scrollTop;
      const list = rowsRef.current;
      const first = Math.min(list.length - 1, Math.floor(top / ROW_H));
      anchor.current = first >= 0 ? { key: list[first].key, offset: top - first * ROW_H } : null;
      if (top <= TOP_SLACK || lastTop.current <= TOP_SLACK) setSeenSeq(newestRef.current);
      lastTop.current = top;
      setScroll({ top, height: el.clientHeight });
      if (top + el.clientHeight > list.length * ROW_H - 3 * ROW_H) loadOlder();
    },
    [loadOlder],
  );

  const atTop = scroll.top <= TOP_SLACK;
  const pending = atTop ? 0 : rows.reduce((n, r) => n + r.events.filter((e) => e.seq > seenSeq).length, 0);

  const toTop = useCallback(() => {
    scrollRef.current?.scrollTo({ top: 0, behavior: prefersReducedMotion() ? 'auto' : 'smooth' });
  }, []);

  const toggleRun = useCallback((key: string) => {
    setExpanded((prev) => {
      const next = new Set(prev);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      return next;
    });
  }, []);

  const open = useCallback((href: string) => {
    window.location.hash = href;
  }, []);

  const setFilters = useCallback((next: Filters) => goToCrew(project, 'feed', { feed: next }, true), [project]);

  // j / k (and arrows inside the list) move the selection; the row takes focus.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.metaKey || e.ctrlKey || e.altKey || isTypingTarget(e.target)) return;
      if (document.querySelector('[aria-modal="true"]')) return;
      const inList = !!scrollRef.current?.contains(document.activeElement);
      let delta = 0;
      if (e.key === 'j' || (inList && e.key === 'ArrowDown')) delta = 1;
      else if (e.key === 'k' || (inList && e.key === 'ArrowUp')) delta = -1;
      else if (inList && e.key === 'PageDown') delta = Math.max(1, Math.floor((scrollRef.current?.clientHeight ?? ROW_H) / ROW_H) - 1);
      else if (inList && e.key === 'PageUp') delta = -Math.max(1, Math.floor((scrollRef.current?.clientHeight ?? ROW_H) / ROW_H) - 1);
      if (!delta) return;
      e.preventDefault();
      const list = rowsRef.current;
      const cur = selectedRef.current && list.some((r) => r.key === selectedRef.current) ? selectedRef.current : null;
      const next = moveSelection(list, cur, cur ? delta : Math.max(0, delta - 1));
      const idx = next ? list.findIndex((r) => r.key === next) : -1;
      const el = scrollRef.current;
      if (el && idx >= 0) {
        const reveal = scrollToReveal(idx, el.scrollTop, el.clientHeight, ROW_H);
        if (reveal !== null) el.scrollTop = reveal;
      }
      keyboardFocus.current = true;
      selectedRef.current = next;
      setSelected(next);
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, []);

  // Move focus to the selected row once it is rendered.
  useEffect(() => {
    if (!keyboardFocus.current || !selected) return;
    const el = scrollRef.current?.querySelector<HTMLElement>(`[data-key="${CSS.escape(selected)}"]`);
    if (el && document.activeElement !== el) {
      el.focus({ preventScroll: true });
      keyboardFocus.current = false;
    }
  });

  const onSelect = useCallback((key: string) => {
    keyboardFocus.current = true;
    setSelected(key);
  }, []);

  if (crew.status === 'not_found') return <ErrorNotice error={crew.error} what={`the ${project} crew`} />;

  const win = windowRange(scroll.top, scroll.height, rows.length, ROW_H);
  const filtered = hasFilters(filters);
  const loading = feed.status === 'idle' || feed.status === 'loading';
  const selectedKey = selected && rows.some((r) => r.key === selected) ? selected : (targetKey ?? rows[0]?.key ?? null);

  return (
    <section aria-labelledby="crew-feed-title" className="space-y-3">
      <header className="rr-card relative isolate overflow-hidden rounded-[3px] px-4 pb-4 pt-3 sm:px-5">
        <DitherCloud shape="right" avoidRef={titleRef} pulse={feed.arrivals} className="-z-10" />
        <div ref={titleRef} className="w-fit max-w-full">
          <a href={crewHref(project)} className="block w-fit font-mono text-[11px] text-ink-3 hover:text-ink">
            <span aria-hidden="true">←</span> track
          </a>
          <p className="mt-2">
            <span className="rr-eyebrow">Event feed</span>
          </p>
          <h2 id="crew-feed-title" className="font-display mt-1 truncate text-xl font-bold leading-tight text-ink">
            {crew.state?.crew?.name || project}
          </h2>
        </div>
        <div className="mt-3 max-w-3xl">
          <LiveStrip status={crew.status} connection={crew.connection} events={feed.events} newestSeq={Math.max(feed.newestSeq, crew.state?.last_seq ?? 0)} arrivals={feed.arrivals} nowMs={nowMs} />
        </div>
      </header>

      <div className="rr-card rounded-[3px] px-4 py-3 sm:px-5">
        <FeedFilters filters={filters} state={crew.state} onChange={setFilters} />
      </div>

      <div className="rr-card relative overflow-hidden rounded-[3px]">
        {feed.status === 'error' && !feed.events.length ? (
          <ErrorNotice error={feed.error} what="the event feed" onRetry={feed.retry} />
        ) : loading ? (
          <TrailSkeleton rows={5} />
        ) : rows.length === 0 ? (
          <EmptyFeed filtered={filtered} onClear={() => setFilters(NO_FILTERS)} />
        ) : (
          <div ref={scrollRef} onScroll={onScroll} className="relative h-[62dvh] min-h-[320px] overflow-y-auto overscroll-contain md:h-[calc(100dvh-24rem)] md:min-h-[360px]">
            <NewEventsPill count={pending} onClick={toTop} />
            <div role="feed" aria-busy={feed.loadingOlder} aria-label="Crew events, newest first" style={{ paddingTop: win.padTop, paddingBottom: win.padBottom }}>
              {rows.slice(win.start, win.end).map((row, i) => {
                const index = win.start + i;
                return (
                  <EventRow
                    key={row.key}
                    row={row}
                    state={crew.state}
                    project={project}
                    nowMs={nowMs}
                    index={index}
                    total={rows.length}
                    selected={row.key === selectedKey}
                    runKey={runKeyOf(rows, index)}
                    fresh={atTop && row.event.seq > feed.loadedHead && row.event.seq > seenSeq}
                    onSelect={onSelect}
                    onToggleRun={toggleRun}
                    onOpen={open}
                  />
                );
              })}
            </div>
            <div className="flex items-center justify-center gap-3 px-4 py-3 font-mono text-[11px] text-ink-3">
              {feed.hasOlder ? (
                <button type="button" onClick={loadOlder} disabled={feed.loadingOlder} className="rr-btn-ghost px-3 py-1.5">
                  {feed.loadingOlder ? 'Loading older events…' : 'Load older events'}
                </button>
              ) : (
                <span>Start of the log · seq {feed.floorSeq || 1}</span>
              )}
            </div>
          </div>
        )}
        {feed.status === 'ready' && feed.error && (
          <p role="status" className="border-t border-rule px-4 py-2 font-mono text-[11px] text-ink-3">
            Could not load everything just now; retrying on the next event.
          </p>
        )}
      </div>

      <p className="hidden flex-wrap gap-x-4 gap-y-1 font-mono text-[11px] text-ink-3 md:flex">
        {CREW_KEY_HELP.map((k) => (
          <span key={k.keys}>
            <kbd className="text-ink-2">{k.keys}</kbd> {k.what}
          </span>
        ))}
      </p>
    </section>
  );
}
