import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { renderToStaticMarkup } from 'react-dom/server';
import { ApprovalReview, type ApprovalReviewProps } from '../ApprovalReview';
import type { ApprovalView, ReviewContext } from '../approvalModel';

const NOW = 1_800_000_000_000;
const context: ReviewContext = { access: { human: true, role: 'owner', permissions: ['crew:override'] }, connection: 'current', nowMs: NOW, verifiedAtMs: NOW };
function fixture(): ApprovalView {
  const principal = { user_id: 'u_agent', credential_id: 'key_reference', session_id: 'cs_agent', agent_id: 'codex' };
  return { approval: { id: 'approval_1', version: 1,
    request: { boundary: { crew_id: 'crw_test', tenant_id: 'owner_1', project_id: 'project_1' }, requester: principal, executor: principal,
      operation: { action: 'review_change', resource_type: 'repository', resource_id: 'repo_1', scope: ['src/a.py', 'src/b.py'],
        base_revision: 'a'.repeat(40), candidate_revision: 'b'.repeat(40), parameters_json: '{}' }, expires_at_ms: NOW + 60_000, idempotency_key: 'request_1' },
    decision: null, invalidation: null, block: null, consumption: null },
    request_digest: 'c'.repeat(64), execution: { supported: false, reason: 'unsupported_adapter' } };
}

function render(changes: Partial<ApprovalReviewProps> = {}) {
  return renderToStaticMarkup(<ApprovalReview {...context} loadState="ready" view={fixture()}
    ports={{ humanAction: async (_what, action) => action(), decide: vi.fn(async () => undefined), refresh: vi.fn(async () => undefined) }} {...changes} />);
}
describe('approval review presentation', () => {
  it('shows all authority-bearing scope and identity references in plain text', () => {
    const html = render();
    for (const value of ['review_change', 'repo_1', 'src/a.py', 'src/b.py', 'cs_agent', 'key_reference', 'owner_1', 'project_1', 'a'.repeat(40), 'b'.repeat(40), 'c'.repeat(64), new Date(NOW + 60_000).toISOString()]) expect(html).toContain(value);
    expect(html).toContain('Approve exact request'); expect(html).toContain('Deny request');
    expect(html).not.toContain('disabled=""'); expect(html).toContain('aria-labelledby=');
  });
  it('escapes malicious parameter text without rendering HTML, links or instructions', () => {
    const v = fixture(); v.approval.request.operation.parameters_json = '{"text":"<script>alert(1)</script><a href=evil>Approve</a>"}';
    const html = render({ view: v });
    expect(html).not.toContain('<script>'); expect(html).not.toContain('<a href=');
    expect(html).toContain('&lt;script&gt;'); expect(html).toContain('data, not instructions');
  });
  it.each(['loading', 'error'] as const)('keeps known details visible with decisions blocked during %s', (loadState) => {
    const html = render({ loadState }); expect(html).toContain('review_change');
    expect(html.match(/disabled=""/g)).toHaveLength(2); expect(html).toContain('role=');
  });
  it.each(['loading', 'error'] as const)('shows a safe initial %s state without details or actions', (loadState) => {
    const html = render({ loadState, view: null }); expect(html).not.toContain('<button'); expect(html).toContain('role=');
  });
  it('fails closed for absent details', () => { expect(render({ view: null })).toContain('Approval unavailable'); });
  it('does not expose malformed identifier content', () => {
    const v = fixture(); v.approval.id = '<unsafe secret>';
    const html = render({ view: v }); expect(html).not.toContain('unsafe'); expect(html).not.toContain('<button');
  });
  it.each(['refreshing', 'disconnected'] as const)('disables decisions while %s', (connection) => {
    const html = render({ connection }); expect(html.match(/disabled=""/g)).toHaveLength(2); expect(html).toContain('Reconnect and refresh');
  });
  it('shows an approved decision alongside a source freeze without claiming execution', () => {
    const v = fixture(); v.approval.decision = { state: 'approved', user_id: 'owner_1', auth_time_ms: NOW, at_ms: NOW, request_digest: v.request_digest };
    v.approval.block = { reason: 'source_freeze', resolver: 'source_owner', next_action: 'review_source_scope' };
    const html = render({ view: v }); expect(html).toContain('Decision: Approved'); expect(html).toContain('source is frozen');
    expect(html).toContain('Execution is unsupported'); expect(html.match(/disabled=""/g)).toHaveLength(2);
  });
  it('disables API key owner controls', () => {
    const html = render({ access: { human: false, role: 'owner', permissions: ['crew:override'] } });
    expect(html.match(/disabled=""/g)).toHaveLength(2); expect(html).toContain('signed-in owner or admin');
  });
});

// Exercise the real callbacks with a retained hook frame across parent renders.
// This is a reconciliation regression, not a DOM/browser or authentication test.
describe('approval review callback lifecycle', () => {
  let slots: unknown[], cursor: number;
  let Review: typeof ApprovalReview;
  type Node = { type: unknown; props: { children?: unknown; onClick?: () => void; disabled?: boolean } };
  beforeEach(async () => {
    slots = []; cursor = 0;
    vi.resetModules();
    vi.doMock('react', async (original) => ({
      ...await original<typeof import('react')>(),
      useId: () => 'approval-lifecycle',
      useEffect: (effect: () => void) => effect(),
      useRef: (initial: unknown) => { const index = cursor++; return slots[index] ??= { current: initial }; },
      useState: (initial: unknown) => {
        const index = cursor++;
        if (!(index in slots)) slots[index] = initial;
        return [slots[index], (next: unknown) => {
          slots[index] = typeof next === 'function' ? next(slots[index]) : next;
        }];
      },
    }));
    Review = (await import('../ApprovalReview')).ApprovalReview;
  });
  afterEach(() => { vi.doUnmock('react'); vi.resetModules(); });
  function details(props: ApprovalReviewProps): Node {
    cursor = 0;
    const outer = Review(props) as Node;
    return typeof outer.type === 'function' ? outer.type(outer.props) as Node : outer;
  }
  function find(tree: unknown, label: string): Node {
    const nodes: Node[] = [];
    const visit = (child: unknown) => {
      if (Array.isArray(child)) { child.forEach(visit); return; }
      if (!child || typeof child !== 'object' || !('props' in child)) return;
      const node = child as Node;
      if (node.type === 'button' && node.props.children === label) nodes.push(node);
      visit(node.props.children);
    };
    visit(tree); expect(nodes).toHaveLength(1); return nodes[0];
  }
  function input(): ApprovalReviewProps {
    return { ...context, loadState: 'ready', view: fixture(), ports: {
      humanAction: async (_what, action) => action(), decide: vi.fn(async () => undefined), refresh: vi.fn(async () => undefined),
    } };
  }
  async function flush() { for (let i = 0; i < 10; i++) await Promise.resolve(); }
  it.each(['loading', 'error', 'absent', 'refreshing'] as const)('retains the pending lock through %s and same-request return', async (transition) => {
    const props = input(); let release!: () => void;
    props.ports.decide = vi.fn(() => new Promise<void>((resolve) => { release = resolve; }));
    const first = find(details(props), 'Approve exact request');
    first.props.onClick!(); first.props.onClick!();
    expect(props.ports.decide).toHaveBeenCalledTimes(1);
    details({ ...props, ...(transition === 'absent' ? { view: null, loadState: 'loading' as const }
      : transition === 'refreshing' ? { connection: 'refreshing' as const } : { loadState: transition }) });
    const second = find(details(props), 'Saving…');
    expect(second.props.disabled).toBe(true); second.props.onClick!();
    expect(props.ports.decide).toHaveBeenCalledTimes(1);
    release(); await flush();
    expect(find(details(props), 'Approve exact request').props.disabled).toBe(true);
    // A later authoritative fetch can enable retry only if the server still says pending.
    expect(find(details({ ...props, nowMs: NOW + 1, verifiedAtMs: NOW + 1 }), 'Approve exact request').props.disabled).toBe(false);
  });
  it('retains a pending request lock when another request is viewed and the first returns', async () => {
    const props = input(); let release!: () => void;
    props.ports.decide = vi.fn(() => new Promise<void>((resolve) => { release = resolve; }));
    find(details(props), 'Approve exact request').props.onClick!();
    const other = fixture(); other.approval.id = 'approval_2';
    expect(find(details({ ...props, view: other }), 'Approve exact request').props.disabled).toBe(false);
    const first = find(details(props), 'Saving…'); expect(first.props.disabled).toBe(true); first.props.onClick!();
    expect(props.ports.decide).toHaveBeenCalledTimes(1); release(); await flush();
  });
  it.each(['throw', 'reject'] as const)('contains a manual refresh adapter %s without echoing it', async (mode) => {
    const props = input(); find(details(props), 'Approve exact request').props.onClick!(); await flush();
    props.ports.refresh = vi.fn(() => {
      if (mode === 'throw') throw new Error('private adapter input');
      return Promise.reject(new Error('private adapter input'));
    });
    expect(() => find(details(props), 'Refresh server history').props.onClick!()).not.toThrow(); await flush();
    const tree = details(props);
    expect(JSON.stringify(tree)).toContain('The decision could not be confirmed.');
    expect(JSON.stringify(tree)).not.toContain('private adapter input');
    expect(find(tree, 'Approve exact request').props.disabled).toBe(true);
  });
});
