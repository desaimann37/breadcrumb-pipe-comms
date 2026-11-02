/* Headless smoke test for the dashboard render path.
 *
 * Runs web/app.js inside a minimal DOM/canvas shim against real telemetry from
 * the running server, so typos and bad property paths fail here instead of in
 * front of an audience.
 *
 *   node tools/uicheck.js [http://127.0.0.1:8765]
 */
'use strict';

const fs = require('fs');
const path = require('path');
const vm = require('vm');

const BASE = process.argv[2] || 'http://127.0.0.1:8765';

function fakeCtx() {
  const grad = { addColorStop() {} };
  return new Proxy({}, {
    get(_t, k) {
      if (k === 'createLinearGradient' || k === 'createRadialGradient') return () => grad;
      if (k === 'canvas') return { width: 900, height: 300 };
      if (k === 'measureText') return () => ({ width: 10 });
      return () => undefined;
    },
    set() { return true; },
  });
}

function makeEl(tag = 'div') {
  const classes = new Set();
  const el = {
    tagName: tag.toUpperCase(),
    textContent: '', innerHTML: '', className: '', value: '', disabled: false,
    dataset: {}, style: {}, children: [], firstChild: null, lastChild: null,
    classList: {
      add: (c) => classes.add(c),
      remove: (c) => classes.delete(c),
      contains: (c) => classes.has(c),
      toggle: (c) => (classes.has(c) ? classes.delete(c) : classes.add(c)),
    },
    setAttribute() {}, removeAttribute() {}, getAttribute: () => null,
    width: 900, height: 300, clientWidth: 900, clientHeight: 300,
    getContext: () => fakeCtx(),
    addEventListener() {},
    appendChild(c) { el.children.push(c); el.lastChild = c; el.firstChild = el.children[0]; return c; },
    insertBefore(c) { el.children.unshift(c); el.firstChild = c; el.lastChild = el.children[el.children.length - 1]; return c; },
    removeChild(c) {
      const i = el.children.indexOf(c);
      if (i >= 0) el.children.splice(i, 1);
      el.firstChild = el.children[0] || null;
      el.lastChild = el.children[el.children.length - 1] || null;
      return c;
    },
    querySelectorAll: () => [],
    querySelector: () => null,
  };
  return el;
}

async function getJson(p) {
  const res = await fetch(BASE + p);
  if (!res.ok) throw new Error(`${p} -> HTTP ${res.status}`);
  return res.json();
}

(async () => {
  const [state, pipe, scenarios] = await Promise.all([
    getJson('/api/state'), getJson('/api/pipe'), getJson('/api/scenarios'),
  ]);

  const els = new Map();
  const document = {
    getElementById(id) {
      if (!els.has(id)) els.set(id, makeEl(id === 'pipe' || id === 'cam' ? 'canvas' : 'div'));
      return els.get(id);
    },
    createElement: (tag) => makeEl(tag),
    querySelectorAll: () => [],
    querySelector: () => makeEl('button'),
    activeElement: null,
  };

  const sandbox = {
    document,
    window: { devicePixelRatio: 1, addEventListener() {} },
    performance: { now: () => Date.now() },
    requestAnimationFrame() { return 0; },
    fetch: () => Promise.resolve({ json: () => Promise.resolve({ scenarios: [] }) }),
    EventSource: function () { this.onmessage = null; },
    Math, Date, JSON, console, Set, Map, Array, Object, String, Number, Boolean, Promise, isFinite,
    setTimeout, parseFloat, parseInt,
  };
  sandbox.globalThis = sandbox;
  vm.createContext(sandbox);

  const src = fs.readFileSync(path.join(__dirname, '..', 'web', 'app.js'), 'utf8');
  vm.runInContext(src, sandbox, { filename: 'app.js' });

  // `const S` is a lexical binding, not a property of the sandbox global, so
  // reach it by evaluating its name inside the context.
  const S = vm.runInContext('S', sandbox);
  const call = (name) => vm.runInContext(`${name}()`, sandbox);

  S.pipe = pipe;
  S.state = state;
  S.scenarios = scenarios.scenarios;

  const steps = ['onTick', 'drawPipe', 'drawZoom', 'drawCam', 'drawChart',
                 'renderWhy', 'renderHops', 'renderTiles']
    .map((n) => [n, () => call(n)]);

  let failed = 0;
  for (const [name, fn] of steps) {
    for (let i = 0; i < 12; i++) {
      try {
        fn();
      } catch (err) {
        console.error(`FAIL ${name}: ${err.message}`);
        failed++;
        break;
      }
    }
    if (!failed) process.stdout.write(`ok ${name}  `);
  }
  console.log('');

  // Spot-check that the panels actually produced content.
  const checks = [
    ['tiles', els.get('tiles').innerHTML.includes('Distance in')],
    ['hops', els.get('hops').innerHTML.length > 40],
    ['why', els.get('why').innerHTML.includes('cutoff')],
    ['clock', /\d\d:\d\d:\d\d/.test(els.get('clock').textContent)],
  ];
  checks.forEach(([n, ok]) => {
    if (!ok) { console.error(`FAIL content: ${n}`); failed++; }
  });

  if (failed) {
    console.error(`\n${failed} dashboard check(s) failed`);
    process.exit(1);
  }
  console.log('dashboard render path OK against live telemetry');
})().catch((e) => { console.error('uicheck error:', e.message); process.exit(1); });
