// Acceptance criteria editor (§5.4, §9.7): Mani can edit criteria at any
// time; agents only before `in_progress`. Saved with If-Match on the task
// version, validated here exactly as the server validates them. A `match` is
// a pattern matched against commands the agent was observed running; nothing
// ever executes it.

import { useState } from 'react';
import { Plus, Trash2 } from 'lucide-react';
import { toast } from 'sonner';
import type { CrewApi } from '../../../lib/crew/api';
import { explainActionError, saveCriteria, type ActionResult } from './actions';
import {
  CRITERION_KINDS,
  MAX_CRITERIA,
  emptyDraft,
  fromDraft,
  toDraft,
  validateCriteria,
  type CriterionDraft,
  type TaskDetail,
} from './model';

const KIND_HINT: Record<CriterionDraft['kind'], string> = {
  test: 'Met when a matching test run passed after the last change to the zone’s files.',
  command: 'Met when a matching command exited 0.',
  file: 'Met when this repo-relative file was changed.',
  commit: 'Met when a commit (optionally this sha prefix) is in the task’s range.',
  deploy: 'Checked by the server itself over https, only for hosts on your live-check list.',
  manual: 'Only an agent’s word or your waiver can meet it.',
};

export function CriteriaFields({
  drafts,
  onChange,
  idPrefix,
}: {
  drafts: CriterionDraft[];
  onChange: (next: CriterionDraft[]) => void;
  idPrefix: string;
}) {
  const set = (i: number, patch: Partial<CriterionDraft>) => onChange(drafts.map((d, j) => (j === i ? { ...d, ...patch } : d)));
  return (
    <div className="space-y-2">
      {drafts.map((d, i) => {
        const base = `${idPrefix}-${i}`;
        return (
          <fieldset key={i} className="rounded-[3px] border border-rule p-2.5">
            <legend className="sr-only">Criterion {i + 1}</legend>
            <div className="flex flex-wrap items-center gap-2">
              <label className="sr-only" htmlFor={`${base}-id`}>
                Id
              </label>
              <input
                id={`${base}-id`}
                value={d.id}
                onChange={(e) => set(i, { id: e.target.value })}
                className="rr-input w-16 px-1.5 py-1 font-mono text-[12px]"
                maxLength={32}
              />
              <label className="sr-only" htmlFor={`${base}-kind`}>
                Kind
              </label>
              <select
                id={`${base}-kind`}
                value={d.kind}
                onChange={(e) => set(i, { kind: e.target.value as CriterionDraft['kind'] })}
                className="rr-input px-1.5 py-1 font-mono text-[12px]"
              >
                {CRITERION_KINDS.map((k) => (
                  <option key={k} value={k}>
                    {k}
                  </option>
                ))}
              </select>
              <label className="ml-auto inline-flex items-center gap-1.5 text-[12px] text-ink-2">
                <input type="checkbox" checked={d.required} onChange={(e) => set(i, { required: e.target.checked })} className="accent-[var(--signal)]" />
                required
              </label>
              <button
                type="button"
                onClick={() => onChange(drafts.filter((_, j) => j !== i))}
                aria-label={`Remove criterion ${d.id || i + 1}`}
                className="rounded-[2px] p-1 text-ink-3 hover:text-fail"
              >
                <Trash2 className="h-3.5 w-3.5" aria-hidden="true" />
              </button>
            </div>
            <label className="mt-1.5 block" htmlFor={`${base}-text`}>
              <span className="sr-only">What done means</span>
              <input
                id={`${base}-text`}
                value={d.text}
                onChange={(e) => set(i, { text: e.target.value })}
                placeholder="What done means, e.g. POS split tender tests pass"
                maxLength={280}
                className="rr-input block w-full px-2 py-1 text-[13px]"
              />
            </label>
            {d.kind === 'deploy' ? (
              <label className="mt-1.5 block" htmlFor={`${base}-url`}>
                <span className="sr-only">https URL</span>
                <input
                  id={`${base}-url`}
                  value={d.url}
                  onChange={(e) => set(i, { url: e.target.value })}
                  placeholder="https://yaadbooks.com/api/health"
                  className="rr-input block w-full px-2 py-1 font-mono text-[12px]"
                />
              </label>
            ) : d.kind !== 'manual' ? (
              <label className="mt-1.5 block" htmlFor={`${base}-match`}>
                <span className="sr-only">{d.kind === 'file' ? 'Repo-relative path' : d.kind === 'commit' ? 'Sha prefix (optional)' : 'Command pattern'}</span>
                <input
                  id={`${base}-match`}
                  value={d.match}
                  onChange={(e) => set(i, { match: e.target.value })}
                  placeholder={d.kind === 'file' ? 'src/app/pos/Receipt.tsx' : d.kind === 'commit' ? 'sha prefix (optional)' : 'npm test -- pos'}
                  className="rr-input block w-full px-2 py-1 font-mono text-[12px]"
                />
              </label>
            ) : null}
            <p className="mt-1 text-[11.5px] leading-snug text-ink-3">{KIND_HINT[d.kind]}</p>
          </fieldset>
        );
      })}
      {drafts.length < MAX_CRITERIA && (
        <button type="button" onClick={() => onChange([...drafts, emptyDraft(drafts)])} className="rr-btn-ghost inline-flex items-center gap-1 px-2 py-1 text-[12.5px]">
          <Plus className="h-3.5 w-3.5" aria-hidden="true" /> Add a criterion
        </button>
      )}
    </div>
  );
}

export function CriteriaEditor({
  api,
  task,
  canEdit,
  onChanged,
}: {
  api: CrewApi;
  task: TaskDetail;
  canEdit: boolean;
  onChanged: (result: ActionResult) => void;
}) {
  const [drafts, setDrafts] = useState<CriterionDraft[] | null>(null);
  const [baseVersion, setBaseVersion] = useState(task.version);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const editing = drafts !== null;
  const problems = drafts ? validateCriteria(drafts) : [];
  const stale = editing && task.version !== baseVersion;

  const save = async () => {
    if (!drafts || problems.length) return;
    setBusy(true);
    setError(null);
    try {
      const res = await saveCriteria(api, task.id, drafts.map(fromDraft), baseVersion);
      toast.success(`Criteria for T-${task.number} saved`);
      setDrafts(null);
      onChanged(res);
    } catch (err) {
      setError(explainActionError(err));
    } finally {
      setBusy(false);
    }
  };

  if (!editing) {
    return (
      <div>
        {task.acceptance.length === 0 ? (
          <p className="text-sm text-ink-3">No criteria yet. Without them the task closes on its report’s tests and push alone.</p>
        ) : (
          <ol className="space-y-1.5">
            {task.acceptance.map((c) => (
              <li key={c.id} className="text-[13px] leading-snug text-ink">
                <span className="font-mono text-[11px] text-ink-3">{c.id}</span> {c.text}{' '}
                <span className="font-mono text-[10.5px] text-ink-3">
                  · {c.kind}
                  {c.match ? ` · ${c.match}` : ''}
                  {c.url ? ` · ${c.url}` : ''}
                  {c.required === false ? ' · optional' : ''}
                  {c.waived ? ' · waived' : ''}
                </span>
              </li>
            ))}
          </ol>
        )}
        {canEdit && (
          <button
            type="button"
            onClick={() => {
              setBaseVersion(task.version);
              setDrafts(task.acceptance.map(toDraft));
            }}
            className="rr-btn-ghost mt-2 px-2.5 py-1 text-[12.5px]"
          >
            Edit criteria
          </button>
        )}
      </div>
    );
  }

  return (
    <div className="space-y-2">
      {task.acceptance_locked && (
        <p className="text-[12px] text-ink-2">Locked since work started: agents can no longer change these. Your change is recorded as a moment.</p>
      )}
      <CriteriaFields drafts={drafts} onChange={setDrafts} idPrefix={`crit-${task.id}`} />
      {problems.length > 0 && (
        <ul className="space-y-0.5 font-mono text-[11px] text-fail" aria-live="polite">
          {problems.slice(0, 4).map((p) => (
            <li key={p}>{p}</li>
          ))}
        </ul>
      )}
      {stale && <p className="font-mono text-[11px] text-signal-ink">This task changed while you were editing. Saving will be refused; cancel and edit again.</p>}
      {error && (
        <p role="alert" className="border-l-[3px] border-fail bg-fail-wash px-3 py-2 text-sm text-ink">
          {error}
        </p>
      )}
      <div className="flex gap-2">
        <button type="button" disabled={busy || problems.length > 0} onClick={() => void save()} className="rr-btn-primary px-3 py-1.5 text-sm">
          {busy ? 'Saving…' : 'Save criteria'}
        </button>
        <button type="button" onClick={() => setDrafts(null)} className="rr-btn-ghost px-3 py-1.5 text-sm">
          Cancel
        </button>
      </div>
    </div>
  );
}
