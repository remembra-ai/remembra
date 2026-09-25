// Constellation graph model: agents, projects, trail entries (handoffs and
// checkpoints) and resolved entities as one graph, built from the relay APIs
// (trail, activity summary, inbox) and the entity graph. Pure functions, so
// the canvas engine and the tests share them.

import type { EntityGraphDataResponse } from '../lib/api';
import type { ActivitySummary, InboxMessage, TrailItem } from '../lib/relay';
import { DASHBOARD_SENDER } from '../lib/relay';
import { agentMeta, canonicalAgentId } from '../lib/agents';
import { parseServerTime } from '../lib/time';

export type NodeKind = 'agent' | 'project' | 'entity' | 'handoff' | 'checkpoint';
export type EdgeKind = 'works' | 'trail' | 'link' | 'inbox';

export interface GNode {
  /** Namespaced id: a:<agent>, p:<project>, e:<entity>, m:<memory>. */
  id: string;
  kind: NodeKind;
  label: string;
  /** Raw id from the API (agent id, project id, entity id, memory id). */
  ref: string;
  project: string | null;
  agent: string | null;
  /** 1..3: drives the sprite size. */
  weight: number;
  /** Last activity (ms since epoch), when known. */
  lastAt: number | null;
  /** Entity type, trail headline, etc. */
  detail?: string;
  /** Lane colour for agents. */
  color?: string;
  failing?: number;
  open?: number;
}

export interface GEdge {
  id: string;
  source: string;
  target: string;
  kind: EdgeKind;
  lastAt: number | null;
  label?: string;
}

export interface GraphData {
  nodes: GNode[];
  edges: GEdge[];
}

export interface GraphInput {
  summary?: ActivitySummary | null;
  trail?: TrailItem[] | null;
  inbox?: InboxMessage[] | null;
  entities?: EntityGraphDataResponse | null;
}

export interface GraphFilter {
  project: string | null;
  agent: string | null;
  /** Only activity at or after this time (ms); null = everything loaded. */
  sinceMs: number | null;
}

export const agentNodeId = (agent: string) => `a:${canonicalAgentId(agent) || agent}`;
export const projectNodeId = (project: string) => `p:${project}`;
export const entityNodeId = (entity: string) => `e:${entity}`;
export const memoryNodeId = (memory: string) => `m:${memory}`;

function ms(value: string | null | undefined): number | null {
  const d = parseServerTime(value ?? null);
  return d ? d.getTime() : null;
}

function projectOf(value: string | null | undefined): string {
  return value && value.trim() ? value : 'default';
}

function newer(a: number | null, b: number | null): number | null {
  if (a === null) return b;
  if (b === null) return a;
  return Math.max(a, b);
}

/** Build the whole graph, then apply the filter. */
export function buildGraph(input: GraphInput, filter: GraphFilter): GraphData {
  const nodes = new Map<string, GNode>();
  const edges = new Map<string, GEdge>();

  const addAgent = (agent: string, lastAt: number | null) => {
    const id = agentNodeId(agent);
    const existing = nodes.get(id);
    if (existing) {
      existing.lastAt = newer(existing.lastAt, lastAt);
      return id;
    }
    const meta = agentMeta(agent);
    nodes.set(id, {
      id,
      kind: 'agent',
      label: agent === DASHBOARD_SENDER ? 'you' : meta.id ? canonicalAgentId(agent) : agent,
      ref: agent,
      project: null,
      agent,
      weight: 2,
      lastAt,
      detail: meta.name,
      color: meta.lane,
    });
    return id;
  };
  const addProject = (project: string, lastAt: number | null) => {
    const id = projectNodeId(project);
    const existing = nodes.get(id);
    if (existing) {
      existing.lastAt = newer(existing.lastAt, lastAt);
      return id;
    }
    nodes.set(id, { id, kind: 'project', label: project, ref: project, project, agent: null, weight: 3, lastAt });
    return id;
  };
  const addEdge = (source: string, target: string, kind: EdgeKind, lastAt: number | null, label?: string) => {
    const [a, b] = kind === 'inbox' ? [source, target] : [source, target].sort();
    const id = `${kind}:${a}|${b}`;
    const existing = edges.get(id);
    if (existing) {
      existing.lastAt = newer(existing.lastAt, lastAt);
      return;
    }
    edges.set(id, { id, source, target, kind, lastAt, label });
  };

  for (const agent of input.summary?.agents ?? []) {
    const aid = addAgent(agent.agent_id, ms(agent.last_active));
    for (const project of agent.projects) addEdge(aid, addProject(projectOf(project), null), 'works', ms(agent.last_active));
  }
  for (const project of input.summary?.projects ?? []) addProject(projectOf(project.project_id), ms(project.last_active));

  for (const item of input.trail ?? []) {
    if (item.memory_type !== 'handoff' && item.memory_type !== 'checkpoint') continue;
    const at = ms(item.created_at);
    const project = projectOf(item.project_id);
    const pid = addProject(project, at);
    const mid = memoryNodeId(item.id);
    nodes.set(mid, {
      id: mid,
      kind: item.memory_type === 'handoff' ? 'handoff' : 'checkpoint',
      label: item.headline || item.memory_type,
      ref: item.id,
      project,
      agent: item.agent_id,
      weight: 1,
      lastAt: at,
      detail: item.branch ?? undefined,
      failing: item.failing,
      open: item.open,
    });
    addEdge(mid, pid, 'trail', at);
    if (item.agent_id) {
      const aid = addAgent(item.agent_id, at);
      addEdge(aid, mid, 'trail', at);
      addEdge(aid, pid, 'works', at);
    }
  }

  for (const message of input.inbox ?? []) {
    const at = ms(message.created_at);
    const from = addAgent(message.from_agent, at);
    const to = addAgent(message.to_agent, at);
    if (from !== to) addEdge(from, to, 'inbox', at, message.subject);
  }

  for (const entity of input.entities?.nodes ?? []) {
    const project = entity.project_id ? projectOf(entity.project_id) : null;
    if (project) addProject(project, null);
    const id = entityNodeId(entity.id);
    nodes.set(id, {
      id,
      kind: 'entity',
      label: entity.label,
      ref: entity.id,
      project,
      agent: null,
      weight: entity.memory_count >= 8 ? 3 : entity.memory_count >= 3 ? 2 : 1,
      lastAt: null,
      detail: entity.type,
    });
  }
  for (const edge of input.entities?.edges ?? []) {
    const s = entityNodeId(edge.source);
    const t = entityNodeId(edge.target);
    if (nodes.has(s) && nodes.has(t) && s !== t) addEdge(s, t, 'link', null, edge.type);
  }

  return applyFilter({ nodes: [...nodes.values()], edges: [...edges.values()] }, filter);
}

export function applyFilter(graph: GraphData, filter: GraphFilter): GraphData {
  const { project, agent, sinceMs } = filter;
  if (!project && !agent && sinceMs === null) return graph;
  const byId = new Map(graph.nodes.map((n) => [n.id, n]));
  const keep = new Set<string>();
  const agentId = agent ? agentNodeId(agent) : null;

  const recent = (n: GNode) => sinceMs === null || (n.lastAt !== null && n.lastAt >= sinceMs);

  // 1. Trail entries decide which agents and projects are in play.
  for (const n of graph.nodes) {
    if (n.kind !== 'handoff' && n.kind !== 'checkpoint') continue;
    if (project && n.project !== project) continue;
    if (agentId && (!n.agent || agentNodeId(n.agent) !== agentId)) continue;
    if (!recent(n)) continue;
    keep.add(n.id);
    keep.add(projectNodeId(n.project ?? 'default'));
    if (n.agent) keep.add(agentNodeId(n.agent));
  }
  // 2. Agents and projects linked by "works" (activity summary), within the window.
  for (const e of graph.edges) {
    if (e.kind !== 'works') continue;
    const [a, p] = byId.get(e.source)?.kind === 'agent' ? [e.source, e.target] : [e.target, e.source];
    const pn = byId.get(p);
    if (!pn) continue;
    if (project && pn.ref !== project) continue;
    if (agentId && a !== agentId) continue;
    if (sinceMs !== null && (e.lastAt === null || e.lastAt < sinceMs)) continue;
    keep.add(a);
    keep.add(p);
  }
  if (project && byId.has(projectNodeId(project))) keep.add(projectNodeId(project));
  if (agentId && byId.has(agentId)) keep.add(agentId);
  // 3. Inbox messages between kept agents (or to/from the filtered agent).
  for (const e of graph.edges) {
    if (e.kind !== 'inbox') continue;
    if (sinceMs !== null && (e.lastAt === null || e.lastAt < sinceMs)) continue;
    if (agentId && e.source !== agentId && e.target !== agentId) continue;
    if (!agentId && !(keep.has(e.source) && keep.has(e.target))) continue;
    keep.add(e.source);
    keep.add(e.target);
  }
  // 4. Entities of the kept projects (entities carry no timestamps).
  const keptProjects = new Set([...keep].filter((id) => byId.get(id)?.kind === 'project').map((id) => byId.get(id)!.ref));
  for (const n of graph.nodes) {
    if (n.kind !== 'entity') continue;
    // Servers that predate project_id on graph nodes: keep unplaced entities unless a project is chosen.
    if (project ? n.project === project : n.project === null || keptProjects.has(n.project)) keep.add(n.id);
  }

  const nodes = graph.nodes.filter((n) => keep.has(n.id));
  const edges = graph.edges.filter((e) => keep.has(e.source) && keep.has(e.target));
  return { nodes, edges };
}

// ---------------------------------------------------------------------------
// Events: what travels as an orange packet, and what the status strip says.
// ---------------------------------------------------------------------------

export interface GraphEvent {
  key: string;
  at: number;
  kind: 'handoff' | 'checkpoint' | 'message';
  /** Node ids the packet visits, in order. */
  path: string[];
  /** Status strip text (without the time prefix). */
  text: string;
  /** Arrived while the page was open (vs. replayed from history on load). */
  live?: boolean;
}

function agentLabel(agent: string | null | undefined): string {
  if (!agent) return 'an agent';
  if (agent === DASHBOARD_SENDER) return 'you';
  return canonicalAgentId(agent) || agent;
}

/** The other agent most recently active in a project (the one that "already knows" next). */
export function nextAgentIn(summary: ActivitySummary | null | undefined, project: string, except: string | null): string | null {
  const exceptId = except ? canonicalAgentId(except) : null;
  let best: { id: string; at: number } | null = null;
  for (const agent of summary?.agents ?? []) {
    if (!agent.projects.map(projectOf).includes(project)) continue;
    const id = canonicalAgentId(agent.agent_id);
    if (id === exceptId || agent.agent_id === DASHBOARD_SENDER) continue;
    const at = ms(agent.last_active) ?? 0;
    if (!best || at > best.at) best = { id: agent.agent_id, at };
  }
  return best?.id ?? null;
}

export function trailEvent(item: TrailItem, summary: ActivitySummary | null | undefined): GraphEvent | null {
  if (item.memory_type !== 'handoff' && item.memory_type !== 'checkpoint') return null;
  const at = ms(item.created_at) ?? Date.now();
  const project = projectOf(item.project_id);
  const who = agentLabel(item.agent_id);
  const path = [...(item.agent_id ? [agentNodeId(item.agent_id)] : []), memoryNodeId(item.id), projectNodeId(project)];
  if (item.memory_type === 'checkpoint') {
    return { key: `t:${item.id}`, at, kind: 'checkpoint', path, text: `${who} checkpointed ${project}` };
  }
  const next = nextAgentIn(summary, project, item.agent_id);
  if (next) path.push(agentNodeId(next));
  const failing = item.failing > 0 ? ` · ${item.failing} failing` : '';
  return {
    key: `t:${item.id}`,
    at,
    kind: 'handoff',
    path,
    text: `${who} stopped · handoff signed${failing} → ${next ? agentLabel(next) : 'the next agent'} already knows`,
  };
}

export function messageEvent(message: InboxMessage): GraphEvent {
  const at = ms(message.created_at) ?? Date.now();
  const subject = message.subject.length > 48 ? `${message.subject.slice(0, 47)}…` : message.subject;
  return {
    key: `i:${message.inbox_id}`,
    at,
    kind: 'message',
    path: [agentNodeId(message.from_agent), agentNodeId(message.to_agent)],
    text: `${agentLabel(message.from_agent)} → ${agentLabel(message.to_agent)}: ${subject}`,
  };
}

/** Events in `next` that were not in `seen`, oldest first. */
export function newEvents(seen: Set<string>, next: GraphEvent[]): GraphEvent[] {
  return next.filter((e) => !seen.has(e.key)).sort((a, b) => a.at - b.at);
}
