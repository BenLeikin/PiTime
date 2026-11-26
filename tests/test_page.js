// Tests for dashboard/static/dashboard.js, run with: node --test tests/
//
// The script is written for a browser, so it is loaded into a sandbox with
// just enough of a fake page for it to start: every element is a stand-in that
// accepts any property or call. The tests then call its functions directly.

const test = require('node:test');
const assert = require('node:assert');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const SOURCE = path.join(__dirname, '..', 'dashboard', 'static', 'dashboard.js');

function standIn() {
  const target = function () { return standIn(); };
  return new Proxy(target, {
    get(t, key) {
      if (key === Symbol.toPrimitive) return () => '';
      if (key === 'then') return undefined;          // not a promise
      if (key === 'firstChild') return null;         // "empty" element
      if (!(key in t)) t[key] = standIn();
      return t[key];
    },
    set(t, key, value) { t[key] = value; return true; },
  });
}

function loadPage() {
  const elements = {};
  const sandbox = {
    console, Intl, Date, Math, JSON, AbortController, performance,
    setInterval: () => 0,
    setTimeout: () => 0,
    clearTimeout: () => {},
    requestAnimationFrame: () => 0,
    fetch: () => new Promise(() => {}),              // never answers
    document: {
      body: { dataset: { timezone: 'Europe/Berlin' } },
      getElementById: (id) => (elements[id] = elements[id] || standIn()),
      createElement: () => standIn(),
      createElementNS: () => standIn(),
      querySelectorAll: () => [],
      addEventListener: () => {},
    },
  };
  sandbox.window = { addEventListener: () => {}, devicePixelRatio: 1 };
  vm.createContext(sandbox);
  vm.runInContext(fs.readFileSync(SOURCE, 'utf8'), sandbox, { filename: SOURCE });
  return { page: sandbox, elements };
}

function snapshot(overrides = {}) {
  return Object.assign({
    tracking: {
      reference_id: '50505300 (PPS)', stratum: '1', ref_time: 'Thu Nov 26 03:08:29 2026',
      last_offset: '+0.000000124 seconds', rms_offset: '0.000000212 seconds',
      freq_error: '9.797 ppm fast', residual_freq: '+0.000 ppm', skew: '0.007 ppm',
      root_delay: '0.000000001 seconds', root_dispersion: '0.000026874 seconds',
      update_interval: '16.0 seconds', leap_status: 'Normal',
      last_offset_s: 1.24e-7, rms_offset_s: 2.12e-7, frequency_ppm: 9.797, skew_ppm: 0.007,
    },
    sources: [{ state: 'synced', name: 'PPS', stratum: '0', poll: '4', reach: '377',
                last_rx: '9', last_sample_offset: '+12ns', margin: '±101ns' }],
    sourcestats: [{ name: 'PPS', np: '9', nr: '7', span: '125', freq_skew: '0.016',
                    std_dev: '398ns', offset: '-1ns' }],
    clients: [{ host: '192.168.1.20', ntp: '1234', drops: '5', last_query: '17' }],
    serverstats: { ntp_packets_received: '22', ntp_packets_dropped: '0',
                   command_packets_received: '9194', command_packets_dropped: '0',
                   client_log_records: '0' },
    activity: { 'sources online': '5' },
    gps: { available: true, has_fix: true, fix_since: 1000, fix_since_exact: true,
           last_fix: 1090, fix_mode: '3D', sats_used: 5, sats_visible: 16, best_snr: 27,
           hdop: 0.9, pdop: 1.6, alt: 12.5,
           satellites: [{ prn: 5, el: 45, az: 120, ss: 38, used: true }] },
    cpu_cores: [{ core: 'cpu0', usage: 1.5 }, { core: 'cpu3', usage: 0 }],
    pps_irq: { total: 5307 },
    pps_history: [-4.2e-8, 3.1e-8, -1e-9],
    next_leap: '2027-06-28',
    system: { hostname: 'time', ip: '192.168.1.2', uptime: 'up 2 hours',
              temperature: '41.8°C', memory: '3% of 7821MB', loadavg: '0.1 0.1 0.1',
              timestamp: '2026-11-26 05:20:18 UTC', epoch: 1100, isolated_cpus: '3' },
    health: { status: 'OK', issues: [] },
    visitors: { current: 2, total_unique: 196 },
  }, overrides);
}

test('the script loads against an empty page', () => {
  const { page } = loadPage();
  assert.strictEqual(typeof page.render, 'function');
  assert.strictEqual(typeof page.fetchData, 'function');
});

test('esc neutralises markup', () => {
  const { page } = loadPage();
  assert.strictEqual(page.esc('<img src=x onerror="a(1)">'),
                     '&lt;img src=x onerror=&quot;a(1)&quot;&gt;');
  assert.strictEqual(page.esc("it's & more"), 'it&#39;s &amp; more');
  assert.strictEqual(page.esc(null), '');
  assert.strictEqual(page.esc(42), '42');
});

test('formatDuration', () => {
  const { page } = loadPage();
  assert.strictEqual(page.formatDuration(5), '5s');
  assert.strictEqual(page.formatDuration(125), '2m 5s');
  assert.strictEqual(page.formatDuration(3 * 3600 + 12 * 60 + 9), '3h 12m 9s');
  assert.strictEqual(page.formatDuration(2 * 86400 + 3600), '2d 1h 0m');
  assert.strictEqual(page.formatDuration(-1), '—');
  assert.strictEqual(page.formatDuration(null), '—');
});

test('formatSecondsAsHuman picks the unit', () => {
  const { page } = loadPage();
  assert.strictEqual(page.formatSecondsAsHuman(4.2e-8), '42.0 ns');
  assert.strictEqual(page.formatSecondsAsHuman(-3.5e-6), '-3.50 µs');
  assert.strictEqual(page.formatSecondsAsHuman(0.0123), '12.300 ms');
  assert.strictEqual(page.formatSecondsAsHuman(null), '—');
});

test('reachBits draws eight bits from the octal reach value', () => {
  const { page } = loadPage();
  const all = page.reachBits('377');
  assert.strictEqual((all.match(/reach-bit on/g) || []).length, 8);
  const some = page.reachBits('17');                  // 00001111
  assert.strictEqual((some.match(/reach-bit on/g) || []).length, 4);
  assert.strictEqual((some.match(/reach-bit off/g) || []).length, 4);
  assert.strictEqual(page.reachBits('<b>'), '&lt;b&gt;');   // not a number: escaped text
});

test('usableTimeZone falls back to UTC for unknown zones', () => {
  const { page } = loadPage();
  assert.strictEqual(page.usableTimeZone('Europe/Berlin'), 'Europe/Berlin');
  assert.strictEqual(page.usableTimeZone('Not/AZone'), 'UTC');
});

test('offsets are coloured by size, from the number the server sends', () => {
  const { page, elements } = loadPage();
  const cls = (seconds) => {
    page.colorizeOffset('text', seconds, 'offset-under-test');
    return elements['offset-under-test'].className;
  };
  assert.strictEqual(cls(1.24e-7), 'stat-val good');     // 124 ns
  assert.strictEqual(cls(-5e-5), 'stat-val accent');     // 50 us, sign ignored
  assert.strictEqual(cls(0.002), 'stat-val bad');        // 2 ms
  assert.strictEqual(elements['offset-under-test'].textContent, 'text');
});

test('fun facts work from numbers and survive a missing offset', () => {
  const { page } = loadPage();
  const withOffset = snapshot();
  for (let i = 0; i < 200; i++) {
    const fact = page.generateFunFact(withOffset);
    assert.ok(!/NaN|undefined|Infinity/.test(fact), fact);
  }
  const unknown = snapshot();
  unknown.tracking = { last_offset: '\u2014', stratum: '\u2014' };   // chrony down
  for (let i = 0; i < 200; i++) {
    const fact = page.generateFunFact(unknown);
    assert.ok(!/NaN|undefined|Infinity/.test(fact), fact);
  }
});

test('render fills the tables and escapes what remote hosts control', () => {
  const { page, elements } = loadPage();
  const data = snapshot({
    sources: [{ state: 'not combined', name: '<img src=x onerror=alert(1)>', stratum: '2',
                poll: '6', reach: '377', last_rx: '12', last_sample_offset: '+1ms',
                margin: '±20ms' }],
  });
  page.render(data);
  const rows = elements['sources-body'].innerHTML;
  assert.ok(rows.includes('&lt;img src=x onerror=alert(1)&gt;'));
  assert.ok(!rows.includes('<img'));
  assert.ok(rows.includes('state-not-combined'));
  assert.ok(elements['clients-body'].innerHTML.includes('192.168.1.20'));
  assert.strictEqual(elements['status-text'].textContent, '◆ NOMINAL ▸ LOCKED & SERVING');
  assert.strictEqual(elements['tx-indicator'].textContent, 'TRANSMITTING');
  assert.strictEqual(elements['clients-note'].style.display, 'none');
});

test('render shows faults, lost fixes and the public-viewer note', () => {
  const { page, elements } = loadPage();
  page.render(snapshot({
    health: { status: 'FAULT', issues: ['No synced source'] },
    gps: { available: true, has_fix: false, fix_since: null, fix_since_exact: false,
           last_fix: 800, fix_mode: 'no fix', sats_used: 0, sats_visible: 0, satellites: [] },
    redacted: true,
  }));
  assert.strictEqual(elements['tx-indicator'].textContent, 'NOT SYNCED');
  assert.strictEqual(elements['status-issues'].textContent, 'No synced source');
  assert.match(elements['g-fix-age'].textContent, /^no fix for 5m/);   // epoch 1100 - 800
  assert.strictEqual(elements['clients-note'].style.display, 'block');
});

test('time since lock counts from the server record', () => {
  const { page, elements } = loadPage();
  page.render(snapshot());                             // locked since 1000, now 1100
  assert.match(elements['g-fix-age'].textContent, /^1m 4\ds$/);
  const unknownStart = snapshot();
  unknownStart.gps.fix_since_exact = false;
  page.render(unknownStart);
  assert.match(elements['g-fix-age'].textContent, /^> 1m 4\ds$/);
});

test('history charts scale robustly and break lines at gaps', () => {
  const { page } = loadPage();
  const spiky = Array.from({ length: 100 }, (_, i) => (i === 50 ? 1000 : i % 2));
  const plain = page.seriesRange([spiky], false);
  const robust = page.seriesRange([spiky], true);
  assert.ok(plain[1] > 1000);            // the spike sets the scale...
  assert.ok(robust[1] < 2);              // ...unless the axis is robust
  assert.strictEqual(page.seriesRange([[null, null]], true), null);
  const d = page.linePath([1, 2, null, 3, 4], 100, 50, 0, 5, 0);
  assert.strictEqual((d.match(/M/g) || []).length, 2);   // two segments, not one
  assert.deepStrictEqual([...page.minMax([3, null, -1, 7])], [-1, 7]);
  assert.strictEqual(page.minMax([null]), null);
});

test('link, expiry and GPS-lost display', () => {
  const { page, elements } = loadPage();
  assert.strictEqual(page.linkText({ kind: 'Wi-Fi', signal_dbm: -38 }), 'Wi-Fi -38 dBm');
  assert.strictEqual(page.linkText({ kind: 'cable', signal_dbm: null }), 'cable');
  assert.strictEqual(page.linkText(null), '—');

  page.setExpiry('exp', { expires: '2027-01-28', days: 63 }, 'none', 21);
  assert.strictEqual(elements.exp.textContent, '2027-01-28 (63 d)');
  assert.strictEqual(elements.exp.className, 'stat-val ');
  page.setExpiry('exp', { expires: '2026-12-10', days: 14 }, 'none', 21);
  assert.strictEqual(elements.exp.className, 'stat-val warn');
  page.setExpiry('exp', { expires: '2026-11-01', days: -25 }, 'none', 21);
  assert.strictEqual(elements.exp.textContent, '2026-11-01 (expired)');

  page.renderHoldover({ on_gps: true }, 1000);
  assert.strictEqual(elements['holdover-banner'].hidden, true);
  page.renderHoldover({ on_gps: false, since: 900, since_exact: true, last_on_gps: 880,
                        reference: 'A29FC87B (162.159.200.123)', error_bound_s: 0.013 }, 1000);
  assert.strictEqual(elements['holdover-banner'].hidden, false);
  assert.match(elements['holdover-text'].textContent, /ON INTERNET TIME/);
  assert.match(elements['holdover-detail'].textContent, /last on GPS 2m 0s ago.*162\.159\.200\.123.*±13\.000 ms/);
  page.renderHoldover({ on_gps: false, since: 900, since_exact: false, last_on_gps: null,
                        reference: '7F7F0101 ()', error_bound_s: null }, 1000);
  assert.match(elements['holdover-text'].textContent, /FREE-RUNNING/);
  assert.match(elements['holdover-detail'].textContent, /^over 1m 40s without GPS$/);
});
