// The report receipt (§9.10): criteria with evidence and a source label per
// item, facts_source, the grounding badge on the agent's summary, reviewer,
// baton lineage and the baton ref. Printed like a till receipt: mono, dashed
// rules, a hand-inked seal. Everything an agent wrote (summary, sections,
// the brief each baton carried) is shown as plain text and labelled as the
// agent's word.

import clsx from 'clsx';
import { ArrowLeft, ArrowRight } from 'lucide-react';
import type { ReactNode } from 'react';
import type { CrewState } from '../../../lib/crew/types';
import { absoluteTime, relativeTime, shortSha } from '../../../lib/time';
import type { Receipt } from './actions';
import {
  STATUS_TEXT,
  boardHref,
  receiptHref,
  sealItems,
  sealLine,
  sourceLabel,
  type ReportDetail,
} from './model';
import { SealLine, SealStamp, SourceTag } from './parts';

function Rule() {
  return <div className="rr-rail-h my-4 h-[2px]" aria-hidden="true" />;
}

function Block({ title, children, id }: { title: string; children: ReactNode; id: string }) {
  return (
    <section aria-labelledby={id}>
      <h3 id={id} className="mb-2 font-mono text-[11px] font-semibold uppercase tracking-[0.1em] text-ink-3">
        {title}
      </h3>
      {children}
    </section>
  );
}

function stampVerdict(r: ReportDetail): 'accepted' | 'review' | 'rejected' | 'waived' | 'partial' | 'superseded' {
  if (r.review_state === 'waived' || r.kind === 'waived') return 'waived';
  if (r.review_state === 'rejected') return 'rejected';
  if (!r.is_current) return 'superseded';
  if (r.review_state === 'accepted') return 'accepted';
  if (r.review_state === 'review') return 'review';
  return 'partial';
}

const VERDICT_TEXT: Record<ReturnType<typeof stampVerdict>, string> = {
  accepted: 'Accepted',
  review: 'Waiting for your review',
  rejected: 'Rejected',
  waived: 'Waived by a person',
  partial: 'Partial',
  superseded: 'Superseded',
};

const SECTION_TITLES: Record<string, string> = {
  done: 'Done',
  not_done: 'Not done',
  failing: 'Failing',
  next: 'Next',
  follow_ups: 'Follow-ups',
};

export function ReportReceipt({
  receipt,
  project,
  state,
  now,
  human,
  onReview,
}: {
  receipt: Receipt;
  project: string;
  state: CrewState | null;
  now: Date;
  human: boolean;
  onReview: () => void;
}) {
  const { task, report, history, batons } = receipt;
  const verdict = stampVerdict(report);
  const callsign = (sid: string | null | undefined) => (sid ? (state?.sessions[sid]?.callsign ?? sid) : null);
  const results = new Map((report.criteria_detail ?? report.criteria).map((c) => [c.id, c]));
  const criteriaIds = new Set(task.acceptance.map((c) => c.id));
  const extra = [...results.keys()].filter((id) => !criteriaIds.has(id));
  const items = report.kind === 'waived' ? [] : sealItems(report, task.acceptance);
  const tests = report.tests ?? [];
  const commits = report.commits ?? [];
  const files = report.files ?? [];
  const outOfZone = report.out_of_zone_files ?? [];
  const live = report.deploy?.live ?? [];
  const sections = Object.entries(report.sections ?? {}).filter(([, v]) => Array.isArray(v) && v.length > 0);
  const lineage = [...batons].sort((a, b) => (a.created_at ?? '').localeCompare(b.created_at ?? ''));
  const canReview = human && report.is_current && report.review_state === 'review' && task.status === 'review';
  const id = (s: string) => `rcpt-${report.id}-${s}`;

  return (
    <article className="crew-board mx-auto max-w-3xl" aria-labelledby={id('title')}>
      <p className="mb-2">
        <a href={boardHref(project, task.id)} className="inline-flex items-center gap-1 font-mono text-[11.5px] text-ink-2 hover:text-ink">
          <ArrowLeft className="h-3.5 w-3.5" aria-hidden="true" /> Task board · T-{task.number}
        </a>
      </p>
      <div className="rr-card rounded-t-[3px] px-4 pb-5 pt-4 sm:px-7 sm:pt-6">
        <header className="flex items-start justify-between gap-4">
          <div className="min-w-0">
            <p className="rr-eyebrow">Receipt · T-{task.number}</p>
            <h2 id={id('title')} className="font-display mt-1 text-2xl font-extrabold leading-tight tracking-tight text-ink [overflow-wrap:anywhere]">
              {task.title}
            </h2>
            <p className="mt-1.5 flex flex-wrap items-center gap-x-2 gap-y-1 font-mono text-[11px] text-ink-3">
              <span>{report.id}</span>
              <span>·</span>
              <time dateTime={report.created_at ?? undefined} title={absoluteTime(report.created_at ?? null)}>
                {report.created_at ? relativeTime(report.created_at, now) : ''}
              </time>
              <span>·</span>
              <span>{report.kind} report</span>
              {report.session_id && (
                <>
                  <span>·</span>
                  <span>
                    {report.kind === 'waived' ? 'owner' : 'by'} {callsign(report.session_id)}
                  </span>
                </>
              )}
              <span>·</span>
              <span>task {STATUS_TEXT[task.status]}</span>
            </p>
          </div>
          <SealStamp seed={report.id} verdict={verdict} size={76} />
        </header>

        <div className={clsx('mt-4 border-l-[3px] px-3 py-2', verdict === 'review' ? 'border-signal bg-signal-wash' : verdict === 'rejected' || verdict === 'partial' ? 'border-fail bg-fail-wash' : 'border-ink bg-paper')}>
          <p className="text-sm font-semibold text-ink">
            {verdict === 'accepted' && report.reviewed_by ? 'Accepted in your review' : VERDICT_TEXT[verdict]}
          </p>
          <div className="mt-1">
            <SealLine items={items} fallback={sealLine(report, task.acceptance)} />
          </div>
          {(report.deploy?.reasons ?? []).length > 0 && (
            <p className="mt-1 font-mono text-[11px] text-fail">Not complete because: {(report.deploy?.reasons ?? []).join(' · ')}</p>
          )}
          <p className="mt-1 flex flex-wrap items-center gap-1.5 font-mono text-[11px] text-ink-2">
            facts from <SourceTag source={report.facts_source} />
            {report.superseded_reason && <span>· superseded ({report.superseded_reason})</span>}
          </p>
          {canReview && (
            <button type="button" onClick={onReview} className="rr-btn-primary mt-2 px-3 py-1.5 text-sm">
              Review this report
            </button>
          )}
        </div>

        <Rule />

        <Block title="Acceptance criteria" id={id('criteria')}>
          {task.acceptance.length === 0 && extra.length === 0 ? (
            <p className="text-sm text-ink-3">The task had no acceptance criteria; the gate checked tests and the push only.</p>
          ) : (
            <ul className="space-y-2.5">
              {[...task.acceptance.map((c) => c.id), ...extra].map((cid) => {
                const c = task.acceptance.find((x) => x.id === cid);
                const r = results.get(cid);
                const status = r?.status ?? 'unknown';
                const detail = r && 'detail' in r ? (r as { detail?: string | null }).detail : null;
                return (
                  <li key={cid} className="grid grid-cols-[18px_1fr] gap-x-2">
                    <span
                      aria-hidden="true"
                      className={clsx('font-mono text-[14px] leading-5', status === 'met' ? 'text-ink' : status === 'unmet' ? 'text-fail' : 'text-ink-3')}
                    >
                      {status === 'met' ? '✓' : status === 'unmet' ? '✗' : status === 'waived' ? '≈' : '?'}
                    </span>
                    <div className="min-w-0">
                      <p className="text-[13.5px] leading-5 text-ink [overflow-wrap:anywhere]">
                        <span className="font-mono text-[11px] text-ink-3">{cid}</span> {c?.text ?? '(criterion removed since)'}
                        {c && c.required === false && <span className="ml-1 font-mono text-[10.5px] text-ink-3">optional</span>}
                      </p>
                      <p className="mt-0.5 flex flex-wrap items-center gap-1.5 font-mono text-[11px] text-ink-3">
                        <span className="sr-only">Status: </span>
                        <span className={status === 'unmet' ? 'text-fail' : 'text-ink-2'}>{status}</span>
                        {c && <span>· {c.kind}</span>}
                        {c?.match && <span className="text-ink-2">· {c.match}</span>}
                        {c?.url && <span className="text-ink-2 [overflow-wrap:anywhere]">· {c.url}</span>}
                        {status === 'met' && (
                          <>
                            <span>·</span>
                            <SourceTag source={r?.source ?? null} />
                          </>
                        )}
                        {detail && <span>· {detail}</span>}
                      </p>
                      {c?.waived && (
                        <p className="mt-0.5 text-[12px] text-ink-2">
                          Waived by a person{c.waived.at ? ` ${relativeTime(c.waived.at, now)}` : ''}: <span className="[overflow-wrap:anywhere]">{c.waived.reason}</span>
                        </p>
                      )}
                    </div>
                  </li>
                );
              })}
            </ul>
          )}
        </Block>

        {(tests.length > 0 || commits.length > 0 || files.length > 0 || live.length > 0 || report.deploy?.pushed !== undefined) && (
          <>
            <Rule />
            <Block title="Evidence" id={id('evidence')}>
              <dl className="grid gap-x-4 gap-y-3 sm:grid-cols-[120px_1fr]">
                {tests.length > 0 && (
                  <>
                    <dt className="font-mono text-[11.5px] text-ink-3">Tests</dt>
                    <dd>
                      <ul className="space-y-1">
                        {tests.map((t) => (
                          <li key={t.fingerprint} className="flex flex-wrap items-center gap-1.5 font-mono text-[12px]">
                            <span className={t.passed ? 'text-ink' : 'text-fail'}>{t.passed ? '✓ pass' : '✗ fail'}</span>
                            <span className="min-w-0 text-ink-2 [overflow-wrap:anywhere]">{t.fingerprint}</span>
                            <SourceTag source={t.source ?? null} />
                          </li>
                        ))}
                      </ul>
                    </dd>
                  </>
                )}
                {report.deploy?.pushed !== undefined && (
                  <>
                    <dt className="font-mono text-[11.5px] text-ink-3">Pushed</dt>
                    <dd className="flex items-center gap-1.5 font-mono text-[12px]">
                      {report.deploy.pushed ? (
                        <>
                          <span className="text-ink">✓ yes</span> <SourceTag source={report.deploy.pushed_source ?? null} />
                        </>
                      ) : (
                        <span className="text-ink-2">not seen</span>
                      )}
                    </dd>
                  </>
                )}
                {live.length > 0 && (
                  <>
                    <dt className="font-mono text-[11.5px] text-ink-3">Live checks</dt>
                    <dd>
                      <ul className="space-y-1">
                        {live.map((l) => (
                          <li key={l.criterion_id} className="font-mono text-[12px]">
                            <span className={l.ok ? 'text-ink' : 'text-fail'}>{l.ok ? '✓' : '✗'} {l.status ?? l.error ?? 'no answer'}</span>{' '}
                            <span className="text-ink-2 [overflow-wrap:anywhere]">{l.url}</span> <SourceTag source="server-verified" />
                          </li>
                        ))}
                      </ul>
                    </dd>
                  </>
                )}
                {commits.length > 0 && (
                  <>
                    <dt className="font-mono text-[11.5px] text-ink-3">Commits</dt>
                    <dd className="flex flex-wrap gap-1.5 font-mono text-[12px] text-ink-2">
                      {commits.slice(0, 30).map((c) => (
                        <span key={c} title={c}>
                          {shortSha(c)}
                        </span>
                      ))}
                      {commits.length > 30 && <span className="text-ink-3">+{commits.length - 30}</span>}
                    </dd>
                  </>
                )}
                {files.length > 0 && (
                  <>
                    <dt className="font-mono text-[11.5px] text-ink-3">Files</dt>
                    <dd>
                      <details>
                        <summary className="cursor-pointer font-mono text-[12px] text-ink-2">
                          {files.length} file{files.length === 1 ? '' : 's'} changed
                        </summary>
                        <ul className="mt-1 max-h-48 space-y-0.5 overflow-y-auto font-mono text-[11.5px] text-ink-2">
                          {files.map((f) => (
                            <li key={f} className="[overflow-wrap:anywhere]">
                              {f}
                            </li>
                          ))}
                        </ul>
                      </details>
                    </dd>
                  </>
                )}
                {outOfZone.length > 0 && (
                  <>
                    <dt className="font-mono text-[11.5px] text-fail">Outside its zones</dt>
                    <dd>
                      <ul className="space-y-0.5 font-mono text-[11.5px] text-ink">
                        {outOfZone.slice(0, 20).map((f) => (
                          <li key={f} className="[overflow-wrap:anywhere]">
                            {f}
                          </li>
                        ))}
                        {outOfZone.length > 20 && <li className="text-ink-3">+{outOfZone.length - 20} more</li>}
                      </ul>
                    </dd>
                  </>
                )}
              </dl>
            </Block>
          </>
        )}

        {report.kind !== 'waived' && (report.summary || sections.length > 0) && (
          <>
            <Rule />
            <Block title="In the agent’s words" id={id('words')}>
              {(
                <p className="mb-2 flex flex-wrap items-center gap-1.5 font-mono text-[11px] text-ink-3">
                  Self-reported, not verified.
                  {report.grounding && report.grounding.status !== 'none' && (
                    <span
                      className={clsx(
                        'rounded-[2px] border px-1 py-px',
                        report.grounding.status === 'contradicted' ? 'border-fail/50 bg-fail-wash text-fail' : 'border-ink/50 text-ink',
                      )}
                      title={(report.grounding.checked ?? []).join(', ') || undefined}
                    >
                      {report.grounding.status === 'contradicted' ? 'summary contradicts the facts' : 'summary consistent with the facts'}
                    </span>
                  )}
                </p>
              )}
              {report.summary && <p className="whitespace-pre-wrap text-[13.5px] leading-relaxed text-ink [overflow-wrap:anywhere]">{report.summary}</p>}
              {(report.grounding?.issues ?? []).length > 0 && (
                <ul className="mt-2 space-y-0.5 font-mono text-[11.5px] text-fail">
                  {(report.grounding?.issues ?? []).map((i) => (
                    <li key={i}>✗ {i}</li>
                  ))}
                </ul>
              )}
              {sections.length > 0 && (
                <div className="mt-3 grid gap-3 sm:grid-cols-2">
                  {sections.map(([k, v]) => (
                    <div key={k}>
                      <p className="font-mono text-[11px] font-semibold text-ink-2">{SECTION_TITLES[k] ?? k}</p>
                      <ul className="mt-0.5 list-disc space-y-0.5 pl-4 text-[12.5px] text-ink-2">
                        {(v as string[]).map((line, i) => (
                          <li key={i} className="[overflow-wrap:anywhere]">
                            {line}
                          </li>
                        ))}
                      </ul>
                    </div>
                  ))}
                </div>
              )}
            </Block>
          </>
        )}

        {(report.reviewed_by || report.review_note) && (
          <>
            <Rule />
            <Block title="Review" id={id('review')}>
              <p className="text-[13px] text-ink">
                {report.review_state === 'waived' ? 'Waived' : report.review_state === 'rejected' ? 'Rejected' : 'Reviewed'} by a person
                <span className="font-mono text-[11px] text-ink-3"> ({report.reviewed_by})</span>
              </p>
              {report.review_note && <p className="mt-1 whitespace-pre-wrap text-[13px] text-ink-2 [overflow-wrap:anywhere]">{report.review_note}</p>}
            </Block>
          </>
        )}

        <Rule />
        <Block title="Baton lineage" id={id('lineage')}>
          {lineage.length === 0 ? (
            <p className="text-sm text-ink-3">One runner from start to finish; no baton changed hands.</p>
          ) : (
            <ol className="relative space-y-3 pl-5">
              <span className="rr-rail absolute bottom-1 left-[5px] top-1 w-[2px]" aria-hidden="true" />
              {lineage.map((b) => (
                <li key={b.id} className="relative">
                  <span aria-hidden="true" className={clsx('absolute -left-5 top-1.5 h-3 w-3 border-2 bg-panel', b.restored ? 'border-signal' : 'border-ink-3')} />
                  <p className="flex flex-wrap items-center gap-1.5 font-mono text-[12px] text-ink">
                    <span>{b.from_callsign ?? callsign(b.from_session) ?? 'start'}</span>
                    <ArrowRight className="h-3 w-3 text-signal" aria-label="to" />
                    <span className="font-semibold">{b.to_callsign ?? callsign(b.to_session)}</span>
                    <span className="text-ink-3">· {b.kind.replace('_', ' ')}</span>
                    <time className="text-ink-3" title={absoluteTime(b.created_at)}>
                      · {relativeTime(b.created_at, now)}
                    </time>
                  </p>
                  {(b.baton_ref || b.restored !== null) && (
                    <p className="mt-0.5 font-mono text-[11px] text-ink-2">
                      {b.baton_ref && <span className="[overflow-wrap:anywhere]">saved work {b.baton_ref}</span>}
                      {b.restored !== null && b.restored !== undefined && <span> · {b.restored ? 'work restored ✓' : 'not restored'}</span>}
                    </p>
                  )}
                  {b.brief_text && (
                    <details className="mt-1">
                      <summary className="cursor-pointer font-mono text-[11px] text-ink-3">the brief it received</summary>
                      <pre className="mt-1 max-h-56 overflow-auto whitespace-pre-wrap rounded-[3px] bg-paper p-2 font-mono text-[11px] leading-relaxed text-ink-2 [overflow-wrap:anywhere]">
                        {b.brief_text}
                      </pre>
                    </details>
                  )}
                </li>
              ))}
            </ol>
          )}
          {(report.baton_ref || report.handoff_id) && (
            <p className="mt-3 font-mono text-[11.5px] text-ink-2">
              {report.baton_ref && <span className="[overflow-wrap:anywhere]">This report carries saved work: {report.baton_ref}. </span>}
              {report.handoff_id && <span>Handoff {report.handoff_id}.</span>}
            </p>
          )}
        </Block>

        {history.length > 1 && (
          <>
            <Rule />
            <Block title="Every report on this task" id={id('history')}>
              <ul className="space-y-1">
                {history.map((r) => (
                  <li key={r.id}>
                    <a
                      href={receiptHref(project, r.id, task.id)}
                      aria-current={r.id === report.id ? 'page' : undefined}
                      className={clsx(
                        'flex flex-wrap items-center gap-2 rounded-[2px] px-2 py-1 font-mono text-[11.5px]',
                        r.id === report.id ? 'bg-ink text-panel' : 'text-ink-2 hover:bg-paper-2',
                      )}
                    >
                      <span>{r.kind}</span>
                      <span>{r.review_state ?? r.verdict ?? ''}</span>
                      <span>{sourceLabel(r.facts_source)}</span>
                      {r.is_current ? <span>current</span> : <span>superseded{r.superseded_reason ? ` (${r.superseded_reason})` : ''}</span>}
                      <span className="ml-auto">{r.created_at ? relativeTime(r.created_at, now) : ''}</span>
                    </a>
                  </li>
                ))}
              </ul>
            </Block>
          </>
        )}
      </div>
      <div className="cb-receipt-edge" aria-hidden="true" />
    </article>
  );
}
