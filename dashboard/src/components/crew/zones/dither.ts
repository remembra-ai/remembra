// Pixel clouds and packets for the live header band (owner art direction:
// dithered pixel clouds, orange pixel packets travelling on the trail).
// Ordered 8×8 Bayer dithering over a two-octave value noise, quantised to
// three stone tones; packets are 2×1-cell orange blocks riding a dashed trail.
// Pure maths here; DitherField paints it.

export const CELL = 6;

const BAYER8 = [
  0, 32, 8, 40, 2, 34, 10, 42, 48, 16, 56, 24, 50, 18, 58, 26, 12, 44, 4, 36, 14, 46, 6, 38, 60, 28, 52, 20, 62, 30, 54, 22, 3, 35, 11, 43, 1, 33, 9,
  41, 51, 19, 59, 27, 49, 17, 57, 25, 15, 47, 7, 39, 13, 45, 5, 37, 63, 31, 55, 23, 61, 29, 53, 21,
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

/**
 * Tone level 0..3 of one cell at time `t` (seconds). Clouds bank towards the
 * right edge and thin out behind the text on the left, so the copy stays legible.
 */
export function cloudLevel(i: number, j: number, cols: number, rows: number, t: number): number {
  const x = (i + 0.5) / Math.max(1, cols);
  const y = (j + 0.5) / Math.max(1, rows);
  const bank = smoothstep(0.52, 0.98, x) * 0.95 + smoothstep(0.62, 1, y) * smoothstep(0.3, 0.9, x) * 0.4;
  const n = fbm(i / 11 + t * 0.05 + 3.1, j / 7 - t * 0.02 + 1.7);
  const v = smoothstep(0.38, 0.8, n) * bank;
  const q = v * 3;
  let level = Math.floor(q);
  if (q - level > bayer(i, j)) level += 1;
  return Math.max(0, Math.min(3, level));
}

/** A rare ember fleck in the densest cloud cells (the brand's orange flecks). */
export function isEmber(i: number, j: number, level: number, tick: number): boolean {
  return level >= 2 && hash(i + tick * 0.37, j - tick * 0.11) > 0.996;
}

export interface Packet {
  /** Start time (ms). */
  born: number;
  /** Travel time across the band (ms). */
  duration: number;
}

/** Cell column of a packet at `now`, or null once it has left the band. */
export function packetColumn(packet: Packet, now: number, cols: number): number | null {
  const p = (now - packet.born) / packet.duration;
  if (p < 0 || p > 1) return null;
  // ease-out: leaves fast, settles into the right edge
  const e = 1 - (1 - p) * (1 - p);
  return Math.floor(e * (cols + 2)) - 2;
}

/** Packets to launch for a seq jump: one per event, at most `max`, staggered. */
export function launchPackets(prevSeq: number, seq: number, now: number, max = 4, duration = 1400): Packet[] {
  const n = Math.max(0, Math.min(max, seq - prevSeq));
  return Array.from({ length: n }, (_, k) => ({ born: now + k * 180, duration }));
}
