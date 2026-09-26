// The last 60 minutes of one lane, a column per minute: marks on top
// (◆ ▲ ✕ ★ ⇢ ⇠ ⬆ ⛔), dithered pixel density below (busier minutes look
// denser), and the dashed track underneath. A working lane has an orange
// pixel packet running down the track toward "now".

import clsx from 'clsx';
import { ALARM_MARKS, MARK_GLYPH, MARK_LABEL, primaryMark, type Strip } from './activity';
import { lit } from './dither';
import './lane.css';

const ROWS = 3;

export function ActivityStrip({ strip, running, label }: { strip: Strip; running: boolean; label: string }) {
  const cols = strip.buckets.length;
  const grid = { gridTemplateColumns: `repeat(${cols}, minmax(0, 1fr))` };
  return (
    <div className="min-w-0">
      <div role="img" aria-label={`${label}: ${strip.summary}`} className="relative">
        <div aria-hidden="true" className="grid h-4 items-end" style={grid}>
          {strip.buckets.map((b) => {
            const mark = primaryMark(b.marks);
            return (
              <span
                key={b.minutesAgo}
                title={b.marks.length ? `${b.minutesAgo}m ago: ${b.marks.map((m) => MARK_LABEL[m]).join(', ')}` : undefined}
                className={clsx(
                  'flex justify-center overflow-visible font-mono text-[10px] leading-none',
                  mark && ALARM_MARKS.has(mark) ? 'font-bold text-signal-ink' : 'text-ink',
                )}
              >
                {mark ? MARK_GLYPH[mark] : ''}
              </span>
            );
          })}
        </div>
        <div aria-hidden="true" className="mt-0.5 grid gap-x-px" style={grid}>
          {strip.buckets.map((b, i) => {
            const density = Math.min(1, b.count / 4);
            return (
              <span key={b.minutesAgo} className="grid gap-px">
                {Array.from({ length: ROWS }, (_, row) => (
                  <span
                    key={row}
                    className={clsx(
                      'block h-[3px]',
                      b.count > 0 && lit(density * (0.55 + 0.45 * ((ROWS - row) / ROWS)), i, row)
                        ? b.marks.some((m) => ALARM_MARKS.has(m))
                          ? 'bg-signal'
                          : 'bg-ink-2'
                        : 'bg-transparent',
                    )}
                  />
                ))}
              </span>
            );
          })}
        </div>
        <div aria-hidden="true" className="relative mt-1 h-2">
          <span className="crew-track absolute inset-x-0 top-1/2 -translate-y-1/2" />
          {running && (
            <>
              <span className="crew-packet" />
              <span className="crew-packet" style={{ ['--packet-delay' as string]: '-1.6s' }} />
            </>
          )}
          <span className={clsx('absolute right-0 top-1/2 h-2 w-2 -translate-y-1/2', running ? 'bg-signal' : 'bg-ink-3')} />
        </div>
      </div>
      <div aria-hidden="true" className="mt-1 flex justify-between font-mono text-[10px] text-ink-3">
        <span>60m</span>
        <span>30m</span>
        <span>now</span>
      </div>
    </div>
  );
}
