/* ------------------------------------------------------------------
   Home page trail demo ("git log for agents").

   The markup ships the complete still frame of the first scenario, so
   the page never shows an empty terminal: that frame is what renders
   without JavaScript, with reduced motion, and before any animation.

   Four scenarios (tabs) replay one agent stopping and the next one
   picking up. Tapping an agent's name hands the work off to a
   different agent on a different machine, continuing the same trail.
------------------------------------------------------------------- */
(function () {
  "use strict";

  var relay = document.getElementById("relay");
  if (!relay) return;

  var AGENTS = ["Claude Code", "OpenAI Codex", "Cursor", "Gemini CLI", "Qwen Code", "Kimi CLI"];
  var AGENT_ID = {
    "Claude Code": "claude-code", "OpenAI Codex": "codex", "Cursor": "cursor",
    "Gemini CLI": "gemini-cli", "Qwen Code": "qwen-code", "Kimi CLI": "kimi"
  };
  var HOSTS = ["studio-laptop", "home-desktop", "cloud VM", "build-server", "office-pc", "work-laptop"];

  function pad(n) { return (n < 10 ? "0" : "") + n; }
  /* t is minutes since midnight of the scenario's first day */
  function clock(t) {
    var day = Math.floor(t / 1440), m = t % 1440;
    var hm = pad(Math.floor(m / 60)) + ":" + pad(m % 60);
    if (day === 0) return hm;
    if (day === 1) return "next day " + hm;
    return "day " + (day + 1) + " " + hm;
  }
  function ago(mins) {
    if (mins < 1) return "just now";
    if (mins < 60) return mins + "m ago";
    var h = Math.floor(mins / 60), m = mins % 60;
    return h + "h" + (m ? pad(m) + "m" : "") + " ago";
  }
  function money(v) { return "$" + v.toFixed(2); }
  function pct(v) { return Math.round(v) + "%"; }

  /* Every number below is internally consistent: times, ids, test counts,
     and which facts a stopped agent can still leave behind. */
  var SCN = {
    credits: {
      repo: "invoices-api", branch: "feat/pdf-export", sha: "a41f2c9",
      past: { agent: "Kimi CLI", where: "home-desktop · yesterday 21:14", line: "handoff 91d0 · picked up by claude-code" },
      from: { agent: "Claude Code", host: "studio-laptop", t: 842 },
      task: "PDF export for invoices",
      meter: { lab: "API credits", from: 1.8, to: 0, max: 1.8, fmt: money },
      stop: "credit balance too low · session ended",
      kind: "Handoff", id: "3c9e", verb: "signed by", sigT: 842,
      facts: [["ok", "3 commits on feat/pdf-export · pushed"], ["ok", "4 files changed"], ["warn", "41 tests passed · 1 failing"], ["todo", "open: rounding in totals()"]],
      to: { agent: "OpenAI Codex", host: "studio-laptop", t: 843 },
      doing: "fixing rounding in totals()", tests: 42,
      followups: ["adding the PDF to invoice emails", "testing PDFs with long line items", "writing the changelog entry"]
    },
    usage: {
      repo: "shop-api", branch: "feat/rate-limit", sha: "8e07b1c",
      past: { agent: "Gemini CLI", where: "office-pc · 09:12", line: "handoff 2b7e · picked up by codex" },
      from: { agent: "OpenAI Codex", host: "office-pc", t: 700 },
      task: "rate limiting on /checkout",
      meter: { lab: "usage left", from: 14, to: 0, max: 100, fmt: pct },
      stop: "usage limit reached · resets 16:00",
      kind: "Handoff", id: "8b41", verb: "signed by", sigT: 700,
      facts: [["ok", "2 commits on feat/rate-limit"], ["ok", "5 files changed · 1 uncommitted"], ["warn", "1 commit not pushed yet"], ["todo", "open: src/middleware/rateLimit.ts"]],
      to: { agent: "Claude Code", host: "office-pc", t: 702 },
      doing: "finishing rateLimit.ts, then the tests", tests: 15, pushedNote: "pushed 2 commits",
      followups: ["adding a burst-window test", "documenting the rate limits", "tuning limits for the admin API"]
    },
    lid: {
      repo: "booking-app", branch: "auth-sessions", sha: "5d90e3a",
      past: { agent: "Qwen Code", where: "home-desktop · 08:30", line: "handoff 44a2 · picked up by cursor" },
      from: { agent: "Cursor", host: "work-laptop", t: 1068 },
      task: "moving login to sessions",
      meter: { lab: "connection", from: 100, to: 0, max: 100, fmt: function (v) { return v > 0.5 ? "online" : "offline"; } },
      stop: "lid closed · session never closed out",
      kind: "Checkpoint", id: "5c21", verb: "saved by", sigT: 1066,
      facts: [["ok", "2 commits on auth-sessions · pushed"], ["ok", "login moved to sessions"], ["warn", "18 tests passed · 3 failing"], ["todo", "next: token refresh in middleware"]],
      to: { agent: "Claude Code", host: "home-desktop", t: 1180 },
      doing: "fixing token refresh, 3 tests to go", tests: 21,
      followups: ["removing the old login tokens", "adding a session timeout test", "updating the auth docs"]
    },
    day: {
      repo: "data-sync", branch: "import-v2", sha: "e7d3a90",
      past: { agent: "Claude Code", where: "studio-laptop · 11:30", line: "handoff 6f18 · picked up by gemini-cli" },
      from: { agent: "Gemini CLI", host: "office-pc", t: 1085 },
      task: "nightly import job",
      meter: { lab: "workday", from: 0.5, to: 0, max: 1, fmt: function (v) { return (Math.round(v * 10) / 10).toFixed(1) + "h left"; } },
      stop: "you logged off · session closed",
      kind: "Handoff", id: "e09b", verb: "signed by", sigT: 1085,
      facts: [["ok", "5 commits on import-v2 · pushed"], ["ok", "9 files changed"], ["ok", "all 64 tests passed"], ["todo", "next: retry on network timeout"]],
      to: { agent: "OpenAI Codex", host: "cloud VM", t: 1925 },
      doing: "adding retry on network timeout", tests: 66,
      followups: ["logging failed imports", "adding a retry test for timeouts", "scheduling the import on the VM"]
    }
  };
  var ORDER = ["credits", "usage", "lid", "day"];

  function clone(s) {
    var c = {};
    for (var k in s) if (Object.prototype.hasOwnProperty.call(s, k)) c[k] = s[k];
    c.fi = 0;
    return c;
  }
  function briefLine(s) {
    var gap = s.to.t - s.sigT;
    var fromId = AGENT_ID[s.from.agent];
    if (s.kind === "Checkpoint") return "trail: last checkpoint " + s.id + " from " + fromId + ", " + ago(gap);
    return "brief: " + fromId + ", " + ago(gap) + ", " + s.branch + "@" + s.sha;
  }
  function toLinesOf(s) {
    return [
      ["", "›", briefLine(s)],
      ["", "→", s.doing],
      ["ok", "✓", s.tests + " passed · " + (s.pushedNote || "pushed")]
    ];
  }

  var reduce = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  var $ = function (id) { return document.getElementById(id); };
  var track = $("track"), rail = $("rail"), railFill = $("railFill"), baton = $("baton");
  var nPast = $("nPast"), nFrom = $("nFrom"), nTo = $("nTo"), nStop = $("nStop");
  var meter = $("meter"), meterFill = $("meterFill"), meterVal = $("meterVal");
  var factsEl = $("facts"), toLines = $("toLines"), cap = $("relay-cap"), status = $("relay-status");
  var buttons = Array.prototype.slice.call(relay.querySelectorAll("[data-scn]"));
  var agentBtns = Array.prototype.slice.call(relay.querySelectorAll("[data-slot]"));
  var pauseBtn = null;

  var S = clone(SCN.credits);
  var current = "credits";          /* scenario key, or "" after a tap handoff */
  var stage = 6;
  var timers = [];
  var raf = 0;
  var visible = true;
  var waiting = false;
  var paused = reduce;
  var HOLD = 6200;
  var taps = 0;

  function el(tag, cls, text) {
    var n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text != null) n.textContent = text;
    return n;
  }
  function setK(key, val) {
    var n = relay.querySelector('[data-k="' + key + '"]');
    if (n) n.textContent = val;
  }

  function render(s) {
    setK("repo", s.repo);
    setK("pastAgent", s.past.agent);
    setK("pastWhere", s.past.where);
    setK("pastLine", s.past.line);
    setK("fromAgent", s.from.agent);
    setK("fromWhere", s.from.host + " · " + clock(s.from.t));
    setK("task", s.task);
    setK("stop", s.stop);
    nStop.classList.toggle("calm", !s.meter);
    setK("kind", s.kind + " " + s.id);
    setK("sig", s.verb + " " + AGENT_ID[s.from.agent] + " · " + clock(s.sigT));
    setK("toAgent", s.to.agent);
    setK("toWhere", s.to.host + " · " + clock(s.to.t));

    meter.hidden = !s.meter;
    if (s.meter) setK("meterLab", s.meter.lab);

    factsEl.textContent = "";
    s.facts.forEach(function (f) { factsEl.appendChild(el("li", f[0], f[1])); });

    toLines.textContent = "";
    toLinesOf(s).forEach(function (l, i, all) {
      var p = el("p", "line" + (l[0] ? " " + l[0] : ""));
      p.setAttribute("data-at", "6");
      p.style.transitionDelay = (i * 0.45) + "s";
      p.appendChild(el("span", "p", l[1]));
      p.appendChild(document.createTextNode(l[2]));
      if (i === all.length - 1) {
        var c = el("span", "cursor");
        c.setAttribute("aria-hidden", "true");
        p.appendChild(c);
      }
      toLines.appendChild(p);
    });

    agentBtns.forEach(function (b) {
      var slot = b.getAttribute("data-slot");
      var name = slot === "past" ? s.past.agent : slot === "from" ? s.from.agent : s.to.agent;
      b.setAttribute("aria-label", slot === "to"
        ? name + " has the work. Hand it off to another agent on another machine"
        : "Hand the work off to " + name + " on another machine");
    });

    cap.textContent = "Example trail for " + s.repo + ": " + s.from.agent + " on " + s.from.host + " stops (" + s.stop + ") and leaves a " +
      s.kind.toLowerCase() + ": " + s.facts.map(function (f) { return f[1]; }).join("; ") + ". " +
      s.to.agent + " on " + s.to.host + " picks it up at " + clock(s.to.t) + " and carries on: " + s.doing + ".";

    buttons.forEach(function (b) { b.setAttribute("aria-pressed", String(b.getAttribute("data-scn") === current)); });
  }

  function centerY(li) {
    var node = li.querySelector(".node");
    var t = track.getBoundingClientRect();
    var r = node.getBoundingClientRect();
    return r.top - t.top + r.height / 2;
  }
  function place() {
    var first = nPast.offsetParent === null ? nFrom : nPast;   /* the past row is hidden on phones */
    var y0 = centerY(first), y1 = centerY(nFrom), y2 = centerY(nTo);
    rail.style.top = y0 + "px";
    rail.style.height = Math.max(0, y2 - y0) + "px";
    railFill.style.top = y0 + "px";
    var holder = stage >= 4 ? y2 : y1;
    railFill.style.height = Math.max(0, holder - y0) + "px";
    baton.style.transform = "translateY(" + (holder - 13) + "px)";
  }

  function setStage(n) {
    stage = n;
    relay.setAttribute("data-stage", String(n));
    relay.querySelectorAll("[data-at]").forEach(function (x) {
      x.classList.toggle("on", Number(x.getAttribute("data-at")) <= n);
    });
    place();
  }

  function paintMeter(v, s) {
    if (!s.meter) return;
    var p = Math.max(0, Math.min(100, (v / s.meter.max) * 100));
    meterFill.style.setProperty("--m", p + "%");
    meterVal.textContent = s.meter.fmt(v);
    meter.classList.toggle("low", p <= 12);
  }
  function drainMeter(s, ms) {
    cancelAnimationFrame(raf);
    if (!s.meter) return;
    var t0 = performance.now();
    function tick(now) {
      var k = Math.min(1, (now - t0) / ms);
      var e = k < 0.5 ? 2 * k * k : 1 - Math.pow(-2 * k + 2, 2) / 2;
      paintMeter(s.meter.from + (s.meter.to - s.meter.from) * e, s);
      if (k < 1) raf = requestAnimationFrame(tick);
    }
    raf = requestAnimationFrame(tick);
  }

  function clearTimers() {
    timers.forEach(clearTimeout);
    timers = [];
    cancelAnimationFrame(raf);
    buttons.forEach(function (b) {
      var p = b.querySelector(".prog");
      p.style.transition = "none";
      p.style.width = "0";
    });
  }
  function at(ms, fn) { timers.push(setTimeout(fn, ms)); }

  function showStill(s) {
    clearTimers();
    relay.classList.remove("is-animating");
    relay.classList.add("no-tr");
    render(s);
    if (s.meter) paintMeter(s.meter.to, s);
    setStage(6);
    void relay.offsetWidth;
    relay.classList.remove("no-tr");
  }

  function startHold() {
    if (paused || reduce || !current) return;
    var btn = buttons.filter(function (b) { return b.getAttribute("data-scn") === current; })[0];
    var p = btn && btn.querySelector(".prog");
    if (p) {
      p.style.transition = "none"; p.style.width = "0"; void p.offsetWidth;
      p.style.transition = "width " + HOLD + "ms linear"; p.style.width = "100%";
    }
    at(HOLD, next);
  }

  function play(s) {
    if (reduce) { showStill(s); return; }
    clearTimers();
    relay.classList.add("no-tr");
    relay.classList.add("is-animating");
    render(s);
    if (s.meter) paintMeter(s.meter.from, s);
    setStage(0);
    void relay.offsetWidth;
    relay.classList.remove("no-tr");

    var d = s.meter ? 0 : 1700;   /* no resource to drain on a manual switch */
    at(80, function () { setStage(1); });
    if (s.meter) at(500, function () { drainMeter(s, 2300); });
    at(2950 - d, function () { setStage(2); });
    at(3550 - d, function () { setStage(3); });
    at(4900 - d, function () { setStage(4); });   /* the baton travels down the trail */
    at(5750 - d, function () { setStage(5); });   /* the next agent lights up */
    at(6150 - d, function () { setStage(6); });   /* and starts already knowing */
    at(7400 - d, function () {
      relay.classList.remove("is-animating");
      startHold();
    });
  }

  function playScenario(key) {
    current = key;
    S = clone(SCN[key]);
    play(S);
  }

  function next() {
    if (paused) return;
    if (!visible || document.hidden) { waiting = true; return; }
    waiting = false;
    var i = ORDER.indexOf(current);
    playScenario(ORDER[(i + 1) % ORDER.length]);
  }

  function hex(n) {
    var s = "";
    for (var i = 0; i < n; i++) s += "0123456789abcdef".charAt(Math.floor(Math.random() * 16));
    return s;
  }
  function otherAgent(name) {
    return AGENTS[(AGENTS.indexOf(name) + 1) % AGENTS.length];
  }
  function otherHost(host) {
    for (var i = 0; i < HOSTS.length; i++) {
      var h = HOSTS[(taps + i) % HOSTS.length];
      if (h !== host) return h;
    }
    return HOSTS[0];
  }

  /* The agent holding the work closes its session; `target` (always a
     different agent than the holder) picks up on a different machine.
     Work is pushed before a cross-machine pickup, so the facts say so. */
  function handOff(tapped) {
    var s = S;
    var holder = s.to;
    var target = tapped && tapped !== holder.agent ? tapped : otherAgent(holder.agent);
    var host = otherHost(holder.host);
    var closeT = holder.t + 9;
    var pickT = closeT + 2 + (taps % 4);
    var follow = s.followups[s.fi % s.followups.length];
    taps += 1;

    S = {
      repo: s.repo, branch: s.branch, sha: hex(7),
      past: { agent: s.from.agent, where: s.from.host + " · " + clock(s.from.t), line: s.kind.toLowerCase() + " " + s.id + " · picked up by " + AGENT_ID[holder.agent] },
      from: { agent: holder.agent, host: holder.host, t: closeT },
      task: s.doing,
      meter: null,
      stop: "you switched to " + target + " on " + host + " · session closed",
      kind: "Handoff", id: hex(4), verb: "signed by", sigT: closeT,
      facts: [["ok", "1 commit on " + s.branch + " · pushed"], ["ok", s.tests + " tests passed"], ["todo", "next: " + follow]],
      to: { agent: target, host: host, t: pickT },
      doing: follow, tests: s.tests + 1 + (taps % 3),
      followups: s.followups, fi: s.fi + 1
    };
    current = "";
    paused = true;
    syncPause();
    play(S);
    status.textContent = holder.agent + " handed off to " + target + " on " + host + ".";
  }

  /* ---------------- Controls ---------------- */
  buttons.forEach(function (b) {
    b.addEventListener("click", function () { playScenario(b.getAttribute("data-scn")); });
  });
  agentBtns.forEach(function (b) {
    b.addEventListener("click", function () { handOff(b.textContent); });
  });

  function syncPause() {
    if (!pauseBtn) return;
    pauseBtn.textContent = paused ? "Play" : "Pause";
    pauseBtn.setAttribute("aria-label", paused ? "Play the demo" : "Pause the demo");
  }
  if (!reduce) {
    var row = relay.querySelector(".relay-foot .row");
    pauseBtn = el("button", "pause");
    pauseBtn.type = "button";
    row.appendChild(pauseBtn);
    pauseBtn.addEventListener("click", function () {
      paused = !paused;
      syncPause();
      if (paused) clearTimers();
      else next();
    });
    syncPause();
  }

  /* Initial state: the complete, finished story as a still frame. */
  showStill(S);
  relay.classList.add("is-ready");

  function relayout() {
    relay.classList.add("no-tr");
    place();
    void relay.offsetWidth;
    relay.classList.remove("no-tr");
  }
  if (window.ResizeObserver) new ResizeObserver(relayout).observe(track);
  else window.addEventListener("resize", relayout);
  if (document.fonts && document.fonts.ready) document.fonts.ready.then(relayout);

  if (!reduce) {
    if (window.IntersectionObserver) {
      new IntersectionObserver(function (entries) {
        visible = entries[0].isIntersecting;
        if (visible && waiting) next();
      }, { threshold: 0.35 }).observe(relay);
    }
    document.addEventListener("visibilitychange", function () { if (!document.hidden && waiting) next(); });
    /* Hold the still frame long enough to read, then show a different cause happening live. */
    startHold();
  }
})();
