const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

function dashboard(options = {}) {
  const elements = new Map();
  const tabButtons = new Map();
  function element(id) {
    if (!elements.has(id)) {
      const classes = new Set();
      const attributes = {};
      elements.set(id, {
        id, style: {}, dataset: {}, textContent: '', innerHTML: '', value: '', disabled: false,
        open: false, scrollLeft: 0, scrollWidth: 0, clientWidth: 0,
        offsetLeft: 0, offsetWidth: 0,
        setAttribute: (name, value) => { attributes[name] = String(value); },
        getAttribute: name => attributes[name],
        addEventListener: () => {}, removeEventListener: () => {},
        appendChild: () => {}, remove: () => {}, click() { this.clicked = true; },
        focus() { this.focused = true; },
        classList: {
          add: x => classes.add(x), remove: x => classes.delete(x),
          contains: x => classes.has(x),
          toggle: (x, on) => on ? classes.add(x) : classes.delete(x),
        },
      });
    }
    return elements.get(id);
  }
  function tabButton(name) {
    if (!tabButtons.has(name)) {
      const btn = element('tab-btn-' + name);
      btn.dataset.tab = name;
      tabButtons.set(name, btn);
    }
    return tabButtons.get(name);
  }
  const storage = new Map(Object.entries(options.localStorage || {}));
  if (options.savedTab) storage.set('activeTab', options.savedTab);
  const timers = [];
  const clearedTimers = new Set();
  const context = vm.createContext({
    console, Headers, AbortController, window: {},
    clearedTimers,
    document: {
      hidden: !!options.hidden, body: { style: {}, appendChild: () => {} },
      fonts: options.fonts,
      getElementById: element,
      querySelectorAll: selector => selector === '.tab-btn' ? [...tabButtons.values()] : [],
      querySelector: selector => {
        const matched = /^\.tab-btn\[data-tab="([^"]+)"\]$/.exec(selector);
        if (matched) return tabButton(matched[1]);
        if (selector === '.tab-btn.active') {
          for (const button of tabButtons.values()) {
            if (button.classList.contains('active')) return button;
          }
        }
        return null;
      },
      addEventListener: () => {}, createElement: () => element('created-' + elements.size),
    },
    localStorage: {
      getItem: key => storage.has(key) ? storage.get(key) : null,
      setItem: (key, value) => { storage.set(key, String(value)); },
      removeItem: key => { storage.delete(key); },
    },
    setTimeout: (fn, ms) => { const timer = {fn, ms, id: timers.length + 1}; timers.push(timer); return timer.id; },
    clearTimeout: id => { clearedTimers.add(id); }, setInterval: () => 1, clearInterval: () => {},
    fetch: () => { throw new Error('Unexpected network request: all tests must mock fetch'); },
  });
  const html = fs.readFileSync(path.join(__dirname, '../app/static/index.html'), 'utf8');
  const script = html.match(/<script>([\s\S]*?)<\/script>/)[1];
  // Suppress only boot polling; run the real definitions and initialization.
  vm.runInContext(script.slice(0, script.indexOf('// ── polling')), context);
  return { context, element, timers, clearedTimers, run: code => vm.runInContext(code, context) };
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

test('apiPost sends the guard header and aborts hung mutations', async () => {
  const d = dashboard();
  let seen;
  d.context.fetch = (_url, options) => new Promise((_resolve, reject) => {
    seen = options;
    options.signal.addEventListener('abort', () => reject(new Error('timeout')));
  });
  const pending = d.run("apiPost('/api/wake')");
  assert.equal(seen.method, 'POST');
  assert.equal(seen.headers.get('X-Homelab-Request'), '1');
  const rejected = assert.rejects(pending, /timeout/);
  d.timers.find(timer => timer.ms === 25000).fn();
  await rejected;
});

test('a hung container action times out and releases its busy state', async () => {
  const d = dashboard();
  d.context.fetch = (_url, options) => new Promise((_resolve, reject) => {
    options.signal.addEventListener('abort', () => reject(new Error('timeout')));
  });
  const pending = d.run("_containersFresh=true; selected={id:'fixture',eid:1,name:'fixture',host:'host',state:'running'}; doAction('stop')");
  assert.equal(d.run('busy'), true);
  d.timers.find(timer => timer.ms === 25000).fn();
  await pending;
  assert.equal(d.run('busy'), false);
  assert.match(d.element('toast').textContent, /network error/);
});

test('a hung router cycle times out and recovers after its cooldown', async () => {
  const d = dashboard();
  d.context.fetch = (_url, options) => new Promise((_resolve, reject) => {
    options.signal.addEventListener('abort', () => reject(new Error('timeout')));
  });
  d.run('_features.shelly2=true; shellyCycle2(); shellyCycle2()');
  assert.equal(d.run('_cycleBusy2'), true);
  d.timers.find(timer => timer.ms === 25000).fn();
  await new Promise(resolve => setImmediate(resolve));
  await new Promise(resolve => setImmediate(resolve));
  const recovery = d.timers.find(timer => timer.ms === 3000);
  assert.ok(recovery, 'catch path schedules the cooldown reset');
  recovery.fn();
  assert.equal(d.run('_cycleBusy2'), false);
  assert.equal(d.element('btn-cycle-2').disabled, false);
});

test('stream safety timer is cleared on confirmation and by a new action', async () => {
  const s = dashboard();
  s.context.fetch = async () => ({ ok: true, json: async () => ({ state: 'headless' }) });
  s.run('_features.stream=true');
  await s.run('fetchStream()');
  s.context.fetch = async (_url, options) => options.method === 'POST'
    ? { ok: true, json: async () => ({ ok: true }) }
    : { ok: true, json: async () => ({ state: 'streaming' }) };
  s.run("streamToggle('deck'); streamToggle('deck')");
  await new Promise(resolve => setImmediate(resolve));
  const safety = s.timers.find(timer => timer.ms === 180000);
  assert.ok(safety);
  await s.run('fetchStream()');
  assert.equal(s.run('_streamBusy'), false);
  assert.ok(s.context.clearedTimers.has(safety.id));
  s.run("_streamSafetyTimer = 'stale'; streamToggle('down'); streamToggle('down')");
  assert.ok(s.context.clearedTimers.has('stale'));
  await new Promise(resolve => setImmediate(resolve));
  const active = s.timers.filter(timer => timer.ms === 180000 && !s.context.clearedTimers.has(timer.id));
  assert.equal(active.length, 1);
  assert.equal(s.run('_streamBusy'), true);
});

// ── SECURITY TAB ──────────────────────────────────────────────────────────────
const settle = async () => {
  await new Promise(resolve => setImmediate(resolve));
  await new Promise(resolve => setImmediate(resolve));
};

function finding(overrides = {}) {
  return {
    component: 'container:api', advisory: 'GHSA-aaaa', title: 'API request smuggling',
    installed_version: '1.0.0', affected_versions: '< 1.2.0', fixed_version: '1.2.0',
    reason: 'The installed version is affected.', action: 'Update the image to 1.2.0.',
    priority: 'act_now', status: 'confirmed', confidence: 'high', exploited: 'unknown',
    sources: ['https://osv.dev/vulnerability/GHSA-aaaa'],
    ...overrides,
  };
}

function securityReport(overrides = {}) {
  const checked = new Date(Date.now() - 2 * 60 * 60 * 1000).toISOString();
  return {
    checked_at: checked,
    summary: 'One urgent advisory, one planned update and one issue to monitor.',
    checked_components: ['container:api', 'host:openssl'],
    limitations: ['Selected host and Python packages only; no full image scan.'],
    findings: [
      finding(),
      finding({
        advisory: 'GHSA-bbbb', title: 'TLS certificate verification issue',
        priority: 'update_soon', status: 'possible', confidence: 'medium',
        component: 'host:openssl', installed_version: '3.0.1', fixed_version: '3.0.2',
        sources: ['https://osv.dev/vulnerability/GHSA-bbbb'],
      }),
      finding({
        advisory: 'GHSA-cccc', title: 'Low-impact parser edge case',
        priority: 'monitor', status: 'unknown', confidence: 'low', exploited: 'no',
        component: 'container:api', installed_version: '1.0.0', fixed_version: '1.3.0',
        sources: ['https://osv.dev/vulnerability/GHSA-cccc'],
      }),
    ],
    assessments: [finding({
      advisory: 'GHSA-dddd', title: 'Evaluated as not affected',
      component: 'container:api', status: 'not_affected', priority: 'monitor',
      reason: 'The vulnerable parser is not enabled in this deployment.',
      action: 'No change required; retain the configuration.',
      sources: ['https://osv.dev/vulnerability/GHSA-dddd'],
    })],
    rejected_assessments: [],
    inventory: {
      collected_at: checked,
      host: { alias: 'homelab', distribution: 'ubuntu', release: '24.04', kernel: '6.8.0' },
      components: [{ id: 'api', image: 'ghcr.io/example/api:1.0', state: 'running', version: '1.0.0' }],
      packages: [{ component: 'api', ecosystem: 'PyPI', name: 'cryptography', version: '41.0.0', source: 'runtime Python metadata' }],
      coverage_gaps: ['Selected packages only.'],
      collection_errors: [],
    },
    advisory_checks: {
      checked_at: checked,
      osv_matches: [{ advisory: 'GHSA-aaaa', url: 'https://osv.dev/vulnerability/GHSA-aaaa' }],
      kev_candidates: [],
      errors: [],
      kev_source: 'https://www.cisa.gov/sites/default/files/feeds/known_exploited_vulnerabilities.json',
      kev_catalog_version: '2026.10.28',
    },
    source_checks_complete: true,
    dependabot: {
      open: [{ branch: 'dependabot/docker-compose/api', opened: '2026-10-27', tip: 'abc1234' }],
      new: [],
    },
    searches: 3,
    usage: { prompt_tokens: 100, completion_tokens: 20 },
    search_sources: ['https://osv.dev/vulnerability/GHSA-aaaa'],
    ...overrides,
  };
}

// Only the fields the producer always emits: every optional section is absent.
function minimalSecurityReport(overrides = {}) {
  return {
    checked_at: new Date(Date.now() - 60 * 1000).toISOString(),
    summary: 'Minimal report with required fields only.',
    checked_components: [],
    limitations: [],
    findings: [],
    searches: 0,
    usage: {},
    ...overrides,
  };
}

const acceptReport = (d, report) => d.run(`securityAcceptReport(${JSON.stringify(report)})`);

test('mobile tab row keeps one swipeable row with 44px targets and edge cues', () => {
  const html = fs.readFileSync(path.join(__dirname, '../app/static/index.html'), 'utf8');
  assert.match(html, /role="tablist"[^>]*aria-label="Dashboard sections"[^>]*onkeydown="tabKeydown\(event\)"/);
  for (const name of ['server', 'opencode', 'mistral', 'containers', 'processes', 'security']) {
    assert.match(html, new RegExp(`id="tab-${name}" class="tab-pane" role="tabpanel" aria-labelledby="tab-btn-${name}"`));
    assert.match(html, new RegExp(`id="tab-btn-${name}"[^>]*aria-controls="tab-${name}"`));
  }
  assert.match(html, /\.tab-bar\s*\{[^}]*flex-wrap:nowrap[^}]*overflow-x:auto/s);
  assert.match(html, /\.tab-btn\s*\{[^}]*flex:0 0 auto[^}]*min-width:44px[^}]*min-height:44px/s);
  assert.match(html, /\.tab-bar-wrap\.can-left::before/);
  assert.match(html, /\.tab-bar-wrap\.can-right::after/);
  // Font metrics settle after restore; the hook must be optional for the VM harness.
  assert.match(html, /function syncTabBarAfterFonts\(\)/);
  assert.match(html, /fonts\.ready\.then\(syncTabBarAfterFonts/);
  assert.match(html, /addEventListener\('loadingdone', syncTabBarAfterFonts\)/);
  assert.match(html, /if \(!fonts\) return;/);
});

test('tablist arrows/Home/End move among visible tabs, activate and focus them', () => {
  const d = dashboard();
  const names = ['server', 'opencode', 'mistral', 'containers', 'processes', 'security'];
  const tabs = {};
  for (const name of names) {
    tabs[name] = d.context.document.querySelector(`.tab-btn[data-tab="${name}"]`);
    tabs[name].tabIndex = name === 'server' ? 0 : -1;
  }
  for (const name of ['opencode', 'mistral', 'security']) tabs[name].style.display = 'none';

  d.run(`switchTab('server', document.querySelector('.tab-btn[data-tab="server"]'))`);
  assert.equal(tabs.server.tabIndex, 0);
  assert.equal(tabs.containers.tabIndex, -1);

  d.run(`tabKeydown({key:'ArrowRight', target: document.querySelector('.tab-btn[data-tab="server"]'), preventDefault(){}})`)
  assert.equal(d.run('_activeTab'), 'containers');
  assert.equal(tabs.containers.classList.contains('active'), true);
  assert.equal(tabs.containers.tabIndex, 0);
  assert.equal(tabs.server.tabIndex, -1);
  assert.equal(tabs.containers.focused, true);

  d.run(`tabKeydown({key:'ArrowLeft', target: document.querySelector('.tab-btn[data-tab="containers"]'), preventDefault(){}})`)
  assert.equal(d.run('_activeTab'), 'server');

  d.run(`tabKeydown({key:'End', target: document.querySelector('.tab-btn[data-tab="server"]'), preventDefault(){}})`)
  assert.equal(d.run('_activeTab'), 'processes');

  d.run(`tabKeydown({key:'Home', target: document.querySelector('.tab-btn[data-tab="processes"]'), preventDefault(){}})`)
  assert.equal(d.run('_activeTab'), 'server');

  d.run(`tabKeydown({key:'ArrowLeft', target: document.querySelector('.tab-btn[data-tab="server"]'), preventDefault(){}})`)
  assert.equal(d.run('_activeTab'), 'processes');
  assert.equal(tabs.processes.focused, true);
});

test('modified arrow shortcuts are left to the browser', () => {
  const d = dashboard();
  d.run(`switchTab('server', document.querySelector('.tab-btn[data-tab="server"]'))`);
  for (const key of ['ArrowLeft', 'ArrowRight', 'Home', 'End']) {
    for (const modifier of ['altKey', 'ctrlKey', 'metaKey']) {
      let prevented = false;
      d.context.modEvent = {
        key,
        target: d.context.document.querySelector('.tab-btn[data-tab="server"]'),
        preventDefault() { prevented = true; },
      };
      d.context.modEvent[modifier] = true;
      d.run('tabKeydown(modEvent)');
      assert.equal(prevented, false, `${modifier}+${key} must not be prevented`);
      assert.equal(d.run('_activeTab'), 'server');
    }
  }
});

test('security tab is feature-gated and a restored hidden tab falls back to server', () => {
  const d = dashboard({ savedTab: 'security' });
  const securityTab = d.context.document.querySelector('.tab-btn[data-tab="security"]');
  assert.equal(d.run('_activeTab'), 'security');
  d.run('securitySyncFeature()');
  assert.equal(securityTab.style.display, 'none');
  assert.equal(d.run('_activeTab'), 'server');
  d.run('_features.security=true; securitySyncFeature()');
  assert.equal(securityTab.style.display, 'inline-block');
  assert.equal(d.run('_activeTab'), 'server');
});

test('the security report endpoint is never requested while the feature flag is off', async () => {
  const d = dashboard();
  let requests = 0;
  d.context.fetch = async () => { requests++; return response({}); };
  d.run(`_features.security=false;
         switchTab('security', document.querySelector('.tab-btn[data-tab="security"]'));
         securityTick();
         securitySyncFeature();`);
  await settle();
  assert.equal(requests, 0);
  assert.equal(d.run('_activeTab'), 'server');
});

test('restored security tab fetches only after enabling, over a GET without mutations', async () => {
  const d = dashboard({ savedTab: 'security' });
  const requests = [];
  d.context.fetch = async (url, options) => {
    requests.push({ url, options });
    return response({ report: securityReport() });
  };
  d.run('_features.security=true; securitySyncFeature()');
  await settle();
  assert.equal(requests.length, 1);
  assert.equal(requests[0].url, '/api/security');
  assert.equal(requests[0].options.method, undefined);
  assert.equal(requests[0].options.cache, 'no-store');
  assert.equal(requests[0].options.headers.has('X-Homelab-Request'), false);
  assert.equal(d.run('_activeTab'), 'security');
  assert.match(d.element('sec-meta').innerHTML, /report checked/);
});

test('security polling is active/visible-only and dedupes an inflight request', async () => {
  const d = dashboard();
  let requests = 0;
  let complete;
  d.context.fetch = () => {
    requests++;
    return new Promise(resolve => { complete = resolve; });
  };
  d.run('_features.security=true; document.hidden=false; _activeTab="server"; securityTick()');
  assert.equal(requests, 0);
  d.run('_activeTab="security"; document.hidden=true; securityTick()');
  assert.equal(requests, 0);
  d.run('document.hidden=false; securityTick(); securityTick()');
  assert.equal(requests, 1);
  complete(response({ report: securityReport() }));
  await settle();
  assert.equal(d.run('_securityInFlight'), null);
  d.context.fetch = async () => {
    requests++;
    return response({ report: securityReport() });
  };
  d.run('securityTick()');
  await settle();
  assert.equal(requests, 2);
});

test('security counts, filters and search report shown/total and keep default urgent cards open', () => {
  const d = dashboard();
  d.run('_features.security=true');
  acceptReport(d, securityReport());
  assert.equal(d.element('sec-count-act_now').textContent, '1');
  assert.equal(d.element('sec-count-update_soon').textContent, '1');
  assert.equal(d.element('sec-count-monitor').textContent, '1');
  assert.equal(d.element('sec-shown').textContent, '3 / 3 shown');
  const html = d.element('sec-findings').innerHTML;
  assert.match(html, /data-priority="act_now"[^>]*\bopen\b/);
  assert.doesNotMatch(html, /data-priority="monitor"[^>]*\bopen\b/);

  d.run("securitySetFilter('act_now')");
  assert.equal(d.element('sec-shown').textContent, '1 / 3 shown');
  assert.match(d.element('sec-findings').innerHTML, /GHSA-aaaa/);
  assert.doesNotMatch(d.element('sec-findings').innerHTML, /GHSA-bbbb/);
  assert.equal(d.element('sec-filter-act_now').classList.contains('active'), true);
  assert.equal(d.element('sec-filter-all').classList.contains('active'), false);
  assert.equal(d.element('sec-filter-act_now').getAttribute('aria-pressed'), 'true');
  assert.equal(d.element('sec-filter-all').getAttribute('aria-pressed'), 'false');

  d.run("securitySetFilter('all')");
  d.element('sec-search').value = 'openssl';
  d.run('securitySearchChanged()');
  assert.equal(d.element('sec-shown').textContent, '1 / 3 shown');
  assert.match(d.element('sec-findings').innerHTML, /GHSA-bbbb/);
  assert.doesNotMatch(d.element('sec-findings').innerHTML, /GHSA-aaaa/);

  d.element('sec-search').value = '';
  d.run('securitySearchChanged(); securityExpandAll(true)');
  assert.match(d.element('sec-findings').innerHTML, /data-priority="monitor"[^>]*\bopen\b/);
  d.run('securityExpandAll(false)');
  assert.doesNotMatch(d.element('sec-findings').innerHTML, /data-priority="act_now"[^>]*\bopen\b/);
});

test('report strings are escaped and only safe public https sources become links', () => {
  const d = dashboard();
  d.run('_features.security=true');
  const unsafe = [
    'http://example.com/advisory', 'javascript:alert(1)',
    'https://user:pass@example.com/advisory', 'https://localhost/advisory',
    'https://127.0.0.1/advisory', 'https://192.168.1.10/advisory',
    'https://[::1]/advisory', 'https://example.com:8443/advisory',
    'https://example.com\\@evil.com/advisory',
  ];
  acceptReport(d, securityReport({
    findings: [finding({
      component: '<img src=x onerror="boom()">',
      advisory: 'ADV-<b>bold</b>',
      title: '<script>alert(1)</script>',
      reason: 'untrusted <iframe> model text',
      action: 'do not render <a href="javascript:alert(2)">this</a>',
      sources: ['https://osv.dev/vulnerability/SAFE', ...unsafe],
    })],
  }));
  const html = d.element('sec-findings').innerHTML;
  assert.match(html, /&lt;img/);
  assert.doesNotMatch(html, /<img/);
  assert.match(html, /&lt;script/);
  assert.doesNotMatch(html, /<script/);
  assert.match(html, /href="https:\/\/osv\.dev\/vulnerability\/SAFE"/);
  assert.match(html, /target="_blank" rel="noopener noreferrer"/);
  assert.match(html, /unsafe link withheld/);
  for (const url of unsafe) {
    const href = `href="${url.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')}"`;
    assert.doesNotMatch(html, new RegExp(href));
  }
  const safe = url => d.run(`securitySafeUrl(${JSON.stringify(url)})`);
  assert.equal(safe('https://osv.dev/vulnerability/X'), true);
  assert.equal(safe('https://example.com:443/x'), true);
  assert.equal(safe('https://github.com/allesgeklaut/home-lab/pulls?q=x'), true);
  for (const url of unsafe) assert.equal(safe(url), false);
  assert.equal(safe('https://10.0.0.5/x'), false);
  assert.equal(safe('https://169.254.169.254/latest/meta-data'), false);
});

test('a zero-finding report never reads as an all-clear', () => {
  const d = dashboard();
  d.run('_features.security=true');
  acceptReport(d, securityReport({ findings: [], assessments: [], summary: 'No actionable findings.' }));
  assert.equal(d.element('sec-shown').textContent, 'no findings');
  assert.equal(d.element('sec-count-act_now').textContent, '0');
  const html = d.element('sec-findings').innerHTML;
  assert.match(html, /NO ACTIONABLE FINDINGS IN THIS REPORT/);
  assert.match(html, /coverage is limited/i);
  assert.doesNotMatch(html, /all.?clear/i);
});

test('stale reports warn; failed refreshes retain and mark the previous report and snapshot', async () => {
  const d = dashboard();
  d.run('_features.security=true');
  const stale = securityReport({
    checked_at: new Date(Date.now() - 37 * 60 * 60 * 1000).toISOString(),
  });
  d.context.fetch = async () => response({ report: stale });
  await d.run('securityRefresh()');
  assert.match(d.element('sec-alerts').innerHTML, /REPORT STALE/);
  assert.match(d.element('sec-alerts').innerHTML, /36 HOURS/);
  assert.match(d.element('sec-meta').innerHTML, /report checked/);
  assert.match(d.element('sec-meta').innerHTML, /ago/);
  const firstSnapshot = d.run('securityReportJSON()');
  const firstChecked = d.run('_securityReport.checked_at');
  assert.equal(JSON.parse(firstSnapshot).checked_at, stale.checked_at);

  d.context.fetch = async () => ({
    ok: false, status: 503,
    json: async () => ({ error: 'Report missing', code: 'missing' }),
  });
  await d.run('securityRefresh()');
  assert.equal(d.run('_securityReport.checked_at'), firstChecked);
  assert.equal(d.run('securityReportJSON()'), firstSnapshot);
  assert.match(d.element('sec-alerts').innerHTML, /REFRESH FAILED/);
  assert.match(d.element('sec-alerts').innerHTML, /not fresh/i);
  assert.equal(d.element('sec-content').style.display, 'block');
  assert.equal(d.element('sec-unavailable').style.display, 'none');

  const fresh = securityReport({ summary: 'Recovered report.' });
  d.context.fetch = async () => response({ report: fresh });
  await d.run('securityRefresh()');
  assert.doesNotMatch(d.element('sec-alerts').innerHTML, /REFRESH FAILED/);
  assert.notEqual(d.run('securityReportJSON()'), firstSnapshot);
  assert.equal(JSON.parse(d.run('securityReportJSON()')).summary, 'Recovered report.');
});

test('a missing report shows a clean escaped error instead of zero counts', async () => {
  const d = dashboard();
  d.run('_features.security=true; _activeTab="security"');
  d.context.fetch = async () => ({
    ok: false, status: 404,
    json: async () => ({ error: '<b>No report</b>', code: 'missing' }),
  });
  await d.run('securityRefresh()');
  assert.equal(d.element('sec-content').style.display, 'none');
  assert.equal(d.element('sec-unavailable').style.display, 'block');
  const html = d.element('sec-unavailable').innerHTML;
  assert.match(html, /SECURITY REPORT UNAVAILABLE/);
  assert.match(html, /No security report has been generated yet\./);
  assert.match(html, /&lt;b&gt;No report/);
  assert.doesNotMatch(html, /<b>No report/);
  assert.match(html, /code: missing/);
  assert.equal(d.element('btn-sec-json').disabled, true);
});

test('assessment evidence, excluded claims, coverage, dependabot and provenance are all rendered', () => {
  const d = dashboard();
  d.run('_features.security=true');
  const report = securityReport({
    source_checks_complete: false,
    rejected_assessments: [{
      component: 'container:api', advisory: 'GHSA-eeee',
      reason: 'Agent assessment did not cite an allowed source.',
    }],
    findings: [
      finding({ priority: 'update_soon', not_rechecked: true, advisory: 'GHSA-ffff' }),
    ],
    advisory_checks: {
      ...securityReport().advisory_checks,
      errors: ['OSV returned incomplete results'],
    },
    inventory: {
      ...securityReport().inventory,
      collection_errors: ['remote: SSH inventory failed'],
    },
    dependabot: {
      open: [
        { branch: 'dependabot/docker-compose/api', opened: '2026-10-27', tip: 'abc1234' },
        { branch: 'dependabot/docker-compose/web', opened: '2026-10-26', tip: 'def5678' },
      ],
      new: [{ branch: 'dependabot/docker-compose/api', opened: '2026-10-27', tip: 'abc1234' }],
    },
    searches: 4,
    usage: { prompt_tokens: 120, completion_tokens: 30 },
    search_sources: ['https://osv.dev/vulnerability/GHSA-ffff', 'javascript:alert(1)'],
  });
  acceptReport(d, report);

  const assessments = d.element('sec-assessments').innerHTML;
  assert.match(assessments, /NOT AFFECTED/);
  assert.match(assessments, /vulnerable parser is not enabled/);
  assert.match(assessments, /href="https:\/\/osv\.dev\/vulnerability\/GHSA-dddd"/);

  const rejected = d.element('sec-rejected').innerHTML;
  assert.match(rejected, /EXCLUDED — NOT ACTIVE/);
  assert.match(rejected, /GHSA-eeee/);
  assert.match(rejected, /did not cite an allowed source/);

  const coverage = d.element('sec-coverage').innerHTML;
  assert.match(coverage, /source checks: errors reported/);
  assert.match(coverage, /status only/);
  assert.match(coverage, /resolution blocked/);
  assert.match(coverage, /OSV returned incomplete results/);
  assert.match(coverage, /remote: SSH inventory failed/);
  assert.match(coverage, /Selected host and Python packages only/);
  assert.match(coverage, /container:api/);

  const dependabot = d.element('sec-dependabot').innerHTML;
  assert.match(dependabot, /UNANNOUNCED/);
  assert.doesNotMatch(dependabot, />NEW</);
  assert.match(dependabot, /branch commit: 2026-10-27/);
  assert.match(dependabot, /tip: abc1234/);
  assert.match(dependabot, /https:\/\/github\.com\/allesgeklaut\/home-lab\/pulls\?q=is%3Apr%20head%3Adependabot%2Fdocker-compose%2Fapi/);
  assert.match(dependabot, /rel="noopener noreferrer"/);

  const details = d.element('sec-details').innerHTML;
  assert.match(details, /inventory — 1 component/);
  assert.match(details, /"cryptography"/);

  const provenance = d.element('sec-provenance').innerHTML;
  assert.match(provenance, /WEB SEARCH RUNS/);
  assert.match(provenance, />4</);
  assert.match(provenance, /prompt_tokens/);
  assert.match(provenance, /href="https:\/\/osv\.dev\/vulnerability\/GHSA-ffff"/);
  assert.match(provenance, /unsafe link withheld/);

  assert.match(d.element('sec-alerts').innerHTML, /SOURCE CHECK ERRORS/);
  assert.match(d.element('sec-alerts').innerHTML, /RESEARCH PARTIAL COVERAGE/);
  assert.match(d.element('sec-alerts').innerHTML, /NOT RECHECKED/);
  assert.match(d.element('sec-findings').innerHTML, /NOT RECHECKED/);
  assert.match(d.element('sec-findings').innerHTML, /Carried forward from an earlier report/);
});

test('JSON download snapshots exactly the currently displayed report', () => {
  const d = dashboard();
  d.run('_features.security=true');
  const report = securityReport();
  acceptReport(d, report);
  const snapshot = d.run('securityReportJSON()');
  assert.equal(JSON.parse(snapshot).checked_at, report.checked_at);
  let captured = null;
  d.context.Blob = function (parts, options) { captured = { parts, options }; };
  d.context.URL = { createObjectURL: () => 'blob:fixture', revokeObjectURL: () => {} };
  assert.equal(d.run('securityDownload()'), true);
  assert.ok(captured);
  assert.equal(String(captured.parts[0]), snapshot);
  assert.equal(captured.options.type, 'application/json');
});

test('the active tab can be revealed in the swipe row with both edge cues', () => {
  const d = dashboard();
  const bar = d.element('tab-bar');
  const wrap = d.element('tab-bar-wrap');
  bar.clientWidth = 180;
  bar.scrollWidth = 600;
  bar.scrollLeft = 0;
  d.run('revealActiveTab({offsetLeft: 420, offsetWidth: 76})');
  assert.ok(bar.scrollLeft > 0);
  assert.equal(wrap.classList.contains('can-left'), true);
  assert.equal(wrap.classList.contains('can-right'), true);

  bar.scrollLeft = 0;
  d.run('updateTabOverflowCues()');
  assert.equal(wrap.classList.contains('can-left'), false);
  assert.equal(wrap.classList.contains('can-right'), true);

  bar.scrollLeft = 420;
  d.run('updateTabOverflowCues()');
  assert.equal(wrap.classList.contains('can-left'), true);
  assert.equal(wrap.classList.contains('can-right'), false);

  bar.scrollLeft = 400;
  d.run('revealActiveTab({offsetLeft: 0, offsetWidth: 70})');
  assert.equal(bar.scrollLeft, 0);
});

test('webfont settle re-reveals the restored tab and clears the stale right cue', () => {
  let settle = null;
  const listeners = {};
  const fonts = {
    ready: { then: (onDone) => { settle = onDone; } },
    addEventListener: (type, fn) => { listeners[type] = fn; },
  };
  const d = dashboard({ savedTab: 'security', fonts });
  const bar = d.element('tab-bar');
  const wrap = d.element('tab-bar-wrap');
  const security = d.element('tab-btn-security');
  const body = d.context.document.body;
  assert.equal(d.run('_activeTab'), 'security');
  assert.equal(typeof settle, 'function');
  assert.equal(typeof listeners.loadingdone, 'function');

  // Restore runs on fallback metrics with SECURITY last: the row scrolls to
  // the computed maximum that exists before the webfont loads.
  bar.clientWidth = 300;
  bar.scrollWidth = 493;
  security.offsetLeft = 420;
  security.offsetWidth = 73;
  security.classList.add('active');
  d.run('revealActiveTab()');
  assert.equal(bar.scrollLeft, 193);
  assert.equal(wrap.classList.contains('can-right'), false);

  // The webfont lands: labels widen, the active tab is clipped again and the
  // right cue reappears even though nothing scrolled or resized.
  bar.scrollWidth = 514;
  security.offsetWidth = 75.64;
  d.run('updateTabOverflowCues()');
  assert.equal(wrap.classList.contains('can-right'), true);
  assert.ok(security.offsetLeft + security.offsetWidth > bar.scrollLeft + bar.clientWidth);

  const padding = body.style.paddingBottom;
  settle();
  assert.equal(bar.scrollLeft, 214);
  assert.equal(wrap.classList.contains('can-left'), true);
  assert.equal(wrap.classList.contains('can-right'), false);
  assert.ok(security.offsetLeft + security.offsetWidth <= bar.scrollLeft + bar.clientWidth);
  assert.equal(body.style.paddingBottom, padding); // horizontal-only: no vertical jump
  assert.equal(d.run('_activeTab'), 'security');

  // A late font subset (loadingdone) repeats the same sync.
  bar.scrollWidth = 493;
  bar.scrollLeft = 0;
  security.offsetWidth = 73;
  d.run('updateTabOverflowCues()');
  assert.equal(wrap.classList.contains('can-right'), true);
  bar.scrollWidth = 514;
  security.offsetWidth = 75.64;
  listeners.loadingdone();
  assert.equal(bar.scrollLeft, 214);
  assert.equal(wrap.classList.contains('can-right'), false);
});

test('absent optional sections stay unknown instead of inventing zero, none or success', () => {
  const d = dashboard();
  d.run('_features.security=true');
  acceptReport(d, minimalSecurityReport());

  assert.match(d.element('sec-assessments').innerHTML, /NOT REPORTED/);
  assert.doesNotMatch(d.element('sec-assessments').innerHTML, /No explicit not-affected/);

  assert.match(d.element('sec-rejected').innerHTML, /NOT REPORTED/);
  assert.doesNotMatch(d.element('sec-rejected').innerHTML, /No claims were excluded/);

  const coverage = d.element('sec-coverage').innerHTML;
  assert.match(coverage, /source checks: not reported — status unknown/);
  assert.match(coverage, /inventory: not reported/);
  assert.match(coverage, /advisory checks: not reported/);
  assert.match(coverage, /resolution status: not reported/);
  assert.doesNotMatch(coverage, /reported complete|reported incomplete|errors reported/);
  assert.doesNotMatch(coverage, /0 coverage gaps/);

  const dependabot = d.element('sec-dependabot').innerHTML;
  assert.match(dependabot, /NOT REPORTED/);
  assert.match(dependabot, /availability is unknown/);
  assert.doesNotMatch(dependabot, /No pending Dependabot update branches/);

  const details = d.element('sec-details').innerHTML;
  assert.equal((details.match(/NOT REPORTED/g) || []).length, 2);
  assert.doesNotMatch(details, /0 components|0 OSV matches/);

  const provenance = d.element('sec-provenance').innerHTML;
  assert.match(provenance, /SEARCH SOURCES[\s\S]*?not reported/);
  assert.match(provenance, /search sources — NOT REPORTED/);
  assert.match(provenance, /usage — full JSON/);

  const alerts = d.element('sec-alerts').innerHTML;
  assert.doesNotMatch(alerts, /SOURCE CHECK|INVENTORY ERRORS/);
});

test('explicitly empty optional sections may state none reported', () => {
  const d = dashboard();
  d.run('_features.security=true');
  const checked = new Date(Date.now() - 60 * 1000).toISOString();
  acceptReport(d, securityReport({
    assessments: [],
    rejected_assessments: [],
    inventory: {
      collected_at: checked, components: [], packages: [],
      coverage_gaps: [], collection_errors: [],
    },
    advisory_checks: {
      checked_at: checked, osv_matches: [], kev_candidates: [], errors: [],
    },
    source_checks_complete: true,
    dependabot: { open: [], new: [] },
    search_sources: [],
  }));

  assert.match(d.element('sec-assessments').innerHTML, /No explicit not-affected assessments were reported/);
  assert.match(d.element('sec-rejected').innerHTML, /No claims were excluded/);
  const coverage = d.element('sec-coverage').innerHTML;
  assert.match(coverage, /source checks: reported complete/);
  assert.match(coverage, /status only, not an all-clear/);
  assert.match(coverage, /0 coverage gaps/);
  assert.doesNotMatch(coverage, /inventory: not reported/);
  assert.doesNotMatch(coverage, /resolution status: not reported|resolution blocked/);
  const dependabot = d.element('sec-dependabot').innerHTML;
  assert.match(dependabot, /No pending Dependabot update branches/);
  assert.doesNotMatch(dependabot, /UNKNOWN|NOT REPORTED/);
  const details = d.element('sec-details').innerHTML;
  assert.match(details, /inventory — 0 components/);
  assert.match(details, /advisory checks — 0 OSV matches/);
  assert.match(d.element('sec-provenance').innerHTML, /search sources — 0/);
  assert.match(d.element('sec-provenance').innerHTML, /No search sources were reported/);
});

test('source-check banner needs an explicit incomplete flag or real errors', () => {
  const d = dashboard();
  d.run('_features.security=true');

  acceptReport(d, minimalSecurityReport({ advisory_checks: { errors: ['OSV failed'] } }));
  assert.match(d.element('sec-alerts').innerHTML, /SOURCE CHECK ERRORS/);
  assert.match(d.element('sec-coverage').innerHTML, /source checks: errors reported/);

  acceptReport(d, minimalSecurityReport({ advisory_checks: { errors: [] } }));
  assert.doesNotMatch(d.element('sec-alerts').innerHTML, /SOURCE CHECK/);
  assert.match(d.element('sec-coverage').innerHTML, /source checks: not reported — status unknown/);

  acceptReport(d, minimalSecurityReport({ source_checks_complete: false }));
  assert.match(d.element('sec-alerts').innerHTML, /SOURCE CHECKS REPORTED INCOMPLETE/);
  assert.match(d.element('sec-coverage').innerHTML, /source checks: reported incomplete/);
  assert.match(d.element('sec-coverage').innerHTML, /check status only/);
  assert.match(d.element('sec-alerts').innerHTML, /not a resolution status/);
});

test('a not-affected assessment never dismisses a retained matching finding', () => {
  const d = dashboard();
  d.run('_features.security=true');
  const hostile = 'container:api<img src=x onerror="boom()">';
  acceptReport(d, securityReport({
    assessments: [finding({
      component: hostile, advisory: 'ghsa-aaaa', status: 'not_affected', confidence: 'high',
      reason: 'The vulnerable parser is disabled.', action: 'No change required.',
    })],
    findings: [finding({ component: hostile, advisory: 'GHSA-aaaa', not_rechecked: true })],
  }));

  const html = d.element('sec-assessments').innerHTML;
  assert.match(html, /NOT DISMISSED/);
  assert.match(html, /still listed as an active finding/);
  assert.match(html, /retained from an earlier report and not rechecked this run/);
  assert.match(html, /remains active; this assessment did not dismiss it/);
  assert.match(html, /source and inventory checks are complete/);
  assert.match(html, /no claims were\s+excluded/);
  assert.match(html, /confidence is high/);
  assert.match(html, /component was checked this run/);
  assert.doesNotMatch(html, /RESOLVED/);
  assert.doesNotMatch(html, />DISMISSED</);
  assert.match(html, /&lt;img/);
  assert.doesNotMatch(html, /<img/);
  assert.match(d.element('sec-findings').innerHTML, /GHSA-aaaa/);
});

test('excluded claims are inactive and block every not-affected dismissal', () => {
  const d = dashboard();
  d.run('_features.security=true');
  acceptReport(d, securityReport({
    source_checks_complete: true,
    rejected_assessments: [{
      component: 'container:api', advisory: 'GHSA-eeee',
      reason: 'Assessment did not cite an allowed source.',
    }],
    assessments: [finding({
      component: 'host:openssl', advisory: 'GHSA-bbbb', status: 'not_affected', confidence: 'high',
    })],
    findings: [finding({ component: 'host:openssl', advisory: 'GHSA-bbbb', not_rechecked: true })],
  }));

  const coverage = d.element('sec-coverage').innerHTML;
  assert.match(coverage, /source checks: reported complete/);
  assert.match(coverage, /status only, not an all-clear/);
  assert.match(coverage, /resolution blocked — 1 excluded claim/);

  const rejected = d.element('sec-rejected').innerHTML;
  assert.match(rejected, /RESOLUTION BLOCKED/);
  assert.match(rejected, /not active findings/);
  assert.match(rejected, /no not-affected assessment in this report can dismiss/);
  assert.match(rejected, /Source-check\s+completeness does not remove this block/);
  assert.match(d.element('sec-assessments').innerHTML, /NOT DISMISSED/);
  assert.match(d.element('sec-alerts').innerHTML, /block all not-affected dismissals/);
});

test('dependabot badges mean unannounced and fetch failures are unknown, never none', () => {
  const d = dashboard();
  d.run('_features.security=true');
  acceptReport(d, securityReport({
    dependabot: {
      open: [{ branch: 'dependabot/npm_and_yarn/web/lodash', opened: '2026-10-27', tip: 'abc1234' }],
      new: [{ branch: 'dependabot/npm_and_yarn/web/lodash', opened: '2026-10-27', tip: 'abc1234' }],
    },
  }));
  let dependabot = d.element('sec-dependabot').innerHTML;
  assert.match(dependabot, /UNANNOUNCED/);
  assert.doesNotMatch(dependabot, />NEW</);
  assert.match(dependabot, /not yet included in a successfully delivered notification/);
  assert.match(dependabot, /across any ecosystem/);
  assert.match(dependabot, /docker-compose, npm or pip/);
  assert.match(dependabot, /Merging a PR does not by itself deploy/);
  assert.match(dependabot, /pulls\?q=is%3Apr%20head%3Adependabot%2Fnpm_and_yarn%2Fweb%2Flodash/);

  acceptReport(d, securityReport({
    dependabot: { open: [], new: [] },
    limitations: securityReport().limitations.concat([
      'Dependabot branches could not be fetched; pending update PRs are unknown this run.',
    ]),
  }));
  dependabot = d.element('sec-dependabot').innerHTML;
  assert.match(dependabot, /BRANCH AVAILABILITY: UNKNOWN — FETCH FAILED/);
  assert.match(dependabot, /not evidence\s+that no update branches exist/);
  assert.doesNotMatch(dependabot, /No pending Dependabot update branches/);
});

test('summary is plain SUMMARY and local validation is scoped to this run', () => {
  const d = dashboard();
  d.run('_features.security=true');
  acceptReport(d, securityReport());
  const summary = d.element('sec-summary').innerHTML;
  assert.match(summary, /class="sec-k">SUMMARY</);
  assert.doesNotMatch(summary, /MODEL-GENERATED/);
  assert.match(summary, /this run/);
  assert.match(summary, /retained findings were not rechecked/);

  acceptReport(d, securityReport({ findings: [], summary: 'Nothing actionable.' }));
  const empty = d.element('sec-summary').innerHTML;
  assert.doesNotMatch(empty, /MODEL-GENERATED/);
  assert.match(empty, /retained findings were not rechecked/);
});

test('a future report timestamp warns about clocks without implying freshness', () => {
  const d = dashboard();
  d.run('_features.security=true');
  acceptReport(d, securityReport({
    checked_at: new Date(Date.now() + 10 * 60 * 1000).toISOString(),
  }));
  const alerts = d.element('sec-alerts').innerHTML;
  assert.match(alerts, /IN THE FUTURE/);
  assert.match(alerts, /clock|timestamp/i);
  assert.match(d.element('sec-meta').innerHTML, /FUTURE TIMESTAMP/);
  assert.doesNotMatch(alerts, /REPORT STALE/);
});

test('an HTTP 200 error envelope retains the prior report like an HTTP error', async () => {
  const d = dashboard();
  d.run('_features.security=true');
  const first = securityReport();
  acceptReport(d, first);
  const snapshot = d.run('securityReportJSON()');
  d.context.fetch = async () => ({
    ok: true, status: 200,
    json: async () => ({ error: 'Report missing', code: 'missing' }),
  });
  await d.run('securityRefresh()');
  assert.equal(d.run('_securityReport.checked_at'), first.checked_at);
  assert.equal(d.run('securityReportJSON()'), snapshot);
  assert.equal(d.run('_securityLoadError.code'), 'missing');
  const alerts = d.element('sec-alerts').innerHTML;
  assert.match(alerts, /REFRESH FAILED/);
  assert.match(alerts, /missing/);
  assert.equal(d.element('sec-content').style.display, 'block');
  assert.equal(d.element('sec-unavailable').style.display, 'none');
});

test('initial-load retry shows busy feedback, disables retry and recovers after timeout', async () => {
  const d = dashboard();
  d.run('_features.security=true; _activeTab="security"');
  let requests = 0;
  d.context.fetch = async () => {
    requests++;
    return { ok: false, status: 404, json: async () => ({ error: 'Report missing', code: 'missing' }) };
  };
  await d.run('securityRefresh()');
  assert.equal(d.element('sec-unavailable').style.display, 'block');
  assert.match(d.element('sec-unavailable').innerHTML, /SECURITY REPORT UNAVAILABLE/);
  assert.equal(d.element('btn-sec-retry').disabled, false);

  d.context.fetch = (url, options) => {
    requests++;
    return new Promise((resolve, reject) => {
      options.signal.addEventListener('abort', () => {
        const error = new Error('aborted');
        error.name = 'AbortError';
        reject(error);
      });
    });
  };
  d.timers.length = 0;
  const pending = d.run('securityRefresh()');
  const duplicate = d.run('securityRefresh()');
  assert.equal(pending, duplicate);
  assert.equal(requests, 2); // initial failure + one hanging retry
  assert.match(d.element('sec-unavailable').innerHTML, /RETRYING SECURITY REPORT/);
  assert.match(d.element('sec-unavailable').innerHTML, /disabled until the/);
  assert.equal(d.element('btn-sec-retry').disabled, true);
  assert.equal(d.run('_securityBusy'), true);

  d.timers.filter(timer => timer.ms === 25000).pop().fn();
  await pending;
  assert.equal(d.run('_securityBusy'), false);
  assert.equal(d.run('_securityLoadError.code'), 'timeout');
  assert.equal(d.element('btn-sec-retry').disabled, false);
  assert.match(d.element('sec-unavailable').innerHTML, /SECURITY REPORT UNAVAILABLE/);
  assert.match(d.element('sec-unavailable').innerHTML, /timed out/);
  assert.equal(d.element('sec-content').style.display, 'none');

  d.context.fetch = async () => { requests++; return response({ report: securityReport() }); };
  await d.run('securityRefresh()');
  assert.equal(d.element('sec-content').style.display, 'block');
  assert.equal(d.element('sec-unavailable').style.display, 'none');
  assert.match(d.element('sec-meta').innerHTML, /report checked/);
  assert.equal(d.run('_securityBusy'), false);
});

test('a timed-out security refresh retains the prior report as not fresh', async () => {
  const d = dashboard();
  d.run('_features.security=true');
  const report = securityReport();
  acceptReport(d, report);
  const snapshot = d.run('securityReportJSON()');
  d.context.fetch = (url, options) => new Promise((resolve, reject) => {
    options.signal.addEventListener('abort', () => {
      const error = new Error('aborted');
      error.name = 'AbortError';
      reject(error);
    });
  });
  d.timers.length = 0;
  const pending = d.run('securityRefresh()');
  assert.equal(d.element('btn-sec-refresh').disabled, true);
  d.timers.filter(timer => timer.ms === 25000).pop().fn();
  await pending;
  assert.equal(d.run('_securityReport.checked_at'), report.checked_at);
  assert.equal(d.run('securityReportJSON()'), snapshot);
  assert.equal(d.run('_securityLoadError.code'), 'timeout');
  assert.match(d.element('sec-alerts').innerHTML, /REFRESH FAILED/);
  assert.match(d.element('sec-alerts').innerHTML, /timed out/);
  assert.match(d.element('sec-alerts').innerHTML, /not fresh/);
  assert.equal(d.element('sec-content').style.display, 'block');
  assert.equal(d.element('btn-sec-refresh').disabled, false);
});

test('an unchanged report keeps DOM, search, filter and open cards untouched', () => {
  const d = dashboard();
  d.run('_features.security=true');
  const report = securityReport();
  acceptReport(d, report);
  d.run("securitySetFilter('act_now')");
  d.element('sec-search').value = 'api';
  d.run('securitySearchChanged(); securityExpandAll(true)');
  const before = {
    findings: d.element('sec-findings').innerHTML,
    shown: d.element('sec-shown').textContent,
    filter: d.run('_securityFilter'),
    open: d.run('Array.from(_securityOpen).sort().join(",")'),
  };
  acceptReport(d, JSON.parse(JSON.stringify(report)));
  assert.equal(d.element('sec-findings').innerHTML, before.findings);
  assert.equal(d.element('sec-shown').textContent, before.shown);
  assert.equal(d.element('sec-search').value, 'api');
  assert.equal(d.run('_securityFilter'), before.filter);
  assert.equal(d.run('Array.from(_securityOpen).sort().join(",")'), before.open);
});

test('boot wiring keeps the 60s poll, feature sync and resize re-sync', () => {
  const html = fs.readFileSync(path.join(__dirname, '../app/static/index.html'), 'utf8');
  assert.match(html, /setInterval\(securityTick, 60000\)/);
  assert.match(html, /visibilitychange[\s\S]{0,120}securityTick\(\)/);
  assert.match(html, /async function fetchFeatures\(\)[\s\S]{0,6000}?securitySyncFeature\(\);/);
  assert.match(html, /window\.addEventListener\('resize', syncTabBarAfterFonts/);
});

test('summary, coverage, dependabot, details and provenance escape report strings', () => {
  const d = dashboard();
  d.run('_features.security=true');
  const base = securityReport();
  acceptReport(d, securityReport({
    summary: '<img src=x onerror="summary()">',
    checked_components: ['<img src=x onerror="component()">'],
    limitations: ['<script>limitation()</script>'],
    rejected_assessments: [{
      component: '<b>bad component</b>', advisory: '<i>bad advisory</i>',
      reason: '<svg onload="reject()">',
    }],
    advisory_checks: { ...base.advisory_checks, errors: ['<img src=x onerror="osv()">'] },
    inventory: {
      ...base.inventory,
      coverage_gaps: ['<script>gap()</script>'],
      collection_errors: ['<img src=x onerror="inventory()">'],
    },
    dependabot: {
      open: [{
        branch: 'dependabot/npm/<img src=x onerror="branch()">',
        opened: '<b>date</b>', tip: '<i>tip</i>',
      }],
      new: [],
    },
    search_sources: ['<img src=x onerror="source()">'],
  }));
  for (const id of ['sec-summary', 'sec-rejected', 'sec-coverage', 'sec-dependabot', 'sec-details', 'sec-provenance']) {
    const html = d.element(id).innerHTML;
    assert.doesNotMatch(html, /<(img|script|svg|iframe)\b/, id);
  }
  assert.match(d.element('sec-summary').innerHTML, /&lt;img/);
  assert.match(d.element('sec-details').innerHTML, /&lt;script/);
  assert.match(d.element('sec-dependabot').innerHTML, /dependabot\/npm\/&lt;img/);
  assert.match(d.element('sec-provenance').innerHTML, /unsafe link withheld/);
});
