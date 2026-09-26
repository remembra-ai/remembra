// "Pick up baton" (§9.7, H): hand a stalled task to a running session. The
// server moves or grants the task's claims, records the baton (human_assign)
// and the offer, and the session gets the saved work when it adopts.

import { useState } from 'react';
import clsx from 'clsx';
import { toast } from 'sonner';
import type { CrewApi } from '../../../lib/crew/api';
import { liveSessions, presenceText } from '../../../lib/crew/selectors';
import type { CrewState } from '../../../lib/crew/types';
import { explainActionError, pickUpBaton, type ActionResult } from './actions';
import { Dialog } from './Dialog';
import { ownerOf, savedWork, type TaskDetail } from './model';
import { CopyCommand } from '../../relay/ui';

export function PickUpBaton({
  api,
  task,
  state,
  onClose,
  onChanged,
}: {
  api: CrewApi;
  task: TaskDetail;
  state: CrewState | null;
  onClose: () => void;
  onChanged: (result: ActionResult) => void;
}) {
  const sessions = state ? liveSessions(state).filter((s) => s.id !== task.owner_session_id && s.state !== 'quota_blocked') : [];
  const [choice, setChoice] = useState<string | null>(sessions.length === 1 ? sessions[0].id : null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const owner = ownerOf(task, state);
  const saved = savedWork(task, state);
  const labelId = `pickup-${task.id}`;

  const submit = async () => {
    if (!choice) return;
    setBusy(true);
    setError(null);
    try {
      const res = await pickUpBaton(api, task.id, choice);
      const who = state?.sessions[choice]?.callsign ?? 'the session';
      toast.success(`T-${task.number} handed to ${who}`);
      onChanged(res);
      onClose();
    } catch (err) {
      setError(explainActionError(err));
    } finally {
      setBusy(false);
    }
  };

  return (
    <Dialog labelId={labelId} eyebrow="Stalled baton" title={`Pick up T-${task.number}`} onClose={onClose}>
      <div className="space-y-3">
        <p className="text-sm text-ink-2">
          <span className="font-semibold text-ink [overflow-wrap:anywhere]">{task.title}</span>
          <br />
          {owner.agentId ? `${owner.label} stopped` : 'Its owner stopped'} before finishing.
          {saved
            ? ` Its uncommitted work is saved${saved.dirtyFiles !== null ? ` (${saved.dirtyFiles} file${saved.dirtyFiles === 1 ? '' : 's'})` : ''} and is restored for whoever you pick.`
            : ' The next runner gets the brief, the last checkpoint and the zones.'}
        </p>
        {sessions.length > 0 ? (
          <fieldset>
            <legend className="text-[12.5px] font-semibold text-ink">Hand the baton to</legend>
            <ul className="mt-1.5 space-y-1.5">
              {sessions.map((s, i) => (
                <li key={s.id}>
                  <label
                    className={clsx(
                      'flex cursor-pointer items-center gap-2.5 rounded-[3px] border px-3 py-2',
                      choice === s.id ? 'border-signal bg-signal-wash' : 'border-rule hover:border-ink-3',
                    )}
                  >
                    <input
                      type="radio"
                      name={`${labelId}-to`}
                      value={s.id}
                      checked={choice === s.id}
                      onChange={() => setChoice(s.id)}
                      data-autofocus={i === 0 ? true : undefined}
                      className="accent-[var(--signal)]"
                    />
                    <span className="font-mono text-sm font-semibold text-ink">{s.callsign}</span>
                    <span className="text-xs text-ink-3">
                      {s.agent_id} ({s.agent_verified ? 'key-verified' : 'self-declared'}) · {s.adapter_enforcement}
                    </span>
                    <span className="ml-auto font-mono text-[11px] text-ink-2">{presenceText(s)}</span>
                  </label>
                </li>
              ))}
            </ul>
          </fieldset>
        ) : (
          <div className="space-y-2">
            <p className="text-sm text-ink">No other agent is running on this crew. Start one in the repo; its brief will offer this baton. Or copy the pickup command into a running agent:</p>
            <CopyCommand command={`remembra-crew adopt T-${task.number}`} label="Pickup command" />
          </div>
        )}
        {error && (
          <p role="alert" className="border-l-[3px] border-fail bg-fail-wash px-3 py-2 text-sm text-ink">
            {error}
          </p>
        )}
        <div className="flex justify-end gap-2 pt-1">
          <button type="button" onClick={onClose} className="rr-btn-ghost px-3 py-1.5 text-sm">
            Cancel
          </button>
          <button type="button" disabled={!choice || busy} onClick={() => void submit()} className="rr-btn-primary px-3 py-1.5 text-sm">
            {busy ? 'Handing over…' : 'Hand the baton over'}
          </button>
        </div>
      </div>
    </Dialog>
  );
}
