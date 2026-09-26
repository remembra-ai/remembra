// The report or waiver flow (§9.7 "No report means no Done", §5.6, D17).
// Opened by dragging a card to Done, by "Review the report", or from the card
// menu. A task in review gets Approve / Reject; any other open task can have
// single criteria waived (the next report counts them as waived) or the whole
// report waived with a reason (a `waived` report; the task is done). Human
// only; waivers need a recent login (step-up).

import { useState } from 'react';
import clsx from 'clsx';
import { toast } from 'sonner';
import { useResource } from '../../../hooks/useResource';
import type { CrewApi } from '../../../lib/crew/api';
import { explainActionError, loadTaskReports, reviewReport, waiveCriterion, waiveReport, type ActionResult } from './actions';
import { Dialog } from './Dialog';
import {
  sealItems,
  sealLine,
  WAIVER_REASON_MAX,
  waiverReasonError,
  type ReportDetail,
  type TaskDetail,
} from './model';
import { SealLine, SealStamp, SourceTag } from './parts';

const STATUS_WORD: Record<string, string> = { met: 'met', waived: 'waived', unmet: 'not met', unknown: 'no evidence yet' };

function CriteriaTable({ task, report }: { task: TaskDetail; report: ReportDetail | null }) {
  const results = new Map((report?.criteria_detail ?? report?.criteria ?? []).map((c) => [c.id, c]));
  if (!task.acceptance.length) return <p className="text-sm text-ink-3">This task has no acceptance criteria.</p>;
  return (
    <ul className="divide-y divide-rule border-y border-rule">
      {task.acceptance.map((c) => {
        const r = results.get(c.id);
        const status = c.waived ? 'waived' : (r?.status ?? 'unknown');
        return (
          <li key={c.id} className="flex items-start gap-3 py-2">
            <span
              aria-hidden="true"
              className={clsx(
                'mt-0.5 w-4 shrink-0 text-center font-mono text-[13px]',
                status === 'met' && 'text-ink',
                status === 'unmet' && 'text-fail',
                (status === 'waived' || status === 'unknown') && 'text-ink-3',
              )}
            >
              {status === 'met' ? '✓' : status === 'unmet' ? '✗' : status === 'waived' ? '≈' : '·'}
            </span>
            <div className="min-w-0 flex-1">
              <p className="text-[13px] leading-snug text-ink [overflow-wrap:anywhere]">
                <span className="font-mono text-[11px] text-ink-3">{c.id}</span> {c.text}
                {!c.required && <span className="ml-1 font-mono text-[10.5px] text-ink-3">(optional)</span>}
              </p>
              <p className="mt-0.5 flex flex-wrap items-center gap-1.5 font-mono text-[10.5px] text-ink-3">
                <span>{c.kind}</span>
                <span>· {STATUS_WORD[status]}</span>
                {status === 'met' && <SourceTag source={r?.source ?? null} />}
                {'detail' in (r ?? {}) && (r as { detail?: string | null }).detail && <span>· {(r as { detail?: string | null }).detail}</span>}
              </p>
              {c.waived?.reason && <p className="mt-0.5 text-[12px] text-ink-2">Waived: {c.waived.reason}</p>}
            </div>
          </li>
        );
      })}
    </ul>
  );
}

function ReasonField({
  id,
  value,
  onChange,
  label,
}: {
  id: string;
  value: string;
  onChange: (v: string) => void;
  label: string;
}) {
  return (
    <label htmlFor={id} className="block">
      <span className="text-[12.5px] font-semibold text-ink">{label}</span>
      <textarea
        id={id}
        value={value}
        maxLength={WAIVER_REASON_MAX + 40}
        onChange={(e) => onChange(e.target.value)}
        rows={2}
        className="rr-input mt-1 block w-full resize-y px-2.5 py-1.5 text-sm"
        placeholder="e.g. Verified by hand on staging; the test runner is broken on CI"
      />
      <span className="mt-0.5 block text-right font-mono text-[10.5px] text-ink-3">
        {value.trim().length}/{WAIVER_REASON_MAX}
      </span>
    </label>
  );
}

export function CompletionReport({
  api,
  task,
  receiptHref,
  onClose,
  onChanged,
}: {
  api: CrewApi;
  task: TaskDetail;
  receiptHref: (reportId: string) => string;
  onClose: () => void;
  onChanged: (result: ActionResult) => void;
}) {
  const reports = useResource<ReportDetail[]>(task.current_report_id ? `reports:${task.id}:${task.current_report_id}` : null, () =>
    loadTaskReports(api, task.id),
  );
  const current = reports.data?.find((r) => r.id === task.current_report_id) ?? null;
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [note, setNote] = useState('');
  const [waiving, setWaiving] = useState<string | null>(null);
  const [reason, setReason] = useState('');
  const inReview = task.status === 'review';
  const closed = task.status === 'done' || task.status === 'cancelled';
  const labelId = `report-flow-${task.id}`;

  const run = async (key: string, fn: () => Promise<ActionResult>, success: string) => {
    setBusy(key);
    setError(null);
    try {
      const res = await fn();
      toast.success(success);
      onChanged(res);
      return true;
    } catch (err) {
      setError(explainActionError(err));
      return false;
    } finally {
      setBusy(null);
    }
  };

  const reasonProblem = waiving ? waiverReasonError(reason) : null;
  const open = task.acceptance.filter((c) => {
    if (c.waived) return false;
    const r = (current?.criteria_detail ?? current?.criteria ?? []).find((x) => x.id === c.id);
    return r?.status !== 'met';
  });

  return (
    <Dialog labelId={labelId} eyebrow={inReview ? 'Report in review' : 'No report means no Done'} title={`T-${task.number} · ${task.title}`} onClose={onClose} wide>
      <div className="space-y-4">
        {current ? (
          <section aria-label="Current report" className="flex items-start gap-3">
            <SealStamp
              seed={current.id}
              verdict={
                current.review_state === 'waived'
                  ? 'waived'
                  : current.review_state === 'accepted'
                    ? 'accepted'
                    : current.review_state === 'rejected'
                      ? 'rejected'
                      : current.review_state === 'review'
                        ? 'review'
                        : 'partial'
              }
            />
            <div className="min-w-0 flex-1">
              <p className="text-sm text-ink">
                <span className="font-semibold">{current.verdict === 'complete' ? 'Complete' : 'Partial'} report</span>{' '}
                <span className="text-ink-3">from {current.facts_source === 'agent-declared' ? 'the agent (self-reported)' : 'hooks and the crew CLI (observed)'}</span>
              </p>
              <div className="mt-1">
                <SealLine items={sealItems(current, task.acceptance)} fallback={sealLine(current, task.acceptance)} />
              </div>
              {(current.deploy?.reasons ?? []).length > 0 && (
                <p className="mt-1 font-mono text-[11px] text-fail">Why not complete: {(current.deploy?.reasons ?? []).join(' · ')}</p>
              )}
              <a href={receiptHref(current.id)} className="mt-1 inline-block font-mono text-[11px] font-semibold text-signal-ink hover:underline">
                Open the full receipt →
              </a>
            </div>
          </section>
        ) : (
          <p className="border-l-[3px] border-signal bg-signal-wash px-3 py-2 text-sm text-ink">
            {reports.loading && task.current_report_id
              ? 'Loading the report…'
              : 'No agent has reported on this task yet. It closes when its owner reports, or when you waive the report with a reason.'}
          </p>
        )}

        <CriteriaTable task={task} report={current} />

        {error && (
          <p role="alert" className="border-l-[3px] border-fail bg-fail-wash px-3 py-2 text-sm text-ink">
            {error}
          </p>
        )}

        {inReview && current && (
          <section aria-labelledby={`${labelId}-review`} className="space-y-2">
            <h3 id={`${labelId}-review`} className="font-display text-base font-bold text-ink">
              Your review
            </h3>
            <label className="block" htmlFor={`${labelId}-note`}>
              <span className="text-[12.5px] text-ink-2">Note for the agent (optional)</span>
              <textarea
                id={`${labelId}-note`}
                value={note}
                onChange={(e) => setNote(e.target.value)}
                rows={2}
                maxLength={2000}
                className="rr-input mt-1 block w-full resize-y px-2.5 py-1.5 text-sm"
              />
            </label>
            <div className="flex flex-wrap gap-2">
              <button
                type="button"
                data-autofocus
                disabled={!!busy}
                onClick={() => void run('approve', () => reviewReport(api, task.id, 'approve', note), `T-${task.number} approved: done`).then((ok) => ok && onClose())}
                className="rr-btn-primary px-3 py-1.5 text-sm"
              >
                {busy === 'approve' ? 'Approving…' : 'Approve: mark done'}
              </button>
              <button
                type="button"
                disabled={!!busy}
                onClick={() => void run('reject', () => reviewReport(api, task.id, 'reject', note), `T-${task.number} sent back to in progress`).then((ok) => ok && onClose())}
                className="rr-btn-ghost px-3 py-1.5 text-sm"
              >
                {busy === 'reject' ? 'Sending back…' : 'Reject: back to in progress'}
              </button>
            </div>
          </section>
        )}

        {!closed && (
          <section aria-labelledby={`${labelId}-waive`} className="space-y-2 border-t border-dashed border-rule pt-3">
            <h3 id={`${labelId}-waive`} className="font-display text-base font-bold text-ink">
              Waive
            </h3>
            <p className="text-[12.5px] text-ink-2">
              A waiver is yours alone, needs a reason, and stays on the receipt and in the audit log. It needs a login from the last 15 minutes.
            </p>
            <div className="flex flex-wrap gap-1.5">
              {open.map((c) => (
                <button
                  key={c.id}
                  type="button"
                  aria-pressed={waiving === c.id}
                  onClick={() => setWaiving(waiving === c.id ? null : c.id)}
                  className={clsx('rr-btn-ghost px-2 py-1 font-mono text-[11.5px]', waiving === c.id && 'border-ink text-ink')}
                >
                  waive {c.id}
                </button>
              ))}
              <button
                type="button"
                aria-pressed={waiving === 'all'}
                onClick={() => setWaiving(waiving === 'all' ? null : 'all')}
                className={clsx('rr-btn-ghost px-2 py-1 font-mono text-[11.5px]', waiving === 'all' && 'border-signal text-signal-ink')}
              >
                waive the whole report
              </button>
            </div>
            {waiving && (
              <div className="space-y-2 rounded-[3px] border border-rule bg-paper p-3">
                <p className="text-[12.5px] text-ink">
                  {waiving === 'all'
                    ? `T-${task.number} closes as done with a waived report instead of evidence.`
                    : `Criterion ${waiving} counts as waived in every report from now on. The task still needs its report.`}
                </p>
                <ReasonField id={`${labelId}-reason`} value={reason} onChange={setReason} label="Reason (required)" />
                {reason && reasonProblem && <p className="font-mono text-[11px] text-fail">{reasonProblem}</p>}
                <button
                  type="button"
                  disabled={!!busy || !!reasonProblem}
                  onClick={() => {
                    const target = waiving;
                    void run(
                      'waive',
                      () => (target === 'all' ? waiveReport(api, task.id, reason) : waiveCriterion(api, task.id, target, reason)),
                      target === 'all' ? `T-${task.number} closed with a waiver` : `Criterion ${target} waived`,
                    ).then((ok) => {
                      if (!ok) return;
                      setReason('');
                      setWaiving(null);
                      if (target === 'all') onClose();
                    });
                  }}
                  className={clsx(waiving === 'all' ? 'rr-btn-primary' : 'rr-btn-ghost border-ink text-ink', 'px-3 py-1.5 text-sm')}
                >
                  {busy === 'waive' ? 'Waiving…' : waiving === 'all' ? 'Waive and mark done' : `Waive ${waiving}`}
                </button>
              </div>
            )}
          </section>
        )}
        {closed && <p className="text-sm text-ink-3">This task is {task.status}.</p>}
      </div>
    </Dialog>
  );
}
