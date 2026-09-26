// A dithered pixel cloud band behind the board header: stone-dust banks that
// drift slowly, with the odd ember fleck. When a task moves on the live
// stream, an ember ring blooms under the column it moved into, so the header
// itself says "something just happened over there". Decorative only
// (aria-hidden); paused while the tab is hidden; static with reduced motion.

import { useEffect, useRef } from 'react';
import { bayer, fbm, hash2, prefersReducedMotion, readTones, sstep, type BoardTones } from './pixel';

const CELL = 5;

export interface CloudBurst {
  /** Unique per burst (a new id starts a new ring). */
  id: string;
  /** Horizontal position as a fraction of the band width. */
  x: number;
}

export function DitherCloud({ burst, className }: { burst: CloudBurst | null; className?: string }) {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const burstRef = useRef<{ x: number; start: number } | null>(null);
  const lastBurstId = useRef<string | null>(null);

  useEffect(() => {
    if (!burst || burst.id === lastBurstId.current) return;
    lastBurstId.current = burst.id;
    burstRef.current = { x: burst.x, start: performance.now() };
  }, [burst]);

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return undefined;
    const ctx = canvas.getContext('2d');
    if (!ctx) return undefined;
    const board = canvas.closest('.crew-board') ?? document.documentElement;
    let tones: BoardTones = readTones(board);
    let w = 1;
    let h = 1;
    let raf = 0;
    let last = 0;
    const reduced = prefersReducedMotion();
    const t0 = performance.now();

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

    const draw = (now: number) => {
      const t = reduced ? 0 : (now - t0) / 1000;
      ctx.clearRect(0, 0, w, h);
      const cols = Math.ceil(w / CELL);
      const rows = Math.ceil(h / CELL);
      const palette = [null, tones.d1, tones.d2, tones.d3];
      const tick = Math.floor(t * 2);
      const b = burstRef.current;
      const age = b ? (now - b.start) / 1000 : 99;
      const ring = age < 1.4 && !reduced ? { cx: b!.x * w, rad: 10 + age * 160, a: 1 - age / 1.4 } : null;
      for (let j = 0; j < rows; j += 1) {
        for (let i = 0; i < cols; i += 1) {
          const x = (i + 0.5) * CELL;
          const y = (j + 0.5) * CELL;
          const th = bayer(i, j);
          if (ring) {
            const d = Math.hypot(x - ring.cx, (y - h) * 1.6);
            const v = Math.exp(-(((d - ring.rad) / 14) ** 2)) * ring.a;
            if (v > th) {
              ctx.fillStyle = tones.ember;
              ctx.fillRect(i * CELL, j * CELL, CELL, CELL);
              continue;
            }
          }
          // banks: heavy at the bottom edge, a second bank drifting in from the right
          const bank = sstep(0.46, 0.8, fbm(x / 150 + t * 0.02 + 3.1, y / 60 + 1.7));
          const zone = sstep(0.25, 1, y / h) * 0.95 + sstep(0.55, 1, x / w) * sstep(0.9, 0.1, y / h) * 0.45;
          const v = bank * 0.85 * zone;
          if (v <= 0.02) continue;
          const q = v * 3;
          let lv = Math.floor(q);
          if (q - lv > th) lv += 1;
          if (lv <= 0) continue;
          if (lv > 3) lv = 3;
          ctx.fillStyle = lv >= 2 && hash2(i + tick * 7, j - tick * 3) > 0.996 ? tones.ember : (palette[lv] as string);
          ctx.fillRect(i * CELL, j * CELL, CELL, CELL);
        }
      }
    };

    const loop = (now: number) => {
      raf = requestAnimationFrame(loop);
      const burstLive = burstRef.current && now - burstRef.current.start < 1500;
      if (now - last < (burstLive ? 1000 / 30 : 1000 / 8)) return;
      last = now;
      draw(now);
    };

    resize();
    draw(performance.now());
    const ro = new ResizeObserver(() => {
      resize();
      draw(performance.now());
    });
    ro.observe(canvas);
    const mo = new MutationObserver(() => {
      tones = readTones(board);
      draw(performance.now());
    });
    mo.observe(document.documentElement, { attributes: true, attributeFilter: ['class'] });
    const onVisibility = () => {
      cancelAnimationFrame(raf);
      if (!document.hidden && !reduced) raf = requestAnimationFrame(loop);
    };
    document.addEventListener('visibilitychange', onVisibility);
    if (!reduced) raf = requestAnimationFrame(loop);
    return () => {
      cancelAnimationFrame(raf);
      ro.disconnect();
      mo.disconnect();
      document.removeEventListener('visibilitychange', onVisibility);
    };
  }, []);

  return <canvas ref={canvasRef} aria-hidden="true" className={className ?? 'cb-cloud'} />;
}
