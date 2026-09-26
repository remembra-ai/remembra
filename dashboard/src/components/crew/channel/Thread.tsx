// The conversation: either the whole channel (thread roots in order, each
// with its reply count) or one thread (root plus replies). Messages hang off
// a dashed trail; a message that arrives live drops in as an orange packet.
// The list keeps the newest message in view unless the reader scrolled up.

import { useEffect, useLayoutEffect, useRef } from 'react';
import type { CrewState } from '../../../lib/crew/types';
import { MessageItem } from './MessageItem';
import type { ChannelMessage, ChannelThread } from './model';

export function Thread({
  mode,
  items,
  threads,
  state,
  userId,
  human,
  now,
  arrivedId,
  onOpenThread,
  onChanged,
  hasOlder,
  loadingOlder,
  onLoadOlder,
  emptyText,
}: {
  mode: 'channel' | 'thread';
  /** channel: thread roots; thread: root first, then replies. */
  items: ChannelMessage[];
  threads: Map<string, ChannelThread>;
  state: CrewState | null;
  userId: string | null;
  human: boolean | null;
  now: Date;
  arrivedId: string | null;
  onOpenThread: (rootId: string) => void;
  onChanged: (message: ChannelMessage) => void;
  hasOlder: boolean;
  loadingOlder: boolean;
  onLoadOlder: () => void;
  emptyText: string;
}) {
  const box = useRef<HTMLDivElement | null>(null);
  const stick = useRef(true);
  const lastId = items.length ? items[items.length - 1].id : null;

  useLayoutEffect(() => {
    const el = box.current;
    if (el && stick.current) el.scrollTop = el.scrollHeight;
  }, [lastId, items.length]);

  useEffect(() => {
    stick.current = true;
  }, [mode]);

  return (
    <div
      ref={box}
      onScroll={(e) => {
        const el = e.currentTarget;
        stick.current = el.scrollHeight - el.scrollTop - el.clientHeight < 80;
      }}
      className="relative max-h-[62vh] min-h-[240px] overflow-y-auto px-4 sm:px-5"
      role="log"
      aria-live="polite"
      aria-relevant="additions"
      aria-label={mode === 'thread' ? 'Thread' : 'Channel messages'}
    >
      {hasOlder && mode === 'channel' && (
        <div className="py-2 text-center">
          <button type="button" onClick={onLoadOlder} disabled={loadingOlder} className="rr-btn-ghost px-3 py-1.5 font-mono text-[11px]">
            {loadingOlder ? 'Loading…' : 'Earlier messages'}
          </button>
        </div>
      )}
      {items.length === 0 ? (
        <p className="py-10 text-center text-sm text-ink-3">{emptyText}</p>
      ) : (
        <div className="relative">
          <span aria-hidden="true" className="crew-trail-v absolute bottom-3 left-[10px] top-4" />
          {items.map((m, i) => {
            const arrived = m.id === arrivedId;
            return (
              <div key={m.id} className="relative">
                {arrived && i > 0 && (
                  <span aria-hidden="true" className="absolute left-[10px] top-[-18px] z-20 h-[30px] w-[6px]">
                    <span className="crew-drop" />
                  </span>
                )}
                <MessageItem
                  message={m}
                  state={state}
                  userId={userId}
                  human={human}
                  now={now}
                  arrived={arrived}
                  isRoot={mode === 'channel' || i === 0}
                  replies={mode === 'channel' ? (threads.get(m.id)?.replies.length ?? 0) : undefined}
                  onOpenThread={mode === 'channel' ? onOpenThread : undefined}
                  onChanged={onChanged}
                />
                {mode === 'thread' && i === 0 && items.length > 1 && (
                  <p className="pb-1 pl-9 font-mono text-[11px] text-ink-3">
                    {items.length - 1} repl{items.length === 2 ? 'y' : 'ies'}
                  </p>
                )}
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}
