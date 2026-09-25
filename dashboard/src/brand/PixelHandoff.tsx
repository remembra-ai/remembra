// A one-shot pixel scene for the first handoff: the brain hands its flat
// orange baton down a dashed pixel trail to the agent's chip, which lights
// up with sparks. Plays once; under reduced motion it draws the end state.

import { useEffect, useRef } from 'react';
import { AXIS_Y, BATON_X, MARK_BOX, MARK_INK } from './geometry';
import { bayer, readTones, watchReducedMotion } from './pixel';

const CHIP = ['..###..', '.#####.', '#######', '#######', '#######', '.#####.', '..###..'];

export function PixelHandoff({ agentColor, className }: { agentColor: string; className?: string }) {
  const ref = useRef<HTMLCanvasElement>(null);

  useEffect(() => {
    const canvas = ref.current;
    const ctx = canvas?.getContext('2d');
    if (!canvas || !ctx) return undefined;
    const tones = readTones(canvas);
    // Drawn on the always-dark head band: ink is the element's text colour.
    const ink = getComputedStyle(canvas).color || tones.ink;
    const rect = canvas.getBoundingClientRect();
    const W = Math.max(1, Math.round(rect.width));
    const H = Math.max(1, Math.round(rect.height));
    const dpr = Math.min(window.devicePixelRatio || 1, 2);
    canvas.width = Math.round(W * dpr);
    canvas.height = Math.round(H * dpr);
    const cell = 3;
    const cols = Math.ceil(W / cell);
    const rows = Math.ceil(H / cell);

    // Rasterise the brain to cells, sized to the strip height.
    const [bx0, by0, , by1] = MARK_BOX;
    const k = ((rows - 2) * cell) / (by1 - by0);
    const ox = 16 - bx0 * k;
    const oy = cell - by0 * k;
    const off = document.createElement('canvas');
    off.width = cols;
    off.height = rows;
    const oc = off.getContext('2d');
    const brain: [number, number][] = [];
    if (oc) {
      oc.setTransform(k / cell, 0, 0, k / cell, ox / cell, oy / cell);
      oc.fill(new Path2D(MARK_INK), 'evenodd');
      const data = oc.getImageData(0, 0, cols, rows).data;
      for (let j = 0; j < rows; j += 1) for (let i = 0; i < cols; i += 1) if (data[(j * cols + i) * 4 + 3] > 110) brain.push([i, j]);
    }
    const axis = Math.round((oy + AXIS_Y * k) / cell);
    const start = Math.round((ox + BATON_X[0] * k) / cell);
    const batonLen = Math.max(4, Math.round(((BATON_X[1] - BATON_X[0]) * k) / cell));
    const chipX = cols - 12;
    const chipY = axis - 3;
    const travelFrom = start;
    const travelTo = chipX - batonLen - 1;

    const fill = (i: number, j: number, color: string) => {
      ctx.fillStyle = color;
      ctx.fillRect(Math.round(i * cell * dpr), Math.round(j * cell * dpr), Math.ceil(cell * dpr), Math.ceil(cell * dpr));
    };

    const draw = (p: number, spark: number) => {
      ctx.clearRect(0, 0, canvas.width, canvas.height);
      for (const [i, j] of brain) fill(i, j, ink);
      for (let i = start + batonLen + 2; i < chipX - 1; i += 1) if ((i >> 1) % 2 === 0) fill(i, axis, tones.ink3);
      const x = Math.round(travelFrom + (travelTo - travelFrom) * p);
      for (let dx = 0; dx < batonLen; dx += 1) {
        fill(x + dx, axis - 1, tones.signal);
        fill(x + dx, axis, tones.signal);
      }
      CHIP.forEach((row, j) => {
        for (let i = 0; i < row.length; i += 1) if (row[i] === '#') fill(chipX + i, chipY + j, p >= 1 ? agentColor : tones.ink3);
      });
      if (spark > 0) {
        for (let s = 0; s < 12; s += 1) {
          if (bayer(s, 2) > spark + 0.1) continue;
          const a = (s / 12) * Math.PI * 2;
          const r = (1 - spark) * 7 + 5;
          fill(Math.round(chipX + 3 + Math.cos(a) * r * 1.3), Math.round(chipY + 3 + Math.sin(a) * r), tones.signal);
        }
      }
    };

    let raf = 0;
    const stop = watchReducedMotion((reduced) => {
      if (raf) cancelAnimationFrame(raf);
      raf = 0;
      if (reduced) {
        draw(1, 0);
        return;
      }
      const t0 = performance.now();
      const step = (now: number) => {
        const t = (now - t0) / 1000;
        const u = Math.min(1, Math.max(0, (t - 0.3) / 1.4));
        const eased = u < 0.5 ? 2 * u * u : 1 - Math.pow(-2 * u + 2, 2) / 2;
        const spark = t > 1.7 ? Math.max(0, 1 - (t - 1.7) / 0.8) : 0;
        draw(eased, spark);
        if (t < 2.6) raf = requestAnimationFrame(step);
        else raf = 0;
      };
      raf = requestAnimationFrame(step);
    });
    return () => {
      if (raf) cancelAnimationFrame(raf);
      stop();
    };
  }, [agentColor]);

  return <canvas ref={ref} aria-hidden="true" className={className} />;
}
