// The "why?" exchange slip under a waiting row of the Home setup checklist.
// A printout of the user's own relay: each read Marshal made (›), the call
// (=, proven [!!] or inferred [??]), the one fix and the doctor lines to copy.
// Rules only (lib/marshal.ts): no model, nothing written. Text only: no HTML
// is ever injected, so nothing a key name or trail entry holds can render as
// markup. Where the account has the Marshal desk, `ask Marshal about this`
// opens it with the question in the prompt (sent only on Enter).

import { useEffect, useState } from 'react';
import clsx from 'clsx';
import { RefreshCw } from 'lucide-react';
import { agentMeta, canonicalAgentId } from '../../lib/agents';
import {
  SLIP_FOOTER,
  dashboardSources,
  initialSlipState,
  readSlip,
  slipOutcome,
  type ReadLine,
  type SlipCommand,
  type SlipSources,
  type SlipState,
  type Verdict,
} from '../../lib/marshal';
import type { AgentActivity } from '../../lib/relay';
import { useMarshalDesk } from '../../hooks/marshalDesk';
import { SlipAskButton } from '../marshal/SlipAskButton';
import { CopyCommand } from './ui';

const LABEL_WIDTH = 'w-[8ch]';

function Read({ line }: { line: ReadLine }) {
  return (
    <p className="rr-win-line flex min-w-0 gap-2 text-ink-3">
      <span aria-hidden="true">›</span>
      <span className={clsx(LABEL_WIDTH, 'shrink-0')}>{line.label}</span>
      <span className={clsx('min-w-0 [overflow-wrap:anywhere]', line.failed ? 'text-fail' : 'text-ink-2')}>{line.text}</span>
    </p>
  );
}

function Mark({ proven, ok }: { proven: boolean; ok: boolean }) {
  if (ok) {
    return (
      <span className="shrink-0 text-ok">
        <span aria-hidden="true">[ok]</span>
        <span className="sr-only">(nothing to fix)</span>
      </span>
    );
  }
  return proven ? (
    <span className="shrink-0 text-fail">
      <span aria-hidden="true">[!!]</span>
      <span className="sr-only">(shown by your data)</span>
    </span>
  ) : (
    <span className="shrink-0 text-ink-3">
      <span aria-hidden="true">[??]</span>
      <span className="sr-only">(inferred, not proven)</span>
    </span>
  );
}

function CopyLines({ commands }: { commands: SlipCommand[] }) {
  return (
    <div className="mt-1.5 space-y-2">
      {commands.map((cmd) => (
        <div key={cmd.text} className="min-w-0">
          {cmd.caption && <p className="mb-1 text-ink-3">{cmd.caption}</p>}
          <CopyCommand command={cmd.text} label={cmd.label} prompt={cmd.prompt} scroll toastText="Copied" />
        </div>
      ))}
    </div>
  );
}

function Call({ verdict }: { verdict: Verdict }) {
  const ok = verdict.code === 'HANDED_OFF';
  return (
    <div className="rr-win-line mt-1.5 space-y-1.5">
      <p aria-hidden="true" className="text-ink-3">
        ┊
      </p>
      <p className="flex items-start gap-2 text-ink">
        <span aria-hidden="true">=</span>
        <span className="min-w-0 flex-1 font-semibold">{verdict.verdict}</span>
        <Mark proven={verdict.proven} ok={ok} />
      </p>
      {verdict.detail && <p className="pl-[2ch] text-ink-2">{verdict.detail}</p>}
      {verdict.unverified && <p className="pl-[2ch] text-ink-2">{verdict.unverified}</p>}
      {verdict.causes.length > 0 && (
        <div className="pl-[2ch] text-ink-2">
          <p>Likely one of:</p>
          <ol>
            {verdict.causes.map((cause, i) => (
              <li key={cause} className="flex gap-2">
                <span className="text-ink-3">{i + 1}</span>
                <span className="min-w-0">{cause}</span>
              </li>
            ))}
          </ol>
        </div>
      )}
      {verdict.fix && (
        <div>
          <p className="flex gap-2">
            <span className="shrink-0 font-bold text-signal-ink">fix →</span>
            <span className="min-w-0 text-ink">{verdict.fix.text}</span>
          </p>
          {verdict.fix.commands.length > 0 && <CopyLines commands={verdict.fix.commands} />}
        </div>
      )}
      {verdict.then && (
        <p className="flex gap-2">
          <span className={clsx(LABEL_WIDTH, 'shrink-0 text-ink-3')}>then</span>
          <span className="min-w-0 text-ink">{verdict.then}</span>
        </p>
      )}
      {verdict.check && (
        <div>
          <p className="flex gap-2">
            <span className={clsx(LABEL_WIDTH, 'shrink-0 text-ink-3')}>check</span>
            <span className="min-w-0 text-ink">{verdict.check.lead}</span>
          </p>
          <CopyLines commands={verdict.check.commands} />
        </div>
      )}
      {!ok && (
        <p className="flex gap-2">
          <span className={clsx(LABEL_WIDTH, 'shrink-0 text-ink-3')}>doc</span>
          <a
            href={verdict.doc}
            target="_blank"
            rel="noreferrer"
            className="min-w-0 text-ink underline decoration-rule underline-offset-2 [overflow-wrap:anywhere] hover:decoration-signal"
          >
            {verdict.doc.replace(/^https:\/\//, '')}
          </a>
        </p>
      )}
      {verdict.caveat && <p className="text-ink-3">{verdict.caveat}</p>}
    </div>
  );
}

/**
 * The slip itself, from a read state: the reading lines so far, then the call
 * once every read is in. Pure: WhySlip feeds it, and the tests render it.
 */
export function SlipView({
  id,
  agentId,
  state,
  summaryAgent,
  now,
  serverUrl,
  onRetry,
}: {
  id: string;
  agentId: string;
  state: SlipState;
  summaryAgent?: AgentActivity | null;
  now: Date;
  serverUrl?: string;
  onRetry?: () => void;
}) {
  const meta = agentMeta(agentId);
  const adapter = meta.adapter ?? canonicalAgentId(agentId);
  const outcome = slipOutcome(state, { agentId, summaryAgent, now, serverUrl });
  const desk = useMarshalDesk();
  return (
    <section id={id} aria-label={`Exchange check for ${meta.name}`} className="rr-win mt-1 mb-3 min-w-0 text-[11px]">
      <div className="rr-win-bar">
        <i aria-hidden="true" />
        exchange check · {adapter}
      </div>
      <div className="min-w-0 px-3 py-2.5 font-mono text-[11px] leading-relaxed">
        <div aria-live="polite" aria-busy={outcome.pending}>
          {outcome.lines.map((line) => (
            <Read key={line.label} line={line} />
          ))}
          {outcome.pending && (
            <p className="text-ink-3">
              <span aria-hidden="true">› </span>reading…
            </p>
          )}
          {outcome.verdict && <Call verdict={outcome.verdict} />}
        </div>
        {!outcome.pending && outcome.failed && (
          <p className="mt-2 flex flex-wrap items-center gap-2 text-ink-2">
            <span>No verdict without every read.</span>
            {onRetry && (
              <button
                type="button"
                onClick={onRetry}
                className="rr-btn-ghost inline-flex min-h-11 items-center gap-1.5 px-2.5 font-mono text-[11px] sm:min-h-0 sm:py-1"
              >
                <RefreshCw className="h-3 w-3" aria-hidden="true" /> read again
              </button>
            )}
          </p>
        )}
        {!outcome.pending && <p className="mt-2 text-ink-3">{SLIP_FOOTER}</p>}
        {!outcome.pending && desk.available && !desk.optedOut && <SlipAskButton agentId={agentId} open={desk.open} />}
      </div>
    </section>
  );
}

/** Reads on open (three GETs in parallel) and shows each line as its read finishes. */
export function WhySlip({
  id,
  agentId,
  summaryAgent,
  now,
  serverUrl,
  sources,
}: {
  id: string;
  agentId: string;
  summaryAgent?: AgentActivity | null;
  now: Date;
  serverUrl?: string;
  sources?: SlipSources;
}) {
  const [state, setState] = useState<SlipState>(initialSlipState);
  const [attempt, setAttempt] = useState(0);
  useEffect(() => {
    let live = true;
    void readSlip(sources ?? dashboardSources(agentId), (next) => {
      if (live) setState(next);
    });
    return () => {
      live = false;
    };
    // `sources` is a test seam; a new object each render must not re-read.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [agentId, attempt]);
  return (
    <SlipView
      id={id}
      agentId={agentId}
      state={state}
      summaryAgent={summaryAgent}
      now={now}
      serverUrl={serverUrl}
      onRetry={() => {
        setState(initialSlipState());
        setAttempt((n) => n + 1);
      }}
    />
  );
}
