// Pixel craft for the board: an 8x8 ordered-dither matrix, a small value
// noise, and the dithered seal stamp. Deterministic (no Math.random), so a
// task's stamp is the same on every render and every screen.

/** 8x8 Bayer thresholds in (0, 1). */
export const BAYER: readonly number[] = [
  0, 32, 8, 40, 2, 34, 10, 42, 48, 16, 56, 24, 50, 18, 58, 26, 12, 44, 4, 36, 14, 46, 6, 38, 60, 28, 52, 20, 62, 30, 54, 22, 3, 35, 11,
  43, 1, 33, 9, 41, 51, 19, 59, 27, 49, 17, 57, 25, 15, 47, 7, 39, 13, 45, 5, 37, 63, 31, 55, 23, 61, 29, 53, 21,
].map((v) => (v + 0.5) / 64);

export function bayer(i: number, j: number): number {
  return BAYER[(j & 7) * 8 + (i & 7)];
}

/** Integer hash to [0, 1). */
export function hash2(x: number, y: number): number {
  let h = Math.imul(x | 0, 374761393) + Math.imul(y | 0, 668265263);
  h = Math.imul(h ^ (h >>> 13), 1274126177);
  return ((h ^ (h >>> 16)) >>> 0) / 4294967296;
}

/** String hash (FNV-1a) to a 32-bit seed. */
export function seedOf(text: string): number {
  let h = 0x811c9dc5;
  for (let i = 0; i < text.length; i += 1) {
    h ^= text.charCodeAt(i);
    h = Math.imul(h, 0x01000193);
  }
  return h >>> 0;
}

export function vnoise(x: number, y: number): number {
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
  return vnoise(x, y) * 0.62 + vnoise(x * 2.03 + 17.1, y * 2.03 - 9.4) * 0.38;
}

export function sstep(a: number, b: number, x: number): number {
  const t = Math.min(1, Math.max(0, (x - a) / (b - a)));
  return t * t * (3 - 2 * t);
}

/** Dither level 0..3 for a value in [0, 1] at pixel (i, j). */
export function ditherLevel(value: number, i: number, j: number): number {
  if (value <= 0.01) return 0;
  const q = value * 3;
  let level = Math.floor(q);
  if (q - level > bayer(i, j)) level += 1;
  return Math.max(0, Math.min(3, level));
}

export interface StampPixel {
  x: number;
  y: number;
}

/**
 * A hand-inked seal ring on a `size`×`size` grid: a slightly wobbly ring
 * whose ink thins out through the dither matrix, seeded by the report id so
 * no two seals are identical but each is stable.
 */
export function stampPixels(seed: string, size = 22): StampPixel[] {
  const s = seedOf(seed);
  const cx = (size - 1) / 2;
  const cy = (size - 1) / 2;
  const radius = size * 0.42;
  const out: StampPixel[] = [];
  for (let y = 0; y < size; y += 1) {
    for (let x = 0; x < size; x += 1) {
      const dx = x - cx;
      const dy = y - cy;
      const angle = Math.atan2(dy, dx);
      const wobble = (vnoise(angle * 1.7 + (s % 97), s % 13) - 0.5) * 1.3;
      const d = Math.abs(Math.sqrt(dx * dx + dy * dy) - (radius + wobble));
      const ink = sstep(1.6, 0.35, d) * (0.72 + 0.4 * vnoise(x * 0.6 + (s % 31), y * 0.6));
      if (ink > bayer(x + s, y + (s >>> 3))) out.push({ x, y });
    }
  }
  return out;
}

export interface BoardTones {
  d1: string;
  d2: string;
  d3: string;
  ember: string;
  signal: string;
}

/** The dither tones from CSS custom properties on the board root (theme aware). */
export function readTones(el: Element): BoardTones {
  const cs = getComputedStyle(el);
  const v = (name: string, fallback: string) => cs.getPropertyValue(name).trim() || fallback;
  return {
    d1: v('--d1', '#e0e1da'),
    d2: v('--d2', '#d2d4cc'),
    d3: v('--d3', '#c1c4bb'),
    ember: v('--d-or', '#ff8a52'),
    signal: v('--signal', '#ff5b14'),
  };
}

export function prefersReducedMotion(): boolean {
  return typeof window !== 'undefined' && !!window.matchMedia?.('(prefers-reduced-motion: reduce)').matches;
}
