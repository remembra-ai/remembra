import { TrailMark } from '../../brand/TrailMark';
import type { DeskOpen } from '../../hooks/marshalDesk';
import { slipAsk } from '../../lib/marshalDesk';

/**
 * `ask Marshal about this` under a why? slip: opens the desk with
 * "why is <agent> waiting" in the prompt, scoped to that agent. Nothing is
 * sent until the user presses Enter there.
 */
export function SlipAskButton({ agentId, open }: { agentId: string; open: (request: DeskOpen) => void }) {
  return (
    <button
      type="button"
      onClick={(event) => open({ ...slipAsk(agentId), invoker: event.currentTarget })}
      className="rr-btn-ghost mt-2 inline-flex min-h-11 items-center gap-1.5 px-2.5 font-mono text-[11px] sm:min-h-0 sm:py-1"
    >
      <TrailMark className="shrink-0" /> ask Marshal about this
    </button>
  );
}
