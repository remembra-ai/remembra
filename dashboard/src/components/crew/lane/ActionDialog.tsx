// The dialog behind every human lane / slot action: pick a target and zones
// when the action needs them, give a reason, confirm. Focus is trapped, Escape
// closes, and a refusal from the server is shown in words (step-up, human-only).

import { useEffect, useId, useRef, useState, type KeyboardEvent } from 'react';
import { toast } from 'sonner';
import { flowErrorMessage } from '../../../lib/crew/commands';
import { LANE_ACTIONS, REASON_MAX, runLaneAction, validateAction, type LaneActionInput, type LaneApi } from './actions';

export interface TargetOption {
  id: string;
  label: string;
  detail?: string;
}

export interface ClaimOption {
  id: string;
  label: string;
  /** Untrusted zone title (plain text). */
  title?: string | null;
}

export interface ActionRequest {
  input: Omit<LaneActionInput, 'reason' | 'to' | 'claimIds'> & { claimIds?: string[] };
  /** "cc-1", "the POS baton" … */
  subject: string;
  targets?: TargetOption[];
  claims?: ClaimOption[];
}

export function ActionDialog({ request, api, onClose }: { request: ActionRequest; api: LaneApi; onClose: () => void }) {
  const def = LANE_ACTIONS[request.input.action];
  const titleId = useId();
  const reasonId = useId();
  const dialogRef = useRef<HTMLDivElement | null>(null);
  const [reason, setReason] = useState('');
  const [to, setTo] = useState<string | null>(request.targets?.length === 1 ? request.targets[0].id : null);
  const [picked, setPicked] = useState<string[]>(() => (def.pickClaims ? [] : (request.input.claimIds ?? [])));
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    const first = dialogRef.current?.querySelector<HTMLElement>('input, textarea, button');
    first?.focus();
  }, []);

  const input: LaneActionInput = {
    ...request.input,
    reason,
    to,
    claimIds: def.pickClaims ? picked : request.input.claimIds,
    names: { ...request.input.names, to: request.targets?.find((t) => t.id === to)?.label },
  };
  const problem = validateAction(input);

  const submit = async () => {
    if (problem || busy) {
      setError(problem);
      return;
    }
    setBusy(true);
    setError(null);
    try {
      const message = await runLaneAction(api, input);
      toast.success(message);
      onClose();
    } catch (err) {
      setError(flowErrorMessage(err));
      setBusy(false);
    }
  };

  const onKeyDown = (e: KeyboardEvent<HTMLDivElement>) => {
    if (e.key === 'Escape') {
      e.stopPropagation();
      onClose();
      return;
    }
    if (e.key !== 'Tab' || !dialogRef.current) return;
    const nodes = [...dialogRef.current.querySelectorAll<HTMLElement>('input, textarea, button:not([disabled])')];
    if (!nodes.length) return;
    const first = nodes[0];
    const last = nodes[nodes.length - 1];
    if (e.shiftKey && document.activeElement === first) {
      e.preventDefault();
      last.focus();
    } else if (!e.shiftKey && document.activeElement === last) {
      e.preventDefault();
      first.focus();
    }
  };

  return (
    <div className="modal-backdrop fixed inset-0 z-50 flex items-end justify-center p-4 sm:items-center" onMouseDown={onClose}>
      <div
        ref={dialogRef}
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        onKeyDown={onKeyDown}
        onMouseDown={(e) => e.stopPropagation()}
        className="modal-surface w-full max-w-md rounded-[3px]"
      >
        <div className="border-b border-rule px-4 py-3">
          <p className="rr-eyebrow">Human action · {request.subject}</p>
          <h2 id={titleId} className="font-display mt-1 text-lg font-bold text-ink">
            {def.label}
          </h2>
        </div>
        <form
          className="space-y-4 px-4 py-4"
          onSubmit={(e) => {
            e.preventDefault();
            void submit();
          }}
        >
          {request.claims && def.pickClaims && (
            <fieldset>
              <legend className="font-mono text-[11px] uppercase tracking-[0.08em] text-ink-3">Zones</legend>
              <div className="mt-2 space-y-1.5">
                {request.claims.map((c) => (
                  <label key={c.id} className="flex items-center gap-2 text-sm text-ink">
                    <input
                      type="checkbox"
                      checked={picked.includes(c.id)}
                      onChange={(e) => setPicked((prev) => (e.target.checked ? [...prev, c.id] : prev.filter((x) => x !== c.id)))}
                    />
                    <span className="font-mono font-semibold">{c.label}</span>
                    {c.title && <span className="truncate text-ink-3">{c.title}</span>}
                  </label>
                ))}
              </div>
            </fieldset>
          )}
          {def.needsTarget && (
            <fieldset>
              <legend className="font-mono text-[11px] uppercase tracking-[0.08em] text-ink-3">Hand to</legend>
              {request.targets && request.targets.length > 0 ? (
                <div className="mt-2 space-y-1.5">
                  {request.targets.map((t) => (
                    <label key={t.id} className="flex items-center gap-2 text-sm text-ink">
                      <input type="radio" name={`${titleId}-to`} checked={to === t.id} onChange={() => setTo(t.id)} />
                      <span className="font-mono font-semibold">{t.label}</span>
                      {t.detail && <span className="truncate text-ink-3">{t.detail}</span>}
                    </label>
                  ))}
                </div>
              ) : (
                <p className="mt-2 text-sm text-ink-3">No other live agent on this crew. Start one in this repo first.</p>
              )}
            </fieldset>
          )}
          <div>
            <label htmlFor={reasonId} className="font-mono text-[11px] uppercase tracking-[0.08em] text-ink-3">
              Reason
            </label>
            <textarea
              id={reasonId}
              value={reason}
              maxLength={REASON_MAX}
              rows={2}
              onChange={(e) => setReason(e.target.value)}
              placeholder="Recorded in the audit trail"
              className="rr-input mt-1.5 block w-full px-3 py-2 text-sm"
            />
          </div>
          {def.stepUp && <p className="text-xs text-ink-3">This may ask you to sign in again (a login within the last 15 minutes).</p>}
          {error && (
            <p role="alert" className="border-l-[3px] border-fail bg-fail-wash px-3 py-2 text-sm text-ink">
              {error}
            </p>
          )}
          <div className="flex justify-end gap-2">
            <button type="button" onClick={onClose} className="rr-btn-ghost px-3 py-2 text-sm">
              Cancel
            </button>
            <button type="submit" disabled={busy} aria-disabled={!!problem} className="rr-btn-primary px-3 py-2 text-sm">
              {busy ? 'Working…' : def.confirm}
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}
