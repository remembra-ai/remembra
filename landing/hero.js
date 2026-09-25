/* ------------------------------------------------------------------
   Home hero: the brand, drawn in pixels.

   A warm pixel cloud drifts behind the hero. Out of it the brain mark and
   the wordmark condense into place, joined by a dashed trail from the
   brain's fold to the crossbar of the e in "mem". Every handoff in the
   trail demo sends the orange baton along that trail, and the retro
   handoff window beside it follows the same handoff.

   Complete on the first frame without motion: under reduced motion the
   clouds hold still and the mark is drawn fully formed. Nothing runs
   while the hero is off screen or the tab is hidden.
------------------------------------------------------------------- */
(function () {
  "use strict";

  var stage = document.getElementById("hero");
  var cv = document.getElementById("heroSky");
  var band = document.getElementById("heroMark");
  if (!stage || !cv || !band || !cv.getContext) return;
  var ctx = cv.getContext("2d");
  if (!ctx) return;

  var GEO = /*@geometry*/{"brain":"M23.29 66.18L24.77 66.95L26.33 67.62L27.67 68.1L29.56 68.65L31.12 69.02L33.29 69.44L37.4 69.95L41.78 70.19L45.64 70.16L48.24 70.03L50.81 69.8L53.34 69.48L55.9 69.06L56.34 69.07L56.87 69.23L57.24 69.49L57.47 69.73L57.69 70.12L57.78 70.44L59.44 82.63L59.64 83.48L59.89 84.09L60.35 84.81L60.97 85.48L61.71 86.01L62.53 86.39L63.18 86.56L64.08 86.64L64.99 86.54L65.8 86.29L66.65 85.83L67.52 85.06L68.08 84.28L68.53 83.15L68.67 82.31L68.67 81.65L68 76.52L68.13 75.85L68.51 75.28L68.97 74.96L69.63 74.79L71.68 75.08L73.16 75.18L74.68 75.15L76.49 74.96L77.98 74.66L79.15 74.34L80.53 73.84L81.85 73.23L83.07 72.52L84.35 71.58L85.29 70.69L86.05 69.76L86.66 68.78L87.14 67.6L87.36 66.55L87.38 65.54L87.22 64.54L86.86 63.54L83.96 63.41L81.85 63.42L79.85 63.51L77.1 63.82L74.66 64.32L73.21 64.75L71.93 65.25L70.8 65.83L69.84 66.46L69.03 67.14L68.21 68.06L67.7 68.46L67.11 68.72L66.31 68.82L65.67 68.71L65.08 68.43L64.58 68.02L64.21 67.49L64.03 67.04L63.93 66.4L64 65.75L64.23 65.15L64.93 64.24L65.52 63.62L66.94 62.42L67.64 61.94L69.03 61.13L70.78 60.35L73.45 59.5L75.29 59.09L77.33 58.77L80.63 58.49L84.39 58.46L84.75 58.14L85.93 57.48L86.73 56.95L88.21 55.71L89.56 54.23L90.33 53.16L90.87 52.28L91.48 51.08L91.89 50.12L92.33 48.83L92.59 47.82L92.79 46.8L92.95 45.46L93 44.41L92.96 43.07L92.85 42.05L92.62 40.76L92.36 39.78L91.93 38.58L91.25 37.13L90.57 36.05L89.63 34.86L88.75 33.99L87.59 33.1L86.55 32.48L85.23 31.92L83.85 31.55L82.89 32.17L81.6 33.21L80.45 34.39L79.77 35.26L79.14 36.19L78.58 37.21L77.84 38.87L77.29 40.42L77.04 40.84L76.71 41.19L76.03 41.63L75.56 41.78L75.08 41.83L74.6 41.79L74.13 41.65L73.7 41.43L73.32 41.13L73 40.75L72.71 40.18L72.57 39.54L72.61 38.89L72.94 37.81L73.51 36.32L74.13 34.99L74.88 33.64L76.24 31.7L77.74 30.02L78.86 29.01L79.97 28.15L81.33 27.27L82.34 26.74L82.26 26.39L81.97 25.32L81.58 24.27L81.09 23.23L80.51 22.23L79.84 21.25L79.08 20.31L78.24 19.42L77.3 18.56L75.97 17.52L74.89 16.79L73.74 16.13L72.54 15.53L71.29 15L70 14.54L67.33 13.84L65.02 13.49L62.71 13.36L60.41 13.46L57.99 13.8L57.67 16.18L57.1 18.8L56.32 21.32L55.33 23.75L54.55 25.31L53.71 26.76L52.13 29.06L51.78 29.4L51.37 29.67L50.92 29.85L50.44 29.95L49.8 29.92L49.33 29.79L48.89 29.57L48.39 29.16L48.1 28.77L47.84 28.18L47.74 27.7L47.77 27.05L48.04 26.29L49.07 24.79L49.84 23.55L50.52 22.28L51.13 20.97L51.66 19.62L52.11 18.23L52.73 15.6L51.98 15.96L51.42 16.04L50.98 15.97L49.17 15.43L47.56 15.15L45.91 15.06L44.24 15.15L42.61 15.43L41.03 15.89L39.53 16.53L38.12 17.33L38.06 17.37L38.7 18.72L39.22 20.08L39.83 22.34L40.17 24.68L40.22 27.08L39.99 29.53L39.7 31.19L39.56 31.66L39.33 32.09L38.9 32.58L38.36 32.94L37.75 33.15L37.27 33.19L36.78 33.15L36.31 33.01L35.89 32.78L35.51 32.47L35.2 32.09L34.86 31.36L34.78 30.55L35.16 28.21L35.25 26.94L35.25 25.72L35.16 24.53L34.98 23.38L34.7 22.26L34.31 21.11L33.45 22.53L33.01 22.88L32.49 23.07L31.93 23.09L30.41 22.83L28.89 22.68L27.34 22.65L26.28 22.7L24.74 22.87L23.22 23.16L21.72 23.57L20.26 24.09L18.86 24.71L17.92 25.2L15.75 26.6L14.12 27.94L12.29 29.82L10.7 31.93L9.38 34.21L8.33 36.67L7.7 38.74L8.42 38.93L9.4 39.07L9.97 39.09L10.68 39.01L11.09 38.9L11.63 38.65L12.6 37.87L13.02 37.63L13.64 37.44L14.29 37.42L15.07 37.62L15.74 38.07L16.16 38.57L16.47 39.32L16.53 39.8L16.5 40.29L16.31 40.91L16.07 41.33L15.75 41.7L14.95 42.39L13.6 43.22L12.12 43.77L10.56 44.03L8.96 44.02L7.01 43.7L7.04 45.72L7.27 47.88L7.69 50.01L8.32 52.08L9.13 54.09L10.13 56.01L11.3 57.82L12.76 59.65L12.95 59.21L13.23 58.81L13.58 58.46L13.98 58.2L16.34 57.23L18.48 56.57L19.69 56.28L21.93 55.87L25.23 55.49L27.49 55.34L35.05 55.02L37.14 54.86L39.17 54.62L41.16 54.28L43.09 53.8L46.07 52.78L48.13 52.22L49.82 51.88L51.85 51.61L55.2 51.44L71.05 51.46L71.83 51.7L72.49 52.17L72.8 52.54L73.02 52.97L73.21 53.76L73.13 54.57L72.96 55.02L72.7 55.44L72.37 55.79L71.97 56.07L71.53 56.27L71.05 56.39L55.23 56.41L53.4 56.47L52 56.59L50.29 56.84L48.93 57.13L47.55 57.52L44.41 58.59L43.27 58.89L40.98 59.36L38.77 59.68L36.46 59.9L27.74 60.3L23.64 60.64L21.68 60.94L19.78 61.36L17.95 61.92L16.08 62.7L17.46 63.64L19.3 64.68L21.22 65.52Z","baton":"M54.54 51.22H70.73A2.7 2.7 0 0 1 70.73 56.62H54.54A2.7 2.7 0 0 1 54.54 51.22Z","batonLine":[[54.536,53.923],[70.731,53.923]],"batonW":5.398,"brainBox":[7.0,13.364,93.0,86.636],"word":{"ink":["M5.5 94.5V33.5H19.5A15.5 15.5 0 0 1 19.5 64.5H5.5M18.5 64.5L33.5 94.5","M83.5 75A17 19.5 0 0 0 49.5 75A17 19.5 0 0 0 76.01 91.17","M49.5 75H83.5","M100.0 94.5V55.5M100.0 71A11.5 15.5 0 0 1 123.0 71V94.5M123.0 71A11.5 15.5 0 0 1 146.0 71V94.5","M196.5 75A17 19.5 0 0 0 162.5 75A17 19.5 0 0 0 189.01 91.17","M213.0 94.5V55.5M213.0 71A11.5 15.5 0 0 1 236.0 71V94.5M236.0 71A11.5 15.5 0 0 1 259.0 71V94.5","M275.5 27.5V94.5M275.5 75A17 19.5 0 0 1 309.5 75A17 19.5 0 0 1 275.5 75","M326.0 94.5V55.5M326.0 73A15 17.5 0 0 1 341.0 55.5","M388.5 75A17 19.5 0 0 0 354.5 75A17 19.5 0 0 0 388.5 75M388.5 55.5V94.5"],"sig":["M162.5 75H196.5"],"sw":11,"width":394.0,"top":22.0,"bottom":100.0},"eBar":[162.5,75.0,196.5,75.0],"lockH":{"k":1.3,"tx":-136.9,"ty":4.9},"lockHero":{"k":2.1,"tx":-217.3,"ty":-38.239},"folds":["M35.10 17.22 L35.47 17.81 L35.80 18.42 L36.11 19.03 L36.40 19.66 L36.65 20.29 L36.88 20.92 L37.09 21.57 L37.26 22.23 L37.41 22.89 L37.53 23.56 L37.63 24.24 L37.69 24.92 L37.73 25.62 L37.75 26.32 L37.73 27.03 L37.69 27.75 L37.63 28.48 L37.53 29.22 L37.41 29.96 L37.26 30.71","M55.62 12.36 L55.55 13.22 L55.46 14.06 L55.36 14.90 L55.23 15.73 L55.08 16.54 L54.90 17.35 L54.71 18.14 L54.49 18.92 L54.26 19.69 L54.00 20.45 L53.72 21.20 L53.41 21.94 L53.09 22.67 L52.74 23.39 L52.38 24.10 L51.99 24.79 L51.58 25.48 L51.15 26.16 L50.69 26.82 L50.22 27.47","M83.15 29.09 L82.56 29.43 L82.00 29.78 L81.45 30.15 L80.92 30.54 L80.41 30.95 L79.92 31.38 L79.45 31.82 L79.00 32.29 L78.57 32.77 L78.15 33.28 L77.76 33.80 L77.38 34.34 L77.02 34.90 L76.68 35.48 L76.37 36.08 L76.06 36.69 L75.78 37.33 L75.52 37.98 L75.27 38.66 L75.05 39.35","M6.49 40.97 L6.97 41.12 L7.44 41.25 L7.90 41.36 L8.35 41.44 L8.79 41.51 L9.21 41.55 L9.63 41.57 L10.03 41.57 L10.43 41.55 L10.81 41.51 L11.18 41.44 L11.54 41.36 L11.90 41.25 L12.24 41.12 L12.57 40.97 L12.88 40.80 L13.19 40.60 L13.49 40.38 L13.77 40.15 L14.05 39.89","M15.13 60.40 L16.12 59.97 L17.12 59.58 L18.13 59.25 L19.15 58.96 L20.19 58.71 L21.23 58.50 L22.27 58.33 L23.33 58.18 L24.39 58.06 L25.46 57.96 L26.53 57.89 L27.61 57.82 L28.69 57.77 L29.77 57.73 L30.86 57.69 L31.94 57.65 L33.03 57.61 L34.12 57.56 L35.20 57.50 L36.29 57.43 L37.37 57.33 L38.45 57.22 L39.53 57.08 L40.60 56.91 L41.66 56.71 L42.72 56.47 L43.77 56.19 L44.82 55.87 L45.22 55.71 L45.62 55.56 L46.02 55.42 L46.41 55.28 L46.80 55.16 L47.18 55.04 L47.57 54.93 L47.95 54.82 L48.33 54.72 L48.71 54.63 L49.09 54.55 L49.47 54.47 L49.84 54.39 L50.22 54.33 L50.59 54.27 L50.97 54.21 L51.35 54.16 L51.72 54.12 L52.10 54.08 L52.48 54.04 L52.87 54.01 L53.25 53.99 L53.64 53.97 L54.03 53.95 L54.42 53.94 L54.81 53.93 L55.21 53.93 L55.62 53.92 L70.73 53.92","M66.41 66.34 L66.84 65.83 L67.30 65.35 L67.81 64.89 L68.35 64.46 L68.94 64.05 L69.56 63.67 L70.22 63.32 L70.91 62.99 L71.64 62.68 L72.40 62.40 L73.20 62.14 L74.03 61.91 L74.90 61.70 L75.79 61.52 L76.72 61.36 L77.67 61.23 L78.66 61.12 L79.68 61.03 L80.72 60.97 L81.79 60.93 L82.89 60.92 L84.01 60.93 L85.16 60.96 L86.33 61.02 L87.52 61.10 L88.74 61.20 L89.98 61.33 L91.24 61.48"],"knock":4.966}/*@end*/;

  var mqReduce = window.matchMedia ? window.matchMedia("(prefers-reduced-motion: reduce)") : { matches: false };
  var BAYER = [0, 8, 2, 10, 12, 4, 14, 6, 3, 11, 1, 9, 15, 7, 13, 5].map(function (v) { return (v + 0.5) / 16; });
  function bay(i, j) { return BAYER[(j & 3) * 4 + (i & 3)]; }
  function clamp(v, a, b) { return v < a ? a : v > b ? b : v; }
  function sstep(a, b, x) { var t = clamp((x - a) / (b - a), 0, 1); return t * t * (3 - 2 * t); }

  /* ---------------- colors ---------------- */
  var C = {};
  function hexToRgb(h) {
    h = h.trim();
    if (h.charAt(0) === "#") h = h.slice(1);
    if (h.length === 3) h = h.replace(/./g, "$&$&");
    var n = parseInt(h, 16);
    return [(n >> 16) & 255, (n >> 8) & 255, n & 255];
  }
  function readColors() {
    var cs = getComputedStyle(document.documentElement);
    var g = function (n) { return cs.getPropertyValue(n).trim() || "#888888"; };
    C = { ink: g("--ink"), sig: g("--signal"), hi: g("--cloud-hi"), lo: g("--cloud-lo"), trail: g("--trail"), paper: g("--paper") };
    C.hiRGB = hexToRgb(C.hi); C.loRGB = hexToRgb(C.lo); C.sigRGB = hexToRgb(C.sig);
  }

  /* ---------------- layout ---------------- */
  var W = 0, H = 0, dpr = 1;
  var g = 4, cols = 0, rows = 0;              // glyph grid (CSS px per cell)
  var c = 8, ccols = 0, crows = 0;            // cloud grid
  var scale = 1, ox = 0, oy = 0;              // word units -> CSS px within the stage
  var ink = [], sigBrain = [], sigWord = [], trail = [], halo = [], cover = null;
  var avoid = [];                             // rects (cloud cells) kept clear for the copy
  var wordBox = null;                         // lockup bbox in cloud cells
  var A = null, B = null;                     // trail endpoints in glyph cells
  var glyphCv = document.createElement("canvas"), gctx = glyphCv.getContext("2d");
  var cloudCv = document.createElement("canvas"), cctx = cloudCv.getContext("2d");
  var cloudImg = null;

  var P2 = {};
  function p2(d) { return P2[d] || (P2[d] = new Path2D(d)); }

  function lockBox() {
    var L = GEO.lockHero, bb = GEO.brainBox, w = GEO.word;
    var x0 = L.tx + bb[0] * L.k, y0 = Math.min(L.ty + bb[1] * L.k, w.top);
    var x1 = w.width + w.sw / 2, y1 = Math.max(L.ty + bb[3] * L.k, w.bottom);
    return [x0, y0, x1, y1];
  }

  /* At hero scale a fold cut at the mark's own width is only a one- or
     two-cell nick, and the brain reads as a blot. The folds are cut again
     along the same centre lines, at least FOLD_CELLS cells wide, so the
     crown, the back and the cerebellum stay distinct lobes. */
  var FOLD_CELLS = 3;
  function drawLayer(cx, which) {
    var L = GEO.lockHero;
    cx.save();
    cx.lineCap = "round"; cx.lineJoin = "round"; cx.fillStyle = cx.strokeStyle = "#000";
    if (which === "ink") {
      cx.save(); cx.translate(L.tx, L.ty); cx.scale(L.k, L.k); cx.fill(p2(GEO.brain), "evenodd");
      var fold = FOLD_CELLS * g / (scale * L.k);
      if (GEO.folds && fold > GEO.knock) {
        cx.globalCompositeOperation = "destination-out";
        cx.lineWidth = fold; cx.lineCap = "butt";   // square ends stay inside the mark's own fold ends
        GEO.folds.forEach(function (d) { cx.stroke(p2(d)); });
        cx.globalCompositeOperation = "source-over"; cx.lineCap = "round";
      }
      cx.restore();
      cx.lineWidth = GEO.word.sw;
      GEO.word.ink.forEach(function (d) { cx.stroke(p2(d)); });
    } else if (which === "sigBrain") {
      cx.save(); cx.translate(L.tx, L.ty); cx.scale(L.k, L.k); cx.fill(p2(GEO.baton)); cx.restore();
    } else {
      cx.lineWidth = GEO.word.sw;
      GEO.word.sig.forEach(function (d) { cx.stroke(p2(d)); });
    }
    cx.restore();
  }

  /* Rasterise a layer into cells: supersampled coverage, kept at >= 50%. */
  function rasterise(which) {
    var SS = 3, off = document.createElement("canvas");
    off.width = cols * SS; off.height = rows * SS;
    var cx = off.getContext("2d");
    var k = SS * scale / g;
    cx.setTransform(k, 0, 0, k, SS * ox / g, SS * oy / g);
    drawLayer(cx, which);
    var data = cx.getImageData(0, 0, off.width, off.height).data;
    var out = [], cov = new Float32Array(cols * rows);
    for (var j = 0; j < rows; j++) for (var i = 0; i < cols; i++) {
      var a = 0;
      for (var y = 0; y < SS; y++) for (var x = 0; x < SS; x++) a += data[(((j * SS + y) * off.width) + i * SS + x) * 4 + 3];
      a /= SS * SS * 255;
      cov[j * cols + i] = a;
      if (a >= 0.5) out.push({ i: i, j: j, sx: 0, sy: 0, d: 0 });
    }
    return { out: out, cov: cov };
  }

  function relRect(el, pad) {
    if (!el) return null;
    var r = el.getBoundingClientRect(), s = stage.getBoundingClientRect();
    return [r.left - s.left - pad, r.top - s.top - pad, r.right - s.left + pad, r.bottom - s.top + pad];
  }

  function layout() {
    var r = stage.getBoundingClientRect();
    W = Math.max(1, Math.round(r.width)); H = Math.max(1, Math.round(r.height));
    dpr = Math.min(window.devicePixelRatio || 1, 2);
    cv.width = Math.round(W * dpr); cv.height = Math.round(H * dpr);

    // the lockup sits in the band, clear of the handoff window and the status strip
    var bandR = relRect(band, 0);
    var winEl = document.getElementById("heroWin"), stEl = document.querySelector(".hero-status");
    var x0 = bandR[0] + 16, x1 = bandR[2] - 16, y0 = bandR[1] + 8, y1 = bandR[3] - 8;
    if (winEl && getComputedStyle(winEl).position === "absolute") x0 = Math.max(x0, relRect(winEl, 0)[2] + 28);
    if (stEl && getComputedStyle(stEl).position === "absolute") {
      // the strip grows upward when a long status wraps; keep clear of two lines
      var sr = relRect(stEl, 0), scs = getComputedStyle(stEl);
      var px = function (v) { return parseFloat(v) || 0; };
      var two = 2 * (px(scs.lineHeight) || 20) + px(scs.paddingTop) + px(scs.paddingBottom) + px(scs.borderTopWidth) + px(scs.borderBottomWidth);
      y1 = Math.min(y1, sr[3] - Math.max(two, sr[3] - sr[1]) - 14);
    }
    var bw = Math.max(40, x1 - x0), bh = Math.max(40, y1 - y0);
    var box = lockBox(), lw = box[2] - box[0], lh = box[3] - box[1];
    scale = Math.min(bw * 0.94 / lw, bh * 0.8 / lh, 980 / lw);
    g = clamp(Math.round(scale * GEO.word.sw / 4.4), 2, 5);
    cols = Math.ceil(W / g); rows = Math.ceil(H / g);
    ox = Math.round((x0 + (bw - lw * scale) / 2) / g) * g - box[0] * scale;
    oy = Math.round((y0 + (bh - lh * scale) / 2) / g) * g - box[1] * scale;

    var inkR = rasterise("ink"), sbR = rasterise("sigBrain"), swR = rasterise("sigWord");
    cover = inkR.cov;
    // the baton sits on top of the ink; keep the two sets apart
    ink = inkR.out.filter(function (q) { var k = q.j * cols + q.i; return sbR.cov[k] < 0.5 && swR.cov[k] < 0.5; });
    sigBrain = sbR.out; sigWord = swR.out;

    // a one-cell ring of paper around every glyph cell, so the mark stands
    // clear of the cloud it condenses out of instead of merging with it
    var solid = new Uint8Array(cols * rows);
    [ink, sigBrain, sigWord].forEach(function (Ls) { Ls.forEach(function (q) { solid[q.j * cols + q.i] = 1; }); });
    halo = [];
    var seen = new Uint8Array(cols * rows);
    [ink, sigBrain, sigWord].forEach(function (Ls) {
      Ls.forEach(function (q) {
        for (var dj = -1; dj <= 1; dj++) for (var di = -1; di <= 1; di++) {
          var hi = q.i + di, hj = q.j + dj;
          if (hi < 0 || hj < 0 || hi >= cols || hj >= rows) continue;
          var hk = hj * cols + hi;
          if (solid[hk] || seen[hk]) continue;
          seen[hk] = 1;
          halo.push({ i: hi, j: hj });
        }
      });
    });

    // the dashed trail: from the brain's baton to the e's crossbar, only in the gaps
    var L = GEO.lockHero, bl = GEO.batonLine;
    A = toCell([L.tx + bl[1][0] * L.k, L.ty + bl[1][1] * L.k]);
    B = toCell([GEO.eBar[0], GEO.eBar[1]]);
    trail = [];
    var t = Math.max(1, Math.round(GEO.word.sw * scale / g / 4));
    var jc = Math.round(A.y - t / 2);
    for (var j = jc; j < jc + t; j++) for (var i = Math.round(A.x) + 2; i < Math.round(B.x); i++) {
      if (Math.floor(i / 2) % 2) continue;
      var k = j * cols + i;
      if (cover[k] < 0.05 && sbR.cov[k] < 0.05 && swR.cov[k] < 0.05) trail.push({ i: i, j: j });
    }

    // where the glyph cells start when they condense out of the cloud
    [ink, sigBrain, sigWord].forEach(function (Ls) {
      Ls.forEach(function (q) {
        q.sx = q.i + (Math.random() - 0.5) * cols * 0.5;
        q.sy = Math.random() < 0.65 ? rows * (0.84 + Math.random() * 0.16) : q.j - rows * (0.08 + Math.random() * 0.2);
        q.d = (q.i - (ox / g)) / (lw * scale / g) * 0.9 + Math.random() * 0.35;
      });
    });

    // clouds: coarser cells, kept away from the copy and thin behind the lettering
    c = clamp(Math.round(W / 170), 5, 9);
    ccols = Math.ceil(W / c); crows = Math.ceil(H / c);
    cloudCv.width = ccols; cloudCv.height = crows;
    cloudImg = cctx.createImageData(ccols, crows);
    glyphCv.width = cols; glyphCv.height = rows;
    avoid = [];
    [document.querySelector(".hero-copy"), document.getElementById("heroWin")].forEach(function (el) {
      var rr = relRect(el, 18);
      if (rr && rr[2] > rr[0]) avoid.push([rr[0] / c, rr[1] / c, rr[2] / c, rr[3] / c]);
    });
    wordBox = [(ox + box[0] * scale) / c, (oy + box[1] * scale) / c, (ox + box[2] * scale) / c, (oy + box[3] * scale) / c];
    bandTop = bandR[1] / c; bandBot = bandR[3] / c;
  }
  var bandTop = 0, bandBot = 0;
  function toCell(p) { return { x: (ox + p[0] * scale) / g, y: (oy + p[1] * scale) / g }; }

  /* ---------------- value-noise clouds ---------------- */
  function hash(x, y) { var h = x * 374761393 + y * 668265263; h = (h ^ (h >>> 13)) * 1274126177; return ((h ^ (h >>> 16)) >>> 0) / 4294967295; }
  function vnoise(x, y) {
    var xi = Math.floor(x), yi = Math.floor(y), xf = x - xi, yf = y - yi;
    var u = xf * xf * (3 - 2 * xf), v = yf * yf * (3 - 2 * yf);
    var a = hash(xi, yi), b = hash(xi + 1, yi), cc = hash(xi, yi + 1), d = hash(xi + 1, yi + 1);
    return a + (b - a) * u + (cc - a) * v + (a - b - cc + d) * u * v;
  }
  function fbm(x, y) { return vnoise(x, y) * 0.55 + vnoise(x * 2.1 + 5.2, y * 2.1 + 1.3) * 0.3 + vnoise(x * 4.3 + 9.1, y * 4.3 + 7.7) * 0.15; }

  function density(i, j, t) {
    var fx = i / ccols;
    var n = fbm(i / 38 + t * 0.03, j / 30 + 3.1);
    // ground: a bank of cloud along the band's lower edge, its top line rolling
    var line = bandBot - (bandBot - bandTop) * (0.08 + 0.36 * fbm(i / 24 + t * 0.02, 7.3));
    var ground = sstep(line - 3, line + 5, j) * 1.1;
    // a drift across the top of the page, heavier to the right
    var top = sstep(bandTop * 0.42, 0, j) * sstep(0.35, 0.95, fx) * 0.5;
    // a low drift along the left edge of the band
    var left = sstep(0.18, 0, fx) * sstep(bandTop - 6, bandTop + 10, j) * 0.42;
    var v = (n - 0.5) * 1.3 + ground + top + left - 0.36;
    // keep the copy and the handoff window clear
    for (var a = 0; a < avoid.length; a++) {
      var R = avoid[a];
      var dx = Math.max(R[0] - i, 0, i - R[2]), dy = Math.max(R[1] - j, 0, j - R[3]);
      v -= (1 - sstep(0, 5, Math.max(dx, dy))) * 0.9;
    }
    // and thin behind the lettering
    if (wordBox) {
      var ex = Math.max(wordBox[0] - i, 0, i - wordBox[2]), ey = Math.max(wordBox[1] - j, 0, j - wordBox[3]);
      v -= (1 - sstep(0, 6, Math.max(ex, ey))) * 0.42;
    }
    return v;
  }

  function paintClouds(t) {
    var px = cloudImg.data, hi = C.hiRGB, lo = C.loRGB, sg = C.sigRGB;
    var sparkT = Math.floor(t * 0.9);
    for (var j = 0; j < crows; j++) {
      for (var i = 0; i < ccols; i++) {
        var o = (j * ccols + i) * 4;
        var v = density(i, j, t), b = bay(i, j), col = null;
        if (v > b * 0.1) {
          // lit tops: where the cloud begins just above this cell, the sand/stone
          // light catches it; the body and undersides keep the shadow tone
          var above = density(i, j - 2, t);
          col = ((v - above) * 3.6 > 0.25 + b * 0.8) ? hi : lo;
          if (v > 0.32 && hash(i * 7 + sparkT, j * 13) > 0.9985) col = sg;   // stray orange particles
        } else if (v > -0.1 + b * 0.1) {
          col = lo;
        }
        if (col) { px[o] = col[0]; px[o + 1] = col[1]; px[o + 2] = col[2]; px[o + 3] = 255; }
        else px[o + 3] = 0;
      }
    }
    cctx.putImageData(cloudImg, 0, 0);
  }

  /* ---------------- motion state ---------------- */
  var t0 = performance.now();
  var assembleAt = null;                // seconds; the condense-from-cloud assembly
  var travelAt = null, TRAVEL = 0.9;    // the baton's run along the trail
  var dockAt = null;
  var particles = [];
  var formed = false;                   // the assembly has played (or was skipped)

  function now() { return (performance.now() - t0) / 1000; }

  function paintGlyphs(t) {
    gctx.clearRect(0, 0, cols, rows);
    var travel = travelAt != null ? clamp((t - travelAt) / TRAVEL, 0, 1) : -1;
    var ease = travel < 0 ? -1 : (travel < 0.5 ? 2 * travel * travel : 1 - Math.pow(-2 * travel + 2, 2) / 2);
    var px = ease >= 0 ? A.x + (B.x - A.x) * ease : -999;
    var assembling = assembleAt != null && t - assembleAt < 3.2;

    // the paper ring, once the mark has formed
    if (!assembling) {
      gctx.fillStyle = C.paper;
      for (var hh = 0; hh < halo.length; hh++) gctx.fillRect(halo[hh].i, halo[hh].j, 1, 1);
    }

    // dashed trail; the stretch the baton has passed glows, then fades
    for (var n = 0; n < trail.length; n++) {
      var q = trail[n], col = C.trail;
      if (ease >= 0 && q.i < px && q.i > px - 60) col = C.sig;
      if (assembling) { if (t - assembleAt < 2.2) continue; }
      gctx.fillStyle = col; gctx.fillRect(q.i, q.j, 1, 1);
    }

    function drawSet(Ls, col, hideWhenTravelling) {
      gctx.fillStyle = col;
      for (var m = 0; m < Ls.length; m++) {
        var q = Ls[m], x = q.i, y = q.j, cc = col;
        if (assembling) {
          var p = clamp((t - assembleAt - q.d) / 0.95, 0, 1);
          if (p <= 0) continue;
          var e = 1 - Math.pow(1 - p, 3);
          x = Math.round(q.sx + (q.i - q.sx) * e); y = Math.round(q.sy + (q.j - q.sy) * e);
          if (p < 0.4) cc = bay(x, y) < p * 2.2 ? col : C.lo;
          if (cc !== col) { gctx.fillStyle = cc; gctx.fillRect(x, y, 1, 1); gctx.fillStyle = col; continue; }
        } else if (hideWhenTravelling && travel >= 0 && travel < 1) {
          continue;
        }
        gctx.fillRect(x, y, 1, 1);
      }
    }
    drawSet(ink, C.ink, false);
    // the brain's baton leaves while it travels and settles back after the dock
    if (travel >= 0 && travel < 1) {
      // nothing in the fold while the baton is out
    } else {
      drawSet(sigBrain, C.sig, false);
    }
    var flash = dockAt != null ? clamp(1 - (t - dockAt) / 0.6, 0, 1) : 0;
    drawSet(sigWord, flash > 0 && (Math.floor(t * 12) % 2) ? C.lo : C.sig, false);

    // the travelling baton: a flat capsule of cells, never stepped
    if (ease >= 0 && travel < 1) {
      var bl = GEO.batonLine;
      var len = Math.max(4, Math.round((bl[1][0] - bl[0][0]) * GEO.lockHero.k * scale / g));
      var th = Math.max(2, Math.round(GEO.batonW * GEO.lockHero.k * scale / g));
      var y0 = Math.round(A.y - th / 2);
      gctx.fillStyle = C.sig;
      gctx.fillRect(Math.round(px - len / 2), y0, len, th);
      if (Math.random() < 0.8) particles.push({ x: px - len / 2, y: A.y + (Math.random() - 0.5) * th * 2.4, vx: -0.4 - Math.random() * 0.8, vy: (Math.random() - 0.5) * 0.5, life: 1 });
    }
    // orange particles shed by the baton, and sparks when it docks
    gctx.fillStyle = C.sig;
    for (var k = particles.length - 1; k >= 0; k--) {
      var pt = particles[k];
      pt.x += pt.vx; pt.y += pt.vy; pt.life -= 0.045;
      if (pt.life <= 0) { particles.splice(k, 1); continue; }
      if (bay(Math.round(pt.x), Math.round(pt.y)) < pt.life) gctx.fillRect(Math.round(pt.x), Math.round(pt.y), 1, 1);
    }
    if (flash > 0) {
      var E = toCell([(GEO.eBar[0] + GEO.eBar[2]) / 2, GEO.eBar[1]]);
      for (var s = 0; s < 16; s++) {
        var ang = s / 16 * Math.PI * 2, rr = (1 - flash) * 10 + 4;
        if (bay(s, 3) > flash + 0.1) continue;
        gctx.fillRect(Math.round(E.x + Math.cos(ang) * rr * 1.6), Math.round(E.y + Math.sin(ang) * rr), 1, 1);
      }
    }
    if (travel >= 1 && dockAt == null) dockAt = t;
    if (dockAt != null && t - dockAt > 0.7) { dockAt = null; travelAt = null; }
    if (assembleAt != null && t - assembleAt >= 3.2) { assembleAt = null; formed = true; }
  }

  function frame() {
    var t = now();
    var still = !animating();
    paintClouds(still ? 0 : t);
    paintGlyphs(t);
    ctx.setTransform(1, 0, 0, 1, 0, 0);
    ctx.clearRect(0, 0, cv.width, cv.height);
    ctx.imageSmoothingEnabled = false;
    ctx.drawImage(cloudCv, 0, 0, ccols * c * dpr, crows * c * dpr);
    ctx.drawImage(glyphCv, 0, 0, cols * g * dpr, rows * g * dpr);
  }

  /* ---------------- loop, visibility, motion preference ---------------- */
  var visible = false, raf = 0, last = 0;
  function animating() { return !mqReduce.matches; }
  function loop(ts) {
    raf = 0;
    if (!visible || document.hidden) return;
    if (ts - last > 1000 / 24) { last = ts; frame(); }
    if (animating() || assembleAt != null || travelAt != null) raf = requestAnimationFrame(loop);
  }
  function kick() { if (!raf && visible && !document.hidden) raf = requestAnimationFrame(loop); }

  function start() {
    readColors();
    layout();
    stage.classList.add("sky-on");
    if (animating() && !formed) assembleAt = now();
    else formed = true;
    frame();
    kick();
  }

  if (window.IntersectionObserver) {
    new IntersectionObserver(function (es) {
      visible = es[0].isIntersecting;
      kick();
    }, { threshold: 0 }).observe(stage);
  } else {
    visible = true;
  }
  document.addEventListener("visibilitychange", kick);
  if (mqReduce.addEventListener) mqReduce.addEventListener("change", function () { frame(); kick(); });
  window.addEventListener("remembra:theme", function () { readColors(); frame(); });
  var rt = 0;
  function relayout() { clearTimeout(rt); rt = setTimeout(function () { layout(); frame(); }, 120); }
  if (window.ResizeObserver) new ResizeObserver(relayout).observe(stage);
  else window.addEventListener("resize", relayout);

  /* ---------------- the handoff window and status strip ---------------- */
  var win = {
    title: document.getElementById("hwTitle"), id: document.getElementById("hwId"),
    from: document.getElementById("hwFrom"), to: document.getElementById("hwTo"),
    via: document.getElementById("hwVia"), facts: document.getElementById("hwFacts"),
    status: document.getElementById("hwStatus"), bar: document.getElementById("hwBar")
  };
  var statusEl = document.getElementById("heroStatus");
  function set(el, v) { if (el && el.textContent !== v) el.textContent = v; }
  function bar(p, ms) {
    if (!win.bar) return;
    win.bar.style.transition = ms ? "background-size " + ms + "ms linear" : "none";
    win.bar.style.setProperty("--p", Math.round(p * 100) + "%");
  }
  var lastSeq = -1;
  function onTrail(ev) {
    var isCp = ev.kind === "Checkpoint";
    set(win.title, ev.title);
    set(win.id, "#" + ev.id);
    set(win.from, ev.from.id + "@" + ev.from.host);
    set(win.via, ev.via);
    set(win.facts, ev.facts + " · " + ev.source);
    if (ev.still || ev.stage >= 5) {
      set(win.to, ev.to.id + "@" + ev.to.host);
      set(win.status, "picked up");
      bar(1, 0);
      set(statusEl, ev.line);
      lastSeq = ev.seq;
      return;
    }
    if (ev.stage <= 1) {
      set(win.to, "waiting");
      set(win.status, "working");
      bar(0, 0);
      set(statusEl, ev.from.id + " is working on " + ev.repo);
    } else if (ev.stage === 2) {
      set(win.status, "stopped");
      set(statusEl, ev.from.id + " stopped · " + ev.stop);
    } else if (ev.stage === 3) {
      set(win.status, "saved");
      set(statusEl, (isCp ? "last checkpoint " : "handoff ") + ev.id + " from " + ev.from.id + " · " + ev.facts + " facts, " + ev.source);
    } else if (ev.stage === 4) {
      set(win.to, ev.to.id + "@" + ev.to.host);
      set(win.status, "in transit");
      bar(0, 0);
      requestAnimationFrame(function () { bar(1, 850); });
      if (ev.seq !== lastSeq && animating() && cols) {
        lastSeq = ev.seq;
        travelAt = now();
        dockAt = null;
        kick();
      }
    }
  }

  start();
  if (window.RemembraTrail) {
    window.RemembraTrail.on(onTrail);
    window.RemembraTrail.watch(stage);
  }
  if (document.fonts && document.fonts.ready) document.fonts.ready.then(function () { layout(); frame(); });
})();
