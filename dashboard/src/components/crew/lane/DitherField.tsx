// A dithered pixel cloud painted behind crew headers and pickup slots (the
// brand hero's stone-dust cloud, in square 6 px cells with rare ember
// flecks). Decorative only: aria-hidden, no pointer events. It drifts slowly
// while visible and stays still with reduced motion or in a hidden tab.

import { useEffect, useRef } from 'react';
import clsx from 'clsx';
import { cloudDensity, emberAt, toneLevel, tonesFor, type CloudShape } from './dither';

const CELL = 6;
const FRAME_MS = 420;

function prefersReducedMotion(): boolean {
  return typeof window !== 'undefined' && !!window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches;
}

export function DitherField({
  shape = 'banks',
  seed = 0,
  animate = true,
  embers = true,
  className,
}: {
  shape?: CloudShape;
  seed?: number;
  animate?: boolean;
  embers?: boolean;
  className?: string;
}) {
  const ref = useRef<HTMLCanvasElement | null>(null);

  useEffect(() => {
    const canvas = ref.current;
    if (!canvas) return;
    const ctx = canvas.getContext('2d');
    if (!ctx) return;
    let w = 0;
    let h = 0;
    let t = seed * 7;
    let timer: number | null = null;
    const still = !animate || prefersReducedMotion();

    const draw = () => {
      const tones = tonesFor(!!canvas.closest('.dark'));
      ctx.clearRect(0, 0, w, h);
      const cols = Math.ceil(w / CELL);
      const rows = Math.ceil(h / CELL);
      const tick = Math.floor(t * 2.2);
      for (let j = 0; j < rows; j += 1) {
        for (let i = 0; i < cols; i += 1) {
          const v = cloudDensity(shape, (i + 0.5) * CELL, (j + 0.5) * CELL, w, h, t, seed);
          const level = toneLevel(v, i, j);
          if (level <= 0) continue;
          ctx.fillStyle = embers && level >= 2 && emberAt(i, j, tick) ? tones.ember : tones.levels[level - 1];
          ctx.fillRect(i * CELL, j * CELL, CELL, CELL);
        }
      }
    };

    const resize = () => {
      const rect = canvas.getBoundingClientRect();
      const dpr = Math.min(window.devicePixelRatio || 1, 2);
      w = Math.max(1, Math.round(rect.width));
      h = Math.max(1, Math.round(rect.height));
      canvas.width = Math.round(w * dpr);
      canvas.height = Math.round(h * dpr);
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      ctx.imageSmoothingEnabled = false;
      draw();
    };

    const loop = () => {
      timer = window.setTimeout(() => {
        if (document.visibilityState === 'visible') {
          t += FRAME_MS / 1000;
          draw();
        }
        loop();
      }, FRAME_MS);
    };

    const observer = typeof ResizeObserver !== 'undefined' ? new ResizeObserver(resize) : null;
    observer?.observe(canvas);
    resize();
    if (!still) loop();
    // A theme switch changes the tones: repaint when the `dark` class toggles on
    // <html> or on the app root (App.tsx wraps the shell in <div class="dark">).
    const themeObserver = new MutationObserver(draw);
    const targets = [document.documentElement, document.getElementById('root')?.firstElementChild ?? null];
    for (const target of targets) {
      if (target) themeObserver.observe(target, { attributes: true, attributeFilter: ['class'] });
    }
    return () => {
      if (timer !== null) window.clearTimeout(timer);
      observer?.disconnect();
      themeObserver.disconnect();
    };
  }, [shape, seed, animate, embers]);

  return (
    <canvas
      ref={ref}
      aria-hidden="true"
      className={clsx('pointer-events-none absolute inset-0 h-full w-full [image-rendering:pixelated]', className)}
    />
  );
}
