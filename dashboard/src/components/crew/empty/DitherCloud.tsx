// A dithered pixel cloud behind a live view or an empty state. Decorative
// (aria-hidden). Drifts slowly while on screen; drawn once and held still
// for reduced motion, off-screen, or a hidden tab. `pulse` (any changing
// number) sends an ember ring through it: the live feed pulses on arrivals.

import { useEffect, useRef } from 'react';
import clsx from 'clsx';
import { useCrewMotion } from '../../../lib/motion';
import { CELL, cloudCells, tonesFrom, type Burst, type CloudShape, type CloudTones, type Rect } from './dither';

function readTones(el: HTMLElement): CloudTones {
  const cs = getComputedStyle(el);
  const get = (name: string, fallback: string) => cs.getPropertyValue(name).trim() || fallback;
  return tonesFrom({
    paper2: get('--paper-2', '#e2e4de'),
    rule: get('--rule', '#c9ccc5'),
    signal: get('--signal', '#ff5b14'),
    panel: get('--panel', '#fafaf7'),
  });
}

export function DitherCloud({
  shape = 'right',
  className,
  avoidRef,
  pulse,
  pulseOrigin = 'right',
}: {
  shape?: CloudShape;
  className?: string;
  /** Keep the cloud thin behind this element (the text). */
  avoidRef?: React.RefObject<HTMLElement | null>;
  pulse?: number;
  pulseOrigin?: 'right' | 'left';
}) {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const motion = useCrewMotion();
  const state = useRef({ t: 0, burst: null as (Burst & { start: number }) | null, visible: true, tones: '' as string, lastPulse: pulse });
  const drawRef = useRef<() => void>(() => {});

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return undefined;
    const ctx = canvas.getContext('2d');
    if (!ctx) return undefined;
    let w = 0;
    let h = 0;
    let tones = readTones(canvas);

    const avoidRect = (): Rect | null => {
      const target = avoidRef?.current;
      if (!target) return null;
      const a = target.getBoundingClientRect();
      const c = canvas.getBoundingClientRect();
      return { l: a.left - c.left - 6, t: a.top - c.top - 6, r: a.right - c.left + 6, b: a.bottom - c.top + 6 };
    };

    const draw = () => {
      const now = performance.now();
      const s = state.current;
      const nextTones = readTones(canvas);
      if (nextTones.fills.join() !== tones.fills.join()) tones = nextTones;
      let burst: Burst | null = null;
      if (s.burst) {
        const age = (now - s.burst.start) / 1000;
        if (age > 1.2) s.burst = null;
        else burst = { ...s.burst, rad: 6 + age * 110, a: Math.max(0, 0.85 - age * 0.7) };
      }
      const { cols, rows, cells } = cloudCells(w, h, s.t, shape, avoidRect(), burst);
      ctx.clearRect(0, 0, w, h);
      for (let lv = 1; lv <= 4; lv += 1) {
        ctx.fillStyle = tones.fills[lv];
        for (let j = 0; j < rows; j += 1) {
          for (let i = 0; i < cols; i += 1) {
            if (cells[j * cols + i] === lv) ctx.fillRect(i * CELL, j * CELL, CELL, CELL);
          }
        }
      }
    };
    drawRef.current = draw;

    const resize = () => {
      const r = canvas.getBoundingClientRect();
      const dpr = Math.min(window.devicePixelRatio || 1, 2);
      w = Math.max(1, Math.round(r.width));
      h = Math.max(1, Math.round(r.height));
      canvas.width = Math.round(w * dpr);
      canvas.height = Math.round(h * dpr);
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      ctx.imageSmoothingEnabled = false;
      draw();
    };
    resize();
    const ro = typeof ResizeObserver !== 'undefined' ? new ResizeObserver(resize) : null;
    ro?.observe(canvas);
    const io =
      typeof IntersectionObserver !== 'undefined'
        ? new IntersectionObserver((entries) => {
            state.current.visible = entries.some((e) => e.isIntersecting);
          })
        : null;
    io?.observe(canvas);

    let raf = 0;
    let last = 0;
    const loop = (ts: number) => {
      raf = requestAnimationFrame(loop);
      const s = state.current;
      const animating = (motion.ambient || s.burst) && s.visible && document.visibilityState === 'visible';
      if (!animating || ts - last < 125) return; // ≤8 fps: a slow drift, not a screensaver
      if (motion.ambient) s.t += (ts - (last || ts)) / 1000;
      last = ts;
      draw();
    };
    raf = requestAnimationFrame(loop);
    return () => {
      cancelAnimationFrame(raf);
      ro?.disconnect();
      io?.disconnect();
    };
  }, [shape, avoidRef, motion.ambient]);

  // Theme switches re-render the tree: redraw so the dust follows the palette.
  useEffect(() => {
    drawRef.current();
  });

  useEffect(() => {
    const s = state.current;
    if (pulse === undefined || pulse === s.lastPulse) return;
    s.lastPulse = pulse;
    if (motion.reduced) return;
    const canvas = canvasRef.current;
    if (!canvas) return;
    const r = canvas.getBoundingClientRect();
    s.burst = { x: pulseOrigin === 'right' ? r.width - 24 : 24, y: r.height / 2, rad: 8, wid: 16, a: 0.9, start: performance.now() };
  }, [pulse, pulseOrigin, motion.reduced]);

  return <canvas ref={canvasRef} aria-hidden="true" className={clsx('pointer-events-none absolute inset-0 h-full w-full [image-rendering:pixelated]', className)} />;
}
