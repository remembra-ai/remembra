// New task from the dashboard (§5.4 "Sources: dashboard"): title, phase,
// zones, dependencies (same crew only) and acceptance criteria. The task
// starts in backlog or ready; agents claim it.

import { useId, useState } from 'react';
import clsx from 'clsx';
import { toast } from 'sonner';
import type { CrewApi } from '../../../lib/crew/api';
import { sortedZones } from '../../../lib/crew/selectors';
import type { CrewState } from '../../../lib/crew/types';
import { createTask, explainActionError, type ActionResult } from './actions';
import { CriteriaFields } from './CriteriaEditor';
import { Dialog } from './Dialog';
import { emptyDraft, fromDraft, validateCriteria, type CriterionDraft, type TaskDetail } from './model';

export function NewTask({
  api,
  crewId,
  state,
  tasks,
  defaultPhase,
  onClose,
  onCreated,
}: {
  api: CrewApi;
  crewId: string;
  state: CrewState | null;
  tasks: readonly TaskDetail[];
  defaultPhase: string | null;
  onClose: () => void;
  onCreated: (result: ActionResult) => void;
}) {
  const uid = useId();
  const [title, setTitle] = useState('');
  const [phase, setPhase] = useState(defaultPhase ?? '');
  const [body, setBody] = useState('');
  const [zones, setZones] = useState<string[]>([]);
  const [deps, setDeps] = useState<string[]>([]);
  const [drafts, setDrafts] = useState<CriterionDraft[]>(() => [emptyDraft([])]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [tried, setTried] = useState(false);
  const zoneList = state ? sortedZones(state).filter((z) => !z.builtin) : [];
  const phases = [...new Set(tasks.map((t) => t.phase).filter((p): p is string => !!p))];
  const open = tasks.filter((t) => t.status !== 'cancelled').sort((a, b) => a.number - b.number);
  const problems = [...(title.trim() ? [] : ['Give the task a title.']), ...validateCriteria(drafts)];

  const submit = async () => {
    setTried(true);
    if (problems.length) return;
    setBusy(true);
    setError(null);
    try {
      const res = await createTask(api, crewId, {
        title,
        phase,
        body,
        zone_ids: zones,
        depends_on: deps,
        acceptance: drafts.map(fromDraft),
      });
      toast.success(res.task ? `T-${res.task.number} created` : 'Task created');
      onCreated(res);
      onClose();
    } catch (err) {
      setError(explainActionError(err));
    } finally {
      setBusy(false);
    }
  };

  const toggle = (list: string[], id: string) => (list.includes(id) ? list.filter((x) => x !== id) : [...list, id]);

  return (
    <Dialog labelId={`${uid}-title`} eyebrow="New task" title="What should get done?" onClose={onClose} wide>
      <form
        className="space-y-4"
        onSubmit={(e) => {
          e.preventDefault();
          void submit();
        }}
      >
        <label className="block" htmlFor={`${uid}-t`}>
          <span className="text-[12.5px] font-semibold text-ink">Title</span>
          <input
            id={`${uid}-t`}
            data-autofocus
            value={title}
            onChange={(e) => setTitle(e.target.value)}
            maxLength={200}
            placeholder="Split tender payments"
            className="rr-input mt-1 block w-full px-2.5 py-1.5 text-sm"
          />
        </label>
        <div className="grid gap-3 sm:grid-cols-2">
          <label className="block" htmlFor={`${uid}-p`}>
            <span className="text-[12.5px] font-semibold text-ink">Phase</span>
            <input
              id={`${uid}-p`}
              list={`${uid}-phases`}
              value={phase}
              onChange={(e) => setPhase(e.target.value)}
              maxLength={64}
              placeholder="Phase 2 · POS"
              className="rr-input mt-1 block w-full px-2.5 py-1.5 text-sm"
            />
            <datalist id={`${uid}-phases`}>
              {phases.map((p) => (
                <option key={p} value={p} />
              ))}
            </datalist>
          </label>
          <label className="block" htmlFor={`${uid}-b`}>
            <span className="text-[12.5px] font-semibold text-ink">Notes (optional)</span>
            <input id={`${uid}-b`} value={body} onChange={(e) => setBody(e.target.value)} maxLength={8000} className="rr-input mt-1 block w-full px-2.5 py-1.5 text-sm" />
          </label>
        </div>
        {zoneList.length > 0 && (
          <fieldset>
            <legend className="text-[12.5px] font-semibold text-ink">Zones it works in</legend>
            <p className="mt-1 flex flex-wrap gap-1.5">
              {zoneList.map((z) => (
                <label key={z.id} className={clsx('cb-zone cursor-pointer', !zones.includes(z.id) && 'opacity-70')} data-mode={zones.includes(z.id) ? 'exclusive' : 'watch'}>
                  <input type="checkbox" className="sr-only" checked={zones.includes(z.id)} onChange={() => setZones(toggle(zones, z.id))} />
                  {z.slug}
                </label>
              ))}
            </p>
          </fieldset>
        )}
        {open.length > 0 && (
          <fieldset>
            <legend className="text-[12.5px] font-semibold text-ink">Starts after</legend>
            <p className="mt-1 flex max-h-24 flex-wrap gap-1.5 overflow-y-auto">
              {open.map((t) => (
                <label
                  key={t.id}
                  className={clsx(
                    'inline-flex cursor-pointer items-center gap-1 rounded-[2px] border px-1.5 py-0.5 font-mono text-[11px]',
                    deps.includes(t.id) ? 'border-ink bg-ink text-panel' : 'border-rule text-ink-2 hover:border-ink-3',
                  )}
                  title={t.title}
                >
                  <input type="checkbox" className="sr-only" checked={deps.includes(t.id)} onChange={() => setDeps(toggle(deps, t.id))} />
                  T-{t.number}
                </label>
              ))}
            </p>
          </fieldset>
        )}
        <fieldset>
          <legend className="text-[12.5px] font-semibold text-ink">Acceptance criteria</legend>
          <p className="mb-2 mt-0.5 text-[12px] text-ink-3">How the report proves it is done. Agents can change these only until work starts; you can change them any time.</p>
          <CriteriaFields drafts={drafts} onChange={setDrafts} idPrefix={`${uid}-c`} />
        </fieldset>
        {tried && problems.length > 0 && (
          <ul className="space-y-0.5 font-mono text-[11px] text-fail">
            {problems.slice(0, 4).map((p) => (
              <li key={p}>{p}</li>
            ))}
          </ul>
        )}
        {error && (
          <p role="alert" className="border-l-[3px] border-fail bg-fail-wash px-3 py-2 text-sm text-ink">
            {error}
          </p>
        )}
        <div className="flex justify-end gap-2">
          <button type="button" onClick={onClose} className="rr-btn-ghost px-3 py-1.5 text-sm">
            Cancel
          </button>
          <button type="submit" disabled={busy} className="rr-btn-primary px-3 py-1.5 text-sm">
            {busy ? 'Creating…' : 'Create task'}
          </button>
        </div>
      </form>
    </Dialog>
  );
}
