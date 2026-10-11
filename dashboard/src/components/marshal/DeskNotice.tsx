import type { DeskNotice as Notice } from '../../lib/marshalDesk';

/** One line above the prompt that says why it is off: offline, the day's limit, opted out, signed out. */
export function DeskNotice({ notice }: { notice: Notice }) {
  return (
    <p
      role="status"
      data-notice={notice.state}
      className="flex gap-2 border-t border-rule bg-paper-2 px-4 py-2 font-mono text-[11px] leading-relaxed text-ink-2"
    >
      <span aria-hidden="true" className="text-ink-3">
        ›
      </span>
      <span className="min-w-0 [overflow-wrap:anywhere]">{notice.message}</span>
    </p>
  );
}
