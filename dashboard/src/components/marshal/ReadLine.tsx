import clsx from 'clsx';
import { DESK_COPY, type ReadEvent } from '../../lib/marshalDesk';
import { NODE_ROW } from './rows';

/** One read Marshal made: `› <label> · <summary> · <ms>ms`, dim; a failed read's summary in the fail colour. */
export function ReadLine({ read }: { read: ReadEvent }) {
  return (
    <p className={clsx(NODE_ROW, 'rr-win-line font-mono text-[11px] leading-relaxed text-ink-3')}>
      <span aria-hidden="true" />
      <span className="min-w-0 [overflow-wrap:anywhere]">
        <span aria-hidden="true">› </span>
        {read.label} · <span className={read.ok ? 'text-ink-2' : 'text-fail'}>{read.summary}</span> · {read.ms}ms
      </span>
    </p>
  );
}

/** The only progress indicator: `› reading…` until the call arrives. */
export function ReadingLine() {
  return (
    <p className={clsx(NODE_ROW, 'font-mono text-[11px] leading-relaxed text-ink-3')}>
      <span aria-hidden="true" />
      <span>
        <span aria-hidden="true">› </span>
        {DESK_COPY.reading}
      </span>
    </p>
  );
}

/** A question closed before its answer arrived. */
export function StoppedLine() {
  return (
    <p className={clsx(NODE_ROW, 'font-mono text-[11px] leading-relaxed text-ink-3')}>
      <span aria-hidden="true" />
      <span>
        <span aria-hidden="true">› </span>
        stopped before the answer
      </span>
    </p>
  );
}
