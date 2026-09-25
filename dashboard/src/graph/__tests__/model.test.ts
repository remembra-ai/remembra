import { describe, expect, it } from 'vitest';
import type { EntityGraphDataResponse } from '../../lib/api';
import type { ActivitySummary, InboxMessage, TrailItem } from '../../lib/relay';
import { buildGraph, eventInView, graphSignature, messageEvent, newEvents, nextAgentIn, trailEvent } from '../model';
import { ForceLayout } from '../layout';

const NOW = Date.parse('2026-09-25T12:00:00Z');
const iso = (hoursAgo: number) => new Date(NOW - hoursAgo * 3600e3).toISOString();

function bucket(hoursAgo: number) {
  return { handoffs: 1, checkpoints: 0, last_active: iso(hoursAgo), sessions_7d: 1, daily: [1] };
}

const summary: ActivitySummary = {
  generated_at: iso(0),
  days: 14,
  tz_offset_minutes: 0,
  first_day: '2026-09-12',
  total_handoffs: 3,
  total_checkpoints: 1,
  week: { handoffs: 3, checkpoints: 1, agents: ['claude-code', 'codex', 'cursor'], projects: ['invoices-api', 'landing-site'] },
  agents: [
    { agent_id: 'claude-code', projects: ['invoices-api'], ...bucket(0.5) },
    { agent_id: 'codex', projects: ['invoices-api'], ...bucket(3) },
    { agent_id: 'cursor', projects: ['landing-site'], ...bucket(20) },
  ],
  projects: [
    { project_id: 'invoices-api', agents: ['claude-code', 'codex'], ...bucket(0.5) },
    { project_id: 'landing-site', agents: ['cursor'], ...bucket(20) },
  ],
};

function item(id: string, agent: string, project: string, hoursAgo: number, type = 'handoff', failing = 0): TrailItem {
  return {
    id,
    project_id: project,
    memory_type: type,
    agent_id: agent,
    session_id: `s-${id}`,
    created_at: iso(hoursAgo),
    branch: 'main',
    head_commit: null,
    headline: `${agent} did ${id}`,
    failing,
    open: 0,
  };
}

const trail: TrailItem[] = [
  item('h1', 'claude-code', 'invoices-api', 0.5, 'handoff', 1),
  item('c1', 'claude-code', 'invoices-api', 1.4, 'checkpoint'),
  item('h2', 'codex', 'invoices-api', 3),
  item('h3', 'cursor', 'landing-site', 20),
  item('h4', 'codex', 'invoices-api', 200),
];

const inbox: InboxMessage[] = [
  {
    inbox_id: 'i1',
    from_agent: 'claude-code',
    to_agent: 'dashboard',
    subject: 'Staging needs PADDLE_API_KEY',
    body: '',
    metadata: {},
    status: 'unread',
    created_at: iso(2),
    ack_at: null,
    ack_note: null,
    ack_result: null,
    expires_at: null,
  },
];

const entities: EntityGraphDataResponse = {
  nodes: [
    { id: 'e1', label: 'GCT', type: 'concept', confidence: 0.9, memory_count: 9, community_id: null, project_id: 'invoices-api' },
    { id: 'e2', label: 'half-even rounding', type: 'concept', confidence: 0.9, memory_count: 2, community_id: null, project_id: 'invoices-api' },
    { id: 'e3', label: 'dither canvas', type: 'technology', confidence: 0.9, memory_count: 1, community_id: null, project_id: 'landing-site' },
  ],
  edges: [{ id: 'r1', source: 'e1', target: 'e2', type: 'requires', confidence: 0.8 }],
  stats: {},
};

const all = { project: null, agent: null, sinceMs: null };

describe('buildGraph', () => {
  it('turns agents, projects, trail entries, messages and entities into nodes and edges', () => {
    const g = buildGraph({ summary, trail, inbox, entities }, all);
    const kinds = (k: string) => g.nodes.filter((n) => n.kind === k).map((n) => n.id).sort();
    expect(kinds('agent')).toEqual(['a:claude-code', 'a:codex', 'a:cursor', 'a:dashboard']);
    expect(kinds('project')).toEqual(['p:invoices-api', 'p:landing-site']);
    expect(kinds('handoff')).toHaveLength(4);
    expect(kinds('checkpoint')).toEqual(['m:c1']);
    expect(kinds('entity')).toEqual(['e:e1', 'e:e2', 'e:e3']);
    expect(g.nodes.find((n) => n.id === 'e:e1')?.weight).toBe(3);
    expect(g.edges.some((e) => e.kind === 'inbox' && e.source === 'a:claude-code' && e.target === 'a:dashboard')).toBe(true);
    expect(g.edges.some((e) => e.kind === 'link')).toBe(true);
    // every edge points at real nodes
    const ids = new Set(g.nodes.map((n) => n.id));
    for (const e of g.edges) expect(ids.has(e.source) && ids.has(e.target)).toBe(true);
  });

  it('filters by project: only its agents, trail and entities', () => {
    const g = buildGraph({ summary, trail, inbox, entities }, { ...all, project: 'landing-site' });
    expect(g.nodes.map((n) => n.id).sort()).toEqual(['a:cursor', 'e:e3', 'm:h3', 'p:landing-site']);
  });

  it('filters by agent, keeping its projects and messages', () => {
    const g = buildGraph({ summary, trail, inbox, entities }, { ...all, agent: 'claude-code' });
    const ids = new Set(g.nodes.map((n) => n.id));
    expect(ids.has('a:claude-code')).toBe(true);
    expect(ids.has('p:invoices-api')).toBe(true);
    expect(ids.has('a:dashboard')).toBe(true);
    expect(ids.has('m:h2')).toBe(false);
    expect(ids.has('a:cursor')).toBe(false);
  });

  it('filters by time window', () => {
    const g = buildGraph({ summary, trail, inbox, entities }, { ...all, sinceMs: NOW - 24 * 3600e3 });
    const ids = new Set(g.nodes.map((n) => n.id));
    expect(ids.has('m:h4')).toBe(false);
    expect(ids.has('m:h3')).toBe(true);
  });

  it('keeps a recent note from you in the default 7d window, even with no trail behind it', () => {
    const note: InboxMessage = { ...inbox[0], inbox_id: 'i2', from_agent: 'dashboard', to_agent: 'codex', subject: 'Ship it', created_at: new Date(NOW - 60e3).toISOString() };
    // An agent that only ever sends notes, no trail entry or summary row.
    const lone: InboxMessage = { ...inbox[0], inbox_id: 'i3', from_agent: 'kimi', to_agent: 'claude-code', subject: 'ping', created_at: iso(5) };
    const old: InboxMessage = { ...inbox[0], inbox_id: 'i4', from_agent: 'gemini', to_agent: 'codex', subject: 'old', created_at: iso(24 * 30) };
    const g = buildGraph({ summary, trail, inbox: [...inbox, note, lone, old], entities }, { ...all, sinceMs: NOW - 7 * 24 * 3600e3 });
    const ids = new Set(g.nodes.map((n) => n.id));
    expect(ids.has('a:dashboard')).toBe(true);
    expect(ids.has('a:kimi')).toBe(true);
    expect(g.edges.some((e) => e.kind === 'inbox' && e.source === 'a:dashboard' && e.target === 'a:codex')).toBe(true);
    expect(g.edges.some((e) => e.kind === 'inbox' && e.source === 'a:kimi' && e.target === 'a:claude-code')).toBe(true);
    // Outside the window: still filtered.
    expect(ids.has('a:gemini')).toBe(false);
  });

  it('with a project filter, keeps notes that touch an agent in play there', () => {
    const toCursor: InboxMessage = { ...inbox[0], inbox_id: 'i5', from_agent: 'dashboard', to_agent: 'cursor', subject: 'hi', created_at: iso(1) };
    const g = buildGraph({ summary, trail, inbox: [...inbox, toCursor], entities }, { ...all, project: 'landing-site', sinceMs: NOW - 7 * 24 * 3600e3 });
    const ids = new Set(g.nodes.map((n) => n.id));
    expect(ids.has('a:dashboard')).toBe(true);
    expect(ids.has('a:claude-code')).toBe(false);
    expect(g.edges.some((e) => e.kind === 'inbox' && e.source === 'a:dashboard' && e.target === 'a:cursor')).toBe(true);
  });

  it('copes with empty and partial data', () => {
    expect(buildGraph({}, all)).toEqual({ nodes: [], edges: [] });
    const g = buildGraph({ trail: [item('x', 'kimi', '', 1)] }, all);
    expect(g.nodes.map((n) => n.id).sort()).toEqual(['a:kimi', 'm:x', 'p:default']);
  });
});

describe('events', () => {
  it('a handoff travels agent -> handoff -> project -> the next agent, and says who already knows', () => {
    const e = trailEvent(trail[0], summary)!;
    expect(e.path).toEqual(['a:claude-code', 'm:h1', 'p:invoices-api', 'a:codex']);
    expect(e.text).toBe('claude-code stopped · handoff signed · 1 failing → codex already knows');
  });

  it('falls back to "the next agent" when nobody else works the project', () => {
    const e = trailEvent(trail[3], summary)!;
    expect(e.text).toBe('cursor stopped · handoff signed → the next agent already knows');
    expect(nextAgentIn(summary, 'landing-site', 'cursor')).toBeNull();
  });

  it('describes checkpoints and messages', () => {
    expect(trailEvent(trail[1], summary)?.text).toBe('claude-code checkpointed invoices-api');
    const m = messageEvent(inbox[0]);
    expect(m.path).toEqual(['a:claude-code', 'a:dashboard']);
    expect(m.text).toBe('claude-code → you: Staging needs PADDLE_API_KEY');
  });

  it('decides what plays by project; notes always play', () => {
    expect(eventInView(trailEvent(trail[3], summary)!, 'landing-site')).toBe(true);
    expect(eventInView(trailEvent(trail[0], summary)!, 'landing-site')).toBe(false);
    expect(eventInView(messageEvent(inbox[0]), 'landing-site')).toBe(true);
    expect(eventInView(trailEvent(trail[0], summary)!, null)).toBe(true);
  });

  it('switching the project filter finds nothing new when seen covers every loaded event', () => {
    const events = [...trail.map((t) => trailEvent(t, summary)!), messageEvent(inbox[0])];
    // First load happened under ?project=landing-site; seen still holds everything.
    const seen = new Set(events.map((e) => e.key));
    expect(newEvents(seen, events)).toEqual([]);
    // A genuinely new handoff is still found, and plays only if it is in view.
    const fresh = trailEvent(item('h9', 'codex', 'invoices-api', 0), summary)!;
    const found = newEvents(seen, [...events, fresh]);
    expect(found.map((e) => e.key)).toEqual(['t:h9']);
    expect(eventInView(found[0], 'landing-site')).toBe(false);
    expect(eventInView(found[0], null)).toBe(true);
  });

  it('reports only unseen events, oldest first', () => {
    const events = trail.map((t) => trailEvent(t, summary)!);
    const seen = new Set(events.slice(2).map((e) => e.key));
    expect(newEvents(seen, events).map((e) => e.key)).toEqual(['t:c1', 't:h1']);
  });
});

describe('ForceLayout', () => {
  it('settles 2,000 nodes quickly and keeps them finite', () => {
    const nodes = [];
    const edges = [];
    for (let p = 0; p < 8; p += 1) nodes.push({ id: `p:P${p}`, kind: 'project' as const, label: `P${p}`, ref: `P${p}`, project: `P${p}`, agent: null, weight: 3, lastAt: null });
    for (let a = 0; a < 10; a += 1) {
      nodes.push({ id: `a:A${a}`, kind: 'agent' as const, label: `A${a}`, ref: `A${a}`, project: null, agent: `A${a}`, weight: 2, lastAt: null });
      edges.push({ id: `w${a}`, source: `a:A${a}`, target: `p:P${a % 8}`, kind: 'works' as const, lastAt: null });
    }
    for (let i = 0; i < 1982; i += 1) {
      nodes.push({ id: `e:E${i}`, kind: 'entity' as const, label: `E${i}`, ref: `E${i}`, project: `P${i % 8}`, agent: null, weight: 1, lastAt: null });
      if (i > 8) edges.push({ id: `l${i}`, source: `e:E${i}`, target: `e:E${i - 8}`, kind: 'link' as const, lastAt: null });
    }
    const layout = new ForceLayout();
    layout.setGraph(nodes, edges);
    const t0 = performance.now();
    let ticks = 0;
    while (!layout.settled && ticks < 600) {
      layout.tick();
      ticks += 1;
    }
    const perTick = (performance.now() - t0) / ticks;
    expect(layout.settled).toBe(true);
    expect(perTick).toBeLessThan(12);
    for (const n of layout.nodes) expect(Number.isFinite(n.x) && Number.isFinite(n.y)).toBe(true);
    // projects keep distinct places on the ring
    const p0 = layout.get('p:P0')!;
    const p4 = layout.get('p:P4')!;
    expect(Math.hypot(p0.x - p4.x, p0.y - p4.y)).toBeGreaterThan(400);
  });

  it('does not reheat when a poll brings the same graph, and does when it changes', () => {
    const g = buildGraph({ summary, trail, inbox, entities }, all);
    const layout = new ForceLayout();
    layout.setGraph(g.nodes, g.edges);
    layout.settle(2000);
    expect(layout.settled).toBe(true);
    // Same content, new arrays (what every poll produces).
    const again = buildGraph({ summary, trail: trail.map((t) => ({ ...t })), inbox, entities }, all);
    layout.setGraph(again.nodes, again.edges);
    expect(layout.settled).toBe(true);
    // settle() is then a no-op: nothing moves.
    const before = layout.nodes.map((n) => [n.x, n.y]);
    layout.settle();
    expect(layout.nodes.map((n) => [n.x, n.y])).toEqual(before);
    // A new handoff: structure changed, so the layout warms up to place it.
    const more = buildGraph({ summary, trail: [...trail, item('h9', 'codex', 'invoices-api', 0)], inbox, entities }, all);
    layout.setGraph(more.nodes, more.edges);
    expect(layout.settled).toBe(false);
    layout.settle(2000);
    // A node leaving also warms it.
    layout.setGraph(g.nodes, g.edges);
    expect(layout.settled).toBe(false);
  });

  it('keeps positions when data refreshes', () => {
    const layout = new ForceLayout();
    const n = [{ id: 'p:A', kind: 'project' as const, label: 'A', ref: 'A', project: 'A', agent: null, weight: 3, lastAt: null }];
    layout.setGraph(n, []);
    layout.settle();
    const before = { ...layout.get('p:A')! };
    layout.setGraph(n, []);
    expect(layout.get('p:A')!.x).toBe(before.x);
    expect(layout.get('p:A')!.y).toBe(before.y);
  });
});

describe('graphSignature', () => {
  it('is equal for an identical rebuild and differs when anything drawn changes', () => {
    const a = buildGraph({ summary, trail, inbox, entities }, all);
    const b = buildGraph({ summary, trail: trail.map((t) => ({ ...t })), inbox: inbox.map((m) => ({ ...m })), entities }, all);
    expect(graphSignature(a)).toBe(graphSignature(b));
    const failing = buildGraph({ summary, trail: trail.map((t) => (t.id === 'h2' ? { ...t, failing: 3 } : t)), inbox, entities }, all);
    expect(graphSignature(failing)).not.toBe(graphSignature(a));
    const windowed = buildGraph({ summary, trail, inbox, entities }, { ...all, sinceMs: NOW - 24 * 3600e3 });
    expect(graphSignature(windowed)).not.toBe(graphSignature(a));
  });
});
