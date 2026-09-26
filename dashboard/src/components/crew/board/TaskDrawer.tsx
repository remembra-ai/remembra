// The task drawer (`view=board&task=T-14`): everything about one task —
// owner, zones, dependencies, criteria (editable by a person at any time),
// reports with their seals and receipt links, recent checkpoints — and the
// human actions the board offers. Agent text is plain text.

import { useState, type ReactNode } from 'react';
import { toast } from 'sonner';
import { useResource } from '../../../hooks/useResource';
import type { CrewApi } from '../../../lib/crew/api';
import type { CrewState } from '../../../lib/crew/types';
import { absoluteTime, relativeTime, shortSha } from '../../../lib/time';
import { explainActionError, loadTaskReports, reopenTask, type ActionResult } from './actions';
import { CriteriaEditor } from './CriteriaEditor';
import { Dialog } from './Dialog';
import {
  STATUS_TEXT,
  dependencyRefs,
  ownerOf,
  savedWork,
  sealItems,
  sealLine,
  zoneChips,
  type CheckpointDetail,
  type ReportDetail,
  type TaskDetail,
} from './model';
import { OwnerTag, SealLine, SealStamp, SourceTag, ZoneChip } from './parts';

function Section({ title, children }: { title: string; children: ReactNode }) {
  return (
    <section className="border-t border-dashed border-rule pt-3">
      <h3 className="rr-eyebrow mb-2">{title}</h3>
      {children}
    </section>
  );
}

export function TaskDrawer({
  api,
  task,
  tasks,
  state,
  checkpoints,
  human,
  now,
  receiptHref,
  onClose,
  onChanged,
  onReport,
  onPickup,
}: {
  api: CrewApi;
  task: TaskDetail;
  tasks: readonly TaskDetail[];
  state: CrewState | null;
  checkpoints: readonly CheckpointDetail[];
  human: boolean;
  now: Date;
  receiptHref: (reportId: string) => string;
  onClose: () => void;
  onChanged: (result: ActionResult) => void;
  onReport: () => void;
  onPickup: () => void;
}) {
  const reports = useResource<ReportDetail[]>(`drawer-reports:${task.id}:${task.current_report_id ?? ''}:${task.version}`, () =>
    loadTaskReports(api, task.id),
  );
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const byId = new Map(tasks.map((t) => [t.id, t]));
  const deps = dependencyRefs(task, byId);
  const dependents = tasks.filter((t) => t.depends_on.includes(task.id));
  const owner = ownerOf(task, state);
  const saved = savedWork(task, state);
  const mine = checkpoints
    .filter((c) => c.task_id === task.id)
    .sort((a, b) => (b.created_at ?? '').localeCompare(a.created_at ?? ''))
    .slice(0, 8);
  const history = [...(reports.data ?? [])].sort((a, b) => (b.created_at ?? '').localeCompare(a.created_at ?? ''));
  const labelId = `drawer-${task.id}`;
  const open = !['done', 'cancelled'].includes(task.status);

  const reopen = async () => {
    setBusy(true);
    setError(null);
    try {
      const res = await reopenTask(api, task.id);
      toast.success(`T-${task.number} reopened`);
      onChanged(res);
    } catch (err) {
      setError(explainActionError(err));
    } finally {
      setBusy(false);
    }
  };

  return (
    <Dialog labelId={labelId} eyebrow={`T-${task.number} · ${STATUS_TEXT[task.status]}`} title={task.title} onClose={onClose} side>
      <div className="space-y-4">
        <div className="flex flex-wrap items-center gap-x-3 gap-y-1.5">
          <OwnerTag owner={owner} />
          {task.phase && <span className="font-mono text-[11px] text-ink-3">phase {task.phase}</span>}
          <span className="font-mono text-[11px] text-ink-3">priority {task.priority}</span>
          {task.started_head && <span className="font-mono text-[11px] text-ink-3">from {shortSha(task.started_head)}</span>}
        </div>
        {task.zone_ids.length > 0 && (
          <p className="flex flex-wrap gap-1">
            {zoneChips(task, state).map((z) => (
              <ZoneChip key={z.zoneId} chip={z} />
            ))}
          </p>
        )}
        {task.body && <p className="whitespace-pre-wrap text-[13px] leading-relaxed text-ink-2 [overflow-wrap:anywhere]">{task.body}</p>}
        {task.blocked_reason && (
          <p className="border-l-2 border-fail/60 pl-2 text-[13px] text-ink-2 [overflow-wrap:anywhere]">Blocked: {task.blocked_reason}</p>
        )}
        {saved && (
          <p className="font-mono text-[11.5px] text-signal-ink">
            Saved work: {saved.ref}
            {saved.dirtyFiles !== null && ` · ${saved.dirtyFiles} files`}
            {saved.unpushed ? ` · ${saved.unpushed} unpushed commits` : ''}
          </p>
        )}

        {human && (
          <div className="flex flex-wrap gap-2">
            {task.status === 'stalled' && (
              <button type="button" onClick={onPickup} className="rr-btn-primary px-3 py-1.5 text-sm">
                Pick up baton
              </button>
            )}
            {open && (
              <button type="button" onClick={onReport} className="rr-btn-ghost px-3 py-1.5 text-sm">
                {task.status === 'review' ? 'Review the report' : 'Close with a report or waiver'}
              </button>
            )}
            {task.status === 'done' && (
              <button type="button" disabled={busy} onClick={() => void reopen()} className="rr-btn-ghost px-3 py-1.5 text-sm">
                {busy ? 'Reopening…' : 'Reopen'}
              </button>
            )}
          </div>
        )}
        {error && (
          <p role="alert" className="border-l-[3px] border-fail bg-fail-wash px-3 py-2 text-sm text-ink">
            {error}
          </p>
        )}

        <Section title="Acceptance criteria">
          <CriteriaEditor api={api} task={task} canEdit={human} onChanged={onChanged} />
        </Section>

        {(deps.length > 0 || dependents.length > 0) && (
          <Section title="Dependencies">
            {deps.length > 0 && (
              <p className="text-[13px] text-ink-2">
                Starts after{' '}
                {deps.map((d, i) => (
                  <span key={d.id} className="font-mono">
                    {i > 0 && ', '}
                    {d.ref} {d.done ? '(done)' : '(open)'}
                  </span>
                ))}
              </p>
            )}
            {dependents.length > 0 && (
              <p className="mt-1 text-[13px] text-ink-2">
                Unblocks <span className="font-mono">{dependents.map((t) => `T-${t.number}`).join(', ')}</span>
              </p>
            )}
          </Section>
        )}

        <Section title="Reports">
          {reports.loading ? (
            <p className="text-sm text-ink-3">Loading reports…</p>
          ) : history.length === 0 ? (
            <p className="text-sm text-ink-3">No reports yet. Tasks only close with a report.</p>
          ) : (
            <ul className="space-y-2">
              {history.map((r) => (
                <li key={r.id}>
                  <a href={receiptHref(r.id)} className="flex items-start gap-2.5 rounded-[3px] border border-rule p-2 hover:border-ink-3">
                    <SealStamp
                      seed={r.id}
                      size={32}
                      verdict={
                        !r.is_current
                          ? 'superseded'
                          : r.review_state === 'waived'
                            ? 'waived'
                            : r.review_state === 'accepted'
                              ? 'accepted'
                              : r.review_state === 'rejected'
                                ? 'rejected'
                                : r.review_state === 'review'
                                  ? 'review'
                                  : 'partial'
                      }
                    />
                    <span className="min-w-0 flex-1">
                      <span className="flex flex-wrap items-center gap-1.5 text-[12.5px] text-ink">
                        <span className="font-semibold">{r.kind}</span>
                        {r.review_state && <span className="font-mono text-[11px] text-ink-3">{r.review_state}</span>}
                        {!r.is_current && <span className="font-mono text-[11px] text-ink-3">superseded{r.superseded_reason ? ` (${r.superseded_reason})` : ''}</span>}
                        <SourceTag source={r.facts_source} />
                        <time className="ml-auto font-mono text-[10.5px] text-ink-3" title={absoluteTime(r.created_at ?? null)}>
                          {r.created_at ? relativeTime(r.created_at, now) : ''}
                        </time>
                      </span>
                      <span className="mt-0.5 block">
                        <SealLine items={r.kind === 'waived' ? [] : sealItems(r, task.acceptance)} fallback={sealLine(r, task.acceptance)} />
                      </span>
                    </span>
                  </a>
                </li>
              ))}
            </ul>
          )}
        </Section>

        {mine.length > 0 && (
          <Section title="Checkpoints">
            <ul className="space-y-1">
              {mine.map((c) => (
                <li key={c.id} className="flex gap-2 font-mono text-[11.5px] text-ink-2">
                  <span className="shrink-0 text-ink-3" title={absoluteTime(c.created_at ?? null)}>
                    {c.created_at ? relativeTime(c.created_at, now) : 'just now'}
                  </span>
                  <span className="shrink-0">{c.trigger}</span>
                  <span className="min-w-0 truncate text-ink">{c.headline}</span>
                </li>
              ))}
            </ul>
          </Section>
        )}
      </div>
    </Dialog>
  );
}
