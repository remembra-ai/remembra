// Hand-drawn pixel vignettes for the empty states. Each is authored cell by
// cell below (no generator): `#` ink, `+` soft ink, `:` dust (the dashed
// trail), `o` signal orange, `.` empty. Orange only appears where the empty
// state asks the viewer to act (§9: orange is for what moves or needs you).

export type Pixel = '#' | '+' | ':' | 'o' | '.';

export type VignetteId = 'trail' | 'runner' | 'zones' | 'receipt' | 'calm' | 'quiet';

export const VIGNETTES: Record<VignetteId, readonly string[]> = {
  // No crews: the baton waits over step 1 of a three-step trail.
  trail: [
    '....oo.....................',
    '...ooo.....................',
    '..ooo......................',
    '.ooo.......................',
    '.oo........................',
    '...........................',
    '###.........+++.........+++',
    '###.:.:.:.:.+.+.:.:.:.:.+.+',
    '###.........+++.........+++',
  ],
  // One agent: a runner on the first lane, the second lane empty and waiting.
  runner: [
    ':.:.:.:.:.:.##.............',
    ':.:.:.:.:.:.##.............',
    '...........................',
    '+.+.+.+.+.+.+.+.+.+.+.+.+.+',
    'o..........................',
    '+.+.+.+.+.+.+.+.+.+.+.+.+.+',
  ],
  // No zones: three dashed plots, one tagged as temporary.
  zones: [
    '+.+.+.+.+..+.+.+.+..+.+.+.oo',
    '.........+........+......+oo',
    '+.........................+.',
    '.........+........+.........',
    '+.+.+.+.+..+.+.+.+..+.+.+.+.',
  ],
  // Empty board: a receipt with a torn edge and an empty seal.
  receipt: [
    '.#########.....',
    '.#.......#.....',
    '.#.+++++.#.....',
    '.#.......#..++.',
    '.#.+++...#.+..+',
    '.#.......#.+..+',
    '.#.#.#.#.#..++.',
  ],
  // Nothing needs you: agents resting on a level trail.
  calm: [
    '....##..........##.......##..',
    '....##..........##.......##..',
    ':.:.:.:.:.:.:.:.:.:.:.:.:.:.:',
  ],
  // Empty feed: the trail with nothing on it yet.
  quiet: [
    '.........................+.',
    ':.:.:.:.:.:.:.:.:.:.:.:.:+.',
    '.........................+.',
  ],
};

export interface PixelRun {
  x: number;
  y: number;
  w: number;
  pixel: Exclude<Pixel, '.'>;
}

/** Horizontal runs of one colour (one SVG rect each). */
export function pixelRuns(rows: readonly string[]): { width: number; height: number; runs: PixelRun[] } {
  const width = Math.max(0, ...rows.map((r) => r.length));
  const runs: PixelRun[] = [];
  rows.forEach((row, y) => {
    let x = 0;
    while (x < row.length) {
      const ch = row[x] as Pixel;
      if (ch === '.') {
        x += 1;
        continue;
      }
      let end = x + 1;
      while (end < row.length && row[end] === ch) end += 1;
      runs.push({ x, y, w: end - x, pixel: ch as PixelRun['pixel'] });
      x = end;
    }
  });
  return { width, height: rows.length, runs };
}

export const PIXEL_FILL: Record<PixelRun['pixel'], string> = {
  '#': 'var(--ink)',
  '+': 'var(--ink-3)',
  ':': 'var(--rule)',
  o: 'var(--signal)',
};
