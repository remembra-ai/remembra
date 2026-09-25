// Pixel craft shared by the brand hero and the Constellation graph: an 8x8
// ordered-dither matrix, a small value-noise field, and the warm cloud tones
// read from CSS (stone/sand lit tops over spruce/peach undersides).

/** 8x8 Bayer thresholds in (0, 1). */
export const BAYER: readonly number[] = [
  0, 32, 8, 40, 2, 34, 10, 42, 48, 16, 56, 24, 50, 18, 58, 26, 12, 44, 4, 36, 14, 46, 6, 38, 60, 28, 52, 20, 62, 30, 54, 22, 3, 35, 11,
  43, 1, 33, 9, 41, 51, 19, 59, 27, 49, 17, 57, 25, 15, 47, 7, 39, 13, 45, 5, 37, 63, 31, 55, 23, 61, 29, 53, 21,
].map((v) => (v + 0.5) / 64);

export function bayer(i: number, j: number): number {
  return BAYER[(j & 7) * 8 + (i & 7)];
}

/** Integer hash to [0, 1). Stable across frames (no Math.random). */
export function hash2(x: number, y: number): number {
  let h = Math.imul(x | 0, 374761393) + Math.imul(y | 0, 668265263);
  h = Math.imul(h ^ (h >>> 13), 1274126177);
  return ((h ^ (h >>> 16)) >>> 0) / 4294967296;
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
  return vnoise(x, y) * 0.55 + vnoise(x * 2.1 + 5.2, y * 2.1 + 1.3) * 0.3 + vnoise(x * 4.3 + 9.1, y * 4.3 + 7.7) * 0.15;
}

export function sstep(a: number, b: number, x: number): number {
  const t = Math.min(1, Math.max(0, (x - a) / (b - a)));
  return t * t * (3 - 2 * t);
}

export interface PixelTones {
  paper: string;
  ink: string;
  ink3: string;
  rule: string;
  signal: string;
  /** Lit top of a cloud (stone/sand). */
  cloudHi: string;
  /** Underside of a cloud (spruce). */
  cloudLo: string;
  /** Warm underside near the light (peach). */
  cloudWarm: string;
  panel: string;
}

/** Read the pixel palette from CSS custom properties on <html>. */
export function readTones(el: Element = document.documentElement): PixelTones {
  const cs = getComputedStyle(el);
  const v = (name: string, fallback: string) => cs.getPropertyValue(name).trim() || fallback;
  return {
    paper: v('--paper', '#121615'),
    ink: v('--ink', '#e9e8e1'),
    ink3: v('--ink-3', '#8c908a'),
    rule: v('--rule', '#2b3331'),
    signal: v('--signal', '#ff6b2b'),
    cloudHi: v('--cloud-hi', '#3a3a33'),
    cloudLo: v('--cloud-lo', '#1c2522'),
    cloudWarm: v('--cloud-warm', '#4a3226'),
    panel: v('--panel', '#1a201e'),
  };
}

/** Subscribe to theme changes (the .dark class on <html>). Returns an unsubscribe. */
export function onThemeChange(callback: () => void): () => void {
  const observer = new MutationObserver(callback);
  observer.observe(document.documentElement, { attributes: true, attributeFilter: ['class'] });
  return () => observer.disconnect();
}

/** prefers-reduced-motion, live. */
export function watchReducedMotion(callback: (reduced: boolean) => void): () => void {
  const mq = window.matchMedia?.('(prefers-reduced-motion: reduce)');
  if (!mq) {
    callback(false);
    return () => undefined;
  }
  const on = () => callback(mq.matches);
  on();
  mq.addEventListener('change', on);
  return () => mq.removeEventListener('change', on);
}
