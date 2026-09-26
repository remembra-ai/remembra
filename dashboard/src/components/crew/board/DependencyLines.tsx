// Dependency lines (§9.7): dashed trail curves from a task to the tasks
// waiting on it, drawn over the board grid. Paths are written straight into
// the SVG after layout (no React state per frame); the hovered or focused
// card's lines are inked darker. Decorative: the same facts are on each card
// as text ("after T-12").

import { useLayoutEffect, useRef } from 'react';
import type { DependencyEdge } from './model';

const SVG_NS = 'http://www.w3.org/2000/svg';

export function DependencyLines({
  edges,
  hot,
  layoutKey,
}: {
  edges: DependencyEdge[];
  /** The hovered or focused task: its lines are emphasised. */
  hot: string | null;
  /** Changes whenever the cards move (forces a redraw). */
  layoutKey: string;
}) {
  const svgRef = useRef<SVGSVGElement>(null);

  useLayoutEffect(() => {
    // The grid is the SVG's parent. (A parent's ref is not attached yet when a child's
    // layout effect first runs, so the parent element is read from the DOM instead.)
    const svg = svgRef.current;
    const grid = svg?.parentElement;
    if (!grid || !svg) return undefined;
    const draw = () => {
      while (svg.firstChild) svg.removeChild(svg.firstChild);
      const box = grid.getBoundingClientRect();
      svg.setAttribute('width', String(grid.scrollWidth));
      svg.setAttribute('height', String(grid.scrollHeight));
      const rectOf = (id: string) => {
        const el = grid.querySelector<HTMLElement>(`[data-task-id="${CSS.escape(id)}"]`);
        if (!el) return null;
        const r = el.getBoundingClientRect();
        return { l: r.left - box.left, r: r.right - box.left, t: r.top - box.top, b: r.bottom - box.top };
      };
      for (const e of edges) {
        const a = rectOf(e.from);
        const b = rectOf(e.to);
        if (!a || !b) continue;
        let d: string;
        if (Math.abs(a.l - b.l) < 4) {
          // same column: loop out on the right edge
          const x = a.r - 2;
          const y1 = a.t + 14;
          const y2 = b.t + 14;
          const bulge = 14 + Math.min(40, Math.abs(y2 - y1) / 6);
          d = `M${x} ${y1} C${x + bulge} ${y1} ${x + bulge} ${y2} ${x} ${y2}`;
        } else {
          const leftToRight = a.l < b.l;
          const x1 = leftToRight ? a.r : a.l;
          const x2 = leftToRight ? b.l : b.r;
          const y1 = a.t + 14;
          const y2 = b.t + 14;
          const dx = (x2 - x1) / 2;
          d = `M${x1} ${y1} C${x1 + dx} ${y1} ${x2 - dx} ${y2} ${x2} ${y2}`;
        }
        const path = document.createElementNS(SVG_NS, 'path');
        path.setAttribute('d', d);
        path.setAttribute('data-open', String(!e.satisfied));
        if (hot && (hot === e.from || hot === e.to)) path.setAttribute('data-hot', 'true');
        svg.appendChild(path);
        // arrival tick: a 3px square where the line lands
        const [ex, ey] = d.split(' ').slice(-2).map(Number);
        const tick = document.createElementNS(SVG_NS, 'rect');
        tick.setAttribute('x', String(ex - 1.5));
        tick.setAttribute('y', String(ey - 1.5));
        tick.setAttribute('width', '3');
        tick.setAttribute('height', '3');
        tick.setAttribute('fill', hot && (hot === e.from || hot === e.to) ? 'var(--ink)' : 'var(--ink-3)');
        svg.appendChild(tick);
      }
    };
    draw();
    const ro = new ResizeObserver(draw);
    ro.observe(grid);
    return () => ro.disconnect();
  }, [edges, hot, layoutKey]);

  return <svg ref={svgRef} className="cb-deps" aria-hidden="true" />;
}
