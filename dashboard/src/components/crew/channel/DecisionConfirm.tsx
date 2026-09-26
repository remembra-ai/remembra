// Decisions (§5.7, D36). An agent-created decision is only `proposed`: it is
// never injected into briefs or mirrored to memory until a human confirms it.
// DecisionConfirm is that human step; DecisionPin lists what is in force.

import { useState } from 'react';
import { Loader2 } from 'lucide-react';
import { toast } from 'sonner';
import { crewApi } from '../../../lib/crew/api';
import type { CrewState, DecisionView, SessionView } from '../../../lib/crew/types';
import { decisionAuthor, decisionRef } from './model';
import { PixelGlyph } from './pixels';
import { actionError } from './useChannel';

export function DecisionConfirm({
  decision,
  state,
  human,
  compact = false,
  sessions = [],
  onDone,
}: {
  decision: DecisionView;
  state: CrewState | null;
  human: boolean | null;
  compact?: boolean;
  /** Sessions to name the proposer when there is no live crew state (all-projects inbox). */
  sessions?: readonly SessionView[];
  onDone?: (outcome: 'confirmed' | 'rejected') => void;
}) {
  const [busy, setBusy] = useState<'confirm' | 'reject' | null>(null);
  const [error, setError] = useState<string | null>(null);
  const act = (what: 'confirm' | 'reject') => {
    setBusy(what);
    setError(null);
    const call = what === 'confirm' ? crewApi.confirmDecision(decision.id) : crewApi.rejectDecision(decision.id);
    call
      .then(() => {
        toast.success(
          what === 'confirm'
            ? `${decisionRef(decision)} is in force: it now leads every agent brief.`
            : `${decisionRef(decision)} rejected. Agents never saw it.`,
        );
        onDone?.(what === 'confirm' ? 'confirmed' : 'rejected');
      })
      .catch((err: unknown) => setError(actionError(err)))
      .finally(() => setBusy(null));
  };
  const blocked = human === false;
  return (
    <div className={compact ? '' : 'rr-card rounded-[3px] border-l-[3px] border-l-signal p-3.5'}>
      {!compact && (
        <p className="flex items-center gap-2 font-mono text-[11px] uppercase tracking-[0.08em] text-signal-ink">
          <PixelGlyph name="decision" size={12} /> {decisionRef(decision)} · to confirm
        </p>
      )}
      <p className="mt-1 font-display text-[15px] font-bold leading-snug text-ink [overflow-wrap:anywhere]">{decision.title}</p>
      {decision.decision && decision.decision !== decision.title && (
        <p className="mt-1 whitespace-pre-wrap text-sm text-ink-2 [overflow-wrap:anywhere]">{decision.decision}</p>
      )}
      <p className="mt-1.5 font-mono text-[11px] text-ink-3">proposed by {decisionAuthor(decision, state, sessions)}</p>
      <p className="mt-1 text-xs text-ink-3">Agents do not see a proposed decision until you confirm it.</p>
      <div className="mt-2.5 flex flex-wrap items-center gap-2">
        <button
          type="button"
          disabled={busy !== null || blocked}
          onClick={() => act('confirm')}
          className="rr-btn-primary inline-flex items-center gap-1.5 px-3 py-1.5 text-xs disabled:opacity-50"
        >
          {busy === 'confirm' ? <Loader2 className="h-3.5 w-3.5 animate-spin" aria-hidden="true" /> : <PixelGlyph name="check" size={11} mono />}
          Confirm
        </button>
        <button
          type="button"
          disabled={busy !== null || blocked}
          onClick={() => act('reject')}
          className="rr-btn-ghost inline-flex items-center gap-1.5 px-3 py-1.5 text-xs disabled:opacity-50"
        >
          {busy === 'reject' && <Loader2 className="h-3.5 w-3.5 animate-spin" aria-hidden="true" />}
          Reject
        </button>
        {blocked && <span className="text-xs text-ink-3">Sign in to the dashboard to decide (API keys cannot).</span>}
      </div>
      {error && (
        <p role="alert" className="mt-2 border-l-[3px] border-fail bg-fail-wash px-2.5 py-1.5 text-xs text-ink">
          {error}
        </p>
      )}
    </div>
  );
}

export function DecisionPin({ decisions, state }: { decisions: DecisionView[]; state: CrewState | null }) {
  if (!decisions.length) {
    return (
      <p className="text-sm text-ink-3">
        No decisions in force yet. Type <code className="font-mono text-[12px] text-ink-2">/decide</code> in the composer to set one.
      </p>
    );
  }
  return (
    <ul className="space-y-2.5">
      {decisions.map((d) => (
        <li key={d.id} className="flex gap-2.5">
          <span className="mt-0.5 shrink-0 whitespace-nowrap font-mono text-[11px] font-bold text-signal-ink">{decisionRef(d)}</span>
          <div className="min-w-0">
            <p className="text-sm font-semibold leading-snug text-ink [overflow-wrap:anywhere]">{d.title}</p>
            <p className="font-mono text-[11px] text-ink-3">
              {d.decided_by_kind === 'human' ? 'set by a human' : `from ${decisionAuthor(d, state)}, confirmed by a human`}
            </p>
          </div>
        </li>
      ))}
    </ul>
  );
}
