const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

function dashboard() {
  const elements = new Map();
  function element(id) {
    if (!elements.has(id)) {
      const classes = new Set();
      elements.set(id, {
        style: {}, dataset: {}, textContent: '', innerHTML: '', disabled: false,
        classList: {
          add: x => classes.add(x), remove: x => classes.delete(x),
          contains: x => classes.has(x),
          toggle: (x, on) => on ? classes.add(x) : classes.delete(x),
        },
      });
    }
    return elements.get(id);
  }
  const timers = [];
  const context = vm.createContext({
    console, Headers, AbortController, window: {},
    document: {
      hidden: false, getElementById: element, querySelectorAll: () => [],
      querySelector: () => null, addEventListener: () => {},
    },
    localStorage: { getItem: () => null, setItem: () => {} },
    setTimeout: (fn, ms) => { timers.push({fn, ms}); return timers.length; },
    clearTimeout: () => {}, setInterval: () => 1, clearInterval: () => {},
    fetch: () => { throw new Error('Unexpected network request: all tests must mock fetch'); },
  });
  const html = fs.readFileSync(path.join(__dirname, '../app/static/index.html'), 'utf8');
  const script = html.match(/<script>([\s\S]*?)<\/script>/)[1];
  // Suppress only boot polling; run the real definitions and initialization.
  vm.runInContext(script.slice(0, script.indexOf('// ── polling')), context);
  return { context, element, timers, run: code => vm.runInContext(code, context) };
}

const response = data => ({ ok: true, status: 200, json: async () => data });

test('mutations carry the mandatory header; reads do not', async () => {
  const d = dashboard();
  const requests = [];
  d.context.fetch = async (url, options) => { requests.push({url, options}); return response({}); };
  await d.run("apiFetch('/api/wake', {method:'POST'})");
  await d.run("apiFetch('/api/features')");
  assert.equal(requests[0].options.headers.get('X-Homelab-Request'), '1');
  assert.equal(requests[1].options.headers.has('X-Homelab-Request'), false);
});

test('Portainer host names are escaped in the action label', () => {
  const d = dashboard();
  d.run(`selected = {name:'fixture', host:'<img src=x onerror="boom()">', state:'running'};
         updateActionBar();`);
  assert.match(d.element('sel-label').innerHTML, /&lt;img/);
  assert.doesNotMatch(d.element('sel-label').innerHTML, /<img/);
});

test('stream polling preserves a visible armed confirmation', async () => {
  const d = dashboard();
  d.context.fetch = async () => response({ state: 'ready' });
  d.run("_features.stream=true; streamToggle('deck')");
  await d.run('fetchStream()');
  assert.equal(d.element('btn-stream-up-deck').textContent, '⚠ CONFIRM?');
  assert.equal(d.run('_streamArmed'), 'deck');
});

test('router polling cannot override an outstanding cycle cooldown', async () => {
  const d = dashboard();
  const posts = [];
  d.context.fetch = async (url, options) => {
    if (options.method === 'POST') posts.push(url);
    return response(options.method === 'POST' ? { ok: true } : { output: true });
  };
  d.run('_features.shelly2=true; shellyCycle2(); shellyCycle2()');
  await new Promise(resolve => setImmediate(resolve));
  await d.run('fetchShelly2()');
  assert.equal(d.element('btn-cycle-2').disabled, true);
  d.run('shellyCycle2(); shellyCycle2()');
  assert.equal(posts.length, 1);
});

test('HTML and HTTP polling failures mark retained usage stale, recovery clears it', async () => {
  const d = dashboard();
  d.run('_features.opencode_go=true');
  d.context.fetch = async () => response({ rolling: {percent:23, status:'ok'} });
  await d.run('fetchOpenCodeGo()');
  const lastGood = d.element('ocg-list').innerHTML;
  assert.match(lastGood, /23%/);
  for (const bad of [
    { ok:false, status:502, json:async () => { throw new Error('HTML'); } },
    { ok:true, status:200, json:async () => { throw new Error('HTML Access login'); } },
  ]) {
    d.context.fetch = async () => bad;
    await d.run('fetchOpenCodeGo()');
    assert.equal(d.element('ocg-list').innerHTML, lastGood);
    assert.equal(d.element('ocg-list').classList.contains('poll-stale'), true);
  }
  d.context.fetch = async () => response({ rolling: {percent:24, status:'ok'} });
  await d.run('fetchOpenCodeGo()');
  assert.equal(d.element('ocg-list').classList.contains('poll-stale'), false);
});

test('failed container polling disables controls and prevents action submission', async () => {
  const d = dashboard();
  d.context.fetch = async () => response([{id:'fixture',name:'target',host:'host',state:'running'}]);
  await d.run('fetchContainers()');
  d.run("selectContainer('fixture')");
  assert.equal(d.element('btn-stop').disabled, false);
  d.context.fetch = async () => { throw new Error('offline'); };
  await d.run('fetchContainers()');
  assert.equal(d.element('btn-stop').disabled, true);
  let posts = 0;
  d.context.fetch = async () => { posts++; return response({ok:true}); };
  await d.run("doAction('stop')");
  assert.equal(posts, 0);
});

test('container action result uses its original target despite selection changes', async () => {
  const d = dashboard();
  let complete;
  d.context.fetch = () => new Promise(resolve => { complete = resolve; });
  d.run("_containersFresh=true; selected={id:'original',name:'actual-target',host:'host',eid:1,state:'running'}");
  const pending = d.run("doAction('stop')");
  d.run('selected=null');
  complete(response({ok:true}));
  await pending;
  assert.equal(d.element('toast').textContent, 'stop → actual-target');
});

test('hung polling is aborted and marked stale', async () => {
  const d = dashboard();
  d.context.fetch = (_url, options) => new Promise((_resolve, reject) => {
    options.signal.addEventListener('abort', () => reject(new Error('timeout')));
  });
  const pending = d.run("fetchJSON('/api/stats')");
  const rejected = assert.rejects(pending, /timeout/);
  d.timers.find(timer => timer.ms === 25000).fn();
  await rejected;
  assert.equal(d.element('stat-grid').classList.contains('poll-stale'), true);
});
