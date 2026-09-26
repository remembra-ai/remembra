// The live band behind a crew screen's header: stone-dust pixel clouds drifting
// slowly, a dashed trail along the bottom, and an orange pixel packet running
// the trail for every new event (seq). Decorative (aria-hidden); the same facts
// are in the live status strip as text. Reduced motion: still clouds, no packets.

import { useEffect, useRef } from 'react';
import { CELL, cloudLevel, isEmber, launchPackets, packetColumn, type Packet } from './dither';

function prefersReducedMotion(): boolean {
  return typeof window !== 'undefined' && !!window.matchMedia?.('(prefers-reduced-motion: reduce)').matches;
}

export function DitherField({ seq, className }: { seq: number; className?: string }) {
  const canvasRef = useRef<HTMLCanvasElement | null>(null);
  const packets = useRef<Packet[]>([]);
  const lastSeq = useRef<number | null>(null);
  const kick = useRef<() => void>(() => {});

  useEffect(() => {
    const canvas = canvasRef.current;
    const ctx = canvas?.getContext('2d');
    if (!canvas || !ctx) return undefined;
    const reduced = prefersReducedMotion();
    let w = 0;
    let h = 0;
    let raf = 0;
    const started = performance.now();
    let tones = { d1: '', d2: '', d3: '', ember: '', trail: '', signal: '' };

    const readTones = () => {
      const cs = getComputedStyle(canvas);
      const v = (name: string) => cs.getPropertyValue(name).trim();
      tones = { d1: v('--cz-d1'), d2: v('--cz-d2'), d3: v('--cz-d3'), ember: v('--cz-ember'), trail: v('--cz-trail'), signal: v('--signal') };
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

    const draw = (now: number) => {
      const t = reduced ? 0 : (now - started) / 1000;
      const cols = Math.ceil(w / CELL);
      const rows = Math.ceil(h / CELL);
      const tick = Math.floor(t * 2);
      const fill = [null, tones.d1, tones.d2, tones.d3];
      ctx.clearRect(0, 0, w, h);
      for (let j = 0; j < rows; j++) {
        for (let i = 0; i < cols; i++) {
          const level = cloudLevel(i, j, cols, rows, t);
          if (!level) continue;
          ctx.fillStyle = isEmber(i, j, level, tick) ? tones.ember : (fill[level] as string);
          ctx.fillRect(i * CELL, j * CELL, CELL, CELL);
        }
      }
      // the trail: dashed, two cells on, one off, one cell above the bottom edge
      const trailRow = rows - 2;
      ctx.fillStyle = tones.trail;
      for (let i = 0; i < cols; i++) if (i % 3 !== 2) ctx.fillRect(i * CELL, trailRow * CELL + CELL / 3, CELL, CELL / 3);
      // packets
      packets.current = packets.current.filter((p) => now - p.born <= p.duration);
      ctx.fillStyle = tones.signal;
      for (const p of packets.current) {
        const col = packetColumn(p, now, cols);
        if (col === null) continue;
        ctx.fillRect(col * CELL, (trailRow - 0.5) * CELL, CELL * 2, CELL);
        ctx.globalAlpha = 0.35;
        ctx.fillRect((col - 1) * CELL, (trailRow - 0.25) * CELL, CELL, CELL / 2);
        ctx.globalAlpha = 1;
      }
    };

    let slow: number | undefined;
    const loop = (now: number) => {
      draw(now);
      raf = packets.current.length ? requestAnimationFrame(loop) : 0;
    };
    kick.current = () => {
      if (!raf && !document.hidden) raf = requestAnimationFrame(loop);
    };

    readTones();
    resize();
    draw(performance.now());
    const ro = new ResizeObserver(() => {
      resize();
      draw(performance.now());
    });
    ro.observe(canvas);
    // theme changes flip html.dark: re-read the tones
    const mo = new MutationObserver(() => {
      readTones();
      draw(performance.now());
    });
    mo.observe(document.documentElement, { attributes: true, attributeFilter: ['class', 'data-theme'] });
    if (!reduced) {
      slow = window.setInterval(() => {
        if (!document.hidden && !raf) draw(performance.now());
      }, 280);
    }
    return () => {
      cancelAnimationFrame(raf);
      window.clearInterval(slow);
      ro.disconnect();
      mo.disconnect();
      kick.current = () => {};
    };
  }, []);

  useEffect(() => {
    const prev = lastSeq.current;
    lastSeq.current = seq;
    if (prev === null || seq <= prev || prefersReducedMotion()) return;
    packets.current.push(...launchPackets(prev, seq, performance.now()));
    kick.current();
  }, [seq]);

  return <canvas ref={canvasRef} aria-hidden="true" className={className} />;
}
