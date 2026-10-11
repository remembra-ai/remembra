import { describe, expect, it, vi } from 'vitest';
import { approvalReviewModel, decisionIntent, submitApprovalDecision, validApprovalView, type ApprovalView, type ReviewContext, type DecisionPorts } from '../approvalModel';

export const NOW = 1_800_000_000_000;
export const context: ReviewContext = {
  access: { human: true, role: 'owner', permissions: ['crew:read', 'crew:override'] },
  connection: 'current', nowMs: NOW, verifiedAtMs: NOW,
};
export function fixture(): ApprovalView {
  const principal = { user_id: 'u_agent', credential_id: 'key_reference', session_id: 'cs_agent', agent_id: 'codex' };
  return {
    approval: {
      id: 'approval_1', version: 1,
      request: { boundary: { crew_id: 'crw_test', tenant_id: 'owner_1', project_id: 'project_1' },
        requester: { ...principal }, executor: { ...principal },
        operation: { action: 'review_change', resource_type: 'repository', resource_id: 'repo_1',
          scope: ['src/a.py', 'src/b.py'], base_revision: 'a'.repeat(40), candidate_revision: 'b'.repeat(40),
          parameters_json: '{"description":"literal request data"}' },
        expires_at_ms: NOW + 60_000, idempotency_key: 'request_1' },
      decision: null, invalidation: null, block: null, consumption: null,
    },
    request_digest: 'c'.repeat(64), execution: { supported: false, reason: 'unsupported_adapter' },
  };
}

describe('owner approval review model', () => {
  it('binds the exact displayed request, not text or caller identity', () => {
    const v = fixture();
    const intent = decisionIntent(v, context, false);
    expect(intent).toEqual({ crewId: 'crw_test', approvalId: 'approval_1', request_digest: 'c'.repeat(64), expected_version: 1, approve: false });
    expect(Object.isFrozen(intent)).toBe(true);
    v.approval.version = 2;
    expect(intent?.expected_version).toBe(1);
  });
  it.each(['owner', 'admin'] as const)('accepts current human %s presentation access', (role) => {
    expect(approvalReviewModel(fixture(), { ...context, access: { ...context.access!, role } }).canDecide).toBe(true);
  });
  it.each<ReviewContext['access']>([
    { human: false, role: 'owner', permissions: ['crew:override'] },
    { human: true, role: 'member', permissions: ['crew:override'] },
    { human: true, role: 'admin', permissions: ['crew:read'] },
    null,
  ])('rejects API keys, missing membership authority, and absent access', (access) => {
    expect(decisionIntent(fixture(), { ...context, access }, true)).toBeNull();
  });
  it.each(['refreshing', 'disconnected'] as const)('blocks %s data', (connection) => {
    expect(decisionIntent(fixture(), { ...context, connection }, true)).toBeNull();
  });
  it.each([NaN, Infinity, 0, NOW + 1, NOW - 30_001])('blocks invalid/stale verification clock %s', (verifiedAtMs) => {
    expect(decisionIntent(fixture(), { ...context, verifiedAtMs }, true)).toBeNull();
  });
  it('blocks at exact expiry without replacing an earlier approved decision', () => {
    const v = fixture(); v.approval.request.expires_at_ms = NOW;
    v.approval.decision = { state: 'approved', user_id: 'owner_1', at_ms: NOW - 1, auth_time_ms: NOW - 100, request_digest: v.request_digest };
    expect(approvalReviewModel(v, context)).toMatchObject({ decision: 'Approved', canDecide: false, restriction: 'Expired. Request a new approval.' });
  });
  it.each(['revoked', 'expired', 'stale_candidate'] as const)('keeps denial history separate from %s', (reason) => {
    const v = fixture();
    v.approval.decision = { state: 'denied', user_id: 'owner_1', at_ms: NOW, auth_time_ms: NOW, request_digest: v.request_digest };
    v.approval.invalidation = { reason, actor_id: 'owner_1', at_ms: NOW };
    expect(approvalReviewModel(v, context)).toMatchObject({ decision: 'Denied', canDecide: false });
  });
  it('does not equate a claim receipt with execution', () => {
    const v = fixture();
    v.approval.decision = { state: 'approved', user_id: 'owner_1', at_ms: NOW, auth_time_ms: NOW, request_digest: v.request_digest };
    v.approval.consumption = { id: 'receipt_1', request_digest: v.request_digest, executor: v.approval.request.executor, at_ms: NOW };
    expect(approvalReviewModel(v, context).restriction).toMatch(/does not prove execution/);
  });
  it.each([
    (v: ApprovalView) => { v.request_digest = 'invalid'; },
    (v: ApprovalView) => { v.approval.version = NaN; },
    (v: ApprovalView) => { v.approval.request.expires_at_ms = Number.MAX_SAFE_INTEGER; },
    (v: ApprovalView) => { v.approval.request.operation.scope = ['../secret']; },
    (v: ApprovalView) => { v.approval.request.operation.scope = ['src/a.py', 'src/a.py']; },
    (v: ApprovalView) => { v.approval.request.operation.parameters_json = '[]'; },
    (v: ApprovalView) => { v.approval.request.executor.session_id = ''; },
    (v: ApprovalView) => { v.approval.decision = { state: 'approved', user_id: 'owner', at_ms: NOW, auth_time_ms: NOW, request_digest: 'd'.repeat(64) }; },
  ])('fails closed on malformed or mismatched presentation data', (mutate) => {
    const v = fixture(); mutate(v);
    expect(validApprovalView(v)).toBe(false);
    expect(decisionIntent(v, context, true)).toBeNull();
  });
  it('does not accept a new executor capability through the old view contract', () => {
    const v = fixture(); Object.assign(v.execution, { supported: true });
    expect(validApprovalView(v)).toBe(false);
  });
});

describe('human decision port', () => {
  function ports(): DecisionPorts {
    return { humanAction: async (_what, action) => action(), decide: vi.fn(async () => undefined), refresh: vi.fn(async () => undefined) };
  }
  it('uses the existing human runner and refreshes only after the decision response', async () => {
    const p = ports(), order: string[] = [];
    p.humanAction = async (_what, action) => { order.push('human'); return action(); };
    p.decide = async () => { order.push('decision'); }; p.refresh = async () => { order.push('refresh'); };
    expect(await submitApprovalDecision(decisionIntent(fixture(), context, true)!, p)).toBe('refresh');
    expect(order).toEqual(['human', 'decision', 'refresh']);
  });
  it('refreshes after an ambiguous response without retrying or echoing errors', async () => {
    const p = ports(); p.decide = vi.fn(async () => { throw new Error('secret input'); });
    expect(await submitApprovalDecision(decisionIntent(fixture(), context, true)!, p)).toBe('unconfirmed');
    expect(p.decide).toHaveBeenCalledTimes(1); expect(p.refresh).toHaveBeenCalledTimes(1);
  });
  it('does not decide when the human runner cancels', async () => {
    const p = ports(); p.humanAction = async () => { throw new Error('cancelled'); };
    expect(await submitApprovalDecision(decisionIntent(fixture(), context, false)!, p)).toBe('unconfirmed');
    expect(p.decide).not.toHaveBeenCalled(); expect(p.refresh).toHaveBeenCalledOnce();
  });
  it('cannot report confirmation if authoritative refresh fails', async () => {
    const p = ports(); p.refresh = async () => { throw new Error('untrusted'); };
    expect(await submitApprovalDecision(decisionIntent(fixture(), context, true)!, p)).toBe('unconfirmed');
  });
});
