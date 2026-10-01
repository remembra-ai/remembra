// Marshal's mark: two hollow stations on a dashed rail with the signal baton
// in flight between them, drawn cell by cell on a 12px pixel grid (the grid
// PixelHandoff draws the first handoff on). Ink is currentColor; only the
// baton is signal orange.

const STATIONS: readonly (readonly [number, number])[] = [
  // left station: a hollow 3×3 node
  [0, 6], [1, 6], [2, 6], [0, 7], [2, 7], [0, 8], [1, 8], [2, 8],
  // right station
  [9, 6], [10, 6], [11, 6], [9, 7], [11, 7], [9, 8], [10, 8], [11, 8],
  // the dashed rail between them
  [4, 7], [7, 7],
];

const INK_PATH = STATIONS.map(([x, y]) => `M${x} ${y}h1v1h-1z`).join('');

export function TrailMark({ className, title }: { className?: string; title?: string }) {
  return (
    <svg
      viewBox="0 0 12 12"
      width={12}
      height={12}
      shapeRendering="crispEdges"
      className={className}
      role={title ? 'img' : undefined}
      aria-hidden={title ? undefined : true}
      focusable="false"
    >
      {title && <title>{title}</title>}
      <path d={INK_PATH} fill="currentColor" />
      <rect x={4} y={3} width={4} height={2} fill="var(--signal)" />
    </svg>
  );
}
