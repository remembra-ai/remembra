// Issue a bypass code (D34, §8.4): a human-issued, single-use code of at most
// 15 minutes that lets ONE named session past the gate once, for one scope
// (a commit, a push, or writes in one zone). Needs a fresh login (step-up).
// The code is shown once here and never again; every use is a moment, an
// audit row and a real-time alert. This mechanism is for people only and is
// never shown to agents.

import { useId, useState } from 'react';
import { Check, Copy, Loader2 } from 'lucide-react';
import type { CrewApi } from '../../../lib/crew/api';
import { sessionLabel, sortedZones } from '../../../lib/crew/selectors';
import type { CrewState, SessionState } from '../../../lib/crew/types';
import { useCopy } from '../../../hooks/useCopy';
import { useNow } from '../../../hooks/useResource';
import { absoluteTime } from '../../../lib/time';
import { Modal } from '../zones/Modal';
import { actionErrorText } from './stepUp';
import { StepUpCancelled, type HumanActionRunner } from './useHumanAction';
import { BYPASS_MINUTES, bypassUsage, codeTimeLeft } from './policyModel';

interface Issued {
  code: string;
  code_id: string;
  session_id: string;
  scope: string;
  expires_at: string;
}

function IssuedCode({ issued, state, offsetMs, onClose }: { issued: Issued; state: CrewState; offsetMs: number; onClose: () => void }) {
  const [copy, copied] = useCopy();
  const now = useNow(1000);
  const left = codeTimeLeft(issued.expires_at, now.getTime(), offsetMs);
  const usage = bypassUsage(issued.scope, issued.code);
  return (
    <div>
      <p className="rr-eyebrow">Shown once</p>
      <p className="cz-code mt-2 text-ink" aria-label={`Bypass code ${issued.code.split('').join(' ')}`}>
        {issued.code}
      </p>
      <p className="mt-2 font-mono text-[12px] text-ink-2">
        for {state.sessions[issued.session_id]?.callsign ?? issued.session_id} · {issued.scope} · single use ·{' '}
        <span className={left ? 'text-signal-ink' : 'text-fail'} title={absoluteTime(issued.expires_at)}>
          {left ?? 'expired'}
        </span>
      </p>
      <div className="mt-3 rr-cmd flex items-stretch rounded-[3px]">
        <code className="min-w-0 flex-1 overflow-x-auto px-3 py-2.5 font-mono text-[12.5px]">{usage}</code>
        <button
          type="button"
          className="flex shrink-0 items-center gap-1.5 border-l border-white/15 px-3 font-mono text-xs text-head-ink hover:bg-signal/20"
          onClick={() => copy(usage, 'Copied')}
          aria-label="Copy the command with the code"
        >
          {copied ? <Check className="h-3.5 w-3.5 text-signal" aria-hidden="true" /> : <Copy className="h-3.5 w-3.5" aria-hidden="true" />} Copy
        </button>
      </div>
      <p className="mt-3 text-sm text-ink-2">Type it yourself at that checkout's terminal. It is not stored anywhere you can read it again.</p>
      <div className="mt-4 flex justify-end">
        <button type="button" className="rr-btn-primary px-3 py-2 text-sm" onClick={onClose}>
          Done
        </button>
      </div>
    </div>
  );
}

export function BypassCodeDialog({
  crewId,
  state,
  sessions,
  offsetMs,
  api,
  runner,
  onIssued,
  onClose,
}: {
  crewId: string;
  state: CrewState;
  sessions: SessionState[];
  offsetMs: number;
  api: CrewApi;
  runner: HumanActionRunner;
  onIssued: () => void;
  onClose: () => void;
}) {
  const id = useId();
  const [sessionId, setSessionId] = useState(sessions[0]?.id ?? '');
  const [scope, setScope] = useState('push');
  const [minutes, setMinutes] = useState<number>(5);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [issued, setIssued] = useState<Issued | null>(null);
  const scopes = [
    { value: 'commit', label: 'commit: one commit past the commit gate' },
    { value: 'push', label: 'push: one push past the pre-push gate' },
    ...sortedZones(state)
      .filter((z) => !z.builtin)
      .map((z) => ({ value: `write:${z.slug}`, label: `write:${z.slug}: edits in zone ${z.slug}` })),
  ];

  const submit = async () => {
    if (!sessionId || busy) return;
    setBusy(true);
    setError(null);
    try {
      const res = await runner.run('Issuing a bypass code', () => api.issueBypassCode(crewId, { session_id: sessionId, scope, minutes }));
      setIssued(res);
      onIssued();
    } catch (err) {
      setError(err instanceof StepUpCancelled ? 'Not issued: sign-in was cancelled.' : actionErrorText(err));
    } finally {
      setBusy(false);
    }
  };

  return (
    <Modal labelledBy={`${id}-t`} onClose={onClose} className="cz-dialog p-5" initialFocus={issued ? 'button' : 'select'}>
      <h2 id={`${id}-t`} className="font-display text-xl font-bold text-ink">
        {issued ? 'Bypass code' : 'Issue a bypass code'}
      </h2>
      {issued ? (
        <div className="mt-3">
          <IssuedCode issued={issued} state={state} offsetMs={offsetMs} onClose={onClose} />
        </div>
      ) : (
        <form
          className="mt-2 space-y-4"
          onSubmit={(e) => {
            e.preventDefault();
            void submit();
          }}
        >
          <p className="text-sm text-ink-2">
            Lets one agent session past the gate once, for one thing, for at most 15 minutes. The use is recorded, alerted and shown in the feed.
          </p>
          <label className="block text-sm">
            <span className="font-mono text-[11px] uppercase tracking-[0.08em] text-ink-3">Session</span>
            {sessions.length ? (
              <select className="rr-input mt-1 w-full px-2 py-2 font-mono text-[13px]" value={sessionId} onChange={(e) => setSessionId(e.target.value)}>
                {sessions.map((s) => (
                  <option key={s.id} value={s.id}>
                    {sessionLabel(s)}
                  </option>
                ))}
              </select>
            ) : (
              <span className="mt-1 block text-ink-3">No live session.</span>
            )}
          </label>
          <label className="block text-sm">
            <span className="font-mono text-[11px] uppercase tracking-[0.08em] text-ink-3">What it may pass</span>
            <select className="rr-input mt-1 w-full px-2 py-2 font-mono text-[13px]" value={scope} onChange={(e) => setScope(e.target.value)}>
              {scopes.map((s) => (
                <option key={s.value} value={s.value}>
                  {s.label}
                </option>
              ))}
            </select>
          </label>
          <fieldset>
            <legend className="font-mono text-[11px] uppercase tracking-[0.08em] text-ink-3">Valid for</legend>
            <div className="cz-seg mt-1">
              {BYPASS_MINUTES.map((m) => (
                <button key={m} type="button" aria-pressed={minutes === m} onClick={() => setMinutes(m)}>
                  {m} min
                </button>
              ))}
            </div>
          </fieldset>
          {error && (
            <p role="alert" className="border-l-[3px] border-fail bg-fail-wash px-3 py-2 text-sm text-ink">
              {error}
            </p>
          )}
          <div className="flex justify-end gap-2">
            <button type="button" className="rr-btn-ghost px-3 py-2 text-sm" onClick={onClose}>
              Cancel
            </button>
            <button type="submit" disabled={!sessionId || busy} className="rr-btn-primary inline-flex items-center gap-2 px-3 py-2 text-sm">
              {busy && <Loader2 className="h-4 w-4 animate-spin" aria-hidden="true" />}
              Issue code
            </button>
          </div>
        </form>
      )}
    </Modal>
  );
}
