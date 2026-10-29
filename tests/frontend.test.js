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
  return {
    context, element, timers, clearedTimers, storage,
    run: code => vm.runInContext(code, context),
  };
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
  for (const name of ['server', 'ai-services', 'containers', 'processes', 'security']) {
    assert.match(html, new RegExp(`id="tab-${name}" class="tab-pane" role="tabpanel" aria-labelledby="tab-btn-${name}"`));
    assert.match(html, new RegExp(`id="tab-btn-${name}"[^>]*aria-controls="tab-${name}"`));
  }
  assert.doesNotMatch(html, /id="tab-(opencode|mistral)"/);
  assert.doesNotMatch(html, /id="tab-btn-(opencode|mistral)"/);
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
  const names = ['server', 'ai-services', 'containers', 'processes', 'security'];
  const tabs = {};
  for (const name of names) {
    tabs[name] = d.context.document.querySelector(`.tab-btn[data-tab="${name}"]`);
    tabs[name].tabIndex = name === 'server' ? 0 : -1;
  }
  for (const name of ['ai-services', 'security']) tabs[name].style.display = 'none';

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
  assert.match(html, /setInterval\(aiServicesTick, 60000\)/);
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

// ── AI SERVICES (unified inventory comparison) ──────────────────────────────
function aiMetric(key, label, name, unit, source) {
  return { key, label, name, unit, source };
}

function aiServicesReport(overrides = {}) {
  // Match the backend contract: check timestamps are epoch seconds, source and
  // evaluation dates are YYYY-MM-DD strings, and disabled metrics are absent.
  const checked = Math.floor(Date.now() / 1000) - 3 * 60;
  const success = Math.floor(Date.now() / 1000) - 4 * 60;
  const updated = '2026-09-25';
  return {
    refreshing: false,
    metrics: [
      aiMetric('tb4', 'TB4', 'Terminal-Bench 4.0', 'pct', 'harbor'),
      aiMetric('livebench_coding', 'LBC', 'LiveBench Coding', 'pct', 'livebench'),
      aiMetric('eci', 'ECI', 'Epoch Capabilities Index', 'index', 'epoch'),
      aiMetric('livebench_reasoning', 'LB-R', 'LiveBench Reasoning', 'pct', 'livebench'),
    ],
    sources: {
      harbor: {
        last_checked: checked, last_success: success, source_updated: updated,
        stale: false, error: null, version: 'tb4-2026-09',
        url: 'https://harbor.example.org/benchmarks/tb4', warning: null,
      },
      epoch: {
        last_checked: checked, last_success: success, source_updated: updated,
        stale: false, error: null, version: 'eci-2026-09',
        url: 'https://epoch.ai/benchmarks', warning: null,
      },
      livebench: {
        last_checked: checked, last_success: success, source_updated: '2026-10-25',
        stale: false, error: null, version: '20261025',
        url: 'https://livebench.ai/data/20261025/table.csv', disabled: true,
        warning: 'LiveBench2.0 reuse terms unverified; opt-in after review',
      },
      deepswe: {
        last_checked: checked, last_success: null, source_updated: null,
        stale: false, error: null, version: null,
        url: 'https://epoch.ai/benchmarks/deepswe/review', disabled: true,
        warning: 'DeepSWE opt-in after review',
      },
    },
    models: [
      {
        name: 'alpha', upstream_model: 'openai/alpha-2026', provider: 'openai', mode: 'chat',
        routes: [
          { name: 'alpha-route', provider: 'openai' },
          { name: 'alpha-backup', provider: 'openrouter' },
        ],
        bench: { tb4: 72.4, eci: 41.2 },
        results: {
          tb4: [{
            source: 'harbor', metric: 'tb4', model: 'alpha-2026', agent: 'native',
            effort: 'high', evaluator: 'Terminal-Bench 4.0', version: 'harbor/2026-09',
            value: 72.4, date: '2026-09-30', date_kind: 'evaluation', ci: { low: 68, high: 76 },
            configuration: 'native agent / high', selected: true,
            metadata: { n_trials: 5, ci_method: 'bootstrap' },
          }],
          eci: [{
            source: 'epoch', metric: 'eci', model: 'alpha-2026', evaluator: 'Epoch AI ECI',
            version: 'eci_scores', value: 41.2, date: '2026-09-12', date_kind: 'model_release',
            ci: { half_width: 1.5 },
            configuration: 'ECI composite', selected: true, metadata: {},
          }],
        },
        missing: { livebench_coding: 'source unavailable' },
      },
      {
        name: 'beta', upstream_model: 'anthropic/beta', provider: 'anthropic', mode: 'chat',
        routes: [{ name: 'beta-route', provider: 'anthropic' }],
        bench: {},
        results: {
          tb4: [
            {
              agent: 'harness-a', value: 54.2, date: '2026-08-15', date_kind: 'benchmark_release',
              configuration: 'harness-a / medium', metadata: {},
            },
            { agent: 'harness-b', value: 51.8, date: null, configuration: 'harness-b / low', metadata: {} },
          ],
        },
        missing: {
          tb4: 'not evaluated',
          eci: 'unmatched identity',
          livebench_coding: 'source unavailable',
        },
      },
      {
        name: 'gamma', upstream_model: 'google/gamma', provider: 'google', mode: 'chat',
        routes: [{ name: 'gamma-route', provider: 'google' }],
        bench: { tb4: 50.0 },
        results: {},
        missing: { eci: 'not evaluated', livebench_coding: 'source unavailable' },
      },
      {
        name: 'embed-one', upstream_model: 'mistral/embed', provider: 'mistral', mode: 'embedding',
        routes: [{ name: 'embed-route', provider: 'mistral' }],
        bench: {},
        results: {},
        missing: {
          tb4: 'unsupported modality', eci: 'unsupported modality', livebench_coding: 'unsupported modality',
        },
      },
    ],
    ...overrides,
  };
}

async function loadAI(report = aiServicesReport(), options = {}) {
  const d = dashboard(options);
  d.run("_features.ai_services=true; _features.opencode_go=false; _features.mistral=false; _activeTab='ai-services';");
  d.context.fetch = async url => {
    assert.equal(url, '/api/ai-services');
    return response(report);
  };
  await d.run('aiFetchServices()');
  return d;
}

test('AI services merges tabs and removes every AA selector, column and credit', () => {
  const html = fs.readFileSync(path.join(__dirname, '../app/static/index.html'), 'utf8');
  assert.match(html, /id="tab-btn-ai-services"[^>]*aria-controls="tab-ai-services"[^>]*>AI SERVICES</);
  assert.match(html, /id="tab-ai-services" class="tab-pane"/);
  assert.match(html, /OPEN MISTRAL ↗/);
  assert.match(html, /id="ocgl-table"/);
  assert.match(html, /id="ai-table"/);
  assert.match(html, /\/api\/ai-services/);
  assert.match(html, /\/api\/benchmarks\/fetch/);
  assert.doesNotMatch(html, /artificialanalysis/i);
  assert.doesNotMatch(html, /Artificial Analysis/i);
  assert.doesNotMatch(html, /id="mistral-(table|controls|metric-btn|metric-menu|wrap|attrib|error|updated|bench-th)"/);
  assert.doesNotMatch(html, /id="ocgl-(metric-btn|metric-menu|controls|attrib|updated|bench-th)"/);
  assert.doesNotMatch(html, /fetchMistralModels|renderMistralModels|sortMistralModels|mistralMetric/);
  assert.doesNotMatch(html, /ocglMetric|setMetric\(|toggleMistralMetricMenu/);

  const limits = html.match(/id="ocgl-table"[\s\S]*?<\/table>/)[0];
  assert.doesNotMatch(limits, /TB4|bench|score/i);
  assert.match(html, /No overall rank is computed/);
  assert.match(html, /reputable, not trusted/i);
  assert.match(html, /Epoch AI ECI © Epoch AI/);
  assert.match(html, /https:\/\/creativecommons\.org\/licenses\/by\/4\.0\//);
  assert.match(html, /\.ai-source-card a \{ color:var\(--g-bright\); \}/);
  assert.match(html, /\.ai-source-card a:focus-visible \{ outline:2px solid var\(--g-bright\); /);
});

test('one inventory table renders core benchmark columns, routes and preserved shell controls', async () => {
  const d = await loadAI();
  const thead = d.element('ai-thead').innerHTML;
  assert.match(thead, /MODEL/);
  assert.match(thead, /AVAILABLE ROUTES/);
  assert.match(thead, /Terminal-Bench4/);
  assert.match(thead, /LiveBench Coding/);
  assert.match(thead, /Epoch ECI/);
  assert.doesNotMatch(thead, /OVERALL|RANK/i);
  assert.match(thead, /Harbor \(Terminal-Bench\)/);
  assert.match(thead, /LiveBench 2\.0 · disabled/);
  assert.match(thead, /Epoch AI/);

  const tbody = d.element('ai-tbody').innerHTML;
  assert.match(tbody, /alpha/);
  assert.match(tbody, /openai\/alpha-2026/);
  assert.match(tbody, /alpha-route/);
  assert.match(tbody, /alpha-backup/);
  assert.match(tbody, /72\.4%/);
  assert.match(tbody, /41\.2/);
  assert.doesNotMatch(tbody, /41\.2%/);
  assert.match(tbody, /source disabled/);
  assert.doesNotMatch(tbody, />0(?:\.0)?%</);
  assert.match(tbody, /not evaluated — see configurations/);
  assert.match(tbody, /unmatched identity/);
  assert.match(tbody, /not evaluated/);
  assert.match(tbody, /unsupported modality/);
  assert.match(tbody, /non-coding model/i);
  assert.match(tbody, /excluded/);
  assert.match(d.element('ai-count').textContent, /4 \/ 4 models/);
  // pct values are already 0–100 percentage points in the contract.
  assert.equal(d.run("aiFormatValue(50,{unit:'pct'})"), '50.0%');
  assert.equal(d.run("aiFormatValue(0.5,{unit:'pct'})"), '0.5%');
  assert.equal(d.run("aiFormatValue(41.2,{unit:'index'})"), '41.2');
});

test('provider and route filters narrow the unified inventory table', async () => {
  const d = await loadAI();
  const providers = d.element('ai-provider-filter').innerHTML;
  for (const provider of ['ALL PROVIDERS', 'anthropic', 'openai', 'openrouter']) {
    assert.match(providers, new RegExp(provider));
  }
  d.run("aiSetProvider('anthropic')");
  assert.equal(d.run('_aiProvider'), 'anthropic');
  assert.match(d.element('ai-tbody').innerHTML, /beta/);
  assert.doesNotMatch(d.element('ai-tbody').innerHTML, /alpha/);
  assert.match(d.element('ai-count').textContent, /1 \/ 4 models/);

  d.run("aiSetProvider('all'); aiSetRoute('alpha-backup')");
  assert.match(d.element('ai-tbody').innerHTML, /alpha/);
  assert.doesNotMatch(d.element('ai-tbody').innerHTML, /beta/);
  d.run("aiSetRoute('all')");
  assert.match(d.element('ai-count').textContent, /4 \/ 4 models/);
});

test('optional LiveBench suites are selectable and disabled sources stay unavailable', async () => {
  const d = await loadAI();
  assert.doesNotMatch(d.element('ai-thead').innerHTML, /LiveBench Reasoning/);
  const picker = d.element('ai-column-options').innerHTML;
  assert.match(picker, /LiveBench Reasoning/);
  assert.match(picker, /disabled/);
  // DeepSWE is only present in the contract once explicitly enabled.
  assert.doesNotMatch(picker, /DeepSWE/);

  d.run("aiToggleColumn('livebench_reasoning', true)");
  assert.match(d.element('ai-thead').innerHTML, /LiveBench Reasoning/);
  assert.match(d.element('ai-thead').innerHTML, /LiveBench 2\.0 · disabled/);
  assert.match(d.storage.get('aiServicesColumns'), /livebench_reasoning/);
});

test('DeepSWE becomes a selectable optional column when the backend provides it', async () => {
  const base = aiServicesReport();
  const report = aiServicesReport({
    metrics: base.metrics.concat([aiMetric('deepswe', 'DSWE', 'DeepSWE pass@1', 'pct', 'deepswe')]),
    sources: {
      ...base.sources,
      deepswe: { ...base.sources.deepswe, disabled: false, warning: 'grading defects documented' },
    },
  });
  const d = await loadAI(report);
  const picker = d.element('ai-column-options').innerHTML;
  assert.match(picker, /DeepSWE pass@1/);
  assert.doesNotMatch(d.element('ai-thead').innerHTML, /DeepSWE/);
  d.run("aiToggleColumn('deepswe', true)");
  assert.match(d.element('ai-thead').innerHTML, /DeepSWE pass@1/);
  assert.match(d.storage.get('aiServicesColumns'), /deepswe/);
  d.run("aiToggleColumn('deepswe', false)");
  assert.doesNotMatch(d.element('ai-thead').innerHTML, /DeepSWE/);
});

test('all evaluation configurations are listed with agents, dates, CIs and metadata', async () => {
  const d = await loadAI();
  const configs = d.element('ai-configs').innerHTML;
  assert.match(configs, /alpha-2026/);
  assert.match(configs, /agent \/ harness/);
  assert.match(configs, /native/);
  assert.match(configs, /effort/);
  assert.match(configs, /high/);
  assert.match(configs, /evaluator/);
  assert.match(configs, /Terminal-Bench 4\.0/);
  assert.match(configs, /evaluation date<\/span><span>2026-09-30/);
  assert.doesNotMatch(configs, /evaluation date<\/span><span>2026-09-12/);
  assert.doesNotMatch(configs, /evaluation date<\/span><span>2026-08-15/);
  assert.match(configs, /model release date<\/span><span>2026-09-12/);
  assert.match(configs, /benchmark release date<\/span><span>2026-08-15/);
  assert.match(configs, /evaluation date<\/span><span>unknown/);
  assert.doesNotMatch(configs, /date \(kind not reported\)/);
  assert.match(configs, /\bCI\b/);
  assert.match(configs, /68 – 76/);
  assert.match(configs, /± 1\.5/);
  assert.match(configs, /configuration/);
  assert.match(configs, /native agent \/ high/);
  assert.match(configs, /metadata\.n_trials/);
  assert.match(configs, /metadata\.ci_method/);
  assert.match(configs, /bootstrap/);
  assert.match(configs, /SUMMARY/);
  assert.match(configs, /72\.4%/);
  assert.match(configs, /41\.2/);
  assert.match(configs, /54\.2%/);
  assert.match(configs, /51\.8%/);
  assert.match(configs, /harness-a/);
  assert.match(configs, /harness-b/);
  assert.match(configs, /medium/);
  assert.match(configs, /low/);
});

test('source status keeps checked, last success, source update and disabled states distinct', async () => {
  const d = await loadAI();
  const sources = d.element('ai-sources').innerHTML;
  assert.match(sources, /checked/);
  assert.match(sources, /last success/);
  assert.match(sources, /source updated/);
  assert.doesNotMatch(sources, /evaluations updated/);
  assert.match(sources, /Harbor \(Terminal-Bench\)/);
  assert.match(sources, /Epoch AI/);
  assert.match(sources, /LiveBench 2\.0/);
  assert.match(sources, /pinned release/);
  assert.match(sources, /DISABLED/);
  assert.match(sources, /2026-09-25/);
  assert.match(sources, /2026-10-25/);
  assert.doesNotMatch(sources, /1970/);
  assert.doesNotMatch(sources, /ERROR/);
  assert.doesNotMatch(sources, /STALE/);
  assert.match(d.element('ai-configs').innerHTML, /evaluation date/);

  const warnings = d.element('ai-warnings').innerHTML;
  assert.match(warnings, /LiveBench ?2\.0 reuse terms unverified; opt-in after review/);
  assert.match(warnings, /shown as disabled, not failed/);
  assert.match(warnings, /23 of 113/);
  assert.match(warnings, /https:\/\/epoch\.ai\/benchmarks\/deepswe\/review/);
  assert.match(warnings, /Datacurve 2026/);
  assert.match(warnings, /Wenqi Huang/);
  assert.match(warnings, /Charley Lee/);
  assert.match(warnings, /Leonard Tng/);
  assert.match(warnings, /Serena Ge/);
  assert.match(d.element('ai-attribution').innerHTML, /CC BY 4\.0/);
});

test('stale and error sources are explicit and do not erase the table', async () => {
  const base = aiServicesReport();
  const report = aiServicesReport({
    sources: {
      ...base.sources,
      epoch: { ...base.sources.epoch, stale: true, error: 'upstream timeout', warning: 'partial coverage' },
    },
  });
  const d = await loadAI(report);
  const sources = d.element('ai-sources').innerHTML;
  assert.match(sources, /STALE/);
  assert.match(sources, /ERROR/);
  assert.match(sources, /upstream timeout/);
  assert.match(sources, /partial coverage/);
  assert.match(d.element('ai-tbody').innerHTML, /41\.2/);
});

test('enabled LiveBench links its homepage and release date, not the CSV artifact', async () => {
  const disabled = await loadAI();
  assert.doesNotMatch(disabled.element('ai-attribution').innerHTML, /LiveBench 2\.0 · release/);

  const base = aiServicesReport();
  const report = aiServicesReport({
    sources: {
      ...base.sources,
      livebench: { ...base.sources.livebench, disabled: false, warning: null },
    },
  });
  const enabled = await loadAI(report);
  const attribution = enabled.element('ai-attribution').innerHTML;
  assert.match(attribution, /LiveBench 2\.0 · release 2026-10-25/);
  assert.match(attribution, /href="https:\/\/livebench\.ai"/);
  assert.doesNotMatch(attribution, /table\.csv/);
  assert.match(attribution, /LiveBench 2\.0/);
  assert.doesNotMatch(enabled.element('ai-warnings').innerHTML, /LIVEBENCH 2\.0 · DISABLED/);
});

test('LiveBench attribution falls back to the compact release version', async () => {
  const base = aiServicesReport();
  const report = aiServicesReport({
    sources: {
      ...base.sources,
      livebench: {
        ...base.sources.livebench, disabled: false, warning: null, source_updated: null,
      },
    },
  });
  const d = await loadAI(report);
  assert.match(d.element('ai-attribution').innerHTML, /release 2026-10-25/);
});

test('attribution groups every metric under one source instead of repeating it', async () => {
  const d = await loadAI();
  const attribution = d.element('ai-attribution').innerHTML;
  assert.equal((attribution.match(/LiveBench 2\.0/g) || []).length, 1);
  assert.match(attribution, /LiveBench Coding/);
  assert.match(attribution, /LiveBench Reasoning/);
  assert.match(attribution, /Harbor \(Terminal-Bench\)/);
  assert.match(attribution, /Epoch AI/);
});

test('result dates follow date_kind, unknown kinds never claim an evaluation date', async () => {
  const values = [
    { agent: 'sub', value: 61.2, date: '2026-07-01', date_kind: 'submission' },
    { agent: 'unknown-kind', value: 59.9, date: '2026-06-01' },
    { agent: 'no-date', value: 58.4, date: null },
    { agent: 'no-score', value: null, date: null },
  ];
  const report = aiServicesReport({
    models: [{
      name: 'date-kinds', upstream_model: 'openai/date-kinds', provider: 'openai', mode: 'chat',
      routes: [{ name: 'date-kinds', provider: 'openai' }],
      bench: { tb4: 61.2 },
      results: { tb4: values.map(entry => ({ ...entry, metadata: {} })) },
      missing: {},
    }],
  });
  const d = await loadAI(report);
  const configs = d.element('ai-configs').innerHTML;
  assert.match(configs, /submission date<\/span><span>2026-07-01/);
  assert.match(configs, /date \(kind not reported\)<\/span><span>2026-06-01/);
  const evalRows = configs.match(/evaluation date<\/span><span>([^<]*)</g) || [];
  assert.ok(evalRows.length >= 3, 'every result without an evaluation date says so');
  assert.ok(evalRows.every(row => /unknown/.test(row)), 'release dates never become evaluation dates');
  assert.match(configs, /61\.2%/);
  assert.match(configs, /59\.9%/);
  assert.match(configs, /58\.4%/);
  assert.match(configs, /score not reported/);
});

test('manual refresh posts once and polls the same GET until refreshing clears', async () => {
  const d = dashboard();
  d.run("_features.ai_services=true; _activeTab='ai-services';");
  let posts = 0, gets = 0;
  d.context.fetch = async (url, options) => {
    if ((options.method || 'GET') === 'POST') {
      assert.equal(url, '/api/benchmarks/fetch');
      posts++;
      return response({ ok: true, refreshing: true });
    }
    gets++;
    return response(aiServicesReport({ refreshing: gets === 1 }));
  };
  const first = d.run('aiRefreshBenchmarks()');
  const second = d.run('aiRefreshBenchmarks()');
  assert.equal(first, second);
  await first;
  assert.equal(posts, 1);
  assert.equal(gets, 0);
  assert.match(d.element('ai-status').textContent, /refreshing/);

  const poll1 = d.timers.filter(timer => timer.ms === 2000)[0];
  assert.ok(poll1, 'first bounded poll is scheduled');
  poll1.fn();
  await settle();
  assert.equal(gets, 1);
  assert.equal(d.element('btn-ai-refresh').disabled, true);
  assert.match(d.element('ai-tbody').innerHTML, /alpha/);

  const poll2 = d.timers.filter(timer => timer.ms === 2000)[1];
  assert.ok(poll2, 'second bounded poll is scheduled');
  poll2.fn();
  await settle();
  assert.equal(gets, 2);
  assert.equal(d.element('btn-ai-refresh').disabled, false);
  assert.match(d.element('toast').textContent, /benchmark scores updated/);
  assert.match(d.element('ai-status').textContent, /inventory loaded/);
});

test('refresh polling is bounded and transient failures preserve the last data', async () => {
  const d = dashboard();
  d.run("_features.ai_services=true; _activeTab='ai-services';");
  let gets = 0;
  d.context.fetch = async (url, options) => {
    if (options.method === 'POST') return response({ ok: true, refreshing: true });
    gets++;
    if (gets === 1) return response(aiServicesReport({ refreshing: true }));
    throw new Error('offline');
  };
  await d.run('aiRefreshBenchmarks()');
  let timerIndex = 0;
  while (d.run('_aiRefreshActive')) {
    const timer = d.timers.filter(item => item.ms === 2000)[timerIndex++];
    assert.ok(timer, 'poll timer exists while a refresh is active');
    timer.fn();
    await settle();
    assert.ok(timerIndex <= 40, 'poll loop must stop at the configured bound');
  }
  assert.equal(timerIndex, 40);
  assert.equal(gets, 40);
  assert.equal(d.element('btn-ai-refresh').disabled, false);
  assert.match(d.element('ai-tbody').innerHTML, /alpha/);
  assert.match(d.element('ai-error').textContent, /refresh status unknown|last loaded inventory/);
  assert.match(d.element('toast').textContent, /previous data retained/);
});

test('concurrent inventory loads share one GET request', async () => {
  const d = dashboard();
  d.run("_features.ai_services=true; _activeTab='ai-services';");
  let requests = 0, complete;
  d.context.fetch = () => {
    requests++;
    return new Promise(resolve => { complete = resolve; });
  };
  const first = d.run('aiFetchServices()');
  const second = d.run('aiFetchServices()');
  assert.equal(first, second);
  assert.equal(requests, 1);
  complete(response(aiServicesReport()));
  await first;
  assert.equal(d.run('_aiInFlight'), null);
  assert.match(d.element('ai-tbody').innerHTML, /alpha/);
});

test('AI inventory text is escaped and only safe public https source links render', async () => {
  const hostile = '<img src=x onerror="boom()">';
  const report = aiServicesReport({
    models: [{
      name: hostile, upstream_model: '<script>upstream()</script>', provider: hostile, mode: 'chat',
      routes: [{ name: '<svg onload="route()">', provider: '<b>provider</b>' }],
      bench: { tb4: 0.5, eci: 1.0 },
      results: {
        tb4: [{
          agent: '<iframe>agent</iframe>', effort: '<b>high</b>', evaluator: '<i>eval</i>',
          version: '<v>', date: '<d>', ci: '<c>',
          configuration: '<b>config</b>',
          metadata: { n_trials: 5, ci_method: '<script>ci()</script>' },
        }],
      },
      missing: { eci: '<script>missing()</script>' },
    }],
    sources: {
      harbor: {
        last_checked: '<b>checked</b>', last_success: '<i>success</i>', source_updated: '<u>updated</u>',
        stale: false, error: '<img src=x onerror="error()">', version: '<v>',
        url: 'https://user:pass@example.com/advisory', warning: '<script>warning()</script>',
      },
      epoch: { ...aiServicesReport().sources.epoch, url: 'http://localhost/epoch' },
      livebench: {
        ...aiServicesReport().sources.livebench,
        warning: '<img src=x onerror="livebench()">',
      },
      deepswe: {
        ...aiServicesReport().sources.deepswe,
        warning: '<svg onload="deepswe()">',
      },
    },
  });
  const d = await loadAI(report);
  for (const id of ['ai-thead', 'ai-tbody', 'ai-sources', 'ai-configs', 'ai-warnings', 'ai-attribution']) {
    const html = d.element(id).innerHTML;
    assert.doesNotMatch(html, /<(img|script|svg|iframe)\b/, id);
  }
  assert.match(d.element('ai-tbody').innerHTML, /&lt;img/);
  assert.match(d.element('ai-configs').innerHTML, /&lt;script/);
  const sources = d.element('ai-sources').innerHTML;
  assert.match(sources, /unsafe link withheld/);
  assert.doesNotMatch(sources, /href="https:\/\/user:pass@example\.com/);
  assert.doesNotMatch(sources, /href="http:\/\/localhost/);
  assert.equal(d.run("securitySafeUrl('https://user:pass@example.com/x')"), false);
  assert.equal(d.run("securitySafeUrl('http://localhost/x')"), false);
});

test('legacy OPENCODE and MISTRAL saved tabs migrate to AI SERVICES', () => {
  for (const old of ['opencode', 'mistral']) {
    const d = dashboard({ savedTab: old });
    assert.equal(d.run('_activeTab'), 'ai-services');
    assert.equal(d.storage.get('activeTab'), 'ai-services');
    const tab = d.context.document.querySelector('.tab-btn[data-tab="ai-services"]');
    assert.equal(tab.classList.contains('active'), true);
    assert.equal(d.element('tab-ai-services').style.display, 'block');
    d.run('aiServicesSyncFeature()');
    assert.equal(d.run('_activeTab'), 'server');
  }
});

test('AI services enables on ai_services or either legacy provider flag', async () => {
  for (const flags of [
    { ai_services: true, opencode_go: false, mistral: false },
    { ai_services: false, opencode_go: true, mistral: false },
    { ai_services: false, opencode_go: false, mistral: true },
  ]) {
    const d = dashboard();
    d.context.fetch = async () => response(aiServicesReport());
    d.run(`_features.ai_services=${flags.ai_services}; _features.opencode_go=${flags.opencode_go}; _features.mistral=${flags.mistral}; aiServicesSyncFeature();`);
    const tab = d.context.document.querySelector('.tab-btn[data-tab="ai-services"]');
    assert.equal(tab.style.display, 'inline-block');
  }
  const off = dashboard();
  off.run('aiServicesSyncFeature()');
  assert.equal(off.context.document.querySelector('.tab-btn[data-tab="ai-services"]').style.display, 'none');
});

test('usage and allowance sections stay above the comparison and keep no score column', async () => {
  const d = dashboard();
  d.context.fetch = async url => {
    if (url === '/api/opencode-go') {
      return response({ rolling: { percent: 23, status: 'ok', resets_at: null } });
    }
    if (url === '/api/opencode-go-limits') {
      return response({ models: [{ name: 'fixture-model', req_5h: 100, monthly: 12 }] });
    }
    return response(aiServicesReport());
  };
  d.run("_features.opencode_go=true; _activeTab='ai-services'; aiServicesSyncFeature();");
  await d.run('aiFetchServices()');
  await d.run('fetchOpenCodeGo()');
  await d.run('fetchOpenCodeGoLimits()');
  assert.match(d.element('ocg-list').innerHTML, /23%/);
  const limits = d.element('ocgl-tbody').innerHTML;
  assert.match(limits, /fixture-model/);
  assert.match(limits, /100/);
  assert.match(limits, /\$12/);
  assert.doesNotMatch(limits, /TB4|bench|score/i);
  assert.equal(d.element('ocgl-wrap').style.display, 'block');
});

test('metric source aliases resolve to the backend source key', async () => {
  const base = aiServicesReport();
  const report = aiServicesReport({
    metrics: base.metrics.map(metric => metric.key === 'tb4'
      ? { ...metric, source: 'tb4' } : metric),
  });
  const d = await loadAI(report);
  assert.match(d.element('ai-thead').innerHTML, /Harbor \(Terminal-Bench\)/);
  assert.match(d.element('ai-attribution').innerHTML, /Harbor \(Terminal-Bench\)/);
});

test('an inventory error keeps valid benchmark sources and warnings visible', async () => {
  const d = await loadAI(aiServicesReport({ models: [], error: 'configuration unavailable' }));
  assert.match(d.element('ai-error').textContent, /inventory unavailable: configuration unavailable/);
  assert.doesNotMatch(d.element('ai-error').textContent, /AI services unavailable/);
  assert.match(d.element('ai-sources').innerHTML, /Harbor \(Terminal-Bench\)/);
  assert.match(d.element('ai-warnings').innerHTML, /LIVEBENCH 2\.0/);
  assert.match(d.element('ai-tbody').innerHTML, /no models reported by the inventory/);
});

test('models without a summary keep every configuration value accessible', async () => {
  const d = await loadAI();
  assert.match(d.element('ai-tbody').innerHTML, /not evaluated — see configurations/);
  const configs = d.element('ai-configs').innerHTML;
  assert.match(configs, /beta/);
  assert.match(configs, /54\.2%/);
  assert.match(configs, /51\.8%/);
  assert.match(configs, /harness-a/);
  assert.match(configs, /harness-b/);
  assert.match(configs, /benchmark release date/);
});

function refreshDashboard(report) {
  const d = dashboard();
  d.run("_features.ai_services=true; _activeTab='ai-services';");
  let gets = 0;
  d.context.fetch = async (url, options) => {
    if (options.method === 'POST') return response({ ok: true, refreshing: true });
    gets++;
    return response({ ...report, refreshing: gets === 1 });
  };
  return { d, gets: () => gets };
}

async function finishRefresh(d) {
  const first = d.timers.filter(timer => timer.ms === 2000)[0];
  assert.ok(first, 'first poll is scheduled');
  first.fn();
  await settle();
  const second = d.timers.filter(timer => timer.ms === 2000)[1];
  assert.ok(second, 'second poll is scheduled');
  second.fn();
  await settle();
  assert.equal(d.run('_aiRefreshActive'), false);
}

test('a refresh completion with a source error is partial, never success', async () => {
  const base = aiServicesReport();
  const report = aiServicesReport({
    sources: {
      ...base.sources,
      harbor: { ...base.sources.harbor, error: 'harbor unavailable' },
    },
  });
  const { d } = refreshDashboard(report);
  await d.run('aiRefreshBenchmarks()');
  await finishRefresh(d);
  const toastText = d.element('toast').textContent;
  assert.match(toastText, /refresh completed with source errors — last-good scores retained/);
  assert.doesNotMatch(toastText, /benchmark scores updated/);
  assert.match(d.element('ai-sources').innerHTML, /harbor unavailable/);
  assert.match(d.element('ai-tbody').innerHTML, /72\.4%/);
  assert.equal(d.element('btn-ai-refresh').disabled, false);
});

test('a refresh completion with every enabled source failed reports failure', async () => {
  const base = aiServicesReport();
  const report = aiServicesReport({
    sources: {
      ...base.sources,
      harbor: { ...base.sources.harbor, error: 'harbor down' },
      epoch: { ...base.sources.epoch, error: 'epoch down' },
    },
  });
  const { d } = refreshDashboard(report);
  await d.run('aiRefreshBenchmarks()');
  await finishRefresh(d);
  const toastText = d.element('toast').textContent;
  assert.match(toastText, /benchmark refresh failed — last-good scores retained/);
  assert.doesNotMatch(toastText, /benchmark scores updated/);
  assert.match(d.element('ai-sources').innerHTML, /harbor down/);
  assert.match(d.element('ai-tbody').innerHTML, /72\.4%/);
});

test('disabled sources stay warnings and never count as refresh failures', async () => {
  const base = aiServicesReport();
  const report = aiServicesReport({
    sources: {
      ...base.sources,
      livebench: { ...base.sources.livebench, error: 'artifact not fetched' },
      deepswe: { ...base.sources.deepswe, error: 'artifact not fetched' },
    },
  });
  const { d } = refreshDashboard(report);
  await d.run('aiRefreshBenchmarks()');
  await finishRefresh(d);
  assert.match(d.element('toast').textContent, /benchmark scores updated/);
  assert.match(d.element('ai-warnings').innerHTML, /LIVEBENCH 2\.0 · DISABLED/);
});

test('an immediate completion from POST also reports source errors', async () => {
  const base = aiServicesReport();
  const report = aiServicesReport({
    sources: {
      ...base.sources,
      harbor: { ...base.sources.harbor, error: 'harbor down' },
      epoch: { ...base.sources.epoch, error: 'epoch down' },
    },
  });
  const d = dashboard();
  d.run("_features.ai_services=true; _activeTab='ai-services';");
  d.context.fetch = async (url, options) => {
    if (options.method === 'POST') return response({ ok: true, refreshing: false });
    return response(report);
  };
  await d.run('aiRefreshBenchmarks()');
  assert.match(d.element('toast').textContent, /benchmark refresh failed — last-good scores retained/);
  assert.equal(d.run('_aiRefreshActive'), false);
  assert.equal(d.element('btn-ai-refresh').disabled, false);
});

test('undated results with no backend reason are ambiguous, not not-evaluated', async () => {
  const report = aiServicesReport({
    models: [{
      name: 'ambiguous', upstream_model: 'openai/ambiguous', provider: 'openai', mode: 'chat',
      routes: [{ name: 'ambiguous', provider: 'openai' }],
      bench: {},
      results: {
        tb4: [
          { agent: 'a', value: 60, date: '2026-01-01', metadata: {} },
          { agent: 'b', value: 61, date: '2026-01-01', metadata: {} },
        ],
      },
      missing: {},
    }],
  });
  const d = await loadAI(report);
  assert.match(d.element('ai-tbody').innerHTML, /no single summary — see configurations/);
  assert.match(d.element('ai-configs').innerHTML, /60\.0%/);
  assert.match(d.element('ai-configs').innerHTML, /61\.0%/);
});

test('an explicit missing reason wins over present results but keeps their values', async () => {
  const d = await loadAI();
  const tbody = d.element('ai-tbody').innerHTML;
  assert.match(tbody, /not evaluated — see configurations/);
  assert.doesNotMatch(tbody, /no single summary — see configurations/);
  assert.match(d.element('ai-configs').innerHTML, /54\.2%/);
});

// ── reviewer regression fixes ────────────────────────────────────────────────
test('the AI table wrapper still scrolls horizontally inside the shared table shell', () => {
  const html = fs.readFileSync(path.join(__dirname, '../app/static/index.html'), 'utf8');
  assert.match(html, /#ai-table-wrap \{ overflow-x:auto; overflow-y:hidden; \}/);
  assert.match(html, /class="proc-table-wrap ai-table-wrap"/);
});

test('a hidden restored AI tab loads when the document becomes visible, deduped', async () => {
  const d = dashboard({ hidden: true, savedTab: 'ai-services' });
  let fetches = 0;
  d.context.fetch = async url => {
    assert.equal(url, '/api/ai-services');
    fetches++;
    return response(aiServicesReport());
  };
  d.run('_features.ai_services=true; aiServicesSyncFeature();');
  await settle();
  assert.equal(fetches, 0, 'no fetch while the document is hidden');

  d.run('document.hidden = false; _aiLastEnterAt = 0; aiHandleVisibility(); aiHandleVisibility();');
  await settle();
  assert.equal(fetches, 1, 'visibilitychange and pageshow coalesce into one load');
  assert.match(d.element('ai-tbody').innerHTML, /alpha/);

  d.run('_aiLastEnterAt = 0; aiHandleVisibility();');
  await settle();
  assert.equal(fetches, 2, 'a later visibility change can load again');
});

test('initial load failure and refresh timeout never leave a fake loading or polling status', async () => {
  const failed = dashboard();
  failed.run("_features.ai_services=true; _activeTab='ai-services';");
  failed.context.fetch = async () => { throw new Error('offline'); };
  await failed.run('aiFetchServices()');
  assert.match(failed.element('ai-status').textContent, /ai services unavailable/);
  assert.doesNotMatch(failed.element('ai-status').textContent, /loading/);

  const timed = dashboard();
  timed.run("_features.ai_services=true; _activeTab='ai-services';");
  let gets = 0;
  timed.context.fetch = async (url, options) => {
    if (options.method === 'POST') return response({ ok: true, refreshing: true });
    gets++;
    const report = aiServicesReport({ refreshing: true });
    return response(report);
  };
  await timed.run('aiRefreshBenchmarks()');
  let polls = 0;
  while (timed.run('_aiRefreshActive')) {
    const timer = timed.timers.filter(item => item.ms === 2000)[polls++];
    assert.ok(timer, 'poll timer exists while refresh is active');
    timer.fn();
    await settle();
  }
  assert.equal(polls, 40);
  assert.equal(gets, 40);
  assert.equal(timed.element('btn-ai-refresh').disabled, false);
  const status = timed.element('ai-status').textContent;
  assert.doesNotMatch(status, /polling/);
  assert.match(status, /refresh status unknown/);
});

test('loaded status shows an absolute time and an aging relative label', async () => {
  const d = dashboard();
  d.run("_features.ai_services=true; _activeTab='ai-services';");
  d.run('_aiLoadedAt = Date.now() - 10 * 60 * 1000; _aiLoadFailed = false; aiRenderStatus();');
  const status = d.element('ai-status').textContent;
  assert.match(status, /inventory loaded \d{1,2}:\d{2}:\d{2}/);
  assert.match(status, /\(10m ago\)/);
  d.run('_aiLoadedAt = Date.now() - 20 * 60 * 1000; aiRenderStatus();');
  assert.match(d.element('ai-status').textContent, /\(20m ago\)/);
});

test('removing the sorted optional column restores the model sort', async () => {
  const d = await loadAI();
  d.run("aiToggleColumn('livebench_reasoning', true); aiSort('livebench_reasoning')");
  assert.equal(d.run('_aiSort.key'), 'livebench_reasoning');
  d.run("aiToggleColumn('livebench_reasoning', false)");
  assert.equal(JSON.stringify(d.run('_aiSort')), JSON.stringify({ key: 'model', dir: 1 }));

  d.run("aiToggleColumn('livebench_reasoning', true); aiSort('livebench_reasoning')");
  await d.run(`aiAcceptServices(${JSON.stringify(aiServicesReport())})`);
  assert.equal(d.run('_aiSort.key'), 'livebench_reasoning', 'still present in the new payload');
  const report = aiServicesReport();
  report.metrics = report.metrics.filter(metric => metric.key !== 'livebench_reasoning');
  await d.run(`aiAcceptServices(${JSON.stringify(report)})`);
  assert.equal(JSON.stringify(d.run('_aiSort')), JSON.stringify({ key: 'model', dir: 1 }));
});

test('the Mistral console link only shows when the Mistral feature is enabled', () => {
  const d = dashboard();
  d.run('_features.ai_services=true; _features.opencode_go=true; _features.mistral=false; aiServicesSyncFeature();');
  assert.equal(d.element('ai-mistral-console').style.display, 'none');
  d.run('_features.mistral=true; aiServicesSyncFeature();');
  assert.equal(d.element('ai-mistral-console').style.display, 'inline-flex');
});

test('open configuration details survive refresh redraws', async () => {
  const d = await loadAI();
  assert.match(d.element('ai-configs').innerHTML, /ontoggle="aiRememberConfigOpen\(this\.id, this\.open\)"/);
  d.run("aiRememberConfigOpen('ai-config-0', true); aiRenderConfigs();");
  assert.match(d.element('ai-configs').innerHTML, /id="ai-config-0" open/);
  d.run('aiRenderConfigs();');
  assert.match(d.element('ai-configs').innerHTML, /id="ai-config-0" open/);
  d.run("aiRememberConfigOpen('ai-config-0', false); aiRenderConfigs();");
  assert.doesNotMatch(d.element('ai-configs').innerHTML, /id="ai-config-0" open/);
});

// ── AI inventory periodic tick (24h TTL / backoff / stale discovery) ─────────
test('the AI inventory tick only fetches on an enabled, active, visible tab', async () => {
  const d = dashboard();
  const requests = [];
  d.context.fetch = async (url, options) => {
    requests.push({ url, options });
    return response(aiServicesReport());
  };
  d.run('document.hidden=false; _activeTab="server"; aiServicesTick()');
  await settle();
  assert.equal(requests.length, 0, 'another tab never polls the inventory');

  d.run('_features.ai_services=true; _activeTab="ai-services"; document.hidden=true; aiServicesTick()');
  await settle();
  assert.equal(requests.length, 0, 'a hidden document never polls');

  d.run('document.hidden=false; aiServicesTick()');
  await settle();
  assert.equal(requests.length, 1);
  assert.equal(requests[0].url, '/api/ai-services');
  assert.equal(requests[0].options.method, undefined, 'the tick is a plain GET');
  assert.equal(requests[0].options.headers.has('X-Homelab-Request'), false);

  // One GET per interval: three more ticks are three request cycles, not six.
  for (let i = 0; i < 3; i++) {
    d.run('aiServicesTick()');
    await settle();
  }
  assert.equal(requests.length, 4);
  assert.ok(requests.every(item => item.url === '/api/ai-services'));

  // Feature off means zero inventory requests even on the active, visible tab.
  const off = dashboard();
  let offRequests = 0;
  off.context.fetch = async () => { offRequests++; return response(aiServicesReport()); };
  off.run('_features.ai_services=false; _features.opencode_go=false; _features.mistral=false;' +
          '_activeTab="ai-services"; document.hidden=false; aiServicesTick()');
  await settle();
  assert.equal(offRequests, 0);
});

test('the AI inventory tick honours legacy provider flags and dedupes an in-flight GET', async () => {
  const d = dashboard();
  d.run("_features.opencode_go=true; _activeTab='ai-services';");
  const urls = [];
  let gets = 0, complete;
  d.context.fetch = url => {
    urls.push(url);
    gets++;
    return new Promise(resolve => { complete = resolve; });
  };
  d.run('aiServicesTick(); aiServicesTick()');
  assert.equal(gets, 1, 'both ticks share the single-flight request');
  complete(response(aiServicesReport()));
  await settle();
  assert.equal(d.run('_aiInFlight'), null);

  d.context.fetch = async url => {
    urls.push(url);
    gets++;
    return response(aiServicesReport());
  };
  d.run('aiServicesTick()');
  await settle();
  assert.equal(gets, 2, 'the next interval issues exactly one fresh GET');
  assert.match(d.element('ai-tbody').innerHTML, /alpha/);
  assert.ok(urls.every(url => url === '/api/ai-services'),
    'the tick never touches the usage or limits endpoints');

  d.run('_aiRefreshActive = true; aiServicesTick()');
  await settle();
  assert.equal(gets, 2, 'a manual refresh monitor owns its own poll loop');
  d.run('_aiRefreshActive = false');
});

test('a tick that finds a running refresh finishes it through the bounded monitor', async () => {
  const d = dashboard();
  d.run("_features.ai_services=true; _activeTab='ai-services';");
  const requests = [];
  let gets = 0;
  d.context.fetch = async (url, options) => {
    requests.push({ url, method: options.method || 'GET' });
    gets++;
    return response(aiServicesReport({ refreshing: true }));
  };
  d.run('aiServicesTick()');
  await settle();
  assert.equal(gets, 1);
  assert.equal(d.run('_aiRefreshActive'), true, 'accepting a refreshing payload starts the monitor');
  assert.equal(d.element('btn-ai-refresh').disabled, true);
  assert.match(d.element('ai-status').textContent, /polling/);

  const timer = d.timers.find(item => item.ms === 2000);
  assert.ok(timer, 'the bounded monitor schedules its poll');
  d.run('aiServicesTick()');
  await settle();
  assert.equal(gets, 1, 'the tick never adds a second loop while the monitor runs');

  // The monitor stops as soon as the backend reports the refresh finished.
  d.context.fetch = async (url, options) => {
    requests.push({ url, method: options.method || 'GET' });
    gets++;
    return response(aiServicesReport({ refreshing: false }));
  };
  timer.fn();
  await settle();
  assert.equal(gets, 2);
  assert.equal(d.run('_aiRefreshActive'), false);
  assert.equal(d.element('btn-ai-refresh').disabled, false);
  assert.ok(requests.every(item => item.url === '/api/ai-services' && item.method === 'GET'),
    'the tick and its monitor never POST a refresh');
  assert.match(d.element('toast').textContent, /benchmark scores updated/);
});

test('the tick surfaces stale/error sources and keeps last-good data on failure', async () => {
  const d = dashboard();
  d.run("_features.ai_services=true; _activeTab='ai-services';");
  d.context.fetch = async () => response(aiServicesReport());
  d.run('aiServicesTick()');
  await settle();
  assert.match(d.element('ai-tbody').innerHTML, /72\.4%/);

  const stale = aiServicesReport();
  stale.sources.harbor = { ...stale.sources.harbor, stale: true, error: 'source fetch timed out' };
  stale.models[0].bench = { tb4: 78.9, eci: 42.0 };
  d.context.fetch = async () => response(stale);
  d.run('aiServicesTick()');
  await settle();
  const sources = d.element('ai-sources').innerHTML;
  assert.match(sources, /ai-badge stale">STALE/);
  assert.match(sources, /ai-badge error">ERROR/);
  assert.match(sources, /source fetch timed out/);
  assert.match(d.element('ai-tbody').innerHTML, /78\.9%/);

  // Recovery: the next successful tick clears the stale/error state again.
  d.context.fetch = async () => response(aiServicesReport());
  d.run('aiServicesTick()');
  await settle();
  const recovered = d.element('ai-sources').innerHTML;
  assert.doesNotMatch(recovered, /STALE/);
  assert.doesNotMatch(recovered, /ai-badge error/);
  assert.match(d.element('ai-tbody').innerHTML, /72\.4%/);

  const lastGood = d.element('ai-tbody').innerHTML;
  d.context.fetch = async () => { throw new Error('offline'); };
  d.run('aiServicesTick()');
  await settle();
  assert.equal(d.element('ai-tbody').innerHTML, lastGood, 'transient errors keep the last good table');
  assert.match(d.element('ai-error').textContent, /last loaded inventory: offline/);
  assert.match(d.element('ai-status').textContent, /inventory loaded/);
});

test('a tick redraw preserves provider/route filters, columns, sort and open configs', async () => {
  const d = await loadAI();
  const posts = [];
  d.context.fetch = async (url, options) => {
    if ((options.method || 'GET') === 'POST') posts.push(url);
    return response(aiServicesReport());
  };
  d.run(`aiSetProvider('openai'); aiSetRoute('alpha-route');
         aiToggleColumn('livebench_reasoning', true);
         aiSort('livebench_reasoning');
         aiRememberConfigOpen('ai-config-0', true);`);
  const sortBefore = JSON.stringify(d.run('_aiSort'));

  d.run('aiServicesTick()');
  await settle();
  assert.equal(posts.length, 0, 'a tick never POSTs');
  assert.equal(d.run('_aiProvider'), 'openai');
  assert.equal(d.run('_aiRoute'), 'alpha-route');
  assert.equal(d.element('ai-provider-filter').value, 'openai');
  assert.equal(d.element('ai-route-filter').value, 'alpha-route');
  assert.equal(JSON.stringify(d.run('_aiSort')), sortBefore);
  assert.ok(d.run('_aiColumns').includes('livebench_reasoning'));
  const header = d.element('ai-thead').innerHTML;
  assert.match(header, /data-ai-key="livebench_reasoning"/);
  assert.match(header, /data-ai-key="livebench_reasoning"[^>]*aria-sort="descending"/);
  assert.match(d.element('ai-tbody').innerHTML, /alpha/);
  assert.doesNotMatch(d.element('ai-tbody').innerHTML, /beta/);
  assert.match(d.element('ai-configs').innerHTML, /id="ai-config-0" open/);
});

test('tick wiring keeps the 60s interval without touching the usage pollers', () => {
  const html = fs.readFileSync(path.join(__dirname, '../app/static/index.html'), 'utf8');
  assert.match(html, /setInterval\(securityTick, 60000\);[\s\S]{0,300}?setInterval\(aiServicesTick, 60000\);/);
  const tick = html.match(/function aiServicesTick\(\) \{[\s\S]*?\n\}/);
  assert.ok(tick, 'aiServicesTick is defined');
  assert.match(tick[0], /aiFetchServices\(\)/);
  assert.doesNotMatch(tick[0], /api\/benchmarks\/fetch|apiPost|fetchOpenCodeGo/);
  assert.equal((html.match(/setInterval\(fetchOpenCodeGo, 60000\)/g) || []).length, 1,
    'the usage poller keeps its own single 60s interval');
  assert.match(html, /setInterval\(fetchOpenCodeGoLimits, 3600000\)/);
});
