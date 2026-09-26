// Hand-set pixel pieces for the crew live views (owner art direction: the
// trail-brain hero): 9-px glyphs drawn cell by cell, a Bayer-dithered
// stone-dust bank that drifts while the view is live, orange pixel packets
// riding the dashed trail, and the mono status strip.
//
// Motion is rationed: packets only while live, the bank redraws at ~8 fps
// only while live and visible, and everything is still under reduced motion.

import { useEffect, useRef, type CSSProperties, type ReactNode, type RefObject } from 'react';
import clsx from 'clsx';
import { GLYPHS, ditherLevels, glyphCells, type AvoidRect, type BankShape, type GlyphName } from './dither';
import './pixels.css';

export function PixelGlyph({
  name,
  size = 18,
  className,
  title,
  mono = false,
}: {
  name: GlyphName;
  size?: number;
  className?: string;
  /** Accessible name; without it the glyph is decorative. */
  title?: string;
  /** Draw the orange cells in ink too (e.g. on an orange button). */
  mono?: boolean;
}) {
  const rows = GLYPHS[name];
  const w = Math.max(...rows.map((r) => r.length));
  const h = rows.length;
  return (
    <svg
      width={size}
      height={size}
      viewBox={`0 0 ${w} ${h}`}
      shapeRendering="crispEdges"
      className={clsx('shrink-0', className)}
      role={title ? 'img' : undefined}
      aria-label={title}
      aria-hidden={title ? undefined : true}
    >
      {glyphCells(rows).map((c) => (
        <rect key={`${c.x}-${c.y}`} x={c.x} y={c.y} width="1" height="1" fill={c.signal && !mono ? 'var(--signal)' : 'currentColor'} />
      ))}
    </svg>
  );
}

function prefersReducedMotion(): boolean {
  return typeof window !== 'undefined' && !!window.matchMedia?.('(prefers-reduced-motion: reduce)').matches;
}

/**
 * A dithered stone-dust bank behind a header. Drifts while `live` (≈8 fps, paused
 * when the tab is hidden); a still frame otherwise and under reduced motion.
 */
export function DitherBank({
  live = false,
  shape = 'right',
  seed = 1,
  ember = 0.006,
  cell = 5,
  avoid,
  minWidth = 520,
  className,
}: {
  live?: boolean;
  shape?: BankShape;
  seed?: number;
  ember?: number;
  cell?: number;
  /** Elements whose box the bank keeps clear of (the heading and copy it sits behind). */
  avoid?: RefObject<HTMLElement | null>[];
  /** Below this width (px) nothing is drawn. */
  minWidth?: number;
  className?: string;
}) {
  const ref = useRef<HTMLCanvasElement | null>(null);
  const avoidRefs = useRef(avoid);
  useEffect(() => {
    avoidRefs.current = avoid;
  });
  useEffect(() => {
    const canvas = ref.current;
    if (!canvas) return undefined;
    const ctx = canvas.getContext('2d');
    if (!ctx) return undefined;
    let raf = 0;
    let last = 0;
    let clock = seed * 3.7;
    let w = 0;
    let h = 0;
    const tones = ['', '', '', '', ''];
    const readTones = () => {
      const cs = getComputedStyle(canvas);
      tones[1] = cs.getPropertyValue('--dz1').trim() || '#e0e1da';
      tones[2] = cs.getPropertyValue('--dz2').trim() || '#d2d4cc';
      tones[3] = cs.getPropertyValue('--dz3').trim() || '#c1c4bb';
      tones[4] = cs.getPropertyValue('--dz-ember').trim() || '#ff8a52';
    };
    const resize = () => {
      const r = canvas.getBoundingClientRect();
      const dpr = Math.min(window.devicePixelRatio || 1, 2);
      w = Math.max(1, Math.round(r.width));
      h = Math.max(1, Math.round(r.height));
      canvas.width = Math.round(w * dpr);
      canvas.height = Math.round(h * dpr);
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      ctx.imageSmoothingEnabled = false;
    };
    const draw = () => {
      // On a phone the copy fills the header: no room for a bank, so no stray pixels either.
      if (w < minWidth) {
        ctx.clearRect(0, 0, w, h);
        return;
      }
      const cols = Math.ceil(w / cell);
      const rows = Math.ceil(h / cell);
      const box = canvas.getBoundingClientRect();
      const rects: AvoidRect[] = (avoidRefs.current ?? [])
        .map((r) => r.current?.getBoundingClientRect())
        .filter((r): r is DOMRect => !!r && r.width > 0)
        .map((r) => ({ l: r.left - box.left - 6, t: r.top - box.top - 6, r: r.right - box.left + 6, b: r.bottom - box.top + 6 }));
      const levels = ditherLevels(cols, rows, clock, { shape, seed, ember, cell, avoid: rects });
      ctx.clearRect(0, 0, w, h);
      for (let lv = 1; lv <= 4; lv++) {
        ctx.fillStyle = tones[lv];
        for (let k = 0; k < levels.length; k++) {
          if (levels[k] === lv) ctx.fillRect((k % cols) * cell, Math.floor(k / cols) * cell, cell, cell);
        }
      }
    };
    readTones();
    resize();
    draw();
    const animate = live && !prefersReducedMotion();
    const frame = (now: number) => {
      raf = requestAnimationFrame(frame);
      if (document.visibilityState !== 'visible') return;
      if (now - last < 125) return;
      clock += last ? (now - last) / 1000 : 0;
      last = now;
      draw();
    };
    if (animate) raf = requestAnimationFrame(frame);
    const observer = typeof ResizeObserver !== 'undefined' ? new ResizeObserver(() => {
      resize();
      draw();
    }) : null;
    observer?.observe(canvas);
    const themeObserver = new MutationObserver(() => {
      readTones();
      draw();
    });
    themeObserver.observe(document.documentElement, { attributes: true, attributeFilter: ['class', 'data-theme'] });
    return () => {
      cancelAnimationFrame(raf);
      observer?.disconnect();
      themeObserver.disconnect();
    };
  }, [live, shape, seed, ember, cell, minWidth]);
  return <canvas ref={ref} aria-hidden="true" className={clsx('crew-px crew-dither absolute inset-0 h-full w-full', className)} />;
}

// ---------------------------------------------------------------------------
// Packets on the dashed trail, and the status strip
// ---------------------------------------------------------------------------

/** Orange pixel packets riding a dashed trail; `count` 0 = just the trail. */
export function PacketTrail({ count, className, speed = 2.6 }: { count: number; className?: string; speed?: number }) {
  const n = Math.max(0, Math.min(4, Math.trunc(count)));
  return (
    <span aria-hidden="true" className={clsx('crew-trail block', className)}>
      {Array.from({ length: n }, (_, i) => (
        <span
          key={i}
          className="crew-packet"
          style={{ '--dur': `${speed}s`, '--delay': `${(-speed * i) / n}s`, '--rest': `${Math.round(((i + 1) * 100) / (n + 1))}` } as CSSProperties}
        />
      ))}
    </span>
  );
}

/** The mono status strip: a dot (solid while live), the words, and packets while traffic flows. */
export function StatusStrip({
  live,
  children,
  packets = 0,
  className,
}: {
  live: boolean;
  children: ReactNode;
  packets?: number;
  className?: string;
}) {
  return (
    <p
      className={clsx(
        'crew-px inline-flex max-w-full items-center gap-2.5 rounded-[3px] border border-rule-strong bg-panel px-3 py-1.5 font-mono text-[12px] text-ink-2 shadow-[var(--shadow)]',
        className,
      )}
    >
      <i className="crew-dot" data-live={live ? 'true' : 'false'} aria-hidden="true" />
      <span className="min-w-0 truncate">{children}</span>
      {live && packets > 0 && <PacketTrail count={packets} className="hidden w-16 shrink-0 sm:block" />}
    </p>
  );
}
