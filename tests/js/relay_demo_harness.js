/* Runs landing/relay-demo.js against the home page's real #relay markup in a
   minimal DOM with a virtual clock, so pytest can drive the demo (click,
   advance time) and inspect what a visitor would see.

   stdin: {"script": path, "dom": node-tree, "reduce": bool, "steps": [...]}
     node-tree: {"t": tag, "a": {attr: value}, "c": [child | "text"]}
     steps:     {"do": "click", "sel": css} | {"do": "advance", "ms": n} | {"do": "snap"}
   stdout: JSON list with one snapshot per "snap" step.

   Visibility mirrors the page CSS: while the figure has .is-animating, a
   [data-at] element without .on is at opacity 0. test_landing_site.py checks
   that rule is still in index.html. */
"use strict";
const fs = require("fs");
const vm = require("vm");

const input = JSON.parse(fs.readFileSync(0, "utf8"));

/* ---------------- virtual clock ---------------- */
let now = 0;
let seq = 0;
const timers = new Map();
function setTimeoutV(fn, ms) {
  const id = ++seq;
  timers.set(id, { at: now + Math.max(0, Number(ms) || 0), fn, id });
  return id;
}
function clearTimeoutV(id) { timers.delete(id); }
function advance(ms) {
  const end = now + ms;
  for (;;) {
    let next = null;
    for (const t of timers.values()) {
      if (t.at <= end && (!next || t.at < next.at || (t.at === next.at && t.id < next.id))) next = t;
    }
    if (!next) break;
    timers.delete(next.id);
    now = next.at;
    next.fn(now);
  }
  now = end;
}

/* ---------------- minimal DOM ---------------- */
class TextNode {
  constructor(s) { this.nodeType = 3; this.data = s; this.parentNode = null; }
  get textContent() { return this.data; }
}
function classListOf(el) {
  const get = () => (el.getAttribute("class") || "").split(/\s+/).filter(Boolean);
  const put = (list) => el.setAttribute("class", list.join(" "));
  return {
    contains: (c) => get().includes(c),
    add: (...cs) => { const l = get(); cs.forEach((c) => { if (!l.includes(c)) l.push(c); }); put(l); },
    remove: (...cs) => put(get().filter((c) => !cs.includes(c))),
    toggle: (c, force) => {
      const has = get().includes(c);
      const want = force === undefined ? !has : Boolean(force);
      if (want && !has) put(get().concat(c));
      if (!want && has) put(get().filter((x) => x !== c));
      return want;
    },
  };
}
class Element {
  constructor(tag) {
    this.nodeType = 1;
    this.tagName = tag.toUpperCase();
    this.attrs = new Map();
    this.childNodes = [];
    this.parentNode = null;
    this.listeners = {};
    this.style = { setProperty(k, v) { this[k] = v; } };
    this.classList = classListOf(this);
  }
  getAttribute(k) { return this.attrs.has(k) ? this.attrs.get(k) : null; }
  setAttribute(k, v) { this.attrs.set(k, String(v)); }
  removeAttribute(k) { this.attrs.delete(k); }
  hasAttribute(k) { return this.attrs.has(k); }
  get className() { return this.getAttribute("class") || ""; }
  set className(v) { this.setAttribute("class", v); }
  get id() { return this.getAttribute("id") || ""; }
  get hidden() { return this.attrs.has("hidden"); }
  set hidden(v) { if (v) this.attrs.set("hidden", ""); else this.attrs.delete("hidden"); }
  get type() { return this.getAttribute("type") || ""; }
  set type(v) { this.setAttribute("type", v); }
  appendChild(n) {
    if (n.parentNode) n.parentNode.removeChild(n);
    n.parentNode = this;
    this.childNodes.push(n);
    return n;
  }
  removeChild(n) {
    const i = this.childNodes.indexOf(n);
    if (i >= 0) this.childNodes.splice(i, 1);
    n.parentNode = null;
    return n;
  }
  get children() { return this.childNodes.filter((n) => n.nodeType === 1); }
  get textContent() { return this.childNodes.map((n) => n.textContent).join(""); }
  set textContent(v) {
    this.childNodes.forEach((n) => { n.parentNode = null; });
    this.childNodes = [];
    if (v !== null && v !== undefined && v !== "") this.appendChild(new TextNode(String(v)));
  }
  addEventListener(type, fn) { (this.listeners[type] = this.listeners[type] || []).push(fn); }
  click() { (this.listeners.click || []).slice().forEach((fn) => fn({ type: "click", target: this })); }
  descendants() {
    const out = [];
    const walk = (n) => n.children.forEach((c) => { out.push(c); walk(c); });
    walk(this);
    return out;
  }
  querySelectorAll(sel) { return this.descendants().filter((e) => matches(e, sel)); }
  querySelector(sel) { return this.querySelectorAll(sel)[0] || null; }
  getBoundingClientRect() { return { top: 0, left: 0, width: 0, height: 0, right: 0, bottom: 0 }; }
  get offsetParent() { return this.hidden ? null : this.parentNode; }
  get offsetWidth() { return 0; }
  /* A layout stand-in: every shown <p> or <li> is one 20px line, and a box
     is as tall as the lines inside it (or its min-height, when larger).
     Enough to see a story with more facts or brief lines make the trail
     taller, and whether the reserved height covers the tallest one. */
  get offsetHeight() {
    if (this.hidden) return 0;
    const own = this.tagName === "P" || this.tagName === "LI" ? 20 : 0;
    const inner = this.tagName === "P" ? 0 : this.children.reduce((sum, c) => sum + c.offsetHeight, 0);
    const min = parseFloat(this.style.minHeight) || 0;
    return Math.max(own + inner, min);
  }
}

/* Selectors: descendant combinator over compounds of tag, #id, .class,
   [attr] and [attr="value"]. That is all relay-demo.js and the tests use. */
function parseCompound(src) {
  const m = /^([a-zA-Z][\w-]*)?((?:#[\w-]+|\.[\w-]+|\[[^\]]+\])*)$/.exec(src);
  if (!m) throw new Error("unsupported selector: " + src);
  const parts = [];
  if (m[1]) parts.push({ tag: m[1].toUpperCase() });
  const re = /#([\w-]+)|\.([\w-]+)|\[([\w-]+)(?:="([^"]*)")?\]/g;
  let p;
  while ((p = re.exec(m[2]))) {
    if (p[1]) parts.push({ id: p[1] });
    else if (p[2]) parts.push({ cls: p[2] });
    else parts.push({ attr: p[3], val: p[4] });
  }
  return parts;
}
function matchCompound(el, parts) {
  return parts.every((p) => {
    if (p.tag) return el.tagName === p.tag;
    if (p.id) return el.id === p.id;
    if (p.cls) return el.classList.contains(p.cls);
    if (!el.hasAttribute(p.attr)) return false;
    return p.val === undefined || el.getAttribute(p.attr) === p.val;
  });
}
function matches(el, sel) {
  const chain = sel.trim().split(/\s+/).map(parseCompound);
  if (!matchCompound(el, chain[chain.length - 1])) return false;
  let i = chain.length - 2;
  let node = el.parentNode;
  while (i >= 0 && node && node.nodeType === 1) {
    if (matchCompound(node, chain[i])) i -= 1;
    node = node.parentNode;
  }
  return i < 0;
}

function build(tree) {
  if (typeof tree === "string") return new TextNode(tree);
  const el = new Element(tree.t);
  Object.entries(tree.a || {}).forEach(([k, v]) => el.setAttribute(k, v));
  (tree.c || []).forEach((c) => el.appendChild(build(c)));
  return el;
}

const body = new Element("body");
body.appendChild(build(input.dom));

const documentV = {
  hidden: false,
  getElementById: (id) => body.descendants().find((e) => e.id === id) || null,
  querySelector: (s) => body.querySelector(s),
  querySelectorAll: (s) => body.querySelectorAll(s),
  createElement: (t) => new Element(t),
  createTextNode: (s) => new TextNode(s),
  addEventListener() {},
};
const windowV = {
  matchMedia: (q) => ({ matches: /reduce/.test(q) ? Boolean(input.reduce) : false }),
  addEventListener() {},
};
const sandbox = {
  window: windowV,
  document: documentV,
  setTimeout: setTimeoutV,
  clearTimeout: clearTimeoutV,
  requestAnimationFrame: (fn) => setTimeoutV(() => fn(now), 16),
  cancelAnimationFrame: clearTimeoutV,
  performance: { now: () => now },
};
vm.runInNewContext(fs.readFileSync(input.script, "utf8"), sandbox, { filename: input.script });

/* ---------------- snapshots ---------------- */
function text(el) { return el ? el.textContent.replace(/\s+/g, " ").trim() : null; }
function k(key) { return text(body.querySelector('[data-k="' + key + '"]')); }
function snap() {
  const relay = documentV.getElementById("relay");
  const classes = relay.className.split(/\s+/).filter(Boolean);
  const animating = classes.includes("is-animating");
  const pause = relay.querySelector("button.pause");
  return {
    t: now,
    classes,
    stage: relay.getAttribute("data-stage"),
    hidden: relay.querySelectorAll("[data-at]")
      .filter((e) => animating && !e.classList.contains("on"))
      .map((e) => e.id || text(e).slice(0, 40)),
    pressed: relay.querySelectorAll("[data-scn]")
      .filter((b) => b.getAttribute("aria-pressed") === "true")
      .map((b) => b.getAttribute("data-scn")),
    pause: pause ? text(pause) : null,
    fromAgent: k("fromAgent"),
    toAgent: k("toAgent"),
    pastAgent: k("pastAgent"),
    kind: k("kind"),
    sig: k("sig"),
    src: k("src"),
    stop: k("stop"),
    facts: documentV.getElementById("facts").children.map((li) => ({ cls: li.className, text: text(li) })),
    toLines: documentV.getElementById("toLines").children.map(text),
    meter: text(documentV.getElementById("meterVal")),
    caption: text(documentV.getElementById("relay-cap")),
    pending: timers.size,
    trail: windowV.RemembraTrail ? windowV.RemembraTrail.last : null,
    minHeight: documentV.getElementById("track").style.minHeight || "",
    contentHeight: (() => {
      const track = documentV.getElementById("track");
      const saved = track.style.minHeight;
      track.style.minHeight = "";
      const h = track.offsetHeight;
      track.style.minHeight = saved;
      return h;
    })(),
  };
}

const out = [];
for (const step of input.steps) {
  if (step.do === "advance") advance(step.ms);
  else if (step.do === "snap") out.push(snap());
  else if (step.do === "click") {
    const el = body.querySelector(step.sel);
    if (!el) throw new Error("no element for " + step.sel);
    el.click();
  } else throw new Error("unknown step " + JSON.stringify(step));
}
process.stdout.write(JSON.stringify(out));
