// Constellation canvas engine: pixel-sprite nodes, dashed trail edges, flat
// orange packets that travel along edges when something happens, and a
// drifting dither field that thickens around busy regions. Canvas 2D with
// sprite caching, viewport culling, batched edges and a quality governor
// that sheds work when frames run long.

import { bayer, fbm, onThemeChange, readTones, type PixelTones } from '../brand/pixel';
import { ForceLayout, type LNode } from './layout';
import type { GEdge, GNode, GraphData, GraphEvent } from './model';
import { renderSprite, spriteKey, spriteSize } from './sprites';

export interface EngineCallbacks {
  onSelect: (id: string | null) => void;
  onHover?: (id: string | null) => void;
  /** A packet started travelling (drives the status strip). */
  onEventStart?: (event: GraphEvent) => void;
}

interface Packet {
  event: GraphEvent;
  hops: LNode[];
  hop: number;
  t: number;
  duration: number;
}

interface Burst {
  x: number;
  y: number;
  start: number;
}

const HOUR = 3600e3;
const DAY = 24 * HOUR;

function hexToRgb(hex: string): [number, number, number] {
  const m = /^#?([0-9a-f]{3}|[0-9a-f]{6})$/i.exec(hex.trim());
  if (!m) return [128, 128, 128];
  let h = m[1];
  if (h.length === 3) h = h.split('').map((c) => c + c).join('');
  const n = parseInt(h, 16);
  return [(n >> 16) & 255, (n >> 8) & 255, n & 255];
}

export class ConstellationEngine {
  readonly layout = new ForceLayout();
  private canvas: HTMLCanvasElement;
  private ctx: CanvasRenderingContext2D;
  private cb: EngineCallbacks;
  private tones: PixelTones;
  private nodes = new Map<string, GNode>();
  private edges: GEdge[] = [];
  private neighbors = new Map<string, Set<string>>();
  private W = 1;
  private H = 1;
  private dpr = 1;
  private cam = { x: 0, y: 0, k: 1 };
  private camTarget: { x: number; y: number; k: number } | null = null;
  private fitted = false;
  private sprites = new Map<string, HTMLCanvasElement>();
  private selected: string | null = null;
  private hovered: string | null = null;
  private highlight: Set<string> | null = null;
  private packets: Packet[] = [];
  private queue: GraphEvent[] = [];
  private nextLaunch = 0;
  private litEdges = new Map<string, number>();
  private bursts: Burst[] = [];
  private still = false;
  private raf = 0;
  private visible = true;
  private dirty = true;
  private t0 = performance.now();
  private lastFrame = 0;
  private frameMs = 16;
  private quality = 0;
  private slowFrames = 0;
  private fastFrames = 0;
  // dither field
  private field: HTMLCanvasElement = document.createElement('canvas');
  private fieldCtx: CanvasRenderingContext2D | null = this.field.getContext('2d');
  private fieldImage: ImageData | null = null;
  private fieldAt = -1;
  private fieldCell = 6;
  private heat: Float32Array = new Float32Array(0);
  private warmHeat: Float32Array = new Float32Array(0);
  private userMoved = false;
  private rgb = { hi: [0, 0, 0], lo: [0, 0, 0], warm: [0, 0, 0], sig: [0, 0, 0] } as Record<'hi' | 'lo' | 'warm' | 'sig', number[]>;
  private cleanup: (() => void)[] = [];
  private pointers = new Map<number, { x: number; y: number }>();
  private drag: { x: number; y: number; camX: number; camY: number; moved: boolean; pinch?: { d: number; k: number } } | null = null;
  private now = Date.now();

  constructor(canvas: HTMLCanvasElement, callbacks: EngineCallbacks) {
    const ctx = canvas.getContext('2d');
    if (!ctx) throw new Error('Canvas 2D is not available in this browser.');
    this.canvas = canvas;
    this.ctx = ctx;
    this.cb = callbacks;
    this.tones = readTones();
    this.readRgb();
    this.resize();
    this.bindInput();
    const ro = new ResizeObserver(() => {
      this.resize();
      this.requestDraw();
    });
    ro.observe(canvas);
    const io = new IntersectionObserver((entries) => {
      this.visible = entries[0]?.isIntersecting ?? true;
      this.kick();
    });
    io.observe(canvas);
    const onVis = () => {
      this.visible = !document.hidden;
      this.kick();
    };
    document.addEventListener('visibilitychange', onVis);
    const stopTheme = onThemeChange(() => this.retheme());
    this.cleanup.push(() => ro.disconnect(), () => io.disconnect(), () => document.removeEventListener('visibilitychange', onVis), stopTheme);
  }

  destroy(): void {
    if (this.raf) cancelAnimationFrame(this.raf);
    this.raf = 0;
    for (const fn of this.cleanup) fn();
    this.cleanup = [];
  }

  // ---------------------------------------------------------------- data

  setData(graph: GraphData): void {
    this.nodes = new Map(graph.nodes.map((n) => [n.id, n]));
    this.edges = graph.edges;
    this.neighbors = new Map();
    for (const e of graph.edges) {
      if (!this.neighbors.has(e.source)) this.neighbors.set(e.source, new Set());
      if (!this.neighbors.has(e.target)) this.neighbors.set(e.target, new Set());
      this.neighbors.get(e.source)!.add(e.target);
      this.neighbors.get(e.target)!.add(e.source);
    }
    this.layout.setGraph(graph.nodes, graph.edges);
    if (this.selected && !this.nodes.has(this.selected)) this.selected = null;
    if (this.still) {
      this.layout.settle();
      if (!this.fitted) this.fit(false);
    }
    this.requestDraw();
    this.kick();
  }

  setSelected(id: string | null): void {
    this.selected = id;
    this.requestDraw();
  }

  setHighlight(ids: Set<string> | null): void {
    this.highlight = ids;
    this.requestDraw();
  }

  setStill(still: boolean): void {
    this.still = still;
    if (still) {
      this.packets = [];
      this.queue = [];
      this.layout.settle();
      if (!this.fitted) this.fit(false);
    }
    this.requestDraw();
    this.kick();
  }

  /** Queue an event's packet. In still mode the path lights up without travel. */
  play(event: GraphEvent): void {
    if (this.still) {
      const until = performance.now() + 2500;
      for (let i = 0; i + 1 < event.path.length; i += 1) this.litEdges.set(this.edgeKey(event.path[i], event.path[i + 1]), until);
      this.cb.onEventStart?.(event);
      this.requestDraw();
      window.setTimeout(() => this.requestDraw(), 2600);
      return;
    }
    this.queue.push(event);
    this.kick();
  }

  // -------------------------------------------------------------- camera

  zoomBy(factor: number, sx = this.W / 2, sy = this.H / 2): void {
    const k = Math.min(4, Math.max(0.12, this.cam.k * factor));
    const wx = this.cam.x + (sx - this.W / 2) / this.cam.k;
    const wy = this.cam.y + (sy - this.H / 2) / this.cam.k;
    this.cam.k = k;
    this.cam.x = wx - (sx - this.W / 2) / k;
    this.cam.y = wy - (sy - this.H / 2) / k;
    this.userMoved = true;
    this.camTarget = null;
    this.fitted = true;
    this.requestDraw();
  }

  pan(dx: number, dy: number): void {
    this.cam.x -= dx / this.cam.k;
    this.cam.y -= dy / this.cam.k;
    this.userMoved = true;
    this.camTarget = null;
    this.fitted = true;
    this.requestDraw();
  }

  fit(animate = true): void {
    this.userMoved = false;
    const b = this.layout.bounds();
    if (!b) return;
    const pad = 90;
    const k = Math.min(1.6, Math.max(0.12, Math.min(this.W / (b.maxX - b.minX + pad * 2), this.H / (b.maxY - b.minY + pad * 2))));
    const target = { x: (b.minX + b.maxX) / 2, y: (b.minY + b.maxY) / 2, k };
    if (animate && !this.still) this.camTarget = target;
    else this.cam = target;
    this.fitted = true;
    this.requestDraw();
    this.kick();
  }

  focus(id: string): void {
    const n = this.layout.get(id);
    if (!n) return;
    this.userMoved = true;
    const target = { x: n.x, y: n.y, k: Math.max(this.cam.k, 1.1) };
    if (this.still) this.cam = target;
    else this.camTarget = target;
    this.requestDraw();
    this.kick();
  }

  stats(): { nodes: number; edges: number; frameMs: number; quality: number } {
    return { nodes: this.nodes.size, edges: this.edges.length, frameMs: Math.round(this.frameMs * 10) / 10, quality: this.quality };
  }

  // ---------------------------------------------------------------- loop

  private requestDraw(): void {
    this.dirty = true;
    if (this.still || !this.raf) this.kick(true);
  }

  private kick(once = false): void {
    if (this.raf || !this.visible) return;
    this.raf = requestAnimationFrame((t) => this.loop(t, once));
  }

  private lastDrawAt = 0;

  private loop(time: number, once: boolean): void {
    this.raf = 0;
    const busyNow = !this.still && (!this.layout.settled || this.packets.length > 0 || this.queue.length > 0 || this.camTarget !== null || this.bursts.length > 0);
    // Only the dither drift is moving: 20 fps is plenty and saves the battery.
    if (!once && !busyNow && !this.dirty && time - this.lastDrawAt < 50) {
      if (this.visible && !this.still) this.raf = requestAnimationFrame((t) => this.loop(t, false));
      return;
    }
    this.lastDrawAt = time;
    const started = performance.now();
    this.now = Date.now();
    const dt = this.lastFrame ? Math.min(0.1, (time - this.lastFrame) / 1000) : 1 / 60;
    this.lastFrame = time;
    const animating = !this.still;
    if (animating) {
      const ticks = this.layout.nodes.length > 1200 ? 1 : 2;
      for (let i = 0; i < ticks; i += 1) this.layout.tick();
      // Keep the whole picture framed while the layout settles, until the viewer pans or zooms.
      if (!this.userMoved && (!this.fitted || (!this.layout.settled && Math.floor(time / 250) !== Math.floor((time - dt * 1000) / 250)))) {
        this.fit(this.fitted);
      }
      this.stepCamera();
      this.stepPackets(dt, time);
    }
    this.draw(time);
    this.dirty = false;
    const cost = performance.now() - started;
    this.frameMs = this.frameMs * 0.9 + cost * 0.1;
    this.govern(cost);
    const busy = animating && (!this.layout.settled || this.packets.length > 0 || this.queue.length > 0 || this.camTarget !== null || this.bursts.length > 0);
    // The dither field drifts even when nothing else moves (unless still).
    if (!once && animating && this.visible) this.raf = requestAnimationFrame((t) => this.loop(t, false));
    else if (busy && this.visible) this.raf = requestAnimationFrame((t) => this.loop(t, false));
    else this.lastFrame = 0;
  }

  private govern(cost: number): void {
    if (cost > 14) {
      this.slowFrames += 1;
      this.fastFrames = 0;
    } else if (cost < 6) {
      this.fastFrames += 1;
      this.slowFrames = Math.max(0, this.slowFrames - 1);
    }
    if (this.slowFrames > 45 && this.quality < 2) {
      this.quality += 1;
      this.slowFrames = 0;
      this.fieldCell = this.quality === 1 ? 8 : 10;
    } else if (this.fastFrames > 240 && this.quality > 0) {
      this.quality -= 1;
      this.fastFrames = 0;
      this.fieldCell = this.quality === 1 ? 8 : 6;
    }
  }

  private stepCamera(): void {
    if (!this.camTarget) return;
    const c = this.cam;
    const t = this.camTarget;
    c.x += (t.x - c.x) * 0.18;
    c.y += (t.y - c.y) * 0.18;
    c.k += (t.k - c.k) * 0.18;
    if (Math.abs(t.x - c.x) < 0.5 && Math.abs(t.y - c.y) < 0.5 && Math.abs(t.k - c.k) < 0.002) {
      this.cam = { ...t };
      this.camTarget = null;
    }
  }

  private edgeKey(a: string, b: string): string {
    return a < b ? `${a}|${b}` : `${b}|${a}`;
  }

  private stepPackets(dt: number, time: number): void {
    if (this.queue.length && time >= this.nextLaunch) {
      const event = this.queue.shift()!;
      const hops = event.path.map((id) => this.layout.get(id)).filter((n): n is LNode => !!n);
      if (hops.length >= 2) {
        this.packets.push({ event, hops, hop: 0, t: 0, duration: this.hopDuration(hops[0], hops[1]) });
        this.cb.onEventStart?.(event);
      } else if (hops.length === 1) {
        this.bursts.push({ x: hops[0].x, y: hops[0].y, start: time });
        this.cb.onEventStart?.(event);
      }
      this.nextLaunch = time + 700;
    }
    for (const p of this.packets) {
      p.t += dt / p.duration;
      if (p.t >= 1) {
        const a = p.hops[p.hop];
        const b = p.hops[p.hop + 1];
        this.litEdges.set(this.edgeKey(a.id, b.id), time + 2200);
        p.hop += 1;
        p.t = 0;
        if (p.hop + 1 < p.hops.length) p.duration = this.hopDuration(p.hops[p.hop], p.hops[p.hop + 1]);
        else this.bursts.push({ x: b.x, y: b.y, start: time });
      }
    }
    this.packets = this.packets.filter((p) => p.hop + 1 < p.hops.length);
    this.bursts = this.bursts.filter((b) => time - b.start < 1600);
    for (const [key, until] of this.litEdges) if (until < time) this.litEdges.delete(key);
  }

  private hopDuration(a: LNode, b: LNode): number {
    return Math.min(1.3, Math.max(0.35, Math.hypot(b.x - a.x, b.y - a.y) / 420));
  }

  // --------------------------------------------------------------- draw

  private resize(): void {
    const r = this.canvas.getBoundingClientRect();
    this.W = Math.max(1, Math.round(r.width));
    this.H = Math.max(1, Math.round(r.height));
    this.dpr = Math.min(window.devicePixelRatio || 1, 2);
    this.canvas.width = Math.round(this.W * this.dpr);
    this.canvas.height = Math.round(this.H * this.dpr);
    this.fieldAt = -1;
    this.layout.setAspect(this.W / this.H);
    if (!this.userMoved) this.fitted = false;
    if (this.still) {
      this.layout.settle();
      if (!this.userMoved) this.fit(false);
    }
    this.kick();
  }

  private retheme(): void {
    this.tones = readTones();
    this.readRgb();
    this.sprites.clear();
    this.fieldAt = -1;
    this.requestDraw();
  }

  private readRgb(): void {
    this.rgb = { hi: hexToRgb(this.tones.cloudHi), lo: hexToRgb(this.tones.cloudLo), warm: hexToRgb(this.tones.cloudWarm), sig: hexToRgb(this.tones.signal) };
  }

  private toScreen(x: number, y: number): [number, number] {
    return [(x - this.cam.x) * this.cam.k + this.W / 2, (y - this.cam.y) * this.cam.k + this.H / 2];
  }

  private unit(): number {
    // Device pixels per sprite pixel: integer, so sprites stay crisp.
    return Math.max(1, Math.min(6, Math.round(2 * this.dpr * Math.min(2, Math.max(0.75, Math.sqrt(this.cam.k))))));
  }

  private heatOf(n: GNode): number {
    const age = n.lastAt === null ? Infinity : this.now - n.lastAt;
    if (n.kind === 'agent' || n.kind === 'project') return age < HOUR ? 0.85 : age < DAY ? 0.5 : age < 7 * DAY ? 0.22 : 0.06;
    if (n.kind === 'handoff' || n.kind === 'checkpoint') return age < DAY ? 0.3 : 0.07;
    return 0.035 * n.weight;
  }

  private drawField(time: number): void {
    const ctx = this.ctx;
    const cell = this.fieldCell;
    const gw = Math.ceil(this.W / cell);
    const gh = Math.ceil(this.H / cell);
    const interval = this.quality === 0 ? 90 : this.quality === 1 ? 220 : Infinity;
    const stale =
      this.fieldAt < 0 ||
      this.field.width !== gw ||
      this.field.height !== gh ||
      (!this.still && time - this.fieldAt > interval) ||
      (this.dirty && time - this.fieldAt > 30);
    if (stale && this.fieldCtx && !(this.quality === 2 && this.fieldAt >= 0 && !this.dirty)) {
      this.fieldAt = time;
      if (this.field.width !== gw || this.field.height !== gh || !this.fieldImage) {
        this.field.width = gw;
        this.field.height = gh;
        this.fieldImage = this.fieldCtx.createImageData(gw, gh);
        this.heat = new Float32Array(gw * gh);
        this.warmHeat = new Float32Array(gw * gh);
      }
      const heat = this.heat;
      const warmHeat = this.warmHeat;
      heat.fill(0);
      warmHeat.fill(0);
      const k = this.cam.k;
      const stamp = (sx: number, sy: number, amount: number, radiusPx: number, into: Float32Array = heat) => {
        const r = Math.max(1, radiusPx / cell);
        const ci = sx / cell;
        const cj = sy / cell;
        const i0 = Math.max(0, Math.floor(ci - r));
        const i1 = Math.min(gw - 1, Math.ceil(ci + r));
        const j0 = Math.max(0, Math.floor(cj - r));
        const j1 = Math.min(gh - 1, Math.ceil(cj + r));
        const inv = 1 / (r * r);
        for (let j = j0; j <= j1; j += 1) {
          const dy = j + 0.5 - cj;
          for (let i = i0; i <= i1; i += 1) {
            const dx = i + 0.5 - ci;
            const d2 = (dx * dx + dy * dy) * inv;
            if (d2 < 1) into[j * gw + i] += amount * (1 - d2) * (1 - d2);
          }
        }
      };
      const margin = 120;
      for (const ln of this.layout.nodes) {
        const n = this.nodes.get(ln.id);
        if (!n) continue;
        const h = this.heatOf(n);
        if (h < 0.03 && this.quality > 0) continue;
        const [sx, sy] = this.toScreen(ln.x, ln.y);
        if (sx < -margin || sy < -margin || sx > this.W + margin || sy > this.H + margin) continue;
        const radius = (n.kind === 'project' ? 130 : n.kind === 'agent' ? 100 : n.kind === 'entity' ? 34 : 46) * Math.sqrt(k);
        stamp(sx, sy, h, radius);
      }
      for (const b of this.bursts) {
        const [sx, sy] = this.toScreen(b.x, b.y);
        const age = (time - b.start) / 1600;
        stamp(sx, sy, 1.2 * (1 - age), (60 + age * 140) * Math.sqrt(k));
        stamp(sx, sy, 1.4 * (1 - age), (40 + age * 120) * Math.sqrt(k), warmHeat);
      }
      for (const p of this.packets) {
        const a = p.hops[p.hop];
        const b = p.hops[p.hop + 1];
        const [sx, sy] = this.toScreen(a.x + (b.x - a.x) * p.t, a.y + (b.y - a.y) * p.t);
        stamp(sx, sy, 0.6, 40 * Math.sqrt(k));
        stamp(sx, sy, 0.9, 30 * Math.sqrt(k), warmHeat);
      }
      const drift = this.still ? 0 : (time - this.t0) / 1000;
      const data = this.fieldImage!.data;
      const { hi, lo, warm } = this.rgb;
      for (let j = 0; j < gh; j += 1) {
        for (let i = 0; i < gw; i += 1) {
          const wx = this.cam.x + ((i + 0.5) * cell - this.W / 2) / k;
          const wy = this.cam.y + ((j + 0.5) * cell - this.H / 2) / k;
          const h = heat[j * gw + i];
          const base = (fbm(wx / 290 + drift * 0.012, wy / 290 - drift * 0.005) - 0.56) * 1.25;
          const v = base + Math.min(1.1, h);
          const o = (j * gw + i) * 4;
          const b = bayer(i, j);
          let c: number[] | null = null;
          if (v > 0.52 + b * 0.25) c = warmHeat[j * gw + i] > 0.35 + b * 0.5 ? warm : hi;
          else if (v > 0.12 + b * 0.3) c = lo;
          if (c) {
            data[o] = c[0];
            data[o + 1] = c[1];
            data[o + 2] = c[2];
            data[o + 3] = 255;
          } else data[o + 3] = 0;
        }
      }
      this.fieldCtx.putImageData(this.fieldImage!, 0, 0);
    }
    ctx.imageSmoothingEnabled = false;
    ctx.drawImage(this.field, 0, 0, gw * cell * this.dpr, gh * cell * this.dpr);
  }

  private sprite(n: GNode, unit: number, active: boolean): HTMLCanvasElement {
    const key = spriteKey(n.kind, n.weight, active);
    let body = this.tones.ink;
    let lit = this.tones.paper;
    const accent = this.tones.signal;
    if (n.kind === 'agent') {
      body = n.color ?? this.tones.ink3;
      lit = this.tones.cloudHi;
    } else if (n.kind === 'entity') body = this.tones.ink3;
    else if (n.kind === 'handoff') body = active ? this.tones.signal : this.tones.ink;
    else if (n.kind === 'checkpoint') body = this.tones.ink3;
    const cacheKey = `${key}|${unit}|${body}|${lit}`;
    let c = this.sprites.get(cacheKey);
    if (!c) {
      c = renderSprite(key, unit, body, lit, accent);
      this.sprites.set(cacheKey, c);
    }
    return c;
  }

  private isActive(n: GNode): boolean {
    if (n.lastAt === null) return false;
    const age = this.now - n.lastAt;
    return n.kind === 'handoff' ? age < 6 * HOUR : age < DAY;
  }

  private draw(time: number): void {
    const ctx = this.ctx;
    const dpr = this.dpr;
    ctx.setTransform(1, 0, 0, 1, 0, 0);
    ctx.fillStyle = this.tones.paper;
    ctx.fillRect(0, 0, this.canvas.width, this.canvas.height);
    this.drawField(time);

    const k = this.cam.k;
    const focusSet = this.selected ? new Set([this.selected, ...(this.neighbors.get(this.selected) ?? [])]) : this.highlight;
    const dim = (id: string) => !!focusSet && !focusSet.has(id);

    // Edges, batched by style.
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    const styles: { kind: GEdge['kind']; dash: number[]; color: string; width: number; alpha: number }[] = [
      { kind: 'link', dash: [2, 3], color: this.tones.ink3, width: 1, alpha: 0.55 },
      { kind: 'works', dash: [5, 5], color: this.tones.ink3, width: 1.5, alpha: 0.75 },
      { kind: 'trail', dash: [3, 4], color: this.tones.ink3, width: 1, alpha: 0.8 },
      { kind: 'inbox', dash: [8, 5], color: this.tones.ink, width: 1, alpha: 0.5 },
    ];
    const lit: [number, number, number, number][] = [];
    for (const style of styles) {
      if (style.kind === 'link' && this.quality === 2 && k < 0.5) continue;
      for (const pass of [false, true]) {
        ctx.beginPath();
        let any = false;
        for (const e of this.edges) {
          if (e.kind !== style.kind) continue;
          const a = this.layout.get(e.source);
          const b = this.layout.get(e.target);
          if (!a || !b) continue;
          const faded = dim(e.source) || dim(e.target);
          if (faded !== pass) continue;
          const [ax, ay] = this.toScreen(a.x, a.y);
          const [bx, by] = this.toScreen(b.x, b.y);
          if ((ax < 0 && bx < 0) || (ay < 0 && by < 0) || (ax > this.W && bx > this.W) || (ay > this.H && by > this.H)) continue;
          if (this.litEdges.has(this.edgeKey(e.source, e.target))) {
            lit.push([ax, ay, bx, by]);
            continue;
          }
          ctx.moveTo(Math.round(ax) + 0.5, Math.round(ay) + 0.5);
          ctx.lineTo(Math.round(bx) + 0.5, Math.round(by) + 0.5);
          any = true;
        }
        if (!any) continue;
        ctx.setLineDash(style.dash);
        ctx.lineWidth = style.width;
        ctx.strokeStyle = style.color;
        ctx.globalAlpha = style.alpha * (pass ? 0.25 : 1);
        ctx.stroke();
      }
    }
    // Paths packets travelled light up in signal for a moment.
    for (const p of this.packets) {
      for (let i = 0; i < p.hop; i += 1) {
        const [ax, ay] = this.toScreen(p.hops[i].x, p.hops[i].y);
        const [bx, by] = this.toScreen(p.hops[i + 1].x, p.hops[i + 1].y);
        lit.push([ax, ay, bx, by]);
      }
    }
    if (lit.length) {
      ctx.beginPath();
      for (const [ax, ay, bx, by] of lit) {
        ctx.moveTo(Math.round(ax) + 0.5, Math.round(ay) + 0.5);
        ctx.lineTo(Math.round(bx) + 0.5, Math.round(by) + 0.5);
      }
      ctx.setLineDash([5, 4]);
      ctx.lineWidth = 2;
      ctx.strokeStyle = this.tones.signal;
      ctx.globalAlpha = 1;
      ctx.stroke();
    }
    ctx.setLineDash([]);
    ctx.globalAlpha = 1;

    // Nodes as pixel sprites (device pixels, snapped).
    ctx.setTransform(1, 0, 0, 1, 0, 0);
    ctx.imageSmoothingEnabled = false;
    const unit = this.unit();
    const labels: { x: number; y: number; text: string; strong: boolean; faded: boolean }[] = [];
    const order: NodeKindOrder = { entity: 0, checkpoint: 1, handoff: 2, project: 3, agent: 4 };
    const sorted = [...this.layout.nodes].sort((a, b) => order[a.kind] - order[b.kind]);
    const showEntityLabels = this.quality < 2 && k >= 1.35;
    const showMemoryLabels = this.quality === 0 && k >= 2;
    for (const ln of sorted) {
      const n = this.nodes.get(ln.id);
      if (!n) continue;
      const [sx, sy] = this.toScreen(ln.x, ln.y);
      if (sx < -40 || sy < -40 || sx > this.W + 40 || sy > this.H + 40) continue;
      const active = this.isActive(n);
      const img = this.sprite(n, unit, active);
      const faded = dim(n.id);
      ctx.globalAlpha = faded ? 0.28 : 1;
      const px = Math.round(sx * this.dpr - img.width / 2);
      const py = Math.round(sy * this.dpr - img.height / 2);
      ctx.drawImage(img, px, py);
      const strong = n.id === this.selected || n.id === this.hovered;
      if (strong) this.brackets(px, py, img.width, img.height, n.id === this.selected ? this.tones.signal : this.tones.ink, unit);
      const wantsLabel =
        n.kind === 'agent' ||
        n.kind === 'project' ||
        strong ||
        (this.highlight?.has(n.id) ?? false) ||
        (n.kind === 'entity' && showEntityLabels && n.weight >= (k > 2.2 ? 1 : 2)) ||
        ((n.kind === 'handoff' || n.kind === 'checkpoint') && showMemoryLabels);
      if (wantsLabel) labels.push({ x: sx, y: sy + img.height / this.dpr / 2 + 5, text: n.label, strong: strong || n.kind !== 'entity', faded });
    }
    ctx.globalAlpha = 1;

    // Packets: a flat orange capsule (never stepped) with a short pixel trail.
    for (const p of this.packets) {
      const a = p.hops[p.hop];
      const b = p.hops[p.hop + 1];
      const e = p.t < 0.5 ? 2 * p.t * p.t : 1 - Math.pow(-2 * p.t + 2, 2) / 2;
      const x = a.x + (b.x - a.x) * e;
      const y = a.y + (b.y - a.y) * e;
      const [sx, sy] = this.toScreen(x, y);
      const u = Math.max(2, unit);
      ctx.fillStyle = this.tones.signal;
      const hx = Math.round(sx * this.dpr);
      const hy = Math.round(sy * this.dpr);
      ctx.fillRect(hx - 2 * u, hy - u, 4 * u, 2 * u);
      const dx = (b.x - a.x) * k;
      const dy = (b.y - a.y) * k;
      const d = Math.hypot(dx, dy) || 1;
      for (let s = 1; s <= 4; s += 1) {
        ctx.globalAlpha = 0.7 - s * 0.15;
        const tx = Math.round((sx - (dx / d) * s * 5) * this.dpr);
        const ty = Math.round((sy - (dy / d) * s * 5) * this.dpr);
        ctx.fillRect(tx - Math.floor(u / 2), ty - Math.floor(u / 2), u, u);
      }
      ctx.globalAlpha = 1;
    }
    // Docking sparks.
    for (const b of this.bursts) {
      const age = (time - b.start) / 1600;
      if (age > 0.45) continue;
      const [sx, sy] = this.toScreen(b.x, b.y);
      const u = Math.max(2, unit);
      ctx.fillStyle = this.tones.signal;
      for (let s = 0; s < 12; s += 1) {
        if (bayer(s, 5) > 1 - age * 2) continue;
        const ang = (s / 12) * Math.PI * 2;
        const r = 10 + age * 60;
        ctx.fillRect(Math.round((sx + Math.cos(ang) * r) * this.dpr), Math.round((sy + Math.sin(ang) * r * 0.8) * this.dpr), u, u);
      }
    }

    // Labels on a paper plate so they read over the field.
    ctx.setTransform(this.dpr, 0, 0, this.dpr, 0, 0);
    ctx.font = '500 11px "JetBrains Mono", ui-monospace, monospace';
    ctx.textAlign = 'center';
    ctx.textBaseline = 'top';
    const maxLabels = this.quality === 0 ? 220 : 90;
    let count = 0;
    for (const l of labels) {
      if (count >= maxLabels) break;
      count += 1;
      const text = l.text.length > 28 ? `${l.text.slice(0, 27)}…` : l.text;
      const w = ctx.measureText(text).width;
      ctx.globalAlpha = l.faded ? 0.35 : 0.88;
      ctx.fillStyle = this.tones.paper;
      ctx.fillRect(Math.round(l.x - w / 2 - 3), Math.round(l.y - 1), Math.ceil(w + 6), 14);
      ctx.globalAlpha = l.faded ? 0.4 : 1;
      ctx.fillStyle = l.strong ? this.tones.ink : this.tones.ink3;
      ctx.fillText(text, Math.round(l.x), Math.round(l.y));
    }
    ctx.globalAlpha = 1;
  }

  private brackets(x: number, y: number, w: number, h: number, color: string, unit: number): void {
    const ctx = this.ctx;
    const g = 3 * unit;
    const l = 3 * unit;
    const u = Math.max(1, unit);
    ctx.fillStyle = color;
    const x0 = x - g;
    const y0 = y - g;
    const x1 = x + w + g;
    const y1 = y + h + g;
    for (const [cx, cy, sx, sy] of [
      [x0, y0, 1, 1],
      [x1, y0, -1, 1],
      [x0, y1, 1, -1],
      [x1, y1, -1, -1],
    ] as const) {
      ctx.fillRect(sx > 0 ? cx : cx - l, sy > 0 ? cy : cy - u, l, u);
      ctx.fillRect(sx > 0 ? cx : cx - u, sy > 0 ? cy : cy - l, u, l);
    }
  }

  // --------------------------------------------------------------- input

  /** The node under a canvas-relative point, if any. */
  hit(sx: number, sy: number): string | null {
    let best: string | null = null;
    let bestD = Infinity;
    const unit = this.unit() / this.dpr;
    for (const ln of this.layout.nodes) {
      const n = this.nodes.get(ln.id);
      if (!n) continue;
      const [x, y] = this.toScreen(ln.x, ln.y);
      const { w, h } = spriteSize(spriteKey(n.kind, n.weight, false));
      const r = (Math.max(w, h) * unit) / 2 + 5;
      const d = Math.hypot(x - sx, y - sy);
      if (d <= r && d < bestD) {
        best = ln.id;
        bestD = d;
      }
    }
    return best;
  }

  private bindInput(): void {
    const c = this.canvas;
    const local = (e: PointerEvent | WheelEvent) => {
      const r = c.getBoundingClientRect();
      return { x: e.clientX - r.left, y: e.clientY - r.top };
    };
    const onWheel = (e: WheelEvent) => {
      e.preventDefault();
      const p = local(e);
      const delta = e.deltaMode === 1 ? e.deltaY * 16 : e.deltaY;
      this.zoomBy(Math.exp(-delta * (e.ctrlKey ? 0.01 : 0.0022)), p.x, p.y);
    };
    const onDown = (e: PointerEvent) => {
      const p = local(e);
      this.pointers.set(e.pointerId, p);
      c.setPointerCapture(e.pointerId);
      if (this.pointers.size === 2) {
        const [a, b] = [...this.pointers.values()];
        this.drag = { x: (a.x + b.x) / 2, y: (a.y + b.y) / 2, camX: this.cam.x, camY: this.cam.y, moved: true, pinch: { d: Math.hypot(a.x - b.x, a.y - b.y), k: this.cam.k } };
      } else this.drag = { x: p.x, y: p.y, camX: this.cam.x, camY: this.cam.y, moved: false };
    };
    const onMove = (e: PointerEvent) => {
      const p = local(e);
      if (this.pointers.has(e.pointerId)) this.pointers.set(e.pointerId, p);
      if (this.drag?.pinch && this.pointers.size === 2) {
        const [a, b] = [...this.pointers.values()];
        const d = Math.hypot(a.x - b.x, a.y - b.y);
        const factor = (this.drag.pinch.k * (d / this.drag.pinch.d)) / this.cam.k;
        this.zoomBy(factor, (a.x + b.x) / 2, (a.y + b.y) / 2);
        return;
      }
      if (this.drag && this.pointers.size === 1) {
        const dx = p.x - this.drag.x;
        const dy = p.y - this.drag.y;
        if (!this.drag.moved && Math.hypot(dx, dy) > 4) this.drag.moved = true;
        if (this.drag.moved) {
          this.cam.x = this.drag.camX - dx / this.cam.k;
          this.cam.y = this.drag.camY - dy / this.cam.k;
          this.userMoved = true;
          this.camTarget = null;
          this.fitted = true;
          c.style.cursor = 'grabbing';
          this.requestDraw();
        }
        return;
      }
      if (e.pointerType === 'mouse') {
        const id = this.hit(p.x, p.y);
        if (id !== this.hovered) {
          this.hovered = id;
          c.style.cursor = id ? 'pointer' : 'grab';
          this.cb.onHover?.(id);
          this.requestDraw();
        }
      }
    };
    const onUp = (e: PointerEvent) => {
      const p = local(e);
      const wasClick = this.drag && !this.drag.moved && this.pointers.size === 1;
      this.pointers.delete(e.pointerId);
      if (this.pointers.size === 0) {
        if (wasClick) {
          const id = this.hit(p.x, p.y);
          this.selected = id;
          this.cb.onSelect(id);
          this.requestDraw();
        }
        this.drag = null;
        c.style.cursor = this.hovered ? 'pointer' : 'grab';
      } else if (this.pointers.size === 1) {
        const [q] = [...this.pointers.values()];
        this.drag = { x: q.x, y: q.y, camX: this.cam.x, camY: this.cam.y, moved: true };
      }
    };
    const onLeave = () => {
      if (this.hovered) {
        this.hovered = null;
        this.cb.onHover?.(null);
        this.requestDraw();
      }
    };
    const onKey = (e: KeyboardEvent) => {
      const step = 60;
      if (e.key === 'ArrowLeft') this.pan(step, 0);
      else if (e.key === 'ArrowRight') this.pan(-step, 0);
      else if (e.key === 'ArrowUp') this.pan(0, step);
      else if (e.key === 'ArrowDown') this.pan(0, -step);
      else if (e.key === '+' || e.key === '=') this.zoomBy(1.25);
      else if (e.key === '-' || e.key === '_') this.zoomBy(0.8);
      else if (e.key === '0') this.fit();
      else if (e.key === 'Escape') {
        this.selected = null;
        this.cb.onSelect(null);
        this.requestDraw();
      } else return;
      e.preventDefault();
    };
    c.addEventListener('wheel', onWheel, { passive: false });
    c.addEventListener('pointerdown', onDown);
    c.addEventListener('pointermove', onMove);
    c.addEventListener('pointerup', onUp);
    c.addEventListener('pointercancel', onUp);
    c.addEventListener('pointerleave', onLeave);
    c.addEventListener('keydown', onKey);
    c.style.cursor = 'grab';
    c.style.touchAction = 'none';
    this.cleanup.push(() => {
      c.removeEventListener('wheel', onWheel);
      c.removeEventListener('pointerdown', onDown);
      c.removeEventListener('pointermove', onMove);
      c.removeEventListener('pointerup', onUp);
      c.removeEventListener('pointercancel', onUp);
      c.removeEventListener('pointerleave', onLeave);
      c.removeEventListener('keydown', onKey);
    });
  }
}

type NodeKindOrder = Record<GNode['kind'], number>;
