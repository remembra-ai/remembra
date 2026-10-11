import { NODE_ROW } from './rows';
import type { DeskEntry, OpenRequest } from '../../lib/marshalDesk';
import { CallEntry } from './CallEntry';
import { QuestionEntry } from './QuestionEntry';
import { ReadLine, ReadingLine, StoppedLine } from './ReadLine';

/**
 * The conversation as a trail: each question a hollow node on the dashed
 * rail, each read a dim line under it as it streams in, the call a solid node.
 * Announced politely, additions only.
 */
export function Transcript({
  entries,
  asking,
  blocked = false,
  onAsk,
  onNewConversation,
}: {
  entries: DeskEntry[];
  asking: boolean;
  /** The model can't take a question now (offline, the day's limit). */
  blocked?: boolean;
  onAsk: (request: OpenRequest) => void;
  onNewConversation: () => void;
}) {
  return (
    <div className="space-y-3">
      <div role="log" aria-live="polite" aria-relevant="additions" aria-label="Marshal's answers" className="relative">
        <span aria-hidden="true" className="rr-rail absolute bottom-3 left-[11px] top-2 w-0.5" />
        {entries.map((entry) => (
          <div key={entry.id} className="relative space-y-1.5 pb-5 last:pb-1">
            <QuestionEntry question={entry.question} />
            {entry.reads.map((read) => (
              <ReadLine key={read.id} read={read} />
            ))}
            {!entry.done && !entry.answer && !entry.error && <ReadingLine />}
            {(entry.answer || entry.error) && <CallEntry entry={entry} blocked={blocked} onAsk={onAsk} />}
            {entry.stopped && !entry.answer && !entry.error && <StoppedLine />}
          </div>
        ))}
      </div>
      {!asking && entries.length > 0 && (
        <div className={NODE_ROW}>
          <span aria-hidden="true" />
          <button
            type="button"
            onClick={onNewConversation}
            className="rr-btn-ghost min-h-11 justify-self-start px-2 font-mono text-[11px] sm:min-h-0 sm:py-0.5"
          >
            new conversation
          </button>
        </div>
      )}
    </div>
  );
}
