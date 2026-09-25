// SEC-20 regression: memory text and API errors must never become live HTML in
// the host page. Usage: node xss_render_check.cjs <path/to/content.js>
// Requires jsdom (resolved via NODE_PATH). Exit 0 = safe, 1 = injectable.
const { JSDOM } = require('jsdom');
const fs = require('fs');
const src = fs.readFileSync(process.argv[2], 'utf8');
async function run(response) {
  const dom = new JSDOM('<!doctype html><body></body>', { url: 'https://claude.ai/', runScripts: 'outside-only' });
  const w = dom.window;
  let listener;
  w.chrome = { runtime: { sendMessage: async () => response, onMessage: { addListener: (fn) => { listener = fn; } } } };
  w.console.log = () => {};
  w.eval(src);
  listener({ type: 'TOGGLE_SIDEBAR' }, {}, () => {});
  const input = w.document.querySelector('#remembra-search-input');
  input.value = 'x';
  w.document.querySelector('#remembra-search-btn').click();
  await new Promise(r => setTimeout(r, 50));
  return w.document;
}
(async () => {
  const payload = '<img src=x onerror="window.pwned=1">"><script>window.pwned=2</script>';
  let doc = await run({ success: true, memories: [{ content: payload }] });
  const injectedA = doc.querySelectorAll('#remembra-memories-list img, #remembra-memories-list script').length;
  const shown = doc.querySelector('.remembra-memory-content').textContent;
  const attr = doc.querySelector('.remembra-insert-btn').dataset.content;
  doc = await run({ success: false, error: payload });
  const injectedB = doc.querySelectorAll('#remembra-memories-list img, #remembra-memories-list script').length;
  const errText = doc.querySelector('.remembra-error').textContent;
  const ok = injectedA === 0 && injectedB === 0 && shown.startsWith('<img') && attr === payload && errText === payload;
  console.log(JSON.stringify({ injectedA, injectedB, shownIsLiteral: shown.startsWith('<img'), attrExact: attr === payload, errLiteral: errText === payload, ok }));
  process.exit(ok ? 0 : 1);
})();
