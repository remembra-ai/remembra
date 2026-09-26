import { describe, expect, it } from 'vitest';
import type { CrewEvent, InboxItemView } from '../../../lib/crew/types';
import { GLYPHS, ditherLevels, glyphCells } from '../channel/dither';
import {
  coalescedLabel,
  foldSessionQueues,
  inboxParams,
  isSafety,
  originLabel,
  parseInboxRoute,
  primaryAction,
  sortItems,
  titleCallsign,
  type InboxItem,
} from '../inbox/model';
import {
  badgeText,
  markReadLocally,
  notificationHref,
  targetProblem,
  unreadUpto,
  webhookRecipe,
  type NotificationItem,
  type NotificationList,
} from '../notify/model';

function item(id: string, extra: Partial<InboxItem> = {}): InboxItem {
  return {
    id,
    crew_id: 'crw_1',
    audience: 'project',
    kind: 'review_report',
    origin: 'server',
    ref_type: 'task',
    ref_id: 'tsk_14',
    priority: 2,
    title: 'Review the report for T-14',
    primary_action: 'review',
    state: 'open',
    coalesced_count: 1,
    safety: false,
    created_at: '2026-09-26T10:00:00Z',
    updated_at: '2026-09-26T10:00:00Z',
    ...extra,
  };
}

describe('inbox ordering and labels', () => {
  it('sorts like the server: safety, then server/human, then agent; priority; newest first', () => {
    const items = [
      item('agent', { origin: 'agent', kind: 'human_question', priority: 1, updated_at: '2026-09-26T11:00:00Z' }),
      item('server-old', { updated_at: '2026-09-26T09:00:00Z' }),
      item('server-new', { updated_at: '2026-09-26T10:30:00Z' }),
      item('urgent', { priority: 1, updated_at: '2026-09-26T08:00:00Z' }),
      item('safety', { kind: 'baton_available', safety: true, priority: 3, updated_at: '2026-09-26T07:00:00Z' }),
    ];
    expect(sortItems(items).map((i) => i.id)).toEqual(['safety', 'urgent', 'server-new', 'server-old', 'agent']);
  });

  it('safety comes from the server flag, else from origin and kind', () => {
    expect(isSafety({ origin: 'server', kind: 'tamper_blocked' })).toBe(true);
    expect(isSafety({ origin: 'agent', kind: 'tamper_blocked' })).toBe(false);
    expect(isSafety({ origin: 'server', kind: 'review_report', safety: true })).toBe(true);
  });

  it('agent-originated items say who, with the key-verified / self-declared label when known', () => {
    const sessions = [
      { callsign: 'cc-2', agent_verified: false },
      { callsign: 'codex-1', agent_verified: true },
    ];
    expect(titleCallsign('cc-2 asked 4 questions')).toBe('cc-2');
    expect(titleCallsign('Review the report for T-14')).toBeNull();
    expect(originLabel({ origin: 'agent', title: 'cc-2 asked 4 questions' }, sessions)).toBe('from cc-2 (self-declared)');
    expect(originLabel({ origin: 'agent', title: 'codex-1 proposed 1 decision to confirm' }, sessions)).toBe('from codex-1 (key-verified)');
    expect(originLabel({ origin: 'agent', title: 'gemini-3 asked 1 question' }, sessions)).toBe('from gemini-3');
    expect(originLabel({ origin: 'server', title: 'cc-2 holds 4 zones' }, sessions)).toBeNull();
    expect(coalescedLabel({ coalesced_count: 4 })).toBe('×4');
    expect(coalescedLabel({ coalesced_count: 1 })).toBeNull();
  });
});

describe('one primary action per item', () => {
  it('maps every server primary_action', () => {
    expect(primaryAction(item('a', { kind: 'decision_to_confirm', primary_action: 'confirm_decision', ref_type: 'decision', ref_id: 'dec_9' }), 'yaadbooks')).toEqual({
      type: 'confirm_decision',
      label: 'Confirm',
      decisionId: 'dec_9',
    });
    expect(primaryAction(item('b', { kind: 'human_question', primary_action: 'answer', ref_type: 'message', ref_id: 'msg_1' }), 'yaadbooks')).toEqual({
      type: 'answer',
      label: 'Answer',
      messageId: 'msg_1',
      href: '#/crew?project=yaadbooks&view=channel&thread=msg_1',
    });
    expect(primaryAction(item('c', { kind: 'zone_hoarding', primary_action: 'release_all', ref_type: 'session', ref_id: 'cs_b' }), 'yaadbooks')).toMatchObject({
      type: 'release_all',
      sessionId: 'cs_b',
    });
    expect(primaryAction(item('d', { kind: 'stuck_agent', primary_action: 'checkpoint', ref_type: 'session', ref_id: 'cs_b' }), 'yaadbooks')).toMatchObject({
      type: 'request_checkpoint',
    });
    expect(primaryAction(item('e'), 'yaadbooks')).toEqual({ type: 'link', label: 'Review report', href: '#/crew?project=yaadbooks&view=board&task=tsk_14' });
    expect(primaryAction(item('f', { kind: 'baton_available', primary_action: 'hand_baton' }), 'yaadbooks')).toEqual({
      type: 'link',
      label: 'Hand baton to…',
      href: '#/crew?project=yaadbooks',
    });
    expect(primaryAction(item('g', { kind: 'zone_change_pending', primary_action: 'approve', ref_type: 'zone_change', ref_id: 'zch_1' }), 'yaadbooks')).toEqual({
      type: 'link',
      label: 'Review zone change',
      href: '#/crew?project=yaadbooks&view=policy',
    });
    expect(primaryAction(item('h', { kind: 'collision_escalated', primary_action: 'review', ref_type: 'collision', ref_id: 'col_1' }), 'yaadbooks')).toEqual({
      type: 'link',
      label: 'Review',
      href: '#/crew?project=yaadbooks&view=feed&type=collision.',
    });
  });

  it('falls back to opening the reference, or nothing without a project', () => {
    expect(primaryAction(item('i', { kind: 'tamper_blocked', primary_action: null, ref_type: 'session', ref_id: 'cs_b' }), 'yaadbooks')).toEqual({
      type: 'link',
      label: 'Open',
      href: '#/crew?project=yaadbooks',
    });
    expect(primaryAction(item('j', { primary_action: null, ref_type: null, ref_id: null }), 'yaadbooks')).toEqual({ type: 'none' });
    expect(primaryAction(item('k', { primary_action: 'answer', ref_type: 'message', ref_id: 'msg_1' }), null)).toEqual({ type: 'none' });
  });

  it('crew items are taken while open, opened once claimed', () => {
    expect(primaryAction(item('l', { audience: 'crew', kind: 'task_ready' }), 'yaadbooks')).toEqual({ type: 'claim', label: 'Take it' });
    expect(primaryAction(item('m', { audience: 'crew', kind: 'task_ready', state: 'claimed' }), 'yaadbooks')).toMatchObject({ type: 'link', label: 'Open' });
  });
});

describe('inbox route', () => {
  it('defaults to Needs you and keeps old agent-inbox links working', () => {
    expect(parseInboxRoute(new URLSearchParams(''))).toEqual({ scope: 'needs-you', project: null, details: null, alerts: false });
    expect(parseInboxRoute(new URLSearchParams('scope=crew&project=yaadbooks'))).toMatchObject({ scope: 'crew', project: 'yaadbooks' });
    expect(parseInboxRoute(new URLSearchParams('compose=1&to=codex'))).toMatchObject({ scope: 'needs-you', details: 'agent' });
    expect(parseInboxRoute(new URLSearchParams('open=inb_1'))).toMatchObject({ details: 'agent' });
    expect(parseInboxRoute(new URLSearchParams('details=sessions&alerts=1'))).toMatchObject({ details: 'sessions', alerts: true });
    expect(parseInboxRoute(new URLSearchParams('scope=bogus&details=bogus'))).toMatchObject({ scope: 'needs-you', details: null });
    expect(inboxParams({ scope: 'crew', project: 'x', details: 'sessions', alerts: false })).toEqual({ scope: 'crew', project: 'x', details: 'sessions', alerts: null });
  });
});

describe('session queues from the event log', () => {
  const view = (id: string, recipient: string, state: InboxItemView['state'], audience: InboxItemView['audience'] = 'session'): InboxItemView => ({
    id,
    audience,
    recipient,
    kind: 'mention',
    origin: 'human',
    ref_type: 'message',
    ref_id: 'msg_1',
    priority: 2,
    title: `human mentioned you (chat, ${id})`,
    primary_action: null,
    state,
    coalesced_count: 1,
  });
  const ev = (seq: number, type: string, item: InboxItemView): CrewEvent => ({
    seq,
    id: `evt_${seq}`,
    crew_id: 'crw_1',
    project_id: 'yaadbooks',
    ts: '2026-09-26T10:00:00Z',
    type,
    v: 1,
    origin: 'server',
    actor: { kind: 'system', id: 'system', verified: true },
    refs: {},
    severity: 'info',
    moment: false,
    summary: type,
    payload: { item },
  });

  it('keeps open session items per recipient in creation order and drops resolved ones', () => {
    const queues = foldSessionQueues([
      ev(3, 'inbox.item_created', view('i2', 'cs_a', 'open')),
      ev(1, 'inbox.item_created', view('i1', 'cs_a', 'open')),
      ev(2, 'inbox.item_created', view('i3', 'cs_b', 'open')),
      ev(4, 'inbox.item_resolved', view('i3', 'cs_b', 'resolved')),
      ev(5, 'inbox.item_created', view('p1', 'x', 'open', 'project')),
      ev(6, 'message.posted', view('zz', 'cs_a', 'open')),
    ]);
    expect([...queues.keys()]).toEqual(['cs_a']);
    expect(queues.get('cs_a')?.map((i) => i.id)).toEqual(['i1', 'i2']);
  });
});

describe('notifications', () => {
  const n = (crew: string, seq: number, read: boolean, kind = 'handoff'): NotificationItem => ({
    crew_id: crew,
    project_id: crew === 'crw_1' ? 'yaadbooks' : 'remembra',
    seq,
    kind,
    event_type: 'session.quota_blocked',
    realtime: true,
    text: 'cc-2 stopped (billing_error, reported). Work saved; pos reserved for the next pickup.',
    link: 'https://app.remembra.dev/#/…',
    ts: '2026-09-26T10:00:00Z',
    read,
  });

  it('badge text and per-crew unread cursors', () => {
    expect(badgeText(0)).toBeNull();
    expect(badgeText(7)).toBe('7');
    expect(badgeText(250)).toBe('99+');
    expect(unreadUpto([n('crw_1', 4, false), n('crw_1', 9, false), n('crw_2', 3, false), n('crw_2', 8, true)])).toEqual({ crw_1: 9, crw_2: 3 });
  });

  it('marks read locally: one crew up to a seq, or everything', () => {
    const list: NotificationList = { items: [n('crw_1', 4, false), n('crw_1', 9, false), n('crw_2', 3, false)], unread: 3 };
    const one = markReadLocally(list, 'crw_1', 4);
    expect(one.items.map((i) => i.read)).toEqual([true, false, false]);
    expect(one.unread).toBe(2);
    expect(markReadLocally(list, null, null)).toMatchObject({ unread: 0 });
    expect(markReadLocally(list, null, null).items.every((i) => i.read)).toBe(true);
  });

  it('opens the exact item: the server deep link when it is a crew route, else the screen per kind with the event', () => {
    // remembra.crew.notify.deep_link (tests/crew/test_notify.py asserts the same strings)
    const server = { ...n('crw_1', 1042, false, 'handoff'), link: 'https://app.remembra.dev/#/crew?project=yaadbooks&view=feed&seq=1042' };
    expect(notificationHref(server)).toBe('#/crew?project=yaadbooks&view=feed&seq=1042');
    const receipt = { ...n('crw_1', 9, false, 'task_done'), link: 'https://x.test/#/crew?project=yaadbooks&view=report&report=rpt_0123456789abcdef&seq=9' };
    expect(notificationHref(receipt)).toBe('#/crew?project=yaadbooks&view=report&report=rpt_0123456789abcdef&seq=9');
    // an old-format or foreign link falls back to the kind's screen, still selecting the event
    expect(notificationHref({ ...n('crw_1', 5, false, 'handoff'), link: 'https://app.remembra.dev/#/crews/crw_1/feed?seq=5' })).toBe(
      '#/crew?project=yaadbooks&view=feed&seq=5',
    );
    expect(notificationHref(n('crw_1', 1, false, 'decision'))).toBe('#/crew?project=yaadbooks&view=channel&seq=1');
    expect(notificationHref(n('crw_1', 1, false, 'zone_change'))).toBe('#/crew?project=yaadbooks&view=policy&seq=1');
    expect(notificationHref(n('crw_1', 1, false, 'collision'))).toBe('#/crew?project=yaadbooks&view=feed&seq=1');
  });

  it('checks targets before they reach the server', () => {
    expect(targetProblem('email', '')).toMatch(/Type an email/);
    expect(targetProblem('email', 'mani@example')).toMatch(/does not look like/);
    expect(targetProblem('email', 'mani@example.com')).toBeNull();
    expect(targetProblem('webhook', 'http://bridge.example.com/x')).toMatch(/https/);
    expect(targetProblem('webhook', 'https://127.0.0.1/hook')).toMatch(/public addresses/);
    expect(targetProblem('webhook', 'https://localhost:8443/hook')).toMatch(/public addresses/);
    expect(targetProblem('webhook', 'https://user:pw@bridge.example.com/x')).toMatch(/credentials/);
    expect(targetProblem('webhook', 'https://bridge.example.com/remembra')).toBeNull();
    expect(webhookRecipe('X-Remembra-Signature')).toContain('X-Remembra-Signature');
  });
});

describe('pixel craft', () => {
  it('glyphs are hand-set on a 9-px grid with ink and signal cells', () => {
    for (const [name, rows] of Object.entries(GLYPHS)) {
      expect(rows.length, name).toBeLessThanOrEqual(9);
      for (const row of rows) expect(row, name).toMatch(/^[#o.]{9}$/);
      expect(glyphCells(rows).length, name).toBeGreaterThan(4);
    }
    const bell = glyphCells(GLYPHS.bell);
    expect(bell.filter((c) => c.signal)).toEqual([{ x: 4, y: 8, signal: true }]);
  });

  it('the dither bank is deterministic, sits where its shape says, and flecks ember only when asked', () => {
    const opts = { shape: 'right' as const, seed: 3, ember: 0, cell: 5 };
    const a = ditherLevels(80, 20, 1.5, opts);
    const b = ditherLevels(80, 20, 1.5, opts);
    expect(Array.from(a)).toEqual(Array.from(b));
    const left = Array.from({ length: 20 }, (_, j) => Array.from(a.slice(j * 80, j * 80 + 30))).flat();
    const right = Array.from({ length: 20 }, (_, j) => Array.from(a.slice(j * 80 + 50, j * 80 + 80))).flat();
    expect(left.every((v) => v === 0)).toBe(true);
    expect(right.some((v) => v > 0)).toBe(true);
    expect(Array.from(a).some((v) => v === 4)).toBe(false);
    const embers = ditherLevels(120, 40, 1.5, { ...opts, ember: 0.2 });
    expect(Array.from(embers).some((v) => v === 4)).toBe(true);
    expect(Array.from(ditherLevels(80, 20, 9, opts))).not.toEqual(Array.from(a)); // it drifts over time
    // it keeps clear of the copy it sits behind
    const clear = ditherLevels(80, 20, 1.5, { ...opts, avoid: [{ l: 250, t: 0, r: 400, b: 100 }] });
    const inside = Array.from({ length: 20 }, (_, j) => Array.from(clear.slice(j * 80 + 50, j * 80 + 80))).flat();
    expect(inside.every((v) => v === 0)).toBe(true);
    const partial = ditherLevels(80, 20, 1.5, { ...opts, avoid: [{ l: 250, t: 0, r: 330, b: 100 }] });
    expect(Array.from(partial).filter((v) => v > 0).length).toBeLessThan(Array.from(a).filter((v) => v > 0).length);
  });
});
