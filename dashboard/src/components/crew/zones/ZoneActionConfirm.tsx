// Every human action on a zone asks for a reason (spec §9.5): it goes into the
// audit trail and the event log. Transfer and Grant also pick the session that
// receives the zone. Revoking an active holder warns it is stopped at its next
// tool call.

import { useId, useState } from 'react';
import { Loader2 } from 'lucide-react';
import { sessionLabel } from '../../../lib/crew/selectors';
import type { SessionState } from '../../../lib/crew/types';
import { actionErrorText } from '../policy/stepUp';
import { StepUpCancelled } from '../policy/useHumanAction';
import { Modal } from './Modal';

const REASON_MAX = 280;

export interface ZoneActionSpec {
  title: string;
  /** What will happen, in one or two sentences. */
  consequence: string;
  /** A warning line in the alarm style (e.g. revoking an active holder). */
  warning?: string | null;
  confirmLabel: string;
  danger?: boolean;
  /** Sessions to pick from (grant, transfer); omitted: no picker. */
  sessions?: SessionState[];
  /** Ask for a reason (default true). */
  reason?: boolean;
  /** Optional "until" time for a freeze. */
  until?: boolean;
}

export function ZoneActionConfirm({
  spec,
  onConfirm,
  onClose,
}: {
  spec: ZoneActionSpec;
  onConfirm: (input: { reason: string; sessionId: string | null; until: string | null }) => Promise<void>;
  onClose: () => void;
}) {
  const id = useId();
  const [reason, setReason] = useState('');
  const [sessionId, setSessionId] = useState<string>(spec.sessions?.[0]?.id ?? '');
  const [until, setUntil] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const askReason = spec.reason !== false;
  const trimmed = reason.trim();
  const tooLong = new TextEncoder().encode(trimmed).length > REASON_MAX;
  const needSession = spec.sessions !== undefined;
  const canSubmit = (!askReason || (trimmed.length > 0 && !tooLong)) && (!needSession || !!sessionId) && !busy;

  const submit = async () => {
    if (!canSubmit) return;
    setBusy(true);
    setError(null);
    try {
      const untilIso = spec.until && until ? new Date(until).toISOString() : null;
      await onConfirm({ reason: trimmed, sessionId: needSession ? sessionId : null, until: untilIso });
      onClose();
    } catch (err) {
      if (err instanceof StepUpCancelled) setError('Not done: sign-in was cancelled.');
      else setError(actionErrorText(err));
      setBusy(false);
    }
  };

  return (
    <Modal labelledBy={`${id}-title`} onClose={onClose} className="cz-dialog p-5" top initialFocus={needSession ? 'select' : 'textarea'}>
      <form
        onSubmit={(e) => {
          e.preventDefault();
          void submit();
        }}
      >
        <p className="rr-eyebrow">Human action</p>
        <h2 id={`${id}-title`} className="font-display mt-1 text-xl font-bold text-ink">
          {spec.title}
        </h2>
        <p className="mt-2 text-sm text-ink-2">{spec.consequence}</p>
        {spec.warning && (
          <p className="mt-3 border-l-[3px] border-signal bg-signal-wash px-3 py-2 text-sm text-ink" role="note">
            {spec.warning}
          </p>
        )}
        {needSession && (
          <label className="mt-4 block text-sm">
            <span className="font-mono text-[11px] uppercase tracking-[0.08em] text-ink-3">To session</span>
            {spec.sessions!.length ? (
              <select className="rr-input mt-1 w-full px-2 py-2 font-mono text-[13px]" value={sessionId} onChange={(e) => setSessionId(e.target.value)}>
                {spec.sessions!.map((s) => (
                  <option key={s.id} value={s.id}>
                    {sessionLabel(s)}
                  </option>
                ))}
              </select>
            ) : (
              <span className="mt-1 block text-ink-3">No live session to hand it to.</span>
            )}
          </label>
        )}
        {spec.until && (
          <label className="mt-4 block text-sm">
            <span className="font-mono text-[11px] uppercase tracking-[0.08em] text-ink-3">Until (optional)</span>
            <input type="datetime-local" className="rr-input mt-1 w-full px-2 py-2 font-mono text-[13px]" value={until} onChange={(e) => setUntil(e.target.value)} />
          </label>
        )}
        {askReason && (
          <label className="mt-4 block text-sm">
            <span className="font-mono text-[11px] uppercase tracking-[0.08em] text-ink-3">Reason (recorded in the audit trail)</span>
            <textarea
              className="rr-input mt-1 w-full px-2 py-2 text-[14px]"
              rows={3}
              value={reason}
              maxLength={REASON_MAX * 2}
              onChange={(e) => setReason(e.target.value)}
              placeholder="e.g. editing POS myself"
            />
            {tooLong && <span className="text-xs text-fail">Too long (max {REASON_MAX} characters).</span>}
          </label>
        )}
        {error && (
          <p role="alert" className="mt-3 border-l-[3px] border-fail bg-fail-wash px-3 py-2 text-sm text-ink">
            {error}
          </p>
        )}
        <div className="mt-5 flex flex-wrap justify-end gap-2">
          <button type="button" className="rr-btn-ghost px-3 py-2 text-sm" onClick={onClose}>
            Cancel
          </button>
          <button
            type="submit"
            disabled={!canSubmit}
            className={spec.danger ? 'inline-flex items-center gap-2 rounded-[3px] border border-fail bg-fail px-3 py-2 text-sm font-semibold text-white disabled:opacity-50' : 'rr-btn-primary inline-flex items-center gap-2 px-3 py-2 text-sm'}
          >
            {busy && <Loader2 className="h-4 w-4 animate-spin" aria-hidden="true" />}
            {spec.confirmLabel}
          </button>
        </div>
      </form>
    </Modal>
  );
}
