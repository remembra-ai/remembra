// The brand panel on the sign-in screens: the pixel brain and pixel wordmark
// condense out of a warm dithered cloud, sit above pixel terrain, and a flat
// orange baton travels from the brain's fold to the crossbar of the e. Canvas,
// 24 fps, paused when hidden; a single still frame under reduced motion.

import { useEffect, useRef } from 'react';
import { LOCKUP_H, LOCKUP_S, MARK_BATON, MARK_INK, WORD_INK, WORD_SIG, WORD_SW, AXIS_Y, BATON_X } from './geometry';
import { bayer, fbm, hash2, onThemeChange, readTones, sstep, watchReducedMotion, type PixelTones } from './pixel';

type Layer = 'ink' | 'sigBrain' | 'sigWord';

interface Comp {
  brain: { tx: number; ty: number; k: number };
  view: readonly number[];
  horizontal: boolean;
}

interface GlyphCell {
  i: number;
  j: number;
  sx: number;
  sy: number;
  d: number;
}

const COMP_H: Comp = { brain: LOCKUP_H, view: LOCKUP_H.viewBox, horizontal: true };
const COMP_S: Comp = { brain: LOCKUP_S, view: LOCKUP_S.viewBox, horizontal: false };

const pathCache = new Map<string, Path2D>();
function p2(d: string): Path2D {
  let path = pathCache.get(d);
  if (!path) {
    path = new Path2D(d);
    pathCache.set(d, path);
  }
  return path;
}

function drawLayer(c: CanvasRenderingContext2D, comp: Comp, which: Layer) {
  c.save();
  c.fillStyle = '#000';
  c.strokeStyle = '#000';
  c.lineCap = 'round';
  c.lineJoin = 'round';
  if (which === 'ink') {
    c.save();
    c.translate(comp.brain.tx, comp.brain.ty);
    c.scale(comp.brain.k, comp.brain.k);
    c.fill(p2(MARK_INK), 'evenodd');
    c.restore();
    c.lineWidth = WORD_SW;
    for (const d of WORD_INK) c.stroke(p2(d));
  } else if (which === 'sigBrain') {
    c.translate(comp.brain.tx, comp.brain.ty);
    c.scale(comp.brain.k, comp.brain.k);
    c.fill(p2(MARK_BATON));
  } else {
    c.lineWidth = WORD_SW;
    for (const d of WORD_SIG) c.stroke(p2(d));
  }
  c.restore();
}

export function BrandHero({ className }: { className?: string }) {
  const canvasRef = useRef<HTMLCanvasElement>(null);

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return undefined;
    const ctx = canvas.getContext('2d');
    if (!ctx) return undefined;

    let tones: PixelTones = readTones();
    let W = 1;
    let H = 1;
    let dpr = 1;
    let cell = 4;
    let cols = 1;
    let rows = 1;
    let ccell = 6;
    let ccols = 1;
    let crows = 1;
    let scale = 1;
    let ox = 0;
    let oy = 0;
    let comp: Comp = COMP_S;
    let ink: GlyphCell[] = [];
    let sigBrain: GlyphCell[] = [];
    let sigWord: GlyphCell[] = [];
    let trail: { i: number; j: number }[] = [];
    let batonStart = { x: 0, y: 0 };
    let batonEnd = { x: 0, y: 0 };
    let reduced = false;
    let visible = true;
    let raf = 0;
    let lastDraw = 0;
    const t0 = performance.now();

    const toCell = (x: number, y: number) => ({ x: (ox + x * scale) / cell, y: (oy + y * scale) / cell });

    function rasterise(which: Layer) {
      const SS = 3;
      const off = document.createElement('canvas');
      off.width = cols * SS;
      off.height = rows * SS;
      const c = off.getContext('2d');
      const out: GlyphCell[] = [];
      if (!c) return out;
      const s = (SS * scale) / cell;
      c.setTransform(s, 0, 0, s, (SS * ox) / cell, (SS * oy) / cell);
      drawLayer(c, comp, which);
      const data = c.getImageData(0, 0, off.width, off.height).data;
      for (let j = 0; j < rows; j++) {
        for (let i = 0; i < cols; i++) {
          let a = 0;
          for (let y = 0; y < SS; y++) for (let x = 0; x < SS; x++) a += data[((j * SS + y) * off.width + i * SS + x) * 4 + 3];
          if (a / (SS * SS * 255) >= 0.5) out.push({ i, j, sx: 0, sy: 0, d: 0 });
        }
      }
      return out;
    }

    function layout() {
      const rect = canvas!.getBoundingClientRect();
      W = Math.max(1, Math.round(rect.width));
      H = Math.max(1, Math.round(rect.height));
      dpr = Math.min(window.devicePixelRatio || 1, 2);
      canvas!.width = Math.round(W * dpr);
      canvas!.height = Math.round(H * dpr);
      comp = W / H > 1.6 ? COMP_H : COMP_S;
      const [vx, vy, vw, vh] = comp.view;
      scale = Math.min((W * 0.78) / vw, (H * 0.5) / vh);
      cell = Math.max(3, Math.min(6, Math.round((scale * WORD_SW) / 3.2)));
      cols = Math.ceil(W / cell);
      rows = Math.ceil(H / cell);
      ccell = Math.max(5, Math.round(W / 120));
      ccols = Math.ceil(W / ccell);
      crows = Math.ceil(H / ccell);
      ox = Math.round((W - vw * scale) / 2 / cell) * cell - vx * scale;
      oy = Math.round((H * 0.4 - (vh * scale) / 2) / cell) * cell - vy * scale;
      ink = rasterise('ink');
      sigBrain = rasterise('sigBrain');
      sigWord = rasterise('sigWord');
      // Assembly sources: scattered through the cloud bands (deterministic).
      let n = 0;
      for (const list of [ink, sigBrain, sigWord]) {
        for (const c of list) {
          n += 1;
          const band = hash2(n, 7) < 0.6 ? rows * (0.72 + hash2(n, 11) * 0.26) : rows * hash2(n, 13) * 0.2;
          c.sx = hash2(n, 17) * cols;
          c.sy = band;
          c.d = (c.i / cols) * 0.9 + hash2(n, 19) * 0.35;
        }
      }
      // The baton's path: brain fold -> the e crossbar (brain space -> word space).
      const b = comp.brain;
      batonStart = toCell(b.tx + b.k * BATON_X[1], b.ty + b.k * AXIS_Y);
      const bar = /M([\d.]+) ([\d.]+)H([\d.]+)/.exec(WORD_SIG[0]);
      const ex = bar ? (Number(bar[1]) + Number(bar[3])) / 2 : 180;
      const ey = bar ? Number(bar[2]) : 75;
      batonEnd = toCell(ex, ey);
      trail = [];
      if (comp.horizontal) {
        const inkSet = new Set(ink.map((c) => c.j * cols + c.i));
        const j = Math.round(batonStart.y);
        for (let i = Math.round(batonStart.x) + 2; i < Math.round(batonEnd.x); i++) {
          if ((i >> 1) % 2) continue;
          if (!inkSet.has(j * cols + i) && !inkSet.has(j * cols + i + 1)) trail.push({ i, j });
        }
      }
    }

    function fill(i: number, j: number, color: string, size: number) {
      ctx!.fillStyle = color;
      const x0 = Math.round(i * size);
      const y0 = Math.round(j * size);
      ctx!.fillRect(x0, y0, Math.round((i + 1) * size) - x0, Math.round((j + 1) * size) - y0);
    }

    function density(i: number, j: number, t: number) {
      const fx = i / ccols;
      const fy = j / crows;
      const n = fbm(fx * 3.4 + t * 0.03, fy * 2.4 + 3.1);
      const bottom = sstep(0.55, 1.0, fy) * 0.75;
      const top = sstep(0.28, 0, fy) * sstep(0.2, 0.95, fx) * 0.5;
      let v = (n - 0.5) * 1.6 + bottom + top - 0.32;
      // keep the lettering clear
      const [vx, vy, vw, vh] = comp.view;
      const cx = (i * ccell - ox) / scale;
      const cy = (j * ccell - oy) / scale;
      const dx = Math.max(vx - cx, 0, cx - (vx + vw));
      const dy = Math.max(vy - cy, 0, cy - (vy + vh));
      v -= (1 - sstep(0, 8, (Math.hypot(dx, dy) * scale) / ccell)) * 0.5;
      return v;
    }

    function terrain(i: number, t: number) {
      // A low pixel ridge along the bottom, drifting slowly.
      const fx = i / ccols;
      return crows * (0.86 - 0.07 * fbm(fx * 2.2 + t * 0.01 + 11, 4.2) - 0.03 * Math.sin(fx * 6.3));
    }

    const PERIOD = 7.2;
    const START = 3.0;
    const TRAVEL = 1.8;

    function frame(now: number) {
      const t = (now - t0) / 1000;
      const tc = reduced ? 0 : t;
      const cd = cell * dpr;
      const ccd = ccell * dpr;
      ctx!.fillStyle = tones.paper;
      ctx!.fillRect(0, 0, canvas!.width, canvas!.height);

      // Clouds: lit tops, dithered spruce bellies, warm peach undersides.
      for (let j = 0; j < crows; j++) {
        for (let i = 0; i < ccols; i++) {
          const v = density(i, j, tc);
          if (v < -0.1) continue;
          const b = bayer(i, j);
          if (v > b * 0.1) {
            const below = density(i, j + 4, tc);
            const shade = (v - below) * 4.5;
            const warm = shade > 0.55 + b * 0.5 && j / crows > 0.45;
            fill(i, j, warm ? tones.cloudWarm : shade > 0.15 + b * 0.7 ? tones.cloudLo : tones.cloudHi, ccd);
            if (v > 0.3 && hash2(i * 7 + Math.floor(tc * 0.8), j * 13) > 0.9993) fill(i, j, tones.signal, ccd);
          } else if (v > -0.1 + b * 0.1) {
            fill(i, j, tones.cloudLo, ccd);
          }
        }
      }
      // Terrain ridge: lit crest, dithered slope, spruce body.
      for (let i = 0; i < ccols; i++) {
        const top = terrain(i, tc);
        for (let j = Math.floor(top); j < crows; j++) {
          const depth = j - top;
          if (depth < 1) fill(i, j, tones.cloudHi, ccd);
          else if (depth < 4) fill(i, j, bayer(i, j) < 0.5 ? tones.cloudWarm : tones.cloudLo, ccd);
          else fill(i, j, tones.cloudLo, ccd);
        }
      }

      // Ambient handoff: the baton leaves the fold and docks on the e.
      let pk = -1;
      let flash = 0;
      if (!reduced && t > START) {
        const local = (t - START) % PERIOD;
        if (local < TRAVEL) {
          const u = local / TRAVEL;
          pk = u < 0.5 ? 2 * u * u : 1 - Math.pow(-2 * u + 2, 2) / 2;
        } else if (local < TRAVEL + 0.6) flash = 1 - (local - TRAVEL) / 0.6;
      }

      for (const c of trail) {
        const lit = pk >= 0 && c.i < batonStart.x + (batonEnd.x - batonStart.x) * pk && c.i > batonStart.x + (batonEnd.x - batonStart.x) * pk - 40;
        fill(c.i, c.j, lit ? tones.signal : tones.rule, cd);
      }

      const assembling = !reduced && t < 3.2;
      const drawSet = (list: GlyphCell[], color: string) => {
        for (const c of list) {
          let x = c.i;
          let y = c.j;
          let col = color;
          if (assembling) {
            const p = Math.min(1, Math.max(0, (t - 0.2 - c.d) / 1.1));
            if (p <= 0) continue;
            const e = 1 - Math.pow(1 - p, 3);
            x = Math.round(c.sx + (c.i - c.sx) * e);
            y = Math.round(c.sy + (c.j - c.sy) * e);
            if (p < 0.4) col = bayer(x, y) < p * 2.2 ? color : tones.cloudHi;
          }
          fill(x, y, col, cd);
        }
      };
      drawSet(ink, tones.ink);
      drawSet(sigBrain, tones.signal);
      drawSet(sigWord, flash > 0 && Math.floor(t * 12) % 2 ? tones.paper : tones.signal);

      // The travelling baton: always a flat run of cells, never stepped.
      if (pk >= 0 && comp.horizontal) {
        const x = batonStart.x + (batonEnd.x - batonStart.x) * pk;
        const y = Math.round(batonStart.y + (batonEnd.y - batonStart.y) * pk);
        const len = Math.max(4, Math.round((24 * scale) / cell));
        const thick = Math.max(2, Math.round((5 * scale) / cell));
        for (let dx = 0; dx < len; dx++) for (let dy = 0; dy < thick; dy++) fill(Math.round(x - len / 2) + dx, y - Math.floor(thick / 2) + dy, tones.signal, cd);
      }
      if (flash > 0) {
        for (let s = 0; s < 12; s++) {
          if (bayer(s, 3) > flash + 0.1) continue;
          const ang = (s / 12) * Math.PI * 2;
          const rr = (1 - flash) * 14 + 4;
          fill(Math.round(batonEnd.x + Math.cos(ang) * rr * 1.4), Math.round(batonEnd.y + Math.sin(ang) * rr), tones.signal, cd);
        }
      }
    }

    function loop(now: number) {
      raf = 0;
      if (reduced || !visible) return;
      if (now - lastDraw > 1000 / 24) {
        lastDraw = now;
        frame(now);
      }
      raf = requestAnimationFrame(loop);
    }
    const kick = () => {
      if (!raf && !reduced && visible) raf = requestAnimationFrame(loop);
    };
    const redraw = () => frame(performance.now());

    layout();
    const stopMotion = watchReducedMotion((r) => {
      reduced = r;
      redraw();
      kick();
    });
    const stopTheme = onThemeChange(() => {
      tones = readTones();
      redraw();
    });
    let resizeTimer = 0;
    const ro = new ResizeObserver(() => {
      window.clearTimeout(resizeTimer);
      resizeTimer = window.setTimeout(() => {
        layout();
        redraw();
      }, 80);
    });
    ro.observe(canvas);
    const io = new IntersectionObserver((entries) => {
      visible = entries[0]?.isIntersecting ?? true;
      kick();
    });
    io.observe(canvas);
    const onVis = () => {
      visible = !document.hidden;
      kick();
    };
    document.addEventListener('visibilitychange', onVis);
    kick();

    return () => {
      if (raf) cancelAnimationFrame(raf);
      window.clearTimeout(resizeTimer);
      stopMotion();
      stopTheme();
      ro.disconnect();
      io.disconnect();
      document.removeEventListener('visibilitychange', onVis);
    };
  }, []);

  return <canvas ref={canvasRef} className={className} role="img" aria-label="Remembra: the pixel brain and wordmark condensing out of a memory cloud" />;
}
