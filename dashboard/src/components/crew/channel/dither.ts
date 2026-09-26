// Pure data and math behind the crew pixel pieces (pixels.tsx): the hand-set
// 9-px glyphs and the Bayer-dithered stone-dust bank. Kept free of React so it
// is unit-tested and the component module exports components only.

// ---------------------------------------------------------------------------
// Glyphs: '#' ink, 'o' signal orange, '.' empty. Drawn by hand on a 9-px grid.
// ---------------------------------------------------------------------------

export const GLYPHS = {
  bell: ['....#....', '...###...', '..#...#..', '..#...#..', '..#...#..', '.#.....#.', '#########', '.........', '....o....'],
  baton: ['.......oo', '......ooo', '.....ooo.', '....ooo..', '...ooo...', '..#......', '.........', '#........', '.........'],
  question: ['..#####..', '.##...##.', '......##.', '.....##..', '....##...', '....##...', '.........', '....oo...', '....oo...'],
  decision: ['.#.......', '.#ooooo..', '.#oooo...', '.#ooooo..', '.#.......', '.#.......', '.#.......', '.#.......', '###......'],
  collision: ['#.......#', '.#.....#.', '..#...#..', '...#.#...', '....o....', '...#.#...', '..#...#..', '.#.....#.', '#.......#'],
  shield: ['.#######.', '.#.....#.', '.#..o..#.', '.#.ooo.#.', '.#..o..#.', '..#...#..', '...#.#...', '....#....', '.........'],
  hook: ['.#.....#.', '.#.....#.', '.#.....#.', '.#....#..', '.#...#...', '.#..#....', '.#.#.....', '.##......', '.o.......'],
  clock: ['..#####..', '.#.....#.', '#...#...#', '#...#...#', '#...ooo.#', '#.......#', '.#.....#.', '..#####..', '.........'],
  zone: ['#########', '#.#.#.#.#', '##.#.#.##', '#.#.o.#.#', '##.#.#.##', '#.#.#.#.#', '#########', '.........', '.........'],
  mail: ['#########', '##.....##', '#.#...#.#', '#..#o#..#', '#...#...#', '#.......#', '#########', '.........', '.........'],
  wire: ['...#.#...', '...#.#...', '..#####..', '..#####..', '...###...', '....#....', '....#....', '....o....', '....o....'],
  chat: ['#########', '#.......#', '#.o.o.o.#', '#.......#', '#########', '.##......', '.#.......', '.........', '.........'],
  check: ['........#', '.......#.', '......#..', '#....#...', '.#..#....', '..##.....', '...#.....', '.........', '.........'],
  crew: ['.##...##.', '.##...##.', '.........', '####.####', '####.####', '.........', '...ooo...', '...ooo...', '.........'],
  pin: ['..#####..', '..#ooo#..', '..#ooo#..', '.#######.', '....#....', '....#....', '....#....', '....#....', '.........'],
  inbox: ['#########', '#.......#', '#.......#', '#.......#', '###...###', '#..#o#..#', '#########', '.........', '.........'],
} as const satisfies Record<string, readonly string[]>;

export type GlyphName = keyof typeof GLYPHS;

export interface GlyphCell {
  x: number;
  y: number;
  signal: boolean;
}

/** The cells of a glyph (pure: tested). */
export function glyphCells(rows: readonly string[]): GlyphCell[] {
  const cells: GlyphCell[] = [];
  rows.forEach((row, y) => {
    [...row].forEach((ch, x) => {
      if (ch === '#' || ch === 'o') cells.push({ x, y, signal: ch === 'o' });
    });
  });
  return cells;
}

// ---------------------------------------------------------------------------
// Dither bank
// ---------------------------------------------------------------------------

const BAYER = [
  0, 32, 8, 40, 2, 34, 10, 42, 48, 16, 56, 24, 50, 18, 58, 26, 12, 44, 4, 36, 14, 46, 6, 38, 60, 28, 52, 20, 62, 30, 54, 22, 3, 35, 11, 43, 1, 33,
  9, 41, 51, 19, 59, 27, 49, 17, 57, 25, 15, 47, 7, 39, 13, 45, 5, 37, 63, 31, 55, 23, 61, 29, 53, 21,
].map((v) => (v + 0.5) / 64);

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

function fbm(x: number, y: number): number {
  return noise(x, y) * 0.62 + noise(x * 2.03 + 17.1, y * 2.03 - 9.4) * 0.38;
}

function sstep(a: number, b: number, x: number): number {
  const t = Math.max(0, Math.min(1, (x - a) / (b - a)));
  return t * t * (3 - 2 * t);
}

export type BankShape = 'right' | 'bottom' | 'corner';

/** Where the bank sits: 0 = no cloud, 1 = full cloud. */
function bankMask(shape: BankShape, fx: number, fy: number): number {
  if (shape === 'right') return sstep(0.3, 0.9, fx);
  if (shape === 'bottom') return sstep(0.35, 1, fy);
  return sstep(0.5, 1, fx) * 0.7 + sstep(0.55, 1, fy) * sstep(0.3, 1, fx) * 0.6;
}

/**
 * Dither levels (0 empty, 1..3 stone dust, 4 ember fleck) for a cols × rows grid at time t.
 * Pure and deterministic for a given seed and time, so it is tested without a canvas.
 */
/** A rectangle (canvas pixels) the bank keeps clear of, fading out over 40 px: the copy it sits behind. */
export interface AvoidRect {
  l: number;
  t: number;
  r: number;
  b: number;
}

export function ditherLevels(
  cols: number,
  rows: number,
  t: number,
  opts: { shape: BankShape; seed: number; ember: number; cell: number; avoid?: readonly AvoidRect[] },
): Uint8Array {
  const out = new Uint8Array(Math.max(0, cols * rows));
  const tick = Math.floor(t * 2.2);
  const w = cols * opts.cell;
  const h = rows * opts.cell;
  for (let j = 0; j < rows; j++) {
    for (let i = 0; i < cols; i++) {
      const x = (i + 0.5) * opts.cell;
      const y = (j + 0.5) * opts.cell;
      const zone = bankMask(opts.shape, x / Math.max(1, w), y / Math.max(1, h));
      if (zone <= 0) continue;
      const bank = sstep(0.28, 0.68, fbm(x / 150 + t * 0.018 + opts.seed, y / 90 + 1.7 + opts.seed * 0.3));
      let v = Math.min(1, bank * zone * 1.2);
      for (const a of opts.avoid ?? []) {
        const ox = Math.max(a.l - x, 0, x - a.r);
        const oy = Math.max(a.t - y, 0, y - a.b);
        v *= sstep(0, 40, Math.sqrt(ox * ox + oy * oy));
      }
      if (v <= 0.01) continue;
      const bay = BAYER[(j & 7) * 8 + (i & 7)];
      const q = v * 3;
      let lv = Math.floor(q);
      if (q - lv > bay) lv++;
      if (lv <= 0) continue;
      if (lv > 3) lv = 3;
      if (lv >= 2 && opts.ember > 0 && hash(i + tick * 0.37 + opts.seed, j - tick * 0.11) > 1 - opts.ember) lv = 4;
      out[j * cols + i] = lv;
    }
  }
  return out;
}
