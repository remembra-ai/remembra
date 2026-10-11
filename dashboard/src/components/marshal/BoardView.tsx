import clsx from 'clsx';
import { DESK_COPY, callAgentLabel, type DeskBoard, type DeskNotice, type OpenRequest } from '../../lib/marshalDesk';
import { NODE_ROW } from './rows';

function CallMark({ proven }: { proven: boolean }) {
  return proven ? (
    <span className="whitespace-nowrap font-mono text-[11px] text-fail">
      <span aria-hidden="true">[!!]</span>
      <span className="sr-only">(shown by your data)</span>
    </span>
  ) : (
    <span className="whitespace-nowrap font-mono text-[11px] text-ink-3">
      <span aria-hidden="true">[??]</span>
      <span className="sr-only">(inferred, not proven)</span>
    </span>
  );
}

const GHOST = 'rr-btn-ghost min-h-11 px-2 font-mono text-[11px] disabled:cursor-not-allowed disabled:opacity-50 sm:min-h-0 sm:py-0.5';

/**
 * What the desk opens on: one to three calls the rules made about this
 * account, each with `see why`, and questions built from the same state.
 * No greeting, no model: the footer says so.
 */
export function BoardView({
  board,
  boardError,
  blocked,
  onAsk,
}: {
  board: DeskBoard | null;
  boardError: DeskNotice | null;
  /** The model can't take a question now (offline, the day's limit): the asks are off. */
  blocked: boolean;
  onAsk: (request: OpenRequest) => void;
}) {
  if (!board) {
    return (
      <p className={clsx('font-mono text-[11px] leading-relaxed', boardError ? 'text-fail' : 'text-ink-3')}>
        <span aria-hidden="true">› </span>
        {boardError ? boardError.message : DESK_COPY.reading}
      </p>
    );
  }
  return (
    <div className="space-y-3">
      {board.calls.length > 0 ? (
        <ul aria-label="Calls on this account" className="space-y-2.5">
          {board.calls.map((call) => (
            <li key={`${call.agent_id ?? 'account'}:${call.code}`} className={NODE_ROW}>
              <span aria-hidden="true" className="justify-self-center font-mono text-[11px] leading-[22px] text-ink">
                ●
              </span>
              <div className="flex min-w-0 flex-wrap items-baseline gap-x-2.5 gap-y-1">
                <b className="font-mono text-[11px] font-bold text-ink">{callAgentLabel(call.agent_id)}</b>
                <p className="min-w-0 flex-1 basis-64 text-[15px] leading-[1.5] text-ink">
                  {call.text} <CallMark proven={call.proven} />
                </p>
                <button
                  type="button"
                  disabled={blocked}
                  onClick={() => onAsk({ question: call.ask, agentId: call.agent_id, ask: true, source: 'board' })}
                  className={GHOST}
                >
                  see why
                </button>
              </div>
            </li>
          ))}
        </ul>
      ) : (
        <p className="font-mono text-[11px] leading-relaxed text-ink-2">
          <span aria-hidden="true">● </span>
          {board.status_line}
        </p>
      )}
      {board.suggestions.length > 0 && (
        <ul aria-label="Questions to ask" className="flex flex-wrap gap-1.5">
          {board.suggestions.map((question) => (
            <li key={question}>
              <button
                type="button"
                disabled={blocked}
                onClick={() => onAsk({ question, ask: true, source: 'board' })}
                className={GHOST}
              >
                {question}
              </button>
            </li>
          ))}
        </ul>
      )}
      <p className="font-mono text-[11px] text-ink-3">{board.footer}</p>
    </div>
  );
}
