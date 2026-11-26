const REFRESH_S = 5;
// Set by render() from the server's own record of the GPS fix; the ticker
// below only adds the time since that snapshot arrived.
let fixInfo = null;
let lastGoodAt = null;   // performance.now() of the last successful poll
let linkLost = false;
let fetching = false;

function reachBits(octal) {
  let dec = parseInt(octal, 8);
  if (isNaN(dec)) return esc(octal);
  let html = '<span class="reach-bits">';
  for (let i = 7; i >= 0; i--) {
    html += `<span class="reach-bit ${(dec >> i) & 1 ? 'on' : 'off'}"></span>`;
  }
  return html + '</span>';
}

// Escape text before it goes into innerHTML. Everything shown comes from
// chronyc/gpsd output, but those include names that remote hosts control.
function esc(v) {
  return String(v == null ? '' : v).replace(/[&<>"']/g, c => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
  })[c]);
}

function stateClass(state) { return 'state-' + (state || '').replace(/ /g, '-'); }

function setText(id, val) {
  const el = document.getElementById(id);
  if (el) el.textContent = (val === undefined || val === null || val === '') ? '\u2014' : val;
}

// Show an offset as chronyc words it, coloured by its size: under 10 us is
// good, under 1 ms acceptable, anything more is bad. `seconds` is the number
// the server sends alongside the text.
function colorizeOffset(text, seconds, id) {
  const el = document.getElementById(id);
  if (!el) return;
  el.textContent = text;
  if (typeof seconds === 'number') {
    const size = Math.abs(seconds);
    el.className = 'stat-val ' + (size < 1e-5 ? 'good' : size < 1e-3 ? 'accent' : 'bad');
  }
}

function formatDuration(seconds) {
  if (seconds == null || seconds < 0) return '\u2014';
  const d = Math.floor(seconds / 86400);
  const h = Math.floor((seconds % 86400) / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  const s = Math.floor(seconds % 60);
  if (d > 0) return `${d}d ${h}h ${m}m`;
  if (h > 0) return `${h}h ${m}m ${s}s`;
  if (m > 0) return `${m}m ${s}s`;
  return `${s}s`;
}

function formatSecondsAsHuman(s) {
  if (s == null) return '\u2014';
  const abs = Math.abs(s);
  if (abs < 1e-6) return (s * 1e9).toFixed(1) + ' ns';
  if (abs < 1e-3) return (s * 1e6).toFixed(2) + ' \u00b5s';
  if (abs < 1)    return (s * 1e3).toFixed(3) + ' ms';
  return s.toFixed(3) + ' s';
}

const canvas = document.getElementById('skyview-canvas');
const ctx = canvas.getContext('2d');
let satsCurrent = [];
let satsAnimated = [];
let radarAngle = 0;

// Setting canvas.width reallocates and clears the canvas, so do it only when
// the size really changes, and keep the size for drawSky() to reuse.
let canvasW = 0;
let canvasH = 0;
function resizeCanvas() {
  const dpr = window.devicePixelRatio || 1;
  const rect = canvas.getBoundingClientRect();
  const pxW = Math.round(rect.width * dpr);
  const pxH = Math.round(rect.height * dpr);
  canvasW = rect.width;
  canvasH = rect.height;
  if (canvas.width !== pxW || canvas.height !== pxH) {
    canvas.width = pxW;
    canvas.height = pxH;
  }
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
}
window.addEventListener('resize', resizeCanvas);
if (typeof ResizeObserver === 'function') {
  new ResizeObserver(resizeCanvas).observe(canvas);
}

function projectSat(el, az, w, h) {
  const cx = w / 2;
  const cy = h / 2;
  const r = Math.min(w, h) / 2 - 28;
  const elRad = (el || 0) * Math.PI / 180;
  const azRad = (az || 0) * Math.PI / 180;
  const dist = r * (1 - Math.sin(elRad));
  const x = cx + dist * Math.sin(azRad);
  const y = cy - dist * Math.cos(azRad);
  return { x, y };
}

function drawSky() {
  const w = canvasW;
  const h = canvasH;
  ctx.clearRect(0, 0, w, h);

  const cx = w / 2;
  const cy = h / 2;
  const r = Math.min(w, h) / 2 - 28;

  if (r <= 0) return;

  const grad = ctx.createRadialGradient(cx, cy, 0, cx, cy, r);
  grad.addColorStop(0, 'rgba(0,212,255,0.04)');
  grad.addColorStop(1, 'rgba(0,212,255,0)');
  ctx.fillStyle = grad;
  ctx.beginPath();
  ctx.arc(cx, cy, r, 0, 2 * Math.PI);
  ctx.fill();

  if (typeof ctx.createConicGradient === 'function') {
    const sweepGrad = ctx.createConicGradient(radarAngle, cx, cy);
    sweepGrad.addColorStop(0, 'rgba(0,212,255,0.18)');
    sweepGrad.addColorStop(0.1, 'rgba(0,212,255,0.0)');
    sweepGrad.addColorStop(1, 'rgba(0,212,255,0.0)');
    ctx.fillStyle = sweepGrad;
    ctx.beginPath();
    ctx.arc(cx, cy, r, 0, 2 * Math.PI);
    ctx.fill();
  }

  ctx.strokeStyle = 'rgba(45,74,120,0.4)';
  ctx.lineWidth = 1;
  for (const elev of [0, 30, 60]) {
    const ringR = r * (1 - elev / 90);
    ctx.beginPath();
    ctx.arc(cx, cy, ringR, 0, 2 * Math.PI);
    ctx.stroke();
  }

  ctx.setLineDash([3, 4]);
  ctx.beginPath();
  ctx.moveTo(cx - r, cy); ctx.lineTo(cx + r, cy);
  ctx.moveTo(cx, cy - r); ctx.lineTo(cx, cy + r);
  ctx.stroke();
  ctx.setLineDash([]);

  ctx.fillStyle = '#00d4ff';
  ctx.font = "bold 11px 'Orbitron', sans-serif";
  ctx.textAlign = 'center';
  ctx.textBaseline = 'middle';
  ctx.fillText('N', cx, cy - r - 12);
  ctx.fillText('S', cx, cy + r + 12);
  ctx.fillText('E', cx + r + 12, cy);
  ctx.fillText('W', cx - r - 12, cy);

  ctx.fillStyle = '#4a6080';
  ctx.font = "9px 'Space Mono', monospace";
  ctx.fillText('30\u00b0', cx + 4, cy - r * 0.667 + 2);
  ctx.fillText('60\u00b0', cx + 4, cy - r * 0.333 + 2);

  for (const sat of satsAnimated) {
    if (sat.el == null || sat.az == null) continue;
    const { x, y } = projectSat(sat.el, sat.az, w, h);
    const used = sat.used;
    const ss = sat.ss || 0;

    if (used) {
      ctx.strokeStyle = 'rgba(0,255,136,0.4)';
      ctx.lineWidth = 1;
      ctx.beginPath();
      ctx.arc(x, y, 11, 0, 2 * Math.PI);
      ctx.stroke();

      ctx.shadowBlur = 14;
      ctx.shadowColor = '#00ff88';
      ctx.fillStyle = '#00ff88';
      ctx.beginPath();
      ctx.arc(x, y, 4 + Math.min(ss / 12, 4), 0, 2 * Math.PI);
      ctx.fill();
      ctx.shadowBlur = 0;

      ctx.strokeStyle = 'rgba(0,255,136,0.8)';
      ctx.lineWidth = 1;
      ctx.beginPath();
      ctx.moveTo(x - 14, y); ctx.lineTo(x - 9, y);
      ctx.moveTo(x + 9, y);  ctx.lineTo(x + 14, y);
      ctx.moveTo(x, y - 14); ctx.lineTo(x, y - 9);
      ctx.moveTo(x, y + 9);  ctx.lineTo(x, y + 14);
      ctx.stroke();
    } else {
      ctx.fillStyle = ss > 25 ? 'rgba(192,208,224,0.7)' : 'rgba(74,96,128,0.7)';
      ctx.beginPath();
      ctx.arc(x, y, 2.5, 0, 2 * Math.PI);
      ctx.fill();
    }

    ctx.fillStyle = used ? '#00ff88' : 'rgba(74,96,128,0.8)';
    ctx.font = "bold 10px 'Space Mono', monospace";
    ctx.fillText(sat.prn, x, y + (used ? 22 : 12));
  }

  ctx.strokeStyle = '#ffaa00';
  ctx.lineWidth = 1;
  ctx.beginPath();
  ctx.arc(cx, cy, 6, 0, 2 * Math.PI);
  ctx.moveTo(cx - 10, cy); ctx.lineTo(cx + 10, cy);
  ctx.moveTo(cx, cy - 10); ctx.lineTo(cx, cy + 10);
  ctx.stroke();
  ctx.fillStyle = '#ffaa00';
  ctx.beginPath();
  ctx.arc(cx, cy, 1.5, 0, 2 * Math.PI);
  ctx.fill();
}

// The sky changes slowly, so 20 frames a second is plenty; the sweep angle
// follows the clock so its speed does not depend on the frame rate.
const SKY_FRAME_MS = 50;
let lastSkyDraw = 0;
function animateSats(now) {
  requestAnimationFrame(animateSats);
  if (now - lastSkyDraw < SKY_FRAME_MS) return;
  lastSkyDraw = now;
  radarAngle = (now / 1000) * 0.72;
  for (let i = 0; i < satsAnimated.length; i++) {
    const sat = satsAnimated[i];
    const target = satsCurrent.find(s => s.prn === sat.prn);
    if (target) {
      if (target.el == null || target.az == null || sat.el == null || sat.az == null) {
        // Position unknown on one side: nothing to ease between.
        sat.el = target.el;
        sat.az = target.az;
      } else {
        const dEl = target.el - sat.el;
        // Take the short way round the compass (359 -> 1 is +2, not -358).
        const dAz = ((target.az - sat.az + 540) % 360) - 180;
        sat.el += dEl * 0.17;
        sat.az = (sat.az + dAz * 0.17 + 360) % 360;
      }
      sat.ss = target.ss;
      sat.used = target.used;
    }
  }
  drawSky();
}

function updateSats(newSats) {
  for (const ns of newSats) {
    const existing = satsAnimated.find(s => s.prn === ns.prn);
    if (!existing) {
      satsAnimated.push({...ns});
    }
  }
  satsAnimated = satsAnimated.filter(s => newSats.find(n => n.prn === s.prn));
  satsCurrent = newSats;
}

requestAnimationFrame(animateSats);

function drawSparkline(values) {
  const svg = document.getElementById('pps-spark');
  while (svg.firstChild) svg.removeChild(svg.firstChild);
  if (!values || values.length === 0) {
    const text = document.createElementNS('http://www.w3.org/2000/svg', 'text');
    text.setAttribute('x', '50%');
    text.setAttribute('y', '50%');
    text.setAttribute('text-anchor', 'middle');
    text.setAttribute('fill', '#4a6080');
    text.setAttribute('font-family', 'Space Mono');
    text.setAttribute('font-size', '10');
    text.textContent = 'COLLECTING - chrony makes one PPS sample every poll';
    svg.appendChild(text);
    return;
  }
  const rect = svg.getBoundingClientRect();
  const w = rect.width;
  const h = rect.height;
  svg.setAttribute('viewBox', `0 0 ${w} ${h}`);

  const min = Math.min(...values);
  const max = Math.max(...values);
  const range = max - min || 1;
  const padding = 6;
  const usableH = h - 2 * padding;

  for (let i = 1; i < 4; i++) {
    const y = padding + (usableH * i / 4);
    const line = document.createElementNS('http://www.w3.org/2000/svg', 'line');
    line.setAttribute('x1', 0);
    line.setAttribute('x2', w);
    line.setAttribute('y1', y);
    line.setAttribute('y2', y);
    line.setAttribute('stroke', 'rgba(45,74,120,0.3)');
    line.setAttribute('stroke-dasharray', '2,3');
    svg.appendChild(line);
  }

  if (min < 0 && max > 0) {
    const zeroY = padding + usableH * (max / range);
    const zeroLine = document.createElementNS('http://www.w3.org/2000/svg', 'line');
    zeroLine.setAttribute('x1', 0);
    zeroLine.setAttribute('x2', w);
    zeroLine.setAttribute('y1', zeroY);
    zeroLine.setAttribute('y2', zeroY);
    zeroLine.setAttribute('stroke', '#ffaa00');
    zeroLine.setAttribute('stroke-dasharray', '4,3');
    zeroLine.setAttribute('opacity', '0.4');
    svg.appendChild(zeroLine);
  }

  const stepX = w / (values.length - 1 || 1);
  let pathStr = `M 0 ${h}`;
  values.forEach((v, i) => {
    const x = i * stepX;
    const y = padding + usableH * (1 - (v - min) / range);
    pathStr += ` L ${x} ${y}`;
  });
  pathStr += ` L ${w} ${h} Z`;

  const fillGrad = `<defs><linearGradient id="ppsGrad" x1="0" x2="0" y1="0" y2="1">
    <stop offset="0%" stop-color="#00d4ff" stop-opacity="0.3"/>
    <stop offset="100%" stop-color="#00d4ff" stop-opacity="0"/>
  </linearGradient></defs>`;
  svg.insertAdjacentHTML('afterbegin', fillGrad);

  const fill = document.createElementNS('http://www.w3.org/2000/svg', 'path');
  fill.setAttribute('d', pathStr);
  fill.setAttribute('fill', 'url(#ppsGrad)');
  svg.appendChild(fill);

  let d = '';
  values.forEach((v, i) => {
    const x = i * stepX;
    const y = padding + usableH * (1 - (v - min) / range);
    d += (i === 0 ? 'M' : 'L') + x + ',' + y + ' ';
  });
  const path = document.createElementNS('http://www.w3.org/2000/svg', 'path');
  path.setAttribute('d', d);
  path.setAttribute('fill', 'none');
  path.setAttribute('stroke', '#00d4ff');
  path.setAttribute('stroke-width', '1.5');
  path.setAttribute('filter', 'drop-shadow(0 0 4px rgba(0,212,255,0.6))');
  svg.appendChild(path);

  const lastX = (values.length - 1) * stepX;
  const lastY = padding + usableH * (1 - (values[values.length - 1] - min) / range);
  const dot = document.createElementNS('http://www.w3.org/2000/svg', 'circle');
  dot.setAttribute('cx', lastX);
  dot.setAttribute('cy', lastY);
  dot.setAttribute('r', '3.5');
  dot.setAttribute('fill', '#00d4ff');
  dot.setAttribute('filter', 'drop-shadow(0 0 6px #00d4ff)');
  svg.appendChild(dot);
}

const TIMEZONES = [
  { city: 'Los Angeles', tz: 'America/Los_Angeles' },
  { city: 'New York',    tz: 'America/New_York' },
  { city: 'London',      tz: 'Europe/London' },
  { city: 'Berlin',      tz: 'Europe/Berlin' },
  { city: 'Tokyo',       tz: 'Asia/Tokyo' },
  { city: 'Sydney',      tz: 'Australia/Sydney' },
  { city: 'UTC',         tz: 'UTC' },
];

function updateWorldClock() {
  const wc = document.getElementById('world-clock');
  const now = new Date();
  wc.innerHTML = TIMEZONES.map(t => {
    const time = now.toLocaleTimeString('en-US', { timeZone: t.tz, hour12: false });
    const date = now.toLocaleDateString('en-US', { timeZone: t.tz, month: 'short', day: 'numeric' });
    return `<div class="tz-cell">
      <div class="city">${t.city}</div>
      <div class="time">${time}</div>
      <div class="date">${date}</div>
    </div>`;
  }).join('');
}
setInterval(updateWorldClock, 1000);

// Header clock in the server's own time zone - sub-second precision, smooth
// update via rAF. Browser Date.now() is millisecond-resolution at best (timer
// precision is intentionally reduced by browsers), so 3 decimal places is the
// honest ceiling.
// The server puts its IANA zone name on <body>; fall back to UTC if the
// browser does not know it.
function usableTimeZone(name) {
  try {
    new Intl.DateTimeFormat('en-US', { timeZone: name });
    return name;
  } catch (e) {
    return 'UTC';
  }
}
const SERVER_TZ = usableTimeZone(document.body.dataset.timezone || 'UTC');
const _ltHms = document.getElementById('lt-hms');
const _ltMs = document.getElementById('lt-ms');
const _ltDate = document.getElementById('lt-date');
const _ltZone = document.getElementById('lt-zone');
const _ltHmsFmt = new Intl.DateTimeFormat('en-US', {
  timeZone: SERVER_TZ,
  hour12: false, hour: '2-digit', minute: '2-digit', second: '2-digit'
});
const _ltDateFmt = new Intl.DateTimeFormat('en-US', {
  timeZone: SERVER_TZ,
  weekday: 'short', month: 'short', day: '2-digit', year: 'numeric'
});
const _ltZoneFmt = new Intl.DateTimeFormat('en-US', {
  timeZone: SERVER_TZ, timeZoneName: 'short'
});
function zoneNamePart(format, d) {
  const p = format.formatToParts(d).find(x => x.type === 'timeZoneName');
  return p ? p.value : '';
}
function _ltGetAbbrev(d) {
  return zoneNamePart(_ltZoneFmt, d) || SERVER_TZ;
}
// Label like "Pacific Time"; older browsers lack 'longGeneric', so fall back
// to the zone's city ("Los Angeles").
(function labelZone() {
  let name = '';
  try {
    name = zoneNamePart(new Intl.DateTimeFormat('en-US', {
      timeZone: SERVER_TZ, timeZoneName: 'longGeneric' }), new Date());
  } catch (e) { /* fall through */ }
  if (!name) name = SERVER_TZ.split('/').pop().replace(/_/g, ' ');
  document.getElementById('lt-name').textContent = name;
})();
function updateHeaderClock() {
  const now = new Date();
  // toLocaleTimeString with fractionalSecondDigits respects the timezone correctly,
  // and we render hh:mm:ss separately from ms to keep the ms in its own color.
  const hms = _ltHmsFmt.format(now).replace(/^24/, '00'); // normalize midnight
  const ms = String(now.getMilliseconds()).padStart(3, '0');
  if (_ltHms.textContent !== hms) _ltHms.textContent = hms;
  _ltMs.textContent = '.' + ms;
  // Date and zone change infrequently. Update them once a second is plenty.
  if (!window._ltLastSec || window._ltLastSec !== hms) {
    window._ltLastSec = hms;
    _ltDate.textContent = _ltDateFmt.format(now);
    _ltZone.textContent = _ltGetAbbrev(now);
  }
  requestAnimationFrame(updateHeaderClock);
}
requestAnimationFrame(updateHeaderClock);

function updateFixAge() {
  const el = document.getElementById('g-fix-age');
  if (!el) return;
  if (!fixInfo || linkLost) {
    el.textContent = '\u2014';
    el.className = 'stat-val';
    return;
  }
  const sinceFetch = (performance.now() - fixInfo.fetchedAt) / 1000;
  if (fixInfo.lockedS != null) {
    // "> " when the fix was already there the first time the server looked.
    el.textContent = (fixInfo.exact ? '' : '> ') + formatDuration(fixInfo.lockedS + sinceFetch);
    el.className = 'stat-val good';
  } else if (fixInfo.lostS != null) {
    el.textContent = 'no fix for ' + formatDuration(fixInfo.lostS + sinceFetch);
    el.className = 'stat-val bad';
  } else {
    el.textContent = 'no fix';
    el.className = 'stat-val bad';
  }
}

// When polls fail, say so where the status is, and how old the data is.
function showLinkLost() {
  linkLost = true;
  const banner = document.getElementById('status-banner');
  banner.classList.remove('ok', 'degraded');
  banner.classList.add('fault');
  setText('status-text', '\u2715 TELEMETRY LINK LOST');
  setText('status-issues', lastGoodAt == null
    ? 'no data received yet'
    : 'showing data from ' + formatDuration((performance.now() - lastGoodAt) / 1000) + ' ago');
  const tx = document.getElementById('tx-indicator');
  tx.textContent = 'NO LINK';
  tx.classList.add('down');
  updateFixAge();
}

setInterval(() => {
  if (linkLost) showLinkLost(); else updateFixAge();
}, 1000);

function render(d) {
  const { tracking: t, sources, sourcestats, clients, serverstats: ss, activity,
          gps, cpu_cores, pps_irq, pps_history, system: s, health } = d;

  setText('h-hostname', s.hostname);
  setText('h-ip', s.ip);
  setText('h-uptime', s.uptime);
  setText('h-temp', s.temperature);
  setText('h-ts', s.timestamp);

  // Fun fact rotation - throttled independent of data refresh
  if (!window._lastFactTime || Date.now() - window._lastFactTime > 15000) {
    document.getElementById('fun-fact-text').innerHTML = generateFunFact(d);
    window._lastFactTime = Date.now();
  }

  const banner = document.getElementById('status-banner');
  banner.classList.remove('ok', 'degraded', 'fault');
  if (health.status === 'OK') {
    banner.classList.add('ok');
    setText('status-text', '\u25C6 NOMINAL \u25B8 LOCKED & SERVING');
  } else if (health.status === 'DEGRADED') {
    banner.classList.add('degraded');
    setText('status-text', '\u26A0 DEGRADED');
  } else {
    banner.classList.add('fault');
    setText('status-text', '\u2715 FAULT');
  }
  setText('status-issues', health.issues.length ? health.issues.join('  \u25B8  ') : '');

  if (gps.satellites) {
    updateSats(gps.satellites);
    setText('skyview-meta', `${gps.sats_used || 0} LOCKED \u25C6 ${gps.sats_visible || 0} VISIBLE`);
  }

  setText('g-fix', gps.fix_mode);
  setText('g-sats', `${gps.sats_used || 0} / ${gps.sats_visible || 0}`);
  setText('g-snr', gps.best_snr || '\u2014');
  setText('g-hdop', gps.hdop != null ? gps.hdop.toFixed(2) : '\u2014');
  setText('g-pdop', gps.pdop != null ? gps.pdop.toFixed(2) : '\u2014');
  setText('g-alt', gps.alt != null ? gps.alt.toFixed(1) + ' m' : '\u2014');

  // Durations are worked out against the server's clock (s.epoch), so a wrong
  // clock on the viewer's machine does not change them.
  fixInfo = {
    lockedS: gps.has_fix && gps.fix_since != null ? s.epoch - gps.fix_since : null,
    exact: !!gps.fix_since_exact,
    lostS: gps.last_fix != null ? s.epoch - gps.last_fix : null,
    fetchedAt: performance.now(),
  };
  updateFixAge();

  const tx = document.getElementById('tx-indicator');
  tx.textContent = health.status === 'FAULT' ? 'NOT SYNCED' : 'TRANSMITTING';
  tx.classList.toggle('down', health.status === 'FAULT');

  colorizeOffset(t.last_offset, t.last_offset_s, 'h-sys-offset');
  setText('h-rms-offset', 'RMS ' + t.rms_offset);
  setText('h-stratum', t.stratum);
  setText('h-leap', 'Leap ' + t.leap_status);
  setText('h-freq', t.freq_error);
  setText('h-skew', 'Skew ' + t.skew);

  setText('t-refid', t.reference_id);
  setText('t-reftime', t.ref_time);
  setText('t-interval', t.update_interval);
  setText('t-rootdelay', t.root_delay);
  setText('t-rootdisp', t.root_dispersion);
  setText('t-residfreq', t.residual_freq);
  const actStr = Object.entries(activity).map(([k, v]) => `${v} ${k}`).join(' \u25B8 ');
  setText('t-activity', actStr || '\u2014');

  setText('s-host', s.hostname);
  setText('s-ip', s.ip);
  setText('s-temp', s.temperature);
  setText('s-mem', s.memory);
  setText('s-load', s.loadavg);
  setText('s-uptime', s.uptime);
  setText('s-isolated', s.isolated_cpus);
  setText('s-leap', d.next_leap || 'no scheduled leap');

  const ccDiv = document.getElementById('cpu-cores');
  const isolatedSet = new Set();
  if (s.isolated_cpus && s.isolated_cpus !== 'none' && s.isolated_cpus !== '\u2014') {
    s.isolated_cpus.split(',').forEach(part => {
      if (part.includes('-')) {
        const [a, b] = part.split('-').map(Number);
        for (let i = a; i <= b; i++) isolatedSet.add('cpu' + i);
      } else {
        isolatedSet.add('cpu' + part.trim());
      }
    });
  }
  ccDiv.innerHTML = cpu_cores.map(c => {
    const isIso = isolatedSet.has(c.core);
    return `<div class="core-bar">
      <span class="label ${isIso ? 'isolated' : ''}">${c.core.toUpperCase()}${isIso ? ' \u25C6' : ''}</span>
      <div class="track"><div class="fill ${isIso ? 'isolated' : ''}" style="width:${c.usage}%"></div></div>
      <span class="pct">${c.usage}%</span>
    </div>`;
  }).join('') + (isolatedSet.size > 0 ? '<div style="font-size:0.62rem;color:var(--magenta);margin-top:0.7rem;letter-spacing:0.1em;">\u25C6 ISOLATED CORE \u25C6</div>' : '');

  setText('ss-rx', Number(ss.ntp_packets_received).toLocaleString());
  setText('ss-drop', ss.ntp_packets_dropped);
  setText('ss-cmd-rx', Number(ss.command_packets_received).toLocaleString());
  setText('ss-cmd-drop', ss.command_packets_dropped);
  setText('ss-clog', ss.client_log_records);

  if (pps_history && pps_history.length > 0) {
    drawSparkline(pps_history);
    const min = Math.min(...pps_history);
    const max = Math.max(...pps_history);
    setText('pps-min', formatSecondsAsHuman(min));
    setText('pps-max', formatSecondsAsHuman(max));
    setText('pps-range', formatSecondsAsHuman(max - min));
    setText('pps-count', pps_history.length);
  } else {
    drawSparkline([]);
    setText('pps-min', '\u2014');
    setText('pps-max', '\u2014');
    setText('pps-range', '\u2014');
    setText('pps-count', '0');
  }
  setText('pps-irq', pps_irq.total ? pps_irq.total.toLocaleString() : '\u2014');

  const sb = document.getElementById('sources-body');
  if (sources.length === 0) {
    sb.innerHTML = '<tr><td colspan="8" style="color:var(--muted);padding:1rem">No sources</td></tr>';
  } else {
    sb.innerHTML = sources.map(src => {
      return `
      <tr>
        <td><span class="state-badge ${esc(stateClass(src.state))}">${esc(src.state)}</span></td>
        <td>${esc(src.name)}</td>
        <td>${esc(src.stratum)}</td>
        <td>${esc(src.poll)}</td>
        <td>${reachBits(src.reach)}</td>
        <td>${esc(src.last_rx)}</td>
        <td>${esc(src.last_sample_offset)}</td>
        <td>${esc(src.margin)}</td>
      </tr>`;
    }).join('');
  }

  const stb = document.getElementById('stats-body');
  if (sourcestats.length === 0) {
    stb.innerHTML = '<tr><td colspan="6" style="color:var(--muted);padding:1rem">No stats</td></tr>';
  } else {
    stb.innerHTML = sourcestats.map(r => `
      <tr>
        <td>${esc(r.name)}</td>
        <td>${esc(r.np)} / ${esc(r.nr)}</td>
        <td>${esc(r.span)}</td>
        <td>${esc(r.freq_skew)}</td>
        <td>${esc(r.std_dev)}</td>
        <td>${esc(r.offset)}</td>
      </tr>`).join('');
  }

  const cb = document.getElementById('clients-body');
  if (clients.length === 0) {
    cb.innerHTML = '<tr><td colspan="4" style="color:var(--muted);padding:1rem">No clients yet</td></tr>';
  } else {
    cb.innerHTML = clients.map(c => `
      <tr>
        <td>${esc(c.host)}</td>
        <td>${esc(c.ntp)}</td>
        <td>${esc(c.drops)}</td>
        <td>${esc(c.last_query)}</td>
      </tr>`).join('');
  }

  document.getElementById('clients-note').style.display = d.redacted ? 'block' : 'none';

  if (d.visitors) {
    setText('visitor-current', d.visitors.current);
    setText('visitor-total', d.visitors.total_unique.toLocaleString());
  }
}


function generateFunFact(d) {
  const offsetStr = esc(d.tracking.last_offset || '');
  // The facts compare the size of the last offset with other things; assume
  // a microsecond if chrony has not reported one.
  const seconds = typeof d.tracking.last_offset_s === 'number'
    ? Math.abs(d.tracking.last_offset_s) : 1e-6;
  const ns = seconds * 1e9;
  const speedOfLight = 299792458;
  const lightDistance = seconds * speedOfLight;
  const soundSpeed = 343;
  const soundDistance = seconds * soundSpeed;

  const fmtMeters = (m) => {
    if (m < 0.001) return (m * 1000).toFixed(2) + ' mm';
    if (m < 1)     return (m * 100).toFixed(1) + ' cm';
    if (m < 1000)  return m.toFixed(2) + ' m';
    return (m / 1000).toFixed(2) + ' km';
  };

  const sats = d.gps.sats_used || 0;
  const visible = d.gps.sats_visible || 0;
  const alt = d.gps.alt;
  const ntpRx = parseInt(d.serverstats.ntp_packets_received) || 0;
  const uptime = esc(d.system.uptime || '');
  const stratum = esc(d.tracking.stratum);
  const freq = d.tracking.frequency_ppm || 0;
  const skew = d.tracking.skew_ppm || 0;
  const ppsTotal = (d.pps_irq && d.pps_irq.total) || 0;
  const temp = esc(d.system.temperature || '');
  const rootDelay = d.tracking.root_delay || '';

  const facts = [
    // Light & sound at your offset
    `Your last offset of <span class="hl">${offsetStr}</span> is the time it takes light to travel <span class="hl-amber">${fmtMeters(lightDistance)}</span> in a vacuum.`,
    `Sound travels just <span class="hl-amber">${fmtMeters(soundDistance)}</span> in the time of your current offset of <span class="hl">${offsetStr}</span>.`,
    `In the time of your offset, an electron travels less than <span class="hl-amber">${fmtMeters(lightDistance * 0.05)}</span> through copper wire (signals propagate at ~5% of c).`,
    `Light travels <span class="hl">~30 cm in 1 nanosecond</span>. Your last offset corresponds to about <span class="hl-amber">${fmtMeters(lightDistance)}</span> of light travel.`,
    `Your offset of <span class="hl">${offsetStr}</span> is roughly the time it takes a photon to travel <span class="hl-amber">${fmtMeters(lightDistance)}</span>. A human hair is 80 microns thick.`,
    `An optical fiber transmits light at about <span class="hl">2/3 the vacuum speed of light</span>. Your offset equates to ${fmtMeters(lightDistance * 0.67)} of fiber travel.`,
    `In your current offset, a radio wave at 2.4 GHz Wi-Fi completes only <span class="hl-amber">${Math.max(1, Math.floor(2.4e9 * ns / 1e9)).toLocaleString()}</span> oscillations.`,
    `A bullet travels at roughly <span class="hl">760 m/s</span>. In the duration of your offset it would move just <span class="hl-amber">${fmtMeters(seconds * 760)}</span>.`,
    `Earth orbits the Sun at <span class="hl">~30 km/s</span>. In your offset duration, Earth moved <span class="hl-amber">${fmtMeters(seconds * 30000)}</span> along its orbit.`,
    `The Moon recedes from Earth at <span class="hl">38 mm/year</span>. Your offset is so brief the Moon moved <span class="hl-amber">${(38 * seconds / 31557600 * 1e9).toExponential(2)} nm</span> in that time.`,

    // NTP precision comparisons
    `A standard NTP query exchange takes around <span class="hl">10 ms</span>. Your server is roughly <span class="hl-amber">${(10000000 / Math.max(ns, 1)).toFixed(0)}x</span> more precise than the round trip.`,
    `A typical home internet connection has <span class="hl">5-30 ms latency</span> to its NTP source. Your local server delivers time orders of magnitude tighter.`,
    `Public NTP pool servers offer accuracy around <span class="hl">1-50 ms</span>. Yours is better by a factor of <span class="hl-amber">${(1000000 / Math.max(ns / 1000, 1)).toFixed(0)}x</span>.`,
    `Windows' built-in w32time targets <span class="hl">~1 second accuracy</span> by default. Your stratum 1 box is roughly <span class="hl-amber">${(1e9 / Math.max(ns, 1)).toExponential(1)}x</span> tighter.`,
    `Cloudflare's NTP service publishes about <span class="hl">200 microsecond</span> accuracy. Yours beats that by <span class="hl-amber">${(200000 / Math.max(ns / 1000, 1)).toFixed(0)}x</span>.`,
    `Apple's time.apple.com is stratum 2. You're <span class="hl">one level closer to the source</span>, with no upstream server between you and atomic time.`,

    // GPS / orbital
    `Right now <span class="hl">${sats} of ${visible}</span> visible satellites are being used to discipline this clock. Each one is roughly <span class="hl-amber">20,200 km</span> overhead, moving at <span class="hl-amber">14,000 km/h</span>.`,
    `GPS satellite atomic clocks tick about <span class="hl">38 microseconds per day</span> faster than ours due to relativity. Your offset is <span class="hl-amber">${(38000 / Math.max(ns / 1000, 1)).toFixed(0)}x</span> better than that drift.`,
    `Each GPS satellite carries <span class="hl">two rubidium and two cesium atomic clocks</span>, kept in agreement and continuously monitored by ground control.`,
    `GPS satellites orbit Earth twice per sidereal day, at an altitude of <span class="hl">20,200 km</span>. Their signals reach you about <span class="hl-amber">67 ms</span> after transmission.`,
    `The GPS constellation has <span class="hl">31 operational satellites</span> currently. You can typically see 8-12 above the horizon at any time.`,
    `A GPS satellite must broadcast its position to within <span class="hl">a few meters</span> for your fix to work. That requires its onboard clocks to agree to within <span class="hl-amber">~10 ns</span>.`,
    `GPS time started at <span class="hl">midnight UTC, January 6, 1980</span>. It has not had a leap second adjustment since, which is why GPS time is now 18 seconds ahead of UTC.`,
    `Your altitude reading of <span class="hl">${alt != null ? alt.toFixed(0) + ' m' : 'unknown'}</span> comes from triangulating signals that left GPS satellites about <span class="hl-amber">67 milliseconds ago</span>.`,
    `Your position fix is computed from at least <span class="hl">4 satellites</span>: three for X/Y/Z, and one extra to solve for the receiver's clock offset.`,
    `Russia's GLONASS, China's BeiDou, and Europe's Galileo each operate <span class="hl">independent satellite constellations</span> for global positioning. Many modern receivers combine all four.`,
    `A GPS satellite weighs about <span class="hl">2,000 kg</span> and is powered by solar panels generating roughly <span class="hl-amber">1.5 kW</span>.`,
    `The signal you receive from each GPS satellite is below the noise floor, weaker than <span class="hl">-130 dBm</span>. Your receiver de-spreads it using known pseudo-random codes.`,
    `If GPS satellites didn't correct for relativity, position fixes would drift by <span class="hl-amber">~11 km per day</span>.`,
    `GPS satellites broadcast on <span class="hl">L1 (1575.42 MHz)</span> and <span class="hl">L2 (1227.60 MHz)</span>. Civilian receivers primarily use L1 C/A code.`,
    `A single GPS L1 carrier wave cycle takes <span class="hl">0.65 nanoseconds</span>. Your timing precision is in that same neighborhood.`,
    `Sky-view satellites visible to your antenna right now: <span class="hl">${visible}</span>. The maximum theoretically observable from any point on Earth is around <span class="hl-amber">12</span> at once.`,

    // Human comparisons
    `A human eye blinks in about <span class="hl">100 milliseconds</span>. Your last offset is <span class="hl-amber">${(1e8 / Math.max(ns, 1)).toFixed(0)}x</span> faster than a blink.`,
    `A neuron in your brain fires in about <span class="hl">1 millisecond</span>. Your last offset is <span class="hl-amber">${(1e6 / Math.max(ns, 1)).toFixed(0)}x</span> shorter than one neural firing.`,
    `The fastest human reaction time on record is about <span class="hl">100 ms</span>. Your offset is <span class="hl-amber">${(1e8 / Math.max(ns, 1)).toFixed(0)}x</span> tighter than human reflexes.`,
    `Audio engineers can perceive timing differences down to about <span class="hl">10 microseconds</span> between ears. Your offset is <span class="hl-amber">${(10000 / Math.max(ns / 1000, 1)).toFixed(0)}x</span> smaller.`,
    `A high-speed camera shooting at <span class="hl">10,000 frames per second</span> captures one frame every 100 microseconds. Your offset is far below that.`,
    `Movies project at <span class="hl">24 frames per second</span>, so one frame is 41.7 ms. Your offset is <span class="hl-amber">${(41.7e6 / Math.max(ns, 1)).toFixed(0)}x</span> shorter than a single film frame.`,

    // Frequency, drift, skew
    `Your clock's frequency error is <span class="hl">${freq.toFixed(2)} ppm</span>. A typical wristwatch drifts at around <span class="hl-amber">10-50 ppm</span>.`,
    `Your skew of <span class="hl">${skew.toFixed(3)} ppm</span> means your local oscillator can drift by less than <span class="hl-amber">${(skew * 86.4).toFixed(0)} microseconds per day</span> if left undisciplined.`,
    `The Pi 4's crystal oscillator runs at <span class="hl">19.2 MHz</span>. Without GPS discipline, it would drift by roughly <span class="hl-amber">${Math.abs(freq * 86.4).toFixed(0)} ms per day</span>.`,
    `A quartz crystal in a typical computer drifts by <span class="hl">several seconds per day</span>. Yours is corrected continuously, kept to nanoseconds.`,
    `Temperature changes of <span class="hl">1°C</span> shift a typical quartz oscillator's frequency by about <span class="hl-amber">0.04 ppm</span>. Your CPU at ${temp || '37°C'} is being compensated in real time.`,
    `Your residual frequency tells chrony how much its current correction is missing the mark. Values near zero mean the loop has fully converged.`,
    `Allan deviation measures how a clock's frequency varies over different averaging intervals. A typical TCXO sits around <span class="hl">10^-7</span>; your GPS-disciplined clock reaches <span class="hl-amber">10^-9</span> or better.`,

    // Stratum and NTP fundamentals
    `Stratum <span class="hl">${stratum}</span> means you're directly disciplined by GPS. Most internet NTP clients you'll meet are stratum 3 or worse.`,
    `NTP stratum increases by one for each layer down the hierarchy. Reference clocks (atomic, GPS) are stratum 0. You read from one directly, making you stratum 1.`,
    `The first NTP RFC was published in <span class="hl">1985</span>. The protocol you're running has been refined for over <span class="hl-amber">40 years</span>.`,
    `NTP version 4, the current spec, was standardized in <span class="hl">RFC 5905 (2010)</span>. Chrony implements it with optional NTS extensions for authentication.`,
    `The NTP packet you send is exactly <span class="hl">48 bytes</span> on the wire. The protocol has changed very little since the 1980s.`,
    `Chrony was created by <span class="hl">Richard Curnow</span> in 1997, designed for systems with intermittent network connections, like dial-up. It now powers Stratum 1 servers worldwide.`,
    `Many Linux distributions replaced ntpd with chrony as the default time daemon between <span class="hl">2014 and 2020</span>, citing faster convergence and lower resource use.`,

    // Server activity
    `Since boot, this server has answered <span class="hl">${ntpRx.toLocaleString()}</span> NTP packets. ${uptime}.`,
    `An NTP exchange uses <span class="hl">UDP port 123</span>. The 48-byte packet has been the same shape since 1985.`,
    `Your server is operating <span class="hl">stateless</span>. It doesn't track clients; each query is independent. Yet you still see <span class="hl-amber">${ntpRx.toLocaleString()}</span> requests served.`,

    // PPS and discipline
    `Your PPS pulse has fired <span class="hl">${ppsTotal.toLocaleString()}</span> times since boot. That's one per second from a satellite signal originating in space.`,
    `PPS stands for <span class="hl">Pulse Per Second</span>. A precise voltage rising edge from your GPS module marks the start of each second to within nanoseconds.`,
    `Your PPS signal arrives as a hardware interrupt, bypassing the OS scheduler entirely. That's why GPS-disciplined Linux can hit nanosecond precision.`,
    `Without PPS, GPS time discipline is limited to about <span class="hl">100 ms</span> due to serial data jitter. With PPS, you can reach <span class="hl-amber">tens of nanoseconds</span>.`,

    // Physics / relativity / metrology
    `Cesium-133 atoms vibrate <span class="hl">9,192,631,770 times per second</span>. That's the official definition of one second since 1967.`,
    `The kilogram was redefined in 2019 using Planck's constant. The second, however, has been atomically defined since <span class="hl">1967</span>. Time is the most precisely measured quantity in physics.`,
    `One nanosecond per year is the accuracy of the best lab atomic clocks. Your server is about <span class="hl">${(ns / 1).toFixed(0)}x</span> less accurate, but still excellent for civilian use.`,
    `NIST's <span class="hl">NIST-F2 fountain clock</span> would neither gain nor lose a second in 300 million years. Your clock is about a million times less precise, but a billion times cheaper.`,
    `The Earth's rotation slows by about <span class="hl">1.7 ms per century</span>. Your clock is more stable than the planet itself.`,
    `A second on a GPS satellite is measurably <span class="hl">~38 microseconds longer per day</span> than one on Earth, due to weaker gravity (general relativity).`,
    `Time runs slower in stronger gravity. The clock at your feet ticks <span class="hl">10^-16</span> slower than one near your head, a difference measurable with optical lattice clocks.`,
    `The world's most precise clock, an optical lattice strontium clock at JILA, has uncertainty around <span class="hl">10^-18</span>, equivalent to losing one second in the age of the universe.`,
    `Coordinated Universal Time (UTC) is maintained by averaging <span class="hl">~400 atomic clocks</span> at <span class="hl-amber">~80 labs</span> worldwide. The result is published monthly by the BIPM in Paris.`,
    `International Atomic Time (TAI) doesn't include leap seconds. UTC stays close to solar time by inserting them periodically. <span class="hl">UTC = TAI - 37 seconds</span> right now.`,
    `Stock exchanges require time accuracy of <span class="hl">100 microseconds</span> for trade timestamps. You're <span class="hl-amber">${(100000 / Math.max(ns / 1000, 1)).toFixed(0)}x</span> better than that requirement.`,
    `MiFID II regulations require financial firms to timestamp orders to <span class="hl">100 microseconds</span> traceable to UTC. Your stratum 1 box would satisfy that requirement out of the box.`,
    `Power grids synchronize their AC waveforms to <span class="hl">±10 microseconds</span> across continents. Time disciplines power as much as it disciplines computers.`,
    `Cellular base stations require time sync to <span class="hl">a few microseconds</span> for handoffs to work. Your server is precise enough to run a small cell network.`,

    // Leap seconds and edge cases
    `GPS time is currently <span class="hl">18 seconds ahead of UTC</span> due to leap seconds. Your chrony daemon handles that conversion automatically.`,
    `A leap second has not been added since <span class="hl">December 31, 2016</span>. In 2022, the world's metrologists voted to abolish leap seconds by 2035.`,
    `The clock has experienced <span class="hl">27 leap seconds</span> since UTC was synchronized to atomic time in 1972. All but one were positive (extra seconds added).`,
    `Negative leap seconds, where the clock skips a second, have never been needed but remain part of the spec. Earth's rotation has slightly sped up in recent years.`,

    // Performance / math
    `A modern CPU executes about <span class="hl">3 billion instructions per second</span>. In your current offset of <span class="hl-amber">${offsetStr}</span>, it could run roughly <span class="hl">${Math.max(1, Math.floor(3 * ns)).toLocaleString()}</span> instructions.`,
    `If your offset stays at <span class="hl">${offsetStr}</span>, your clock would gain or lose only <span class="hl-amber">${(ns * 86400 / 1e9).toFixed(6)} seconds per day</span>.`,
    `Root delay of <span class="hl">${rootDelay}</span> is the round-trip latency to your time reference. For GPS-disciplined clocks this should be near zero.`,
    `Your CPU is running at <span class="hl">${temp}</span>. Atomic clocks in GPS satellites are kept at constant temperature in deep space, varying by less than <span class="hl-amber">1°C</span>.`,
    `The Allan deviation of a typical TCXO is <span class="hl">10^-7</span>. Your GPS-disciplined clock achieves better than <span class="hl-amber">10^-9</span> after stabilization.`,

    // Hardware tidbits
    `The Raspberry Pi's broadcom SoC has a hardware <span class="hl">system timer</span> running at 1 MHz, accessible via memory-mapped registers, giving microsecond precision before any GPS help.`,
    `Your GPS module's antenna is sensitive to signals weaker than a millionth of a billionth of a watt. Yet from <span class="hl">20,200 km away</span>, it pulls out timing to nanoseconds.`,
    `A typical GPS receiver chipset draws under <span class="hl">100 mW</span>. The satellites broadcasting to it draw <span class="hl-amber">15,000x</span> more power.`,
    `The PPS signal often arrives via a single GPIO pin. From a hardware perspective, a Stratum 1 time server can be built around <span class="hl">three wires and a $30 module</span>.`,

    // History
    `Before atomic clocks, the second was defined as <span class="hl">1/86,400 of a mean solar day</span>. We discovered Earth's rotation was too erratic for precision science.`,
    `John Harrison built the first marine chronometer in 1761 to solve the longitude problem. His H4 lost only <span class="hl">5 seconds in 81 days</span> at sea, a feat unrivaled for a century.`,
    `The transatlantic telegraph cable of 1858 was the first time signal sent globally. It marked the beginning of synchronized world time.`,
    `Railway time forced the first time zones in the 1840s. Before that, every town kept its own local solar time. Your clock now synchronizes with all of them via a single second standard.`,
    `Greenwich Mean Time (GMT) was adopted as the world reference in <span class="hl">1884</span> at the International Meridian Conference. UTC replaced it in 1972 as the atomic-based successor.`,

    // Bandwidth and packet trivia
    `NTP traffic is among the lightest on the internet, around <span class="hl">90 bytes per packet</span> including UDP/IP headers. Your server handles thousands of these for negligible bandwidth.`,
    `An NTP packet's timestamp field uses <span class="hl">64 bits</span>: 32 for seconds since 1900, 32 for fractional seconds (~233 picosecond resolution).`,
    `NTP's timestamp will roll over in <span class="hl">2036</span>. NTPv4's 128-bit Era field already solves this, but most implementations still rely on the 32-bit format.`,
    `Public NTP server "ntp.org" handles <span class="hl">over a billion packets per day</span> globally. Your private server avoids that load entirely.`,
  ];

  return facts[Math.floor(Math.random() * facts.length)];
}

// ============================================================================
// History charts: /api/history returns averages over equal time buckets; the
// charts are drawn as plain SVG so the page needs no chart library.
// ============================================================================
const SVG_NS = 'http://www.w3.org/2000/svg';
const HISTORY_REFRESH_MS = 5 * 60 * 1000;
const HEALTH_COLORS = ['#00ff88', '#ffd700', '#ff3355'];   // OK, DEGRADED, FAULT
let historyRange = '24h';

function svgEl(name, attrs) {
  const el = document.createElementNS(SVG_NS, name);
  for (const [k, v] of Object.entries(attrs)) el.setAttribute(k, v);
  return el;
}

// The lowest and highest non-null value across several series, or null.
// With `robust`, the 2nd and 98th percentiles instead, so one spike (a
// restart, a moment off GPS) does not flatten everything else into a line;
// values beyond them are drawn at the edge.
function seriesRange(lists, robust) {
  const all = [];
  for (const values of lists) for (const v of values) if (v != null) all.push(v);
  if (all.length === 0) return null;
  all.sort((a, b) => a - b);
  const pick = q => all[Math.min(all.length - 1, Math.floor(q * all.length))];
  let lo = robust && all.length >= 20 ? pick(0.02) : all[0];
  let hi = robust && all.length >= 20 ? pick(0.98) : all[all.length - 1];
  if (lo === hi) { lo -= 1e-9 + Math.abs(lo) * 0.05; hi += 1e-9 + Math.abs(hi) * 0.05; }
  const margin = (hi - lo) * 0.08;
  return [lo - margin, hi + margin];
}

// SVG path for one series, broken wherever a bucket has no data.
function linePath(values, w, h, lo, hi, pad) {
  const stepX = values.length > 1 ? w / (values.length - 1) : w;
  let d = '';
  let pen = false;
  values.forEach((v, i) => {
    if (v == null) { pen = false; return; }
    const x = (i * stepX).toFixed(1);
    const clipped = Math.min(hi, Math.max(lo, v));
    const y = (pad + (h - 2 * pad) * (1 - (clipped - lo) / (hi - lo))).toFixed(1);
    d += (pen ? 'L' : 'M') + x + ',' + y + ' ';
    pen = true;
  });
  return d;
}

// The plain lowest and highest values of one series, for its legend.
function minMax(values) {
  const known = values.filter(v => v != null);
  return known.length ? [Math.min(...known), Math.max(...known)] : null;
}

function lastValue(values) {
  for (let i = values.length - 1; i >= 0; i--) if (values[i] != null) return values[i];
  return null;
}

// Draw a chart of up to a few series. Each series: {values, color, label,
// format, axis, robust}. Series on the same axis ('left' or 'right') share a
// scale; `robust` scales that axis to the 2nd-98th percentile.
function drawHistoryChart(id, series) {
  const svg = document.getElementById(id);
  while (svg.firstChild) svg.removeChild(svg.firstChild);
  const rect = svg.getBoundingClientRect();
  const w = rect.width || 600;
  const h = rect.height || 120;
  const pad = 6;
  svg.setAttribute('viewBox', `0 0 ${w} ${h}`);
  for (let i = 1; i < 4; i++) {
    const y = pad + (h - 2 * pad) * i / 4;
    svg.appendChild(svgEl('line', { x1: 0, x2: w, y1: y, y2: y,
      stroke: 'rgba(45,74,120,0.3)', 'stroke-dasharray': '2,3' }));
  }
  const legend = document.getElementById(id + '-legend');
  legend.textContent = '';
  let drew = false;
  for (const axis of ['left', 'right']) {
    const group = series.filter(s => (s.axis || 'left') === axis);
    const range = seriesRange(group.map(s => s.values), group.some(s => s.robust));
    if (!range) continue;
    for (const s of group) {
      svg.appendChild(svgEl('path', { d: linePath(s.values, w, h, range[0], range[1], pad),
        fill: 'none', stroke: s.color, 'stroke-width': 1.4, 'vector-effect': 'non-scaling-stroke' }));
      drew = true;
    }
  }
  for (const s of series) {
    const item = document.createElement('span');
    const swatch = document.createElement('span');
    swatch.className = 'swatch';
    swatch.style.background = s.color;
    item.appendChild(swatch);
    const now = lastValue(s.values);
    const range = minMax(s.values);
    item.appendChild(document.createTextNode(
      `${s.label} ${now == null ? '—' : s.format(now)}` +
      (range ? ` (${s.format(range[0])} – ${s.format(range[1])})` : '')));
    legend.appendChild(item);
  }
  if (!drew) {
    svg.appendChild(svgEl('text', { x: '50%', y: '50%', 'text-anchor': 'middle',
      fill: '#4a6080', 'font-size': 10, 'font-family': 'Space Mono' }))
      .textContent = 'NO DATA YET - one sample is recorded each minute';
  }
}

// Health as a coloured band: one block per bucket, empty where nothing was recorded.
function drawHealthBand(id, values) {
  const svg = document.getElementById(id);
  while (svg.firstChild) svg.removeChild(svg.firstChild);
  const rect = svg.getBoundingClientRect();
  const w = rect.width || 600;
  const h = rect.height || 22;
  svg.setAttribute('viewBox', `0 0 ${w} ${h}`);
  const bw = w / values.length;
  const time = [0, 0, 0];
  values.forEach((v, i) => {
    if (v == null) return;
    time[v] += 1;
    svg.appendChild(svgEl('rect', { x: (i * bw).toFixed(2), y: 0,
      width: (bw + 0.6).toFixed(2), height: h, fill: HEALTH_COLORS[v], opacity: 0.75 }));
  });
  const recorded = time[0] + time[1] + time[2];
  const legend = document.getElementById(id + '-legend');
  legend.textContent = recorded === 0 ? 'no data yet' : ['OK', 'DEGRADED', 'FAULT']
    .map((name, i) => `${name} ${Math.round(100 * time[i] / recorded)}%`).join('  ◆  ');
}

function rangeLabel(seconds) {
  return seconds >= 86400 ? `${Math.round(seconds / 86400)} d ago` : `${Math.round(seconds / 3600)} h ago`;
}

function renderHistory(h) {
  const f = h.fields;
  const ns = v => formatSecondsAsHuman(v);
  drawHistoryChart('hist-offset', [
    { values: f.pps_offset_s, color: '#00d4ff', label: 'PPS', format: ns, robust: true },
    { values: f.last_offset_s, color: '#ffaa00', label: 'clock correction', format: ns,
      axis: 'right', robust: true },
  ]);
  drawHistoryChart('hist-freq', [
    { values: f.frequency_ppm, color: '#00ff88', label: 'frequency', format: v => v.toFixed(3) + ' ppm' },
    { values: f.temperature_c, color: '#ffaa00', label: 'CPU', format: v => v.toFixed(1) + ' °C', axis: 'right' },
  ]);
  drawHistoryChart('hist-gps', [
    { values: f.sats_used, color: '#00ff88', label: 'satellites used', format: v => v.toFixed(1) },
    { values: f.best_snr, color: '#00d4ff', label: 'best signal', format: v => v.toFixed(0) + ' dB-Hz', axis: 'right' },
  ]);
  drawHealthBand('hist-health', f.health);
  setText('hist-start', rangeLabel(h.step * f.health.length));
  setText('hist-note', `${h.range} ◆ averages over ${formatDuration(h.step)} each`);
}

async function fetchHistory() {
  try {
    const res = await fetch('/api/history?range=' + encodeURIComponent(historyRange),
                            { cache: 'no-store' });
    if (!res.ok) throw new Error('HTTP ' + res.status);
    renderHistory(await res.json());
  } catch (e) {
    setText('hist-note', 'history unavailable');
    console.error(e);
  }
}

document.querySelectorAll('#history-range button').forEach(button => {
  button.addEventListener('click', () => {
    historyRange = button.dataset.range;
    document.querySelectorAll('#history-range button')
      .forEach(b => b.classList.toggle('active', b === button));
    fetchHistory();
  });
});

let prog = document.getElementById('refresh-progress');
function startProgress() {
  prog.style.transition = 'none';
  prog.style.width = '0%';
  requestAnimationFrame(() => {
    prog.style.transition = `width ${REFRESH_S}s linear`;
    prog.style.width = '100%';
  });
}

// ============================================================================
// Tooltip system: definitions for every labeled metric on the dashboard.
// Add or edit entries here, then attachTooltips() picks them up automatically.
// ============================================================================
const GLOSSARY = {
  // Header meta
  'Node': 'The IP address this server is reachable at on the local network. Clients use this address to query for time.',
  'Uptime': 'How long the chrony daemon has been continuously running since the last boot or restart.',
  'Core Temp': 'Temperature of the CPU. Affects the local oscillator frequency. Stable temperatures yield more stable clocks.',
  'Mission Time': 'The current system time as reported by the server, in UTC. Refreshed each time telemetry is fetched.',
  'Status': 'TRANSMITTING means chrony has a synchronised source and is serving time. NOT SYNCED means it has none. NO LINK means this page cannot reach the dashboard server, so everything shown is old.',

  // GPS Subsystem
  'Fix Mode': '3D means latitude, longitude, and altitude are all being computed. 2D means altitude is unavailable. No Fix means the receiver cannot solve a position.',
  'Satellites Locked / Visible': 'Locked: satellites actively being used in the time/position solution. Visible: total satellites the receiver can see in the sky.',
  'Best SNR (dB-Hz)': 'Signal-to-noise ratio of the strongest satellite signal. Higher is better. Values above 40 dB-Hz indicate excellent reception.',
  'HDOP': 'Horizontal Dilution of Precision. A geometric quality factor for the horizontal position fix. Lower is better: <1 is ideal, 1-2 is excellent, 2-5 is good, >5 is poor.',
  'PDOP': 'Position Dilution of Precision. Combines horizontal and vertical geometry. Lower means the satellites are well-distributed for an accurate 3D fix.',
  'Altitude': 'Height above the WGS84 ellipsoid (approximately mean sea level). Typically less accurate than horizontal position due to satellite geometry.',
  'Time Since Lock': 'How long the receiver has held a fix without a break, as tracked by this dashboard (checked every 10 seconds). A leading ">" means the fix was already there when the dashboard started, so the true figure is longer.',

  // Offset / Stratum / Frequency
  'System Offset': 'The estimated difference between your system clock and the true reference time. Smaller is better; near-zero values indicate excellent discipline.',
  'Last Offset': 'The most recent measured offset between your clock and the GPS reference at the last update.',
  'Stratum Level': 'Your position in the NTP hierarchy. Stratum 0 is the reference clock itself (GPS, atomic). Stratum 1 reads directly from it. Each downstream layer adds 1.',
  'NTP Stratum': 'Stratum number assigned to packets your server sends out. Lower means closer to the time source. 1 is the lowest a server can advertise.',
  'Frequency Drift': 'How much the local oscillator drifts per second relative to true time, expressed in parts per million (ppm). Chrony compensates for this in real time.',
  'Clock Offset': 'The current frequency correction chrony is applying to keep your clock locked. Reflects how far off the raw oscillator runs.',

  // PPS card
  'PPS Discipline (recent samples)': 'A sparkline of recent Pulse-Per-Second samples from the GPS receiver. Tighter clustering means more stable hardware timing.',
  'Min': 'Smallest (most negative or zero) sample value in the recent PPS history.',
  'Max': 'Largest sample value in the recent PPS history.',
  'Range': 'Difference between Min and Max. A small range indicates a stable PPS source.',
  'Sample count': 'Number of recent PPS samples included in this view.',
  'PPS IRQ Total': 'Total count of PPS hardware interrupts received since boot. One pulse per second when GPS is locked.',

  // History
  'History': 'One sample is recorded every minute and kept for 30 days. Each point on a chart is the average over a slice of the chosen range; gaps mean nothing was recorded (the dashboard or the Pi was down).',
  'Clock offset': 'PPS: how far each GPS pulse was from the system clock. Clock correction: the adjustment chrony made at each update. Both should hover around zero.',
  'Frequency and temperature': 'How fast the uncorrected crystal runs (chrony cancels this) next to the CPU temperature. The frequency follows temperature; this is what temperature compensation removes.',
  'GPS reception': 'Satellites in the fix and the strongest signal among them. A falling signal warns of a lost fix before it happens.',
  'Health': 'The dashboard status over time: green OK, yellow DEGRADED, red FAULT.',

  // Tracking detail
  'Tracking Detail': 'Detailed metrics from "chronyc tracking" describing your synchronization state.',
  'Reference ID': 'Identifier of the time source chrony is currently using. For GPS this is typically a refclock name like "GPS" or "PPS".',
  'Ref Time (UTC)': 'The UTC timestamp of the most recent measurement from the reference clock.',
  'Update Interval': 'How often chrony updates its estimate of the system clock, in seconds.',
  'Root Delay': 'Total round-trip network delay to the stratum 0 reference. For GPS-disciplined clocks this is effectively zero.',
  'Root Dispersion': 'Accumulated maximum error estimate up the stratum chain. Bounds how wrong your time could plausibly be.',
  'Residual Freq': 'The portion of frequency error chrony has not yet corrected. Approaches zero as the loop converges.',
  'Activity': 'Counts of NTP sources by state: online, offline, burst-in-progress, etc.',

  // Hardware vitals
  'Hardware Vitals': 'System-level metrics about the machine running chrony.',
  'Hostname': 'The machine name as reported by the kernel.',
  'IP Address': 'Primary network interface IP address.',
  'Core Temperature': 'Current CPU package temperature. Higher temperatures can affect oscillator stability.',
  'Memory': 'RAM usage on the server.',
  'Load Average': 'System load over 1, 5, and 15 minute windows. Values below your core count are healthy.',
  'System Uptime': 'How long the OS has been running since the last boot.',
  'Isolated Cores': 'CPU cores reserved via isolcpus for time-critical tasks, kept off the general scheduler.',
  'Leap Data Expiry': 'Date by which the current leap-second table will need updating. Stale leap data can introduce errors.',

  // CPU
  'CPU Core Utilization': 'Per-core CPU usage. Spikes on isolated cores can affect timestamping precision.',

  // NTP service
  'NTP Service Statistics': 'Aggregate counts of NTP and command packets handled by chrony since startup.',
  'NTP Packets Received': 'Total NTP queries answered since chrony started.',
  'NTP Packets Dropped': 'NTP queries refused due to rate limiting, access control, or malformed packets.',
  'Command Packets Received': 'chronyc administrative commands received over the local socket.',
  'Command Packets Dropped': 'Administrative commands rejected, usually due to permission rules.',
  'Client Log Records Dropped': 'Per-client tracking entries that overflowed the log buffer. High values may indicate many distinct clients.',

  // Cards / section titles
  'GPS Subsystem': 'Live status of the GPS receiver and the satellites it can see.',
  'Authoritative Time (Disseminated From This Node)': 'The current time this server is broadcasting to NTP clients. This is what your network sees as "the truth".',
  'Time Sources': 'List of upstream time references chrony is comparing against. For a stratum 1 server, refclocks (GPS, PPS) appear here.',
  'Source Statistics': 'Long-term statistics for each source: sample counts, frequency, offset, and standard deviation.',
  'Connected Clients': 'Devices currently being served NTP time by this node.',

  // Time Sources table headers
  'State': 'Source state symbol: * (synced), + (combined), - (not combined), ? (unreachable, or unusable if it still answers), x (false ticker), ~ (too variable). Sources configured with noselect show as noselect.',
  'Source': 'Hostname, IP, or refclock identifier of the time source.',
  'Stratum': 'Stratum level reported by this source.',
  'Poll': 'log2 of the polling interval in seconds. A poll of 6 means querying every 64 seconds.',
  'Reach': 'Octal value tracking the last 8 polls. 377 (octal) means all 8 recent polls succeeded.',
  'Last Rx': 'Time since the last packet was received from this source, in seconds.',
  'Offset': 'Measured time difference between your clock and this source.',
  'Margin': 'Estimated error margin on the last measurement.',

  // Source stats table headers
  'Samples (NP/NR)': 'Number of points used (NP) and runs (NR) in the regression analysis for this source.',
  'Span': 'Time span covered by the samples used in the regression, in seconds.',
  'Freq Skew': 'Estimated uncertainty in the frequency estimate, in ppm.',
  'Std Dev': 'Standard deviation of the measurements, indicating consistency.',

  // Clients table headers
  'Client': 'Hostname or IP of the connecting NTP client.',
  'NTP Requests': 'Total queries received from this client.',
  'Drops': 'Queries from this client that were dropped (e.g., rate-limited).',
  'Last Query': 'Time since the last query from this client.',
};

let _tipPop = null;
function _ensureTipPop() {
  if (!_tipPop) {
    _tipPop = document.createElement('div');
    _tipPop.className = 'tip-pop';
    document.body.appendChild(_tipPop);
  }
  return _tipPop;
}

function _showTip(target, label, text) {
  const pop = _ensureTipPop();
  pop.innerHTML = `<span class="tip-title">${label}</span>${text}`;
  pop.classList.add('visible');
  // Position after render so we know its size
  requestAnimationFrame(() => {
    const r = target.getBoundingClientRect();
    const popR = pop.getBoundingClientRect();
    const margin = 8;
    let left = r.left;
    let top = r.bottom + margin;
    // Flip below->above if it would overflow the viewport bottom
    if (top + popR.height > window.innerHeight - 8) {
      top = r.top - popR.height - margin;
    }
    // Clamp horizontally
    if (left + popR.width > window.innerWidth - 8) {
      left = window.innerWidth - popR.width - 8;
    }
    if (left < 8) left = 8;
    pop.style.left = left + 'px';
    pop.style.top = top + 'px';
  });
}

function _hideTip() {
  if (_tipPop) _tipPop.classList.remove('visible');
}

function attachTooltips(root) {
  root = root || document;
  // Selectors covering every kind of label that should be hoverable
  const selectors = ['.stat-key', '.card-title', '.header-meta dt', 'th', '.hero-stat .lbl', '.history-title'];
  const nodes = root.querySelectorAll(selectors.join(','));
  nodes.forEach(el => {
    if (el.dataset.tipBound) return;
    const text = (el.textContent || '').trim();
    if (!text) return;
    const def = GLOSSARY[text];
    if (!def) return;
    el.setAttribute('data-tip', '1');
    el.dataset.tipBound = '1';
    el.addEventListener('mouseenter', () => _showTip(el, text, def));
    el.addEventListener('mouseleave', _hideTip);
    el.addEventListener('focus', () => _showTip(el, text, def));
    el.addEventListener('blur', _hideTip);
    // Tap support for touch devices: tap the label to toggle the tooltip
    el.addEventListener('click', (ev) => {
      ev.stopPropagation();
      const pop = _ensureTipPop();
      const isVisibleForThis = pop.classList.contains('visible') && pop.dataset.owner === el.dataset.tipBound + el.textContent;
      if (isVisibleForThis) {
        _hideTip();
      } else {
        pop.dataset.owner = el.dataset.tipBound + el.textContent;
        _showTip(el, text, def);
      }
    });
    el.tabIndex = 0;
  });
}

// Dismiss any visible tooltip when tapping/clicking elsewhere
document.addEventListener('click', _hideTip);

async function fetchData() {
  if (fetching) return;  // the previous poll has not finished; do not pile up
  fetching = true;
  const abort = new AbortController();
  const timer = setTimeout(() => abort.abort(), 10000);
  try {
    const res = await fetch('/api/status', { signal: abort.signal, cache: 'no-store' });
    if (!res.ok) throw new Error('HTTP ' + res.status);
    const data = await res.json();
    const wasHidden = document.getElementById('dashboard').style.display !== 'block';
    document.getElementById('loading').style.display = 'none';
    document.getElementById('dashboard').style.display = 'block';
    if (wasHidden) { requestAnimationFrame(resizeCanvas); }
    linkLost = false;
    render(data);
    lastGoodAt = performance.now();
    attachTooltips();
  } catch (e) {
    document.getElementById('loading').textContent = '\u2715 TELEMETRY LINK FAILED';
    showLinkLost();
    console.error(e);
  } finally {
    clearTimeout(timer);
    fetching = false;
  }
  startProgress();
}

updateWorldClock();
attachTooltips();
fetchData();
setInterval(fetchData, REFRESH_S * 1000);
fetchHistory();
setInterval(fetchHistory, HISTORY_REFRESH_MS);
