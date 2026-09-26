// Hand-set 7×7 pixel marks for zone states: drawn cell by cell for this
// product (no icon set). Always paired with a text label: status never
// depends on the mark or its colour alone (§9).

import type { ZonePrimary } from './zoneModel';

type GlyphName = ZonePrimary | 'pending' | 'policy' | 'folder';

// '#' = ink cell, '+' = accent cell (orange or red by state), '.' = empty
const BITMAPS: Record<GlyphName, string[]> = {
  free: ['.......', '.#.#.#.', '.......', '.#...#.', '.......', '.#.#.#.', '.......'],
  held: ['#######', '#######', '##...##', '##...##', '##...##', '#######', '#######'],
  shared: ['#..#..#', '..#..#.', '.#..#..', '#..#..#', '..#..#.', '.#..#..', '#..#..#'],
  watched: ['.......', '..###..', '.#...#.', '#..+..#', '.#...#.', '..###..', '.......'],
  reserved: ['.....++', '....+++', '...+++.', '..+++..', '.+++...', '#++....', '##.....'],
  contested: ['###.+++', '###.+++', '###.+++', '.......', '+++.###', '+++.###', '+++.###'],
  frozen: ['...#...', '.#.#.#.', '..###..', '#######', '..###..', '.#.#.#.', '...#...'],
  breach: ['+.....+', '.+...+.', '..+.+..', '...+...', '..+.+..', '.+...+.', '+.....+'],
  pending: ['#######', '.#...#.', '..#+#..', '...#...', '..#.#..', '.#+++#.', '#######'],
  policy: ['..###..', '.#...#.', '.#...#.', '#######', '###.###', '###.###', '#######'],
  folder: ['.......', '###....', '#######', '#.....#', '#.....#', '#######', '.......'],
};

const ACCENT: Partial<Record<GlyphName, string>> = {
  reserved: 'var(--signal)',
  contested: 'var(--signal)',
  pending: 'var(--signal)',
  watched: 'var(--signal)',
  breach: 'var(--fail)',
};

const INK: Partial<Record<GlyphName, string>> = {
  free: 'var(--ink-3)',
  folder: 'var(--ink-3)',
  frozen: 'var(--ink-2)',
};

export function PixelGlyph({ name, size = 12, title }: { name: GlyphName; size?: number; title?: string }) {
  const rows = BITMAPS[name];
  const ink = INK[name] ?? 'var(--ink)';
  const accent = ACCENT[name] ?? ink;
  const cells: { x: number; y: number; fill: string }[] = [];
  rows.forEach((row, y) => {
    for (let x = 0; x < row.length; x++) {
      if (row[x] === '#') cells.push({ x, y, fill: ink });
      else if (row[x] === '+') cells.push({ x, y, fill: accent });
    }
  });
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 7 7"
      shapeRendering="crispEdges"
      aria-hidden={title ? undefined : true}
      role={title ? 'img' : undefined}
      aria-label={title}
      className="inline-block shrink-0"
    >
      {cells.map((c) => (
        <rect key={`${c.x}-${c.y}`} x={c.x} y={c.y} width="1" height="1" fill={c.fill} />
      ))}
    </svg>
  );
}
