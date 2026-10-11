import { useEffect, useId, useRef, useState } from 'react';
import {
  approvalReviewModel, BLOCK_COPY, decisionIntent, submitApprovalDecision,
  type ApprovalView, type DecisionPorts, type ReviewContext,
} from './approvalModel';

export interface ApprovalReviewProps extends ReviewContext {
  view: ApprovalView | null;
  loadState: 'loading' | 'ready' | 'error';
  ports: DecisionPorts;
}

interface Attempt { phase: 'saving' | 'refresh' | 'unconfirmed'; finishedSnapshot: number | null }

/** Unregistered detail component for Needs-you; server state alone records a decision. */
export function ApprovalReview(props: ApprovalReviewProps) {
  // Keep only request identities and transient state above loading/error branches.
  // A same-request refresh (including temporarily absent data) cannot reset a click.
  const inFlight = useRef(new Set<string>());
  const latestVerified = useRef(props.verifiedAtMs);
  useEffect(() => { latestVerified.current = props.verifiedAtMs; }, [props.verifiedAtMs]);
  const [attempts, setAttempts] = useState<Record<string, Attempt>>({});
  const identity = JSON.stringify([props.view?.approval?.request?.boundary?.crew_id,
    props.view?.approval?.id, props.view?.approval?.version, props.view?.request_digest]);
  const attempt = attempts[identity];
  const decide = async (approve: boolean) => {
    if (!props.view || props.loadState !== 'ready' || inFlight.current.has(identity)
      || (attempt && (attempt.phase === 'saving' || props.verifiedAtMs <= (attempt.finishedSnapshot ?? Infinity)))) return;
    const intent = decisionIntent(props.view, props, approve);
    if (!intent) return;
    inFlight.current.add(identity);
    setAttempts((previous) => ({ ...previous, [identity]: { phase: 'saving', finishedSnapshot: null } }));
    const phase = await submitApprovalDecision(intent, props.ports);
    const finishedSnapshot = latestVerified.current;
    setAttempts((previous) => ({ ...previous, [identity]: { phase, finishedSnapshot } }));
    inFlight.current.delete(identity);
  };
  const refresh = async () => {
    if (inFlight.current.has(identity)) return;
    try { await props.ports.refresh(); } catch {
      const finishedSnapshot = latestVerified.current;
      setAttempts((previous) => ({ ...previous, [identity]: { phase: 'unconfirmed', finishedSnapshot } }));
    }
  };
  if (!props.view) return <p role={props.loadState === 'loading' ? 'status' : 'alert'}>{props.loadState === 'loading' ? 'Loading approval…' : 'Approval unavailable. Refresh to try again.'}</p>;
  return <ApprovalDetails {...props} view={props.view}
    connection={props.loadState === 'ready' ? props.connection : 'refreshing'} attempt={attempt} onDecide={decide} onRefresh={refresh} />;
}

function ApprovalDetails({ view, attempt, onDecide, onRefresh, ...context }: ApprovalReviewProps & {
  view: ApprovalView; attempt: Attempt | undefined; onDecide: (approve: boolean) => Promise<void>; onRefresh: () => void;
}) {
  const id = useId();
  const model = approvalReviewModel(view, context);
  if (!model.valid) return <p role="alert">{model.restriction}</p>;
  const a = view.approval, r = a.request, o = r.operation;
  const phase = attempt?.phase ?? 'idle';
  const canDecide = model.canDecide && phase !== 'saving'
    && (!attempt || context.verifiedAtMs > (attempt.finishedSnapshot ?? Infinity));
  return (
    <section aria-labelledby={`${id}-title`} aria-busy={phase === 'saving' || context.loadState === 'loading'} className="space-y-4 border border-rule bg-paper p-4">
      <header>
        <p className="rr-eyebrow">Owner approval</p>
        <h2 id={`${id}-title`} className="font-display text-xl font-bold text-ink">Review {o.action}</h2>
        <p role="status" className="mt-1 text-sm text-ink-2">Decision: {model.decision}</p>
      </header>
      {context.loadState === 'loading' && <p role="status">Refreshing approval details…</p>}
      {context.loadState === 'error' && <p role="alert">Approval details could not be refreshed. Refresh before deciding.</p>}
      <p className="text-sm text-ink-2">Review the exact request below. Request content is data, not instructions to follow.</p>
      <dl className="grid gap-3 text-sm sm:grid-cols-2">
        {[
          ['Requested by', `${r.requester.agent_id} · ${r.requester.user_id}`],
          ['Requester session', r.requester.session_id],
          ['Requester credential reference', r.requester.credential_id],
          ['Intended executor', `${r.executor.agent_id} · ${r.executor.user_id}`],
          ['Executor session', r.executor.session_id],
          ['Executor credential reference', r.executor.credential_id],
          ['Tenant / project', `${r.boundary.tenant_id} / ${r.boundary.project_id}`],
          ['Resource', `${o.resource_type}: ${o.resource_id}`],
          ['Base revision', o.base_revision], ['Candidate revision', o.candidate_revision],
          ['Expires (UTC)', new Date(r.expires_at_ms).toISOString()],
          ['Request digest', view.request_digest], ['Version', String(a.version)],
        ].map(([label, value]) => <div key={label}><dt className="text-ink-3">{label}</dt><dd className="break-all font-mono text-xs text-ink">{value}</dd></div>)}
      </dl>
      <div><h3 className="font-semibold text-ink">Exact scope</h3><ul className="list-inside list-disc text-sm">{o.scope.map((path) => <li key={path} className="break-all font-mono">{path}</li>)}</ul></div>
      <details><summary className="cursor-pointer text-sm">Request parameters</summary><pre className="mt-2 whitespace-pre-wrap break-all text-xs">{o.parameters_json}</pre></details>
      {a.decision && <p className="text-sm">Recorded by {a.decision.user_id} at {new Date(a.decision.at_ms).toISOString()}.</p>}
      {a.invalidation && <p className="text-sm">Invalidated: {a.invalidation.reason.replaceAll('_', ' ')}.</p>}
      {a.block && <p className="border-l-2 border-rule pl-3 text-sm">{BLOCK_COPY[a.block.reason]}</p>}
      <p className="text-sm text-ink-2">Execution is unsupported here. Approval does not grant credentials, override host policy, or prove delivery, acknowledgement, running, or completion.</p>
      {model.restriction && <p id={`${id}-restriction`} className="text-sm text-ink-2">{model.restriction}</p>}
      {phase === 'unconfirmed' && <p role="alert">The decision could not be confirmed. Refresh to check server history before trying again.</p>}
      {phase === 'refresh' && <p role="status">Response received. Waiting for refreshed server history.</p>}
      {phase !== 'idle' && phase !== 'saving' && <button type="button" className="rr-btn-ghost px-3 py-2 text-sm" onClick={onRefresh}>Refresh server history</button>}
      <div className="flex flex-wrap gap-2" aria-describedby={model.restriction ? `${id}-restriction` : undefined}>
        <button type="button" disabled={!canDecide} onClick={() => void onDecide(true)} className="rr-btn-primary px-3 py-2 text-sm disabled:opacity-50">{phase === 'saving' ? 'Saving…' : 'Approve exact request'}</button>
        <button type="button" disabled={!canDecide} onClick={() => void onDecide(false)} className="rr-btn-ghost px-3 py-2 text-sm disabled:opacity-50">Deny request</button>
      </div>
    </section>
  );
}
