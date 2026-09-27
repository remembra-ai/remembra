/* Runs landing/site.js against the home page's real install markup (the
   "terminal | your agent" tabs and their panels) in a minimal DOM, so pytest
   can press keys and click like a visitor and read back what they would see.

   stdin: {"script": path, "dom": node-tree, "steps": [...]}
     node-tree: {"t": tag, "a": {attr: value}, "c": [child | "text"]}
     steps:     {"do": "click", "sel": css}
                | {"do": "key", "sel": css, "key": "ArrowRight"}   (keydown on that element)
                | {"do": "snap"}
   stdout: JSON list with one snapshot per "snap" step.

   Selectors: descendant combinator over compounds of tag, #id, .class,
   [attr] and [attr="value"]: all site.js and the tests use. */
"use strict";
const fs = require("fs");
const vm = require("vm");

const input = JSON.parse(fs.readFileSync(0, "utf8"));
let focused = null;
const clipboard = [];

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
    this.style = {};
    this.classList = classListOf(this);
  }
  getAttribute(k) { return this.attrs.has(k) ? this.attrs.get(k) : null; }
  setAttribute(k, v) { this.attrs.set(k, String(v)); }
  removeAttribute(k) { this.attrs.delete(k); }
  hasAttribute(k) { return this.attrs.has(k); }
  get id() { return this.getAttribute("id") || ""; }
  get hidden() { return this.attrs.has("hidden"); }
  set hidden(v) { if (v) this.attrs.set("hidden", ""); else this.attrs.delete("hidden"); }
  get children() { return this.childNodes.filter((n) => n.nodeType === 1); }
  get textContent() { return this.childNodes.map((n) => n.textContent).join(""); }
  set textContent(v) { this.childNodes = v === "" || v == null ? [] : [new TextNode(String(v))]; }
  appendChild(n) { n.parentNode = this; this.childNodes.push(n); return n; }
  removeChild(n) { this.childNodes = this.childNodes.filter((c) => c !== n); n.parentNode = null; return n; }
  addEventListener(type, fn) { (this.listeners[type] = this.listeners[type] || []).push(fn); }
  dispatch(event) {
    /* bubbles to the ancestors, like a real click or keydown */
    for (let node = this; node; node = node.parentNode) {
      (node.listeners && node.listeners[event.type] ? node.listeners[event.type] : []).slice().forEach((fn) => fn(event));
    }
    return event;
  }
  focus() { focused = this; }
  closest(sel) { for (let n = this; n && n.nodeType === 1; n = n.parentNode) if (matches(n, sel)) return n; return null; }
  descendants() {
    const out = [];
    const walk = (n) => n.children.forEach((c) => { out.push(c); walk(c); });
    walk(this);
    return out;
  }
  querySelectorAll(sel) { return this.descendants().filter((e) => matches(e, sel)); }
  querySelector(sel) { return this.querySelectorAll(sel)[0] || null; }
}
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

const html = new Element("html");
const body = html.appendChild(new Element("body"));
body.appendChild(build(input.dom));
const documentV = {
  documentElement: html,
  body,
  get activeElement() { return focused; },
  getElementById: (id) => body.descendants().find((e) => e.id === id) || null,
  querySelector: (s) => body.querySelector(s),
  querySelectorAll: (s) => body.querySelectorAll(s),
  createElement: (t) => new Element(t),
  addEventListener() {},
  execCommand: () => false,
};
const windowV = { innerWidth: 1280, scrollY: 0, addEventListener() {}, dispatchEvent() {} };
const sandbox = {
  window: windowV,
  document: documentV,
  navigator: { clipboard: { writeText: (t) => { clipboard.push(t); return Promise.resolve(); } } },
  localStorage: { getItem: () => null, setItem() {} },
  CustomEvent: function CustomEvent(type, init) { this.type = type; this.detail = init && init.detail; },
  setTimeout: () => 0,
  clearTimeout() {},
};
vm.runInNewContext(fs.readFileSync(input.script, "utf8"), sandbox, { filename: input.script });

function text(el) { return el ? el.textContent.replace(/\s+/g, " ").trim() : null; }
function snap(lastKey) {
  const tabs = body.querySelectorAll('[role="tab"]');
  return {
    tabs: tabs.map((t) => ({
      id: t.id,
      label: text(t),
      selected: t.getAttribute("aria-selected"),
      tabindex: t.getAttribute("tabindex"),
      solid: t.classList.contains("solid"),
      controls: t.getAttribute("aria-controls"),
    })),
    panels: body.querySelectorAll('[role="tabpanel"]').map((p) => ({ id: p.id, hidden: p.hidden })),
    focused: focused ? focused.id || focused.tagName : null,
    clipboard: clipboard.slice(),
    copyStatus: body.querySelectorAll("[data-copy-status]").map(text),
    lastKeyPrevented: lastKey ? lastKey.defaultPrevented : null,
  };
}

(async () => {
  const out = [];
  let lastKey = null;
  for (const step of input.steps) {
    if (step.do === "snap") { out.push(snap(lastKey)); continue; }
    const el = body.querySelector(step.sel);
    if (!el) throw new Error("no element for " + step.sel);
    if (step.do === "click") {
      el.dispatch({ type: "click", target: el, preventDefault() {} });
    } else if (step.do === "key") {
      lastKey = el.dispatch({
        type: "keydown", key: step.key, target: el, defaultPrevented: false,
        preventDefault() { this.defaultPrevented = true; },
      });
    } else throw new Error("unknown step " + JSON.stringify(step));
    for (let i = 0; i < 5; i += 1) await Promise.resolve();
  }
  process.stdout.write(JSON.stringify(out));
})().catch((err) => { process.stderr.write(String(err && err.stack || err)); process.exit(1); });
