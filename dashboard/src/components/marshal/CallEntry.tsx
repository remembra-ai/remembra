import { RotateCcw } from 'lucide-react';
import { CONTACT_URL, DESK_COPY, allowedHref, hrefLabel, type DeskEntry, type OpenRequest } from '../../lib/marshalDesk';
import { DeskCommands } from './DeskCommand';
import { EvidenceChip } from './EvidenceChip';
import { InlineText } from './InlineText';
import { UsageFooter } from './UsageFooter';
import { NODE_ROW } from './rows';

const [ASK_A_PERSON, CONTACT_LABEL] = DESK_COPY.fallbackDoc.split(' · ');

/**
 * The call: a solid ink node, at most three sentences in the body face, the
 * reads it rests on as chips, the commands and the cost. An answer that
 * quotes a remembra.dev page (a price) links it under "the page governs". A
 * validation fallback shows the deterministic read lines and "That's all I
 * can confirm." with a person to ask; an error shows the server's sentence.
 */
export function CallEntry({
  entry,
  blocked = false,
  onAsk,
}: {
  entry: DeskEntry;
  /** The model can't take a question now: `ask again` is off. */
  blocked?: boolean;
  onAsk: (request: OpenRequest) => void;
}) {
  const { answer, error, usage } = entry;
  const page = answer && !answer.fallback && answer.doc ? allowedHref(answer.doc) : null;
  return (
    <div className={NODE_ROW}>
      <span aria-hidden="true" className="relative z-[1] mt-[7px] h-2 w-2 justify-self-center bg-ink" />
      <div className="min-w-0 space-y-2.5">
        {answer?.fallback && answer.summary.length > 0 && (
          <ul aria-label="What Marshal read" className="space-y-0.5 font-mono text-[11px] leading-relaxed text-ink-2">
            {answer.summary.map((line, index) => (
              <li key={index} className="[overflow-wrap:anywhere]">
                {line}
              </li>
            ))}
          </ul>
        )}
        {answer && (
          <p className="text-[15px] leading-[1.5] text-ink [overflow-wrap:anywhere]">
            <InlineText text={answer.text} />
          </p>
        )}
        {page && (
          <p className="font-mono text-[11px] text-ink-3 [overflow-wrap:anywhere]">
            {DESK_COPY.pagesGovern} ·{' '}
            <a
              href={page}
              target="_blank"
              rel="noopener noreferrer"
              className="text-ink underline decoration-rule underline-offset-2 hover:decoration-signal"
            >
              {hrefLabel(page)}
            </a>
          </p>
        )}
        {answer?.fallback && (
          <p className="font-mono text-[11px] text-ink-3">
            {ASK_A_PERSON} ·{' '}
            <a
              href={(answer.doc && allowedHref(answer.doc)) || CONTACT_URL}
              target="_blank"
              rel="noopener noreferrer"
              className="text-ink underline decoration-rule underline-offset-2 hover:decoration-signal"
            >
              {CONTACT_LABEL}
            </a>
          </p>
        )}
        {error && <p className="text-[15px] leading-[1.5] text-ink">{error.message}</p>}
        {answer && answer.evidence.length > 0 && (
          <ul aria-label="Evidence" className="flex flex-wrap gap-1.5">
            {answer.evidence.map((evidence, index) => (
              <li key={evidence.ref} className="min-w-0 max-w-full">
                <EvidenceChip n={index + 1} evidence={evidence} />
              </li>
            ))}
          </ul>
        )}
        {answer && <DeskCommands commands={answer.commands} />}
        {usage && <UsageFooter usage={usage} />}
        {error?.retryable && entry.done && (
          <button
            type="button"
            disabled={blocked}
            onClick={() => onAsk({ question: entry.question, agentId: entry.context?.agent_id, ask: true, source: entry.source })}
            className="rr-btn-ghost inline-flex min-h-11 items-center gap-1.5 px-2.5 font-mono text-[11px] disabled:cursor-not-allowed disabled:opacity-50 sm:min-h-0 sm:py-1"
          >
            <RotateCcw className="h-3 w-3" aria-hidden="true" /> ask again
          </button>
        )}
      </div>
    </div>
  );
}
