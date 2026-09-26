// The dithered stone-dust cloud (brand art direction: trail-brain hero).
// An ordered 8×8 Bayer dither over a soft value-noise field, quantised to
// three dust tones plus a rare ember fleck in signal orange. Pure and
// deterministic: the same size, time and shape always give the same cells,
// so it can be tested and drawn once (reduced motion) or drifted slowly.

export const CELL = 6;

/** 8×8 Bayer matrix, normalised to (0, 1). */
export const BAYER8: readonly number[] = [
  0, 32, 8, 40, 2, 34, 10, 42, 48, 16, 56, 24, 50, 18, 58, 26, 12, 44, 4, 36, 14, 46, 6, 38, 60, 28, 52, 20, 62, 30, 54, 22, 3, 35, 11, 43, 1,
  33, 9, 41, 51, 19, 59, 27, 49, 17, 57, 25, 15, 47, 7, 39, 13, 45, 5, 37, 63, 31, 55, 23, 61, 29, 53, 21,
].map((v) => (v + 0.5) / 64);

export function hash2(x: number, y: number): number {
  const h = Math.sin(x * 127.1 + y * 311.7) * 43758.5453;
  return h - Math.floor(h);
}

function noise(x: number, y: number): number {
  const xi = Math.floor(x);
  const yi = Math.floor(y);
  const xf = x - xi;
  const yf = y - yi;
  const u = xf * xf * (3 - 2 * xf);
  const v = yf * yf * (3 - 2 * yf);
  const a = hash2(xi, yi);
  const b = hash2(xi + 1, yi);
  const c = hash2(xi, yi + 1);
  const d = hash2(xi + 1, yi + 1);
  return a + (b - a) * u + (c - a) * v + (a - b - c + d) * u * v;
}

export function fbm(x: number, y: number): number {
  return noise(x, y) * 0.62 + noise(x * 2.03 + 17.1, y * 2.03 - 9.4) * 0.38;
}

export function smoothstep(a: number, b: number, x: number): number {
  const t = Math.max(0, Math.min(1, (x - a) / (b - a)));
  return t * t * (3 - 2 * t);
}

export interface Rect {
  l: number;
  t: number;
  r: number;
  b: number;
}

/** Where the cloud banks up: `right` hugs the right edge, `floor` the bottom, `strip` a thin band. */
export type CloudShape = 'right' | 'floor' | 'strip';

function bankWeight(shape: CloudShape, x: number, y: number, w: number, h: number): number {
  switch (shape) {
    case 'right':
      return smoothstep(0.38, 0.98, x / w);
    case 'floor':
      return smoothstep(0.35, 1, y / h) * 0.95 + smoothstep(0.25, 0, y / h) * smoothstep(0.55, 1, x / w) * 0.55;
    case 'strip':
      return smoothstep(0.2, 1, x / w) * 0.85;
  }
}

export interface Burst {
  /** Centre (px). */
  x: number;
  y: number;
  /** Ring radius (px) and width. */
  rad: number;
  wid: number;
  /** Strength 0..1. */
  a: number;
}

export const EMBER = 4;

/**
 * Levels per cell, row-major: 0 = empty, 1..3 = dust tones, 4 = ember fleck.
 * `t` is seconds of drift (0 for a still cloud); `avoid` keeps text legible.
 */
export function cloudCells(w: number, h: number, t: number, shape: CloudShape, avoid: Rect | null = null, burst: Burst | null = null): { cols: number; rows: number; cells: Uint8Array } {
  const cols = Math.max(0, Math.ceil(w / CELL));
  const rows = Math.max(0, Math.ceil(h / CELL));
  const cells = new Uint8Array(cols * rows);
  const tick = Math.floor(t * 2.2);
  for (let j = 0; j < rows; j += 1) {
    for (let i = 0; i < cols; i += 1) {
      const x = (i + 0.5) * CELL;
      const y = (j + 0.5) * CELL;
      const bay = BAYER8[(j & 7) * 8 + (i & 7)];
      if (burst && burst.a > 0) {
        const d = Math.hypot(x - burst.x, y - burst.y);
        const bv = Math.exp(-(((d - burst.rad) / burst.wid) ** 2)) * burst.a;
        if (bv > bay) {
          cells[j * cols + i] = EMBER;
          continue;
        }
      }
      const bank = smoothstep(0.48, 0.8, fbm(x / 190 + t * 0.018 + 3.1, y / 120 + 1.7));
      let v = bank * 0.8 * bankWeight(shape, x, y, w, h);
      if (avoid && v > 0.01) {
        const ox = Math.max(avoid.l - x, 0, x - avoid.r);
        const oy = Math.max(avoid.t - y, 0, y - avoid.b);
        v *= smoothstep(0, 48, Math.hypot(ox, oy));
      }
      if (v <= 0.01) continue;
      const q = v * 3;
      let lv = Math.floor(q);
      if (q - lv > bay) lv += 1;
      if (lv <= 0) continue;
      if (lv > 3) lv = 3;
      cells[j * cols + i] = lv >= 2 && hash2(i + tick * 0.37, j - tick * 0.11) > 0.996 ? EMBER : lv;
    }
  }
  return { cols, rows, cells };
}

// ---------------------------------------------------------------------------
// Tones from the theme tokens (so the cloud follows light and dark)
// ---------------------------------------------------------------------------

export function parseHex(color: string): [number, number, number] | null {
  const m = /^#([0-9a-f]{3}|[0-9a-f]{6})$/i.exec(color.trim());
  if (!m) return null;
  let hex = m[1];
  if (hex.length === 3) hex = hex.split('').map((c) => c + c).join('');
  return [parseInt(hex.slice(0, 2), 16), parseInt(hex.slice(2, 4), 16), parseInt(hex.slice(4, 6), 16)];
}

export function mix(a: string, b: string, t: number): string {
  const pa = parseHex(a);
  const pb = parseHex(b);
  if (!pa || !pb) return a;
  const c = pa.map((v, i) => Math.round(v + (pb[i] - v) * t));
  return `#${c.map((v) => v.toString(16).padStart(2, '0')).join('')}`;
}

export interface CloudTones {
  /** Index = level: [unused, d1, d2, d3, ember]. */
  fills: [string, string, string, string, string];
}

/** Dust tones between the panel and the rule colour, ember from the signal colour. */
export function tonesFrom(vars: { paper2: string; rule: string; signal: string; panel: string }): CloudTones {
  const d1 = mix(vars.panel, vars.paper2, 0.85);
  const d2 = mix(vars.paper2, vars.rule, 0.5);
  const d3 = vars.rule;
  const ember = mix(vars.signal, vars.panel, 0.25);
  return { fills: ['transparent', d1, d2, d3, ember] };
}
