// A small force layout sized for a few thousand nodes: springs on edges,
// short-range repulsion through a spatial hash (O(n) per tick), and anchors
// that keep projects on a ring with their entities and trail entries around
// them. Positions persist across data refreshes so the picture never jumps.

import type { GEdge, GNode, NodeKind } from './model';

export interface LNode {
  id: string;
  kind: NodeKind;
  x: number;
  y: number;
  vx: number;
  vy: number;
  /** Anchor the node is pulled toward (its project's position), if any. */
  anchor: string | null;
  pinned: boolean;
}

const REST: Record<GEdge['kind'], number> = { works: 190, trail: 70, link: 46, inbox: 230 };
const STIFF: Record<GEdge['kind'], number> = { works: 0.012, trail: 0.05, link: 0.04, inbox: 0.004 };
const RADIUS: Record<NodeKind, number> = { project: 46, agent: 40, entity: 16, handoff: 14, checkpoint: 12 };

function hashSeed(id: string): number {
  let h = 2166136261;
  for (let i = 0; i < id.length; i += 1) h = Math.imul(h ^ id.charCodeAt(i), 16777619);
  return ((h >>> 0) % 100000) / 100000;
}

export class ForceLayout {
  nodes: LNode[] = [];
  private byId = new Map<string, LNode>();
  private springs: { a: LNode; b: LNode; rest: number; k: number }[] = [];
  private springKeys = new Set<string>();
  private ring = new Map<string, { x: number; y: number }>();
  alpha = 1;
  /** Canvas width / height: the project ring is an ellipse that matches it. */
  private aspect = 1.6;
  private lastNodes: GNode[] = [];
  private lastEdges: GEdge[] = [];

  setAspect(aspect: number): void {
    if (!Number.isFinite(aspect) || aspect <= 0 || Math.abs(aspect - this.aspect) / this.aspect < 0.15) return;
    this.aspect = aspect;
    if (this.lastNodes.length) this.setGraph(this.lastNodes, this.lastEdges);
    this.alpha = Math.max(this.alpha, 0.3);
  }

  setGraph(nodes: GNode[], edges: GEdge[]): void {
    this.lastNodes = nodes;
    this.lastEdges = edges;
    const previous = this.byId;
    const projects = nodes.filter((n) => n.kind === 'project').sort((a, b) => a.id.localeCompare(b.id));
    // Each project needs room for its members (entities and trail entries);
    // the ring grows so neighbouring clusters do not overlap.
    const members = new Map<string, number>();
    for (const n of nodes) if (n.project && n.kind !== 'project') members.set(n.project, (members.get(n.project) ?? 0) + 1);
    const clusterR = Math.max(0, ...[...members.values()].map((m) => 60 + Math.sqrt(m) * 18));
    const ringR = projects.length <= 1 ? 0 : Math.max(150 + projects.length * 55, (projects.length * clusterR * 1.15) / Math.PI);
    this.ring = new Map(
      projects.map((p, i) => {
        // An ellipse shaped like the canvas, starting on the left.
        const a = (i / Math.max(1, projects.length)) * Math.PI * 2 + Math.PI;
        const stretch = Math.min(1.4, Math.max(0.72, Math.sqrt(this.aspect)));
        return [p.id, { x: Math.cos(a) * ringR * stretch, y: Math.sin(a) * (ringR / stretch) }];
      }),
    );
    const next = new Map<string, LNode>();
    let fresh = 0;
    let moved = 0;
    for (const n of nodes) {
      const anchor = n.kind === 'project' || n.kind === 'agent' ? null : n.project ? `p:${n.project}` : null;
      const old = previous.get(n.id);
      if (old) {
        if (old.anchor !== anchor) moved += 1;
        old.anchor = anchor;
        next.set(n.id, old);
        continue;
      }
      fresh += 1;
      const seed = hashSeed(n.id);
      const base = n.kind === 'project' ? this.ring.get(n.id) : anchor ? (previous.get(anchor) ?? this.ring.get(anchor)) : null;
      const spread = n.kind === 'project' ? 0 : n.kind === 'agent' ? ringR * 0.55 + 120 : 90;
      const angle = seed * Math.PI * 2;
      const node: LNode = {
        id: n.id,
        kind: n.kind,
        x: (base?.x ?? 0) + Math.cos(angle) * spread * (0.6 + seed * 0.4),
        y: (base?.y ?? 0) + Math.sin(angle) * spread * (0.6 + seed * 0.4),
        vx: 0,
        vy: 0,
        anchor,
        pinned: false,
      };
      next.set(n.id, node);
    }
    const removed = previous.size - (next.size - fresh);
    this.byId = next;
    this.nodes = [...next.values()];
    this.springs = [];
    const springKeys = new Set<string>();
    for (const e of edges) {
      const a = next.get(e.source);
      const b = next.get(e.target);
      if (!a || !b) continue;
      this.springs.push({ a, b, rest: REST[e.kind], k: STIFF[e.kind] });
      springKeys.add(`${e.kind}|${a.id}|${b.id}`);
    }
    let springsChanged = springKeys.size !== this.springKeys.size;
    if (!springsChanged) for (const k of springKeys) if (!this.springKeys.has(k)) springsChanged = true;
    this.springKeys = springKeys;
    // Reheat: fully for a new picture, gently when the structure changed
    // (nodes or springs came or went, or a node changed cluster). A poll that
    // brings the same graph leaves the layout at rest, so it costs nothing.
    if (previous.size === 0) this.alpha = nodes.length ? 1 : this.alpha;
    else if (fresh > 0 || removed > 0 || moved > 0 || springsChanged) {
      this.alpha = Math.max(this.alpha, Math.min(0.6, 0.12 + (fresh + removed + moved) / Math.max(10, this.nodes.length)));
    }
  }

  get(id: string): LNode | undefined {
    return this.byId.get(id);
  }

  get settled(): boolean {
    return this.alpha < 0.004;
  }

  tick(): void {
    if (this.settled) return;
    const alpha = this.alpha;
    const nodes = this.nodes;

    for (const s of this.springs) {
      const dx = s.b.x - s.a.x;
      const dy = s.b.y - s.a.y;
      const d = Math.hypot(dx, dy) || 0.01;
      const f = ((d - s.rest) * s.k * alpha) / d;
      s.a.vx += dx * f;
      s.a.vy += dy * f;
      s.b.vx -= dx * f;
      s.b.vy -= dy * f;
    }

    // Anchors: projects hold their ring slot; members stay near their project.
    for (const n of nodes) {
      if (n.kind === 'project') {
        // Projects are the fixed stars: they glide to their ring slot and stay.
        const slot = this.ring.get(n.id);
        if (slot) {
          n.x += (slot.x - n.x) * 0.2;
          n.y += (slot.y - n.y) * 0.2;
          n.vx = 0;
          n.vy = 0;
        }
      } else if (n.anchor) {
        const p = this.byId.get(n.anchor);
        if (p) {
          const dx = p.x - n.x;
          const dy = p.y - n.y;
          const d = Math.hypot(dx, dy) || 0.01;
          const f = ((d - (n.kind === 'entity' ? 110 : 70)) * 0.02 * alpha) / d;
          n.vx += dx * f;
          n.vy += dy * f;
        }
      } else {
        n.vx -= n.x * 0.0015 * alpha;
        n.vy -= n.y * 0.0015 * alpha;
      }
    }

    // Short-range repulsion through a uniform grid.
    const cell = 64;
    const grid = new Map<number, LNode[]>();
    const key = (i: number, j: number) => ((i + 32768) << 16) | (j + 32768);
    for (const n of nodes) {
      const k = key(Math.floor(n.x / cell), Math.floor(n.y / cell));
      const bucket = grid.get(k);
      if (bucket) bucket.push(n);
      else grid.set(k, [n]);
    }
    for (const n of nodes) {
      const ci = Math.floor(n.x / cell);
      const cj = Math.floor(n.y / cell);
      const rn = RADIUS[n.kind];
      for (let di = -1; di <= 1; di += 1) {
        for (let dj = -1; dj <= 1; dj += 1) {
          const bucket = grid.get(key(ci + di, cj + dj));
          if (!bucket) continue;
          for (const m of bucket) {
            if (m === n) continue;
            let dx = n.x - m.x;
            let dy = n.y - m.y;
            let d2 = dx * dx + dy * dy;
            if (d2 === 0) {
              dx = hashSeed(n.id) - 0.5;
              dy = hashSeed(m.id) - 0.5;
              d2 = dx * dx + dy * dy;
            }
            const min = rn + RADIUS[m.kind];
            const reach = Math.max(min * 1.6, cell);
            if (d2 > reach * reach) continue;
            const d = Math.sqrt(d2);
            const push = (d < min ? (min - d) * 0.5 : (reach - d) * 0.02) * alpha;
            n.vx += (dx / d) * push;
            n.vy += (dy / d) * push;
          }
        }
      }
    }

    for (const n of nodes) {
      if (n.pinned || n.kind === 'project') {
        n.vx = 0;
        n.vy = 0;
        continue;
      }
      n.vx *= 0.62;
      n.vy *= 0.62;
      const v = Math.hypot(n.vx, n.vy);
      if (v > 40) {
        n.vx = (n.vx / v) * 40;
        n.vy = (n.vy / v) * 40;
      }
      n.x += n.vx;
      n.y += n.vy;
    }
    this.alpha *= 0.985;
  }

  /** Run to rest (reduced motion, or a first paint without animation). */
  settle(maxTicks = 400): void {
    for (let i = 0; i < maxTicks && !this.settled; i += 1) this.tick();
  }

  bounds(): { minX: number; minY: number; maxX: number; maxY: number } | null {
    if (!this.nodes.length) return null;
    let minX = Infinity;
    let minY = Infinity;
    let maxX = -Infinity;
    let maxY = -Infinity;
    for (const n of this.nodes) {
      minX = Math.min(minX, n.x);
      minY = Math.min(minY, n.y);
      maxX = Math.max(maxX, n.x);
      maxY = Math.max(maxY, n.y);
    }
    return { minX, minY, maxX, maxY };
  }
}
