/* ------------------------------------------------------------------
   Constellation: every agent, on every machine, around one memory.

   The agent nodes are real buttons laid over a canvas. The canvas draws
   the dotted pixel links to the memory at the centre, a small drifting
   cloud around it, and the handoff packet: out of the agent that stopped,
   into the memory, and on to the agent that picks up. It follows the same
   handoff as the trail demo in the hero (through RemembraTrail), and a tap
   on a node hands the work to that agent there too.

   Starts only when the section comes near the screen, stops when it
   leaves, and under reduced motion shows the latest handoff as a still.
------------------------------------------------------------------- */
(function () {
  "use strict";

  var box = document.getElementById("constellation");
  var cv = document.getElementById("constSky");
  if (!box || !cv || !cv.getContext) return;
  var ctx = cv.getContext("2d");
  var core = box.querySelector(".core");
  var nodes = Array.prototype.slice.call(box.querySelectorAll("[data-agent]"));
  var statusEl = document.getElementById("constStatus");
  var mqReduce = window.matchMedia ? window.matchMedia("(prefers-reduced-motion: reduce)") : { matches: false };

  var BAYER = [0, 8, 2, 10, 12, 4, 14, 6, 3, 11, 1, 9, 15, 7, 13, 5].map(function (v) { return (v + 0.5) / 16; });
  function bay(i, j) { return BAYER[(j & 3) * 4 + (i & 3)]; }
  function clamp(v, a, b) { return v < a ? a : v > b ? b : v; }

  /* Machines, as 12 x 12 pixel icons. */
  var ICONS = {
    laptop: "M2 2h8v1H2zM2 3h1v4H2zM9 3h1v4H9zM2 7h8v1H2zM0 9h12v1H0zM1 8h10v1H1z",
    desktop: "M1 1h10v1H1zM1 2h1v5H1zM10 2h1v5h-1zM1 7h10v1H1zM5 8h2v2H5zM3 10h6v1H3z",
    cloud: "M5 2h3v1H5zM4 3h1v1H4zM8 3h1v2H8zM2 4h2v1H2zM9 5h2v1H9zM1 5h1v1H1zM0 6h1v3H0zM11 6h1v3h-1zM1 9h10v1H1z",
    server: "M1 1h10v1H1zM1 2h1v2H1zM10 2h1v2h-1zM1 4h10v1H1zM3 2h1v1H3zM1 6h10v1H1zM1 7h1v2H1zM10 7h1v2h-1zM1 9h10v1H1zM3 7h1v1H3z"
  };
  function machineOf(host) {
    if (/laptop/i.test(host)) return "laptop";
    if (/cloud|vm/i.test(host)) return "cloud";
    if (/server/i.test(host)) return "server";
    return "desktop";
  }
  function setHost(node, host) {
    var h = node.querySelector(".host"), ic = node.querySelector(".pix path");
    if (h && h.textContent !== host) h.textContent = host;
    if (ic) ic.setAttribute("d", ICONS[machineOf(host)]);
    node.setAttribute("data-host", host);
  }
  nodes.forEach(function (n) { setHost(n, n.getAttribute("data-host") || "laptop"); });

  /* ---------------- colors and layout ---------------- */
  var C = {}, W = 0, H = 0, dpr = 1, g = 3, cols = 0, rows = 0;
  var pts = {}, corePt = null;
  var buf = document.createElement("canvas"), bctx = buf.getContext("2d");
  function readColors() {
    var cs = getComputedStyle(document.documentElement);
    var v = function (n) { return cs.getPropertyValue(n).trim(); };
    C = { ink: v("--ink"), ink3: v("--ink-3"), sig: v("--signal"), hi: v("--cloud-hi"), lo: v("--cloud-lo"), trail: v("--trail") };
  }
  function center(el) {
    var r = el.getBoundingClientRect(), b = box.getBoundingClientRect();
    return { x: (r.left + r.width / 2 - b.left) / g, y: (r.top + r.height / 2 - b.top) / g, w: r.width / g, h: r.height / g };
  }
  function layout() {
    var r = box.getBoundingClientRect();
    W = Math.max(1, Math.round(r.width)); H = Math.max(1, Math.round(r.height));
    dpr = Math.min(window.devicePixelRatio || 1, 2);
    g = W < 520 ? 2 : 3;
    cols = Math.ceil(W / g); rows = Math.ceil(H / g);
    cv.width = Math.round(W * dpr); cv.height = Math.round(H * dpr);
    buf.width = cols; buf.height = rows;
    corePt = center(core);
    pts = {};
    nodes.forEach(function (n) { pts[n.getAttribute("data-agent")] = center(n); });
  }

  /* A link from a node's edge to the memory's edge, as grid cells. */
  function line(a, b) {
    var out = [], dx = b.x - a.x, dy = b.y - a.y, len = Math.hypot(dx, dy) || 1;
    var ux = dx / len, uy = dy / len;
    // start outside the node chip and stop short of the core
    // leave the chip where the ray crosses its edge
    var s0 = Math.min(len, (a.w / 2) / Math.max(0.001, Math.abs(ux)), (a.h / 2) / Math.max(0.001, Math.abs(uy))) + 3;
    var s1 = len - Math.max(b.w, b.h) * 0.45;
    for (var s = s0; s < s1; s += 1) out.push({ x: Math.round(a.x + ux * s), y: Math.round(a.y + uy * s), s: s - s0 });
    return out;
  }

  /* ---------------- state from the trail ---------------- */
  var from = null, to = null, kind = "Handoff";
  var leg1 = null, leg2 = null, LEG = 0.85;   // packet legs: from -> memory, memory -> to
  var done = false, particles = [];
  var t0 = performance.now();
  function now() { return (performance.now() - t0) / 1000; }
  function nodeFor(agent) { return nodes.filter(function (n) { return n.getAttribute("data-agent") === agent; })[0]; }
  function mark(agent, cls, on) { var n = nodeFor(agent); if (n) n.classList.toggle(cls, on); }
  function setState(n, text) { var st = n && n.querySelector(".st"); if (st && st.textContent !== text) st.textContent = text; }

  var lastSeq = -1;
  function onTrail(ev) {
    var fresh = ev.seq !== lastSeq;
    if (fresh) {
      nodes.forEach(function (n) { n.classList.remove("is-from", "is-to"); setState(n, ""); });
      lastSeq = ev.seq; leg1 = leg2 = null; done = false;
    }
    from = ev.from.agent; to = ev.to.agent; kind = ev.kind;
    var fn = nodeFor(from), tn = nodeFor(to);
    if (fn) setHost(fn, ev.from.host);
    mark(from, "is-from", true);
    if (ev.still || ev.stage >= 5) {
      if (tn) setHost(tn, ev.to.host);
      mark(to, "is-to", true);
      setState(fn, ev.kind === "Checkpoint" ? "offline" : "stopped");
      setState(tn, "picked up");
      done = true;
      if (statusEl) statusEl.textContent = ev.line;
    } else if (ev.stage === 2) {
      setState(fn, ev.kind === "Checkpoint" ? "offline" : "stopped");
      if (statusEl) statusEl.textContent = ev.from.id + " stopped · " + ev.stop;
    } else if (ev.stage === 3) {
      setState(fn, ev.kind === "Checkpoint" ? "checkpoint" : "handed off");
      if (running()) leg1 = now();
    } else if (ev.stage === 4) {
      if (tn) setHost(tn, ev.to.host);
      if (running()) { leg2 = now(); if (leg1 == null) leg1 = leg2 - LEG; }
    } else if (ev.stage <= 1) {
      setState(fn, "working");
      if (statusEl) statusEl.textContent = ev.from.id + " is working on " + ev.repo;
    }
    if (ev.still) paint();
    kick();
  }

  /* ---------------- painting ---------------- */
  function hash(x, y) { var h = x * 374761393 + y * 668265263; h = (h ^ (h >>> 13)) * 1274126177; return ((h ^ (h >>> 16)) >>> 0) / 4294967295; }
  function vnoise(x, y) {
    var xi = Math.floor(x), yi = Math.floor(y), xf = x - xi, yf = y - yi;
    var u = xf * xf * (3 - 2 * xf), v = yf * yf * (3 - 2 * yf);
    var a = hash(xi, yi), b = hash(xi + 1, yi), c = hash(xi, yi + 1), d = hash(xi + 1, yi + 1);
    return a + (b - a) * u + (c - a) * v + (a - b - c + d) * u * v;
  }

  function cloud(t) {
    // the one memory: a small warm cloud around the core that never quite holds still
    var R = Math.max(corePt.w, corePt.h) * 0.95;
    var x0 = Math.floor(corePt.x - R * 2.6), x1 = Math.ceil(corePt.x + R * 2.6);
    var y0 = Math.floor(corePt.y - R * 1.9), y1 = Math.ceil(corePt.y + R * 1.9);
    for (var j = y0; j < y1; j++) for (var i = x0; i < x1; i++) {
      var dx = (i - corePt.x) / (R * 1.5), dy = (j - corePt.y) / (R * 1.05);
      var d = Math.sqrt(dx * dx + dy * dy);
      var v = (1 - d) * 1.3 + (vnoise(i / 9 + t * 0.25, j / 7) - 0.5) * 0.8;
      if (v <= 0.05) continue;
      // a quiet pocket for the brain and its label, rounded, not a box
      var px = (i - corePt.x) / (corePt.w * 0.5), py = (j - corePt.y) / (corePt.h * 0.55);
      if (px * px + py * py < 1) continue;
      var b = bay(i, j);
      if (v > 0.25 + b * 0.35) {
        var by = (j + 3 - corePt.y) / (R * 1.05);
        var below = (1 - Math.sqrt(dx * dx + by * by)) * 1.3;
        bctx.fillStyle = (v - below > 0.02 + b * 0.1) ? C.lo : C.hi;
        bctx.fillRect(i, j, 1, 1);
      } else if (v > 0.05 + b * 0.25) {
        bctx.fillStyle = C.lo; bctx.fillRect(i, j, 1, 1);
      }
    }
  }

  function packet(path, k, trailLen) {
    if (!path.length) return null;
    var e = k < 0.5 ? 2 * k * k : 1 - Math.pow(-2 * k + 2, 2) / 2;
    var idx = Math.min(path.length - 1, Math.floor(e * (path.length - 1)));
    var p = path[idx];
    bctx.fillStyle = C.sig;
    for (var q = Math.max(0, idx - trailLen); q < idx; q++) {
      if (bay(path[q].x, path[q].y) < (q - idx + trailLen) / trailLen) bctx.fillRect(path[q].x, path[q].y, 1, 1);
    }
    bctx.fillRect(p.x - 3, p.y - 1, 7, 3);   // the baton: flat, three cells tall
    if (Math.random() < 0.7) particles.push({ x: p.x, y: p.y, vx: (Math.random() - 0.5) * 0.8, vy: (Math.random() - 0.5) * 0.8, life: 1 });
    return p;
  }

  function paint() {
    if (!corePt) return;
    var t = running() ? now() : 0;
    bctx.clearRect(0, 0, cols, rows);
    cloud(t);
    // links: dotted, and orange where this handoff travelled
    var lit1 = done || leg1 != null, lit2 = done || (leg2 != null && now() - leg2 >= LEG);
    nodes.forEach(function (n) {
      var agent = n.getAttribute("data-agent"), path = line(pts[agent], corePt);
      var hot = (agent === from && lit1) || (agent === to && lit2);
      var dashed = n.classList.contains("any");
      for (var s = 0; s < path.length; s++) {
        var p = path[s];
        if (hot) { bctx.fillStyle = C.sig; if (s % 3 === 2) continue; }
        else { bctx.fillStyle = C.trail; if (dashed ? (Math.floor(s / 3) % 2) : (s % 3 !== 0)) continue; }
        bctx.fillRect(p.x, p.y, 1, 1);
      }
    });
    // the packet
    if (running() && !done) {
      var tt = now();
      if (leg2 != null && tt - leg2 < LEG && pts[to]) packet(line(pts[to], corePt).reverse(), (tt - leg2) / LEG, 14);
      else if (leg1 != null && tt - leg1 < LEG && pts[from]) packet(line(pts[from], corePt), (tt - leg1) / LEG, 14);
    }
    bctx.fillStyle = C.sig;
    for (var k = particles.length - 1; k >= 0; k--) {
      var pt = particles[k];
      pt.x += pt.vx; pt.y += pt.vy; pt.life -= 0.05;
      if (pt.life <= 0) { particles.splice(k, 1); continue; }
      if (bay(Math.round(pt.x), Math.round(pt.y)) < pt.life) bctx.fillRect(Math.round(pt.x), Math.round(pt.y), 1, 1);
    }
    ctx.setTransform(1, 0, 0, 1, 0, 0);
    ctx.clearRect(0, 0, cv.width, cv.height);
    ctx.imageSmoothingEnabled = false;
    ctx.drawImage(buf, 0, 0, cols * g * dpr, rows * g * dpr);
  }

  /* ---------------- lifecycle ---------------- */
  var started = false, visible = false, raf = 0, last = 0;
  function running() { return !mqReduce.matches; }
  function loop(ts) {
    raf = 0;
    if (!visible || document.hidden) return;
    if (ts - last > 1000 / 24) { last = ts; paint(); }
    if (running()) raf = requestAnimationFrame(loop);
  }
  function kick() { if (started && !raf && visible && !document.hidden) raf = requestAnimationFrame(loop); }
  function start() {
    if (started) return;
    started = true;
    readColors(); layout(); paint();
    if (window.RemembraTrail) window.RemembraTrail.on(onTrail);
    kick();
  }

  nodes.forEach(function (n) {
    if (n.tagName !== "BUTTON") return;
    n.addEventListener("click", function () {
      if (window.RemembraTrail) window.RemembraTrail.handOff(n.getAttribute("data-agent"));
    });
  });

  if (window.IntersectionObserver) {
    new IntersectionObserver(function (es) {
      es.forEach(function (e) { if (e.isIntersecting) start(); });
    }, { rootMargin: "240px 0px" }).observe(box);
    new IntersectionObserver(function (es) {
      visible = es[0].isIntersecting;
      kick();
    }, { threshold: 0 }).observe(box);
  } else {
    visible = true; start();
  }
  if (window.RemembraTrail) window.RemembraTrail.watch(box);
  document.addEventListener("visibilitychange", kick);
  window.addEventListener("remembra:theme", function () { if (started) { readColors(); paint(); } });
  if (mqReduce.addEventListener) mqReduce.addEventListener("change", function () { if (started) { paint(); kick(); } });
  var rt = 0;
  function relayout() { if (!started) return; clearTimeout(rt); rt = setTimeout(function () { layout(); paint(); }, 120); }
  if (window.ResizeObserver) new ResizeObserver(relayout).observe(box);
  else window.addEventListener("resize", relayout);
})();
