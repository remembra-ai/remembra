// Pure presentation of the reviewed approval transport. No authority or network.
import { actorAccess } from '../zones/live';
import type { HumanActionRunner } from '../policy/useHumanAction';

export interface ApprovalPrincipal {
  user_id: string;
  credential_id: string;
  session_id: string;
  agent_id: string;
}

export interface ApprovalView {
  approval: {
    id: string;
    version: number;
    request: {
      boundary: { crew_id: string; tenant_id: string; project_id: string };
      requester: ApprovalPrincipal;
      executor: ApprovalPrincipal;
      operation: {
        action: string; resource_type: string; resource_id: string;
        scope: readonly string[]; base_revision: string; candidate_revision: string;
        parameters_json: string;
      };
      expires_at_ms: number;
      idempotency_key: string;
    };
    decision: null | {
      state: 'approved' | 'denied'; user_id: string; auth_time_ms: number;
      at_ms: number; request_digest: string;
    };
    invalidation: null | { reason: 'revoked' | 'expired' | 'stale_candidate'; actor_id: string; at_ms: number };
    block: null | {
      reason: 'host_policy' | 'source_freeze' | 'human_handoff_required' | 'unsupported_adapter';
      resolver: 'human_owner' | 'host_operator' | 'source_owner';
      next_action: 'confirm_in_host' | 'review_source_scope' | 'enable_supported_adapter';
    };
    consumption: null | { id: string; request_digest: string; executor: ApprovalPrincipal; at_ms: number };
  };
  request_digest: string;
  execution: { supported: false; reason: 'unsupported_adapter' };
}

export interface ReviewContext {
  access: Parameters<typeof actorAccess>[0];
  connection: 'current' | 'refreshing' | 'disconnected';
  /** The containing Crew view supplies its ticking, server-adjusted clock. */
  nowMs: number;
  /** Set only on an authoritative fetch; a changed value resets ambiguous UI state. */
  verifiedAtMs: number;
}

export interface DecisionIntent {
  crewId: string;
  approvalId: string;
  request_digest: string;
  expected_version: number;
  approve: boolean;
}

export interface DecisionPorts {
  /** Use the existing useHumanAction().run, with its existing StepUpDialog. */
  humanAction: HumanActionRunner['run'];
  /** Reviewed authenticated API adapter; no executor or consume callback. */
  decide: (intent: Readonly<DecisionIntent>) => Promise<unknown>;
  /** Fetch authoritative state after success, conflict, or an ambiguous response. */
  refresh: () => Promise<unknown>;
}

const identifier = (v: unknown): v is string => typeof v === 'string' && /^[A-Za-z0-9][A-Za-z0-9._:/@-]{0,199}$/.test(v);
const revision = (v: unknown): v is string => typeof v === 'string' && /^(?:[a-f0-9]{40}|[a-f0-9]{64})$/.test(v);
const digest = (v: unknown): v is string => typeof v === 'string' && /^[a-f0-9]{64}$/.test(v);
const positive = (v: unknown): v is number => typeof v === 'number' && Number.isSafeInteger(v) && v > 0;
const timestamp = (v: unknown): v is number => positive(v) && v <= 8_640_000_000_000_000;
const principal = (v: ApprovalPrincipal) => v && [v.user_id, v.credential_id, v.session_id, v.agent_id].every(identifier);

/** Defensive display checks, not a replacement for server contract/auth checks. */
export function validApprovalView(value: ApprovalView): boolean {
  try {
    const a = value.approval, r = a.request, o = r.operation;
    const params: unknown = JSON.parse(o.parameters_json);
    return identifier(a.id) && positive(a.version) && digest(value.request_digest)
      && [r.boundary.crew_id, r.boundary.tenant_id, r.boundary.project_id, r.idempotency_key].every(identifier)
      && principal(r.requester) && principal(r.executor) && timestamp(r.expires_at_ms)
      && [o.action, o.resource_type, o.resource_id].every(identifier)
      && revision(o.base_revision) && revision(o.candidate_revision)
      && Array.isArray(o.scope) && o.scope.length > 0 && o.scope.length <= 100
      && o.scope.every((s, i) => identifier(s) && !s.startsWith('/')
        && s.split('/').every((part) => part !== '' && part !== '.' && part !== '..')
        && (i === 0 || o.scope[i - 1] < s))
      && o.parameters_json.length <= 8192 && params !== null && typeof params === 'object' && !Array.isArray(params)
      && (a.decision === null || (['approved', 'denied'].includes(a.decision.state)
        && identifier(a.decision.user_id) && timestamp(a.decision.at_ms) && timestamp(a.decision.auth_time_ms)
        && a.decision.request_digest === value.request_digest))
      && (a.invalidation === null || (['revoked', 'expired', 'stale_candidate'].includes(a.invalidation.reason)
        && identifier(a.invalidation.actor_id) && timestamp(a.invalidation.at_ms)))
      && (a.block === null || (['host_policy', 'source_freeze', 'human_handoff_required', 'unsupported_adapter'].includes(a.block.reason)
        && ['human_owner', 'host_operator', 'source_owner'].includes(a.block.resolver)
        && ['confirm_in_host', 'review_source_scope', 'enable_supported_adapter'].includes(a.block.next_action)))
      && (a.consumption === null || (identifier(a.consumption.id) && timestamp(a.consumption.at_ms)
        && a.consumption.request_digest === value.request_digest && principal(a.consumption.executor)
        && a.decision?.state === 'approved'))
      && value.execution.supported === false && value.execution.reason === 'unsupported_adapter';
  } catch {
    return false;
  }
}

export function approvalReviewModel(view: ApprovalView, context: ReviewContext) {
  if (!validApprovalView(view)) {
    return { valid: false, decision: 'Unavailable', restriction: 'Approval details could not be verified. Refresh before deciding.', canDecide: false };
  }
  const a = view.approval;
  const decision = a.decision?.state === 'approved' ? 'Approved' : a.decision?.state === 'denied' ? 'Denied' : 'Awaiting owner';
  const access = actorAccess(context.access);
  let restriction: string | null = null;
  if (context.connection !== 'current') restriction = 'Reconnect and refresh this request before deciding.';
  else if (!timestamp(context.nowMs) || !timestamp(context.verifiedAtMs)
    || context.verifiedAtMs > context.nowMs || context.nowMs - context.verifiedAtMs > 30_000) restriction = 'Refresh to verify the current request and expiry.';
  else if (!access.canAct || !['owner', 'admin'].includes(context.access?.role ?? '')) restriction = 'A signed-in owner or admin must make this decision.';
  else if (a.invalidation?.reason === 'revoked') restriction = 'Revoked. The original decision remains in history.';
  else if (a.invalidation?.reason === 'stale_candidate') restriction = 'The candidate changed. Request a new approval for the current revision.';
  else if (a.invalidation?.reason === 'expired' || context.nowMs >= a.request.expires_at_ms) restriction = 'Expired. Request a new approval.';
  else if (a.consumption) restriction = 'A claim receipt exists. This does not prove execution or completion.';
  else if (a.decision) restriction = 'This decision is recorded. Refresh to see later changes.';
  return { valid: true, decision, restriction, canDecide: restriction === null };
}

/** Capture only the exact displayed request; never take authority from prose. */
export function decisionIntent(view: ApprovalView, context: ReviewContext, approve: boolean): Readonly<DecisionIntent> | null {
  if (!approvalReviewModel(view, context).canDecide) return null;
  return Object.freeze({ crewId: view.approval.request.boundary.crew_id, approvalId: view.approval.id,
    request_digest: view.request_digest, expected_version: view.approval.version, approve });
}

/** No raw exception/status text reaches the review screen. */
export async function submitApprovalDecision(intent: Readonly<DecisionIntent>, ports: DecisionPorts): Promise<'refresh' | 'unconfirmed'> {
  let result: 'refresh' | 'unconfirmed' = 'unconfirmed';
  try {
    await ports.humanAction(intent.approve ? 'Approve this exact request' : 'Deny this exact request', () => ports.decide(intent));
    result = 'refresh';
  } catch {
    // A lost response may follow a committed decision. Fetch, never auto-replay.
  }
  try { await ports.refresh(); } catch { result = 'unconfirmed'; }
  return result;
}

export const BLOCK_COPY = {
  host_policy: 'The host’s policy blocks this action. The host operator must review it.',
  source_freeze: 'The requested source is frozen. Its owner must review the exact scope.',
  human_handoff_required: 'The host requires a direct human handoff. Confirm in that host.',
  unsupported_adapter: 'This host cannot resume this action through a supported adapter.',
} as const;
