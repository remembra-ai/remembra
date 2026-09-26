// Ordered (Bayer 8×8) dithering for the crew views' pixel clouds and activity
// strips: the same stone-dust cloud as the brand hero, drawn in square cells
// with the odd ember fleck. Pure functions; the canvas component draws them.

/** The 8×8 Bayer matrix as thresholds in (0, 1). */
export const BAYER8: readonly number[] = [
  0, 32, 8, 40, 2, 34, 10, 42, 48, 16, 56, 24, 50, 18, 58, 26, 12, 44, 4, 36, 14, 46, 6, 38, 60, 28, 52, 20, 62, 30, 54, 22, 3, 35, 11, 43,
  1, 33, 9, 41, 51, 19, 59, 27, 49, 17, 57, 25, 15, 47, 7, 39, 13, 45, 5, 37, 63, 31, 55, 23, 61, 29, 53, 21,
].map((v) => (v + 0.5) / 64);

export function bayer(i: number, j: number): number {
  return BAYER8[(j & 7) * 8 + (i & 7)];
}

function hash(x: number, y: number): number {
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
  const a = hash(xi, yi);
  const b = hash(xi + 1, yi);
  const c = hash(xi, yi + 1);
  const d = hash(xi + 1, yi + 1);
  return a + (b - a) * u + (c - a) * v + (a - b - c + d) * u * v;
}

export function fbm(x: number, y: number): number {
  return noise(x, y) * 0.62 + noise(x * 2.03 + 17.1, y * 2.03 - 9.4) * 0.38;
}

export function smoothstep(a: number, b: number, x: number): number {
  const t = Math.max(0, Math.min(1, (x - a) / (b - a)));
  return t * t * (3 - 2 * t);
}

/** Tone level 0..3 for a density in [0, 1] at cell (i, j): ordered dithering between 3 tones. */
export function toneLevel(value: number, i: number, j: number): number {
  if (value <= 0.01) return 0;
  const q = Math.min(1, value) * 3;
  let level = Math.floor(q);
  if (q - level > bayer(i, j)) level += 1;
  return Math.max(0, Math.min(3, level));
}

/** Is this cell lit for a 0..1 density (1-bit dither)? */
export function lit(value: number, i: number, j: number): boolean {
  return value > bayer(i, j);
}

export type CloudShape = 'banks' | 'right' | 'slot';

/**
 * Cloud density at pixel (x, y) of a w×h field at time t (seconds).
 * `banks`: low banks along the bottom with a wisp in the top right (headers).
 * `right`: a bank that thickens to the right (status strips, cards).
 * `slot`: a thin haze around an empty pickup slot, heavier at the start of the track.
 */
export function cloudDensity(shape: CloudShape, x: number, y: number, w: number, h: number, t: number, seed = 0): number {
  const bank = smoothstep(0.46, 0.8, fbm(x / 170 + t * 0.018 + seed * 3.1, y / 110 + 1.7 + seed));
  let zone: number;
  switch (shape) {
    case 'banks':
      zone = smoothstep(0.55, 1.0, y / h) * 0.95 + smoothstep(0.35, 0, y / h) * smoothstep(0.55, 1, x / w) * 0.7;
      break;
    case 'right':
      zone = smoothstep(0.3, 1.0, x / w);
      break;
    case 'slot':
      zone = smoothstep(0.55, 0, x / w) * 0.8 + smoothstep(0.7, 1, y / h) * 0.35;
      break;
  }
  return bank * 0.8 * zone;
}

export interface DitherTones {
  levels: [string, string, string];
  ember: string;
}

/** Stone dust on paper, charcoal on the dark panel; ember flecks in both. */
export function tonesFor(dark: boolean): DitherTones {
  return dark
    ? { levels: ['#23262a', '#2d3136', '#3b4046'], ember: '#b4501e' }
    : { levels: ['#e0e1da', '#d2d4cc', '#c1c4bb'], ember: '#ff8a52' };
}

/** A rare, slowly drifting ember fleck at a cell (the "packet in the cloud"). */
export function emberAt(i: number, j: number, tick: number, rate = 0.996): boolean {
  return hash(i + tick * 0.37, j - tick * 0.11) > rate;
}
