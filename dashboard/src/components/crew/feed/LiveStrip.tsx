// The live status strip: connection state in words, the crew's head seq,
// how busy the last ten minutes were and the newest event, over a dashed
// trail that orange pixel packets run along as events arrive (one packet per
// event, at most four in flight). Reduced motion: no packets; the text still
// updates. Not a live region: arrivals are announced by the crew announcer.

import clsx from 'clsx';
import { motion } from 'framer-motion';
import { useCrewMotion } from '../../../lib/motion';
import type { CrewStreamStatus } from '../../../lib/crew/store';
import type { ConnectionStatus } from '../../../lib/crew/socket';
import type { CrewEvent } from '../../../lib/crew/types';
import { liveWords, recentCount } from './live';
import { ageText } from './model';

const MAX_PACKETS = 4;

export function LiveStrip({
  status,
  connection,
  events,
  newestSeq,
  arrivals,
  nowMs,
}: {
  status: CrewStreamStatus;
  connection: ConnectionStatus;
  events: readonly CrewEvent[];
  newestSeq: number;
  arrivals: number;
  nowMs: number;
}) {
  const crewMotion = useCrewMotion();
  const words = liveWords(status, connection);
  const last = events.length ? events[events.length - 1] : null;
  const recent = recentCount(events, nowMs);
  const packets: number[] = [];
  if (crewMotion.packetMs > 0) for (let n = Math.max(1, arrivals - MAX_PACKETS + 1); n <= arrivals; n += 1) packets.push(n);

  return (
    <div className="relative min-w-0 rounded-[3px] border border-rule bg-panel/90 px-3 pb-2.5 pt-2 font-mono text-[12px] text-ink-2 backdrop-blur-[1px]" aria-live="off" data-live-strip>
      <div className="flex min-w-0 items-center gap-2">
        <span
          aria-hidden="true"
          className={clsx('h-2 w-2 shrink-0 rounded-full', words.live ? 'rr-pulse bg-signal' : 'border border-ink-3 bg-transparent')}
        />
        <span className={clsx('shrink-0 font-semibold', words.live ? 'text-ink' : 'text-ink-3')}>{words.text}</span>
        <span className="shrink-0 text-ink-3">· seq {newestSeq}</span>
        <span className="hidden shrink-0 text-ink-3 sm:inline">
          · {recent} in the last 10 min
        </span>
        {last && (
          <span className="min-w-0 truncate text-ink-3">
            · {ageText(last.ts, nowMs)} ago <span className="text-ink-2">{last.summary}</span>
          </span>
        )}
      </div>
      {/* the trail the packets run along */}
      <div aria-hidden="true" className="relative mt-2 h-[4px]">
        <span className="rr-rail-h absolute inset-x-0 top-[1px] h-[2px]" />
        {packets.map((n) => (
          <motion.span
            key={n}
            className="absolute top-0 h-[4px] w-[4px] bg-signal"
            initial={{ left: '0%', opacity: 1 }}
            animate={{ left: 'calc(100% - 4px)', opacity: [1, 1, 0] }}
            transition={{ duration: crewMotion.packetMs / 1000, delay: ((n - 1) % MAX_PACKETS) * 0.1, ease: 'linear' }}
          />
        ))}
      </div>
    </div>
  );
}
