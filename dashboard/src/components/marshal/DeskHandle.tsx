import { useRef } from 'react';
import { clampHeight, nextHeight, type HeightBounds } from '../../lib/marshalDesk';

/**
 * The resize handle: a 6px dither strip under the bar. Drag it, or focus it
 * and use ArrowUp/ArrowDown (24px) and Home/End (smallest, tallest).
 */
export function DeskHandle({
  height,
  bounds,
  onResize,
}: {
  height: number;
  bounds: HeightBounds;
  /** `commit`: the drag ended (or a key moved it): store the height. */
  onResize: (height: number, commit: boolean) => void;
}) {
  const drag = useRef<{ pointer: number; y: number; height: number } | null>(null);
  return (
    <div
      role="separator"
      aria-orientation="horizontal"
      aria-label="Resize Marshal"
      aria-valuemin={bounds.min}
      aria-valuemax={bounds.max}
      aria-valuenow={height}
      aria-valuetext={`${height} pixels tall`}
      tabIndex={0}
      className="rr-desk-handle relative shrink-0 after:absolute after:inset-x-0 after:-bottom-1.5 after:-top-1.5 after:content-['']"
      onPointerDown={(event) => {
        if (event.button !== 0) return;
        event.currentTarget.setPointerCapture(event.pointerId);
        drag.current = { pointer: event.pointerId, y: event.clientY, height };
      }}
      onPointerMove={(event) => {
        const start = drag.current;
        if (!start || start.pointer !== event.pointerId) return;
        onResize(clampHeight(start.height + (start.y - event.clientY), bounds), false);
      }}
      onPointerUp={(event) => {
        const start = drag.current;
        if (!start || start.pointer !== event.pointerId) return;
        drag.current = null;
        onResize(clampHeight(start.height + (start.y - event.clientY), bounds), true);
      }}
      onPointerCancel={() => {
        drag.current = null;
      }}
      onKeyDown={(event) => {
        const next = nextHeight(event.key, height, bounds);
        if (next === null) return;
        event.preventDefault();
        onResize(next, true);
      }}
    />
  );
}
