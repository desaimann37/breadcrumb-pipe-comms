/* BREADCRUMB dashboard
 * Draws three things that together explain the architecture:
 *   1. the pipe run, the relay chain, and packets physically hopping along it
 *   2. the operator's video, degraded by exactly the link the model computed
 *   3. the link budget per hop, so the picture is never just a cartoon
 */
'use strict';

const S = {
  state: null,
  pipe: null,
  scenarios: [],
  packets: [],
  chart: [],
  posHistory: [],
  seenEvents: new Set(),
  lastFrameAt: 0,
  camSeed: Math.random() * 1000,
  connected: false,
};

const $ = (id) => document.getElementById(id);
const clamp = (v, a, b) => Math.max(a, Math.min(b, v));
const lerp = (a, b, t) => a + (b - a) * t;
const M_TO_FT = 3.28084;

/* ------------------------------------------------------------------ net */

function post(cmd, payload = {}) {
  return fetch('/api/cmd', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(Object.assign({ cmd }, payload)),
  }).then((r) => r.json());
}

function connect() {
  const es = new EventSource('/api/stream');
  es.onmessage = (e) => {
    S.state = JSON.parse(e.data);
    S.connected = true;
    onTick();
  };
  es.onerror = () => {
    S.connected = false;
    $('conn').textContent = 'reconnecting';
    $('conn').className = 'conn dead';
  };
  es.onopen = () => {
    $('conn').textContent = 'live telemetry';
    $('conn').className = 'conn live';
  };
}

function loadPipe() {
  return fetch('/api/pipe').then((r) => r.json()).then((p) => {
    S.pipe = p;
    S.chart = [];
    S.packets = [];
    S.posHistory = [];
  });
}

function loadScenarios() {
  return fetch('/api/scenarios').then((r) => r.json()).then((d) => {
    S.scenarios = d.scenarios;
    const sel = $('scenario');
    sel.innerHTML = '';
    d.scenarios.forEach((s) => {
      const o = document.createElement('option');
      o.value = s.key;
      o.textContent = `${s.title}`;
      sel.appendChild(o);
    });
  });
}

/* --------------------------------------------------------------- ticking */

function onTick() {
  const st = S.state;
  if (!st) return;

  $('clock').textContent = hms(st.t_s);
  $('timescale-label').textContent = `${st.time_scale}× sim speed`;
  const chip = $('phase-chip');
  chip.textContent = st.phase;
  chip.className = 'status ' + st.phase;

  if ($('scenario').value !== st.scenario.key) $('scenario').value = st.scenario.key;

  if (st.live) {
    const age = st.live.telemetry_age_ms;
    const stale = age === null || age > 600;
    $('conn').className = 'conn livehw';
    $('conn').textContent = stale
      ? `LIVE · NO TELEMETRY${age !== null ? ` (${(age / 1000).toFixed(1)} s)` : ''}`
      : `LIVE · ${st.live.transport} · ${age} ms`;
  }

  // Record a trace point every few metres for the mission chart.
  const pos = st.crawler.position_m;
  const last = S.chart[S.chart.length - 1];
  if (!last || Math.abs(pos - last.x) >= 3) {
    S.chart.push({
      x: pos,
      margin: st.chain.min_margin_db,
      mbps: st.chain.video_goodput_mbps,
      ctrl: st.chain.control_ok,
    });
    if (S.chart.length > 4000) S.chart.shift();
  }

  // Position history powers the video latency: the camera shows where the
  // crawler *was* when the photons left it.
  S.posHistory.push({ t: performance.now(), pos });
  if (S.posHistory.length > 1200) S.posHistory.shift();

  renderTiles();
  renderHops();
  renderLog();
  renderWhy();
  syncControls();
}

function hms(sec) {
  const s = Math.floor(sec % 60), m = Math.floor((sec / 60) % 60), h = Math.floor(sec / 3600);
  return [h, m, s].map((v) => String(v).padStart(2, '0')).join(':');
}

/* ----------------------------------------------------------------- tiles */

function tile(k, v, u, cls) {
  return `<div class="tile ${cls || ''}"><div class="k">${k}</div>` +
         `<div class="v">${v}<span class="u">${u || ''}</span></div></div>`;
}

function renderTiles() {
  const st = S.state, c = st.crawler, ch = st.chain, v = st.video, cx = st.context;
  const marginCls = ch.min_margin_db > 10 ? 'good' : ch.min_margin_db > 4 ? 'warn' : 'bad';
  const vidCls = v.name === 'no video' ? 'bad' : (v.height >= 720 ? 'good' : 'warn');
  const battCls = c.battery_pct > 45 ? 'good' : c.battery_pct > 20 ? 'warn' : 'bad';
  const ctrlCls = ch.control_ok ? 'good' : 'bad';

  $('tiles').innerHTML = [
    tile('Distance in', Math.round(c.position_m), `m / ${Math.round(c.position_m * M_TO_FT)} ft`),
    tile('Of target', (c.position_m / 3218.7 * 100).toFixed(0), '% of 2 mi'),
    tile('Relay hops', ch.hops.length, ''),
    tile('Relays left', c.nodes_remaining, `/ ${st.policy.magazine}`),
    tile('Weakest margin', ch.min_margin_db.toFixed(1), 'dB', marginCls),
    tile('Video goodput', ch.video_goodput_mbps.toFixed(2), 'Mbps', vidCls),
    tile('Video quality', v.name, '', vidCls),
    tile('Glass-to-glass', isFinite(v.glass_to_glass_ms) ? Math.round(v.glass_to_glass_ms) : '—', 'ms'),
    tile('Control RTT', Math.round(ch.control_rtt_ms), 'ms', ctrlCls),
    tile('Control link', ch.control_ok ? 'UP' : 'DOWN', '', ctrlCls),
    tile('Attenuation here', cx.db_per_km.toFixed(1), 'dB/km'),
    tile('Battery', c.battery_pct.toFixed(0), '%', battCls),
  ].join('');
}

/* ------------------------------------------------------------------ hops */

function marginColor(db) {
  if (db > 12) return '#3ddc97';
  if (db > 6) return '#9fd84a';
  if (db > 2) return '#ffc857';
  return '#ff5f70';
}

function renderHops() {
  const ch = S.state.chain;
  if (!ch.hops.length) { $('hops').innerHTML = '<div class="hop">no chain</div>'; return; }
  $('hop-summary').textContent =
    `${ch.hops.length} hops · ${Math.round(ch.total_path_loss_db)} dB total · ` +
    `bottleneck hop ${ch.weakest_hop + 1}`;
  $('hops').innerHTML = ch.hops.map((h, i) => {
    const frac = clamp((h.margin_db + 4) / 34, 0.02, 1);
    const col = marginColor(h.margin_db);
    const weak = i === ch.weakest_hop ? ' weak' : '';
    const label = i === ch.hops.length - 1 ? 'crawler' : `relay ${i + 1}`;
    return `<div class="hop${weak}">
      <span class="idx">${i + 1}</span>
      <span>${Math.round(h.distance_m)} m</span>
      <span class="bar"><i style="width:${(frac * 100).toFixed(0)}%;background:${col}"></i></span>
      <span class="mcs">${h.mcs}</span>
      <span style="color:${col}">${h.margin_db.toFixed(1)} dB</span>
      <span class="${h.control_ok ? 'ctrl-ok' : 'ctrl-bad'}">${
        h.measured && h.model_error_db !== undefined
          ? `<span class="err">${h.model_error_db > 0 ? '+' : ''}${h.model_error_db} dB</span>`
          : label}</span>
    </div>`;
  }).join('');
}

/* ------------------------------------------------------------------- log */

function renderLog() {
  const ul = $('log');
  S.state.events.forEach((e) => {
    if (S.seenEvents.has(e.id)) return;
    S.seenEvents.add(e.id);
    const li = document.createElement('li');
    li.className = e.level;
    li.innerHTML = `<span class="t">${hms(e.t_s)}</span><span>${e.text}</span>`;
    ul.insertBefore(li, ul.firstChild);
    while (ul.children.length > 160) ul.removeChild(ul.lastChild);
  });
}

/* ------------------------------------------------------------------- why */

function renderWhy() {
  const st = S.state, cx = st.context, ch = st.chain, r = st.radio;
  const ratio = (r.frequency_mhz / cx.cutoff_mhz);
  const ds = ch.hops.length ? ch.hops[ch.hops.length - 1].delay_spread_ns : 0;
  const cards = [
    ['Waveguide cutoff',
     cx.below_cutoff
       ? `<b>${r.frequency_mhz} MHz</b> is <b>below</b> the <b>${cx.cutoff_mhz} MHz</b> cutoff of this
          ${cx.diameter_in}&quot; bore. The field is evanescent: nothing propagates, at any power.`
       : `${cx.diameter_in}&quot; bore cuts off at <b>${cx.cutoff_mhz} MHz</b>. Running at
          <b>${r.frequency_mhz} MHz</b> = <b>${ratio.toFixed(1)}×</b> cutoff, so the ray grazes the
          wall at <b>${cx.ray_angle_deg}&deg;</b> and bounces only <b>${cx.bounces_per_m}</b> times a metre.`],
    ['Where the loss goes',
     `<b>${cx.db_per_km} dB/km</b> distributed in ${cx.wall.toLowerCase()}.
      ${cx.water_bounce_fraction > 0.01
        ? `<b>${(cx.water_bounce_fraction * 100).toFixed(0)}%</b> of bounces land on the water surface,
           which is the dominant loss term here.`
        : 'Dry bore, so every bounce is off the pipe wall.'}
      Section: ${cx.section_label}.`],
    ['Dispersion',
     `Modal delay spread on the last hop is <b>${ds.toFixed(0)} ns</b>.
      An 802.11 OFDM guard interval is 800 ns, so the waveform rides through it —
      this is why a stock Wi-Fi PHY works inside a pipe at all.`],
    ['Airtime',
     `${r.radios_per_node === 1
        ? `Single-radio relays on one channel: every hop shares the air, so goodput is divided by
           <b>${r.airtime_divisor}</b>.`
        : `Dual radios on <b>${r.channel_count}</b> alternating channels: hops run in parallel,
           divisor down to <b>${r.airtime_divisor}</b>.`}`],
    ['Next obstacle',
     cx.next_feature
       ? `<b>${cx.next_feature.label}</b> in <b>${cx.next_feature_distance_m} m</b>,
          worth <b>${cx.next_feature.loss_db} dB</b>.
          ${cx.next_feature.loss_db >= 5 ? 'A relay is dropped before this one.' : ''}`
       : 'Clear run ahead.'],
    ['Model validity',
     cx.model_valid
       ? `Bore is <b>${cx.d_over_lambda}</b> wavelengths across, comfortably inside the
          grazing-ray model's validated range (D/&lambda; &ge; 3).`
       : `Bore is only <b>${cx.d_over_lambda}</b> wavelengths across. Below D/&lambda; = 3 the pipe is
          barely oversized and these numbers are <b>indicative only</b> &mdash; this case needs a
          bench measurement before anyone designs against it.`],
    ['Safety',
     `Control uses a spread-spectrum waveform that works <b>${(ch.hops.length
        ? (ch.hops[ch.hops.length - 1].control_margin_db - ch.hops[ch.hops.length - 1].margin_db)
        : 0).toFixed(0)} dB</b> below the video link.
      If it does go quiet, the crawler stops itself after
      <b>600 ms</b> without a command frame.`],
  ];
  if (st.live) {
    const c = st.live.calibration;
    const fit = c.measured_db_per_km === null
      ? `Collecting samples (<b>${c.samples}</b> so far; 20 needed before a fit).`
      : `Measured <b>${c.measured_db_per_km} dB/km</b> against a modelled
         <b>${c.modelled_db_per_km} dB/km</b> &mdash; ratio <b>${c.ratio}&times;</b>,
         fit r&sup2; <b>${c.r_squared}</b> over <b>${c.samples}</b> samples.
         ${c.r_squared !== null && c.r_squared < 0.5
            ? 'Poor fit: split the run by section, or check the distance tags.' : ''}`;
    cards.unshift(
      ['Measured vs model',
       `${fit}<br>Noise floor <b>${c.noise_floor_dbm} dBm</b>
        (${c.noise_floor_measured ? 'measured' : 'modelled &mdash; pass --noise-floor once you have measured it'}).`],
      ['Live link health',
       `Telemetry age <b>${st.live.telemetry_age_ms === null ? '--' : st.live.telemetry_age_ms + ' ms'}</b>,
        delivery <b>${st.live.delivery_pct}%</b> over <b>${st.live.frames_rx}</b> frames.
        ${st.live.command_rtt_ms !== null && st.live.command_rtt_ms !== undefined
          ? `Last command confirmed by the crawler in <b>${st.live.command_rtt_ms} ms</b>.`
          : 'No command confirmed yet &mdash; press the stop to measure a real round trip.'}
        ${st.live.awaiting_confirmation ? `<br>Awaiting: <b>${st.live.awaiting_confirmation}</b>` : ''}`]);
  }

  $('why').innerHTML = cards.map(([h, p], i) => {
    const measured = st.live && i < 2 ? ' measured' : '';
    return `<div class="card${measured}"><h3>${h}</h3><p>${p}</p></div>`;
  }).join('');
}

/* ------------------------------------------------------------ pipe canvas */

function fitCanvas(cv, cssHeight) {
  const dpr = window.devicePixelRatio || 1;
  const w = cv.clientWidth || 900;
  if (cv.width !== Math.round(w * dpr) || cv.height !== Math.round(cssHeight * dpr)) {
    cv.width = Math.round(w * dpr);
    cv.height = Math.round(cssHeight * dpr);
  }
  const ctx = cv.getContext('2d');
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  return { ctx, w, h: cssHeight };
}

const FEATURE_COLORS = {
  joint: '#4a5890', bend: '#ff9f43', offset: '#ffc857',
  debris: '#ff5f70', manhole: '#7b6cff', lateral: '#2fb8f2', sag: '#4ad6c4',
};

function drawPipe() {
  const cv = $('pipe');
  const { ctx, w, h } = fitCanvas(cv, 300);
  ctx.clearRect(0, 0, w, h);
  if (!S.pipe || !S.state) return;

  const st = S.state;
  const L = S.pipe.length_m;
  const ml = 58, mr = 22, groundY = 52, axis = 176, halfH = 26;
  const X = (m) => ml + (m / L) * (w - ml - mr);

  // sky and soil
  const sky = ctx.createLinearGradient(0, 0, 0, groundY);
  sky.addColorStop(0, '#0d1838'); sky.addColorStop(1, '#17254d');
  ctx.fillStyle = sky; ctx.fillRect(0, 0, w, groundY);
  const soil = ctx.createLinearGradient(0, groundY, 0, h);
  soil.addColorStop(0, '#1a1526'); soil.addColorStop(1, '#0a0812');
  ctx.fillStyle = soil; ctx.fillRect(0, groundY, w, h - groundY);
  ctx.strokeStyle = '#3a2f49'; ctx.lineWidth = 1.5;
  ctx.beginPath(); ctx.moveTo(0, groundY); ctx.lineTo(w, groundY); ctx.stroke();
  ctx.strokeStyle = 'rgba(120,100,150,.10)';
  for (let x = -h; x < w; x += 16) {
    ctx.beginPath(); ctx.moveTo(x, h); ctx.lineTo(x + (h - groundY), groundY); ctx.stroke();
  }

  // pipe barrel, per section (colour follows water depth)
  S.pipe.sections.forEach((sec) => {
    const x0 = X(sec.start_m), x1 = X(sec.end_m);
    const g = ctx.createLinearGradient(0, axis - halfH, 0, axis + halfH);
    g.addColorStop(0, '#2b3566'); g.addColorStop(0.45, '#161e3f'); g.addColorStop(1, '#0d1430');
    ctx.fillStyle = g;
    ctx.fillRect(x0, axis - halfH, Math.max(x1 - x0, 1), halfH * 2);
    if (sec.fill_fraction > 0.005) {
      const wh = sec.fill_fraction * halfH * 2;
      const wg = ctx.createLinearGradient(0, axis + halfH - wh, 0, axis + halfH);
      wg.addColorStop(0, 'rgba(60,180,220,.55)'); wg.addColorStop(1, 'rgba(20,80,130,.75)');
      ctx.fillStyle = wg;
      ctx.fillRect(x0, axis + halfH - wh, Math.max(x1 - x0, 1), wh);
      ctx.strokeStyle = 'rgba(140,230,255,.5)'; ctx.lineWidth = 1;
      ctx.beginPath();
      ctx.moveTo(x0, axis + halfH - wh); ctx.lineTo(x1, axis + halfH - wh); ctx.stroke();
    }
    ctx.strokeStyle = '#3b4a86'; ctx.lineWidth = 1.5;
    ctx.beginPath();
    ctx.moveTo(x0, axis - halfH); ctx.lineTo(x1, axis - halfH);
    ctx.moveTo(x0, axis + halfH); ctx.lineTo(x1, axis + halfH);
    ctx.stroke();
    if (x1 - x0 > 60) {
      ctx.fillStyle = 'rgba(141,154,206,.55)'; ctx.font = '9px ui-monospace,monospace';
      ctx.fillText(sec.label, x0 + 5, axis + halfH + 13);
    }
  });

  // features
  S.pipe.features.forEach((f) => {
    const x = X(f.position_m), col = FEATURE_COLORS[f.kind] || '#6679b5';
    if (f.kind === 'manhole') {
      ctx.strokeStyle = 'rgba(123,108,255,.5)'; ctx.lineWidth = 6;
      ctx.beginPath(); ctx.moveTo(x, groundY); ctx.lineTo(x, axis - halfH); ctx.stroke();
      ctx.fillStyle = col; ctx.fillRect(x - 6, groundY - 5, 12, 5);
    } else {
      ctx.strokeStyle = col; ctx.lineWidth = Math.min(1 + f.loss_db / 5, 4);
      ctx.beginPath();
      ctx.moveTo(x, axis - halfH - 4); ctx.lineTo(x, axis - halfH - 4 - Math.min(6 + f.loss_db, 22));
      ctx.stroke();
    }
  });

  // base station above the access shaft
  ctx.fillStyle = '#e8edff';
  ctx.fillRect(X(0) - 16, groundY - 22, 32, 16);
  ctx.strokeStyle = '#e8edff'; ctx.lineWidth = 2;
  ctx.beginPath(); ctx.moveTo(X(0), groundY - 22); ctx.lineTo(X(0), groundY - 40); ctx.stroke();
  for (let i = 1; i <= 3; i++) {
    ctx.strokeStyle = `rgba(232,237,255,${0.35 / i})`; ctx.lineWidth = 1.5;
    ctx.beginPath(); ctx.arc(X(0), groundY - 40, i * 7, -Math.PI * 0.85, -Math.PI * 0.15); ctx.stroke();
  }
  ctx.strokeStyle = 'rgba(232,237,255,.45)'; ctx.lineWidth = 4;
  ctx.beginPath(); ctx.moveTo(X(0), groundY); ctx.lineTo(X(0), axis - halfH); ctx.stroke();

  // link arcs and the packets riding them
  const chain = st.chain;
  const positions = [0].concat(st.nodes.map((n) => n.position_m)).concat([st.crawler.position_m]);
  chain.hops.forEach((hop, i) => {
    const x0 = X(positions[i]), x1 = X(positions[i + 1]);
    const lift = axis - halfH - 30 - Math.min(i * 4, 26);
    ctx.strokeStyle = marginColor(hop.margin_db);
    ctx.globalAlpha = hop.rate_mbps > 0 ? 0.85 : 0.3;
    ctx.lineWidth = hop.rate_mbps > 0 ? clamp(1 + hop.rate_mbps / 12, 1, 4) : 1;
    ctx.setLineDash(hop.rate_mbps > 0 ? [] : [3, 4]);
    ctx.beginPath();
    ctx.moveTo(x0, axis - halfH - 6);
    ctx.quadraticCurveTo((x0 + x1) / 2, lift, x1, axis - halfH - 6);
    ctx.stroke();
    ctx.setLineDash([]); ctx.globalAlpha = 1;
  });

  drawPackets(ctx, X, axis, halfH, positions, chain);

  // relay nodes
  st.nodes.forEach((n, i) => {
    const x = X(n.position_m);
    ctx.fillStyle = '#7b6cff';
    ctx.beginPath(); ctx.arc(x, axis, 6, 0, Math.PI * 2); ctx.fill();
    ctx.strokeStyle = 'rgba(123,108,255,.5)'; ctx.lineWidth = 2;
    ctx.beginPath(); ctx.arc(x, axis, 10, -Math.PI / 2, -Math.PI / 2 + Math.PI * 2 * (n.battery_wh / 26)); ctx.stroke();
    ctx.fillStyle = 'rgba(200,195,255,.8)'; ctx.font = '9px ui-monospace,monospace';
    ctx.fillText(`R${n.index}`, x - 7, axis + 22);
  });

  // crawler
  const cx = X(st.crawler.position_m);
  ctx.fillStyle = 'rgba(47,184,242,.18)';
  ctx.beginPath();
  ctx.moveTo(cx, axis); ctx.lineTo(cx + 46, axis - 14); ctx.lineTo(cx + 46, axis + 14);
  ctx.closePath(); ctx.fill();
  ctx.fillStyle = '#2fb8f2';
  ctx.fillRect(cx - 9, axis - 7, 18, 13);
  ctx.fillStyle = '#0a0e20';
  ctx.fillRect(cx + 3, axis - 4, 5, 5);
  ctx.fillStyle = '#9fe6ff';
  ctx.fillRect(cx - 9, axis + 6, 18, 2);

  // scale bar
  ctx.fillStyle = 'rgba(141,154,206,.7)'; ctx.font = '10px ui-monospace,monospace';
  ctx.fillText('0', ml - 4, h - 12);
  for (let mile = 1; mile * 1609.344 <= L; mile++) {
    const x = X(mile * 1609.344);
    ctx.strokeStyle = 'rgba(141,154,206,.35)';
    ctx.beginPath(); ctx.moveTo(x, h - 26); ctx.lineTo(x, h - 18); ctx.stroke();
    ctx.fillText(`${mile} mi`, x - 12, h - 6);
  }
  ctx.fillText(`${Math.round(L)} m`, w - mr - 34, h - 12);
  ctx.fillStyle = 'rgba(141,154,206,.55)';
  ctx.fillText(S.pipe.name, ml, 18);
  ctx.fillText(`buried ${S.pipe.burial_depth_m} m`, ml, 32);
}

/* --------------------------------------------------------------- packets */

function drawPackets(ctx, X, axis, halfH, positions, chain) {
  const now = performance.now();
  const dt = Math.min((now - (S.lastFrameAt || now)) / 1000, 0.1);
  S.lastFrameAt = now;
  const end = positions[positions.length - 1];
  if (end <= 0 || !chain.hops.length) { S.packets = []; return; }

  const videoUp = chain.video_goodput_mbps > 0.01;
  const span = Math.max(end, 1);
  const travel = span / 1.1;   // metres per second of *visual* travel

  // spawn
  S.spawnV = (S.spawnV || 0) + dt;
  if (videoUp && S.spawnV > 0.085) {
    S.spawnV = 0;
    S.packets.push({ x: end, dir: -1, kind: 'video' });
  }
  S.spawnC = (S.spawnC || 0) + dt;
  if (S.spawnC > 0.22) {
    S.spawnC = 0;
    S.packets.push({ x: 0, dir: 1, kind: 'ctrl' });
  }

  const hopOf = (x) => {
    for (let i = 0; i < positions.length - 1; i++) {
      if (x >= positions[i] && x <= positions[i + 1]) return i;
    }
    return positions.length - 2;
  };

  S.packets = S.packets.filter((p) => {
    p.x += p.dir * travel * dt;
    if (p.x < -5 || p.x > end + 5) return false;
    const h = chain.hops[hopOf(p.x)];
    if (h) {
      const per = p.kind === 'video' ? h.per : (h.control_ok ? 0 : 0.9);
      if (per > 0.05 && Math.random() < per * dt * 6) { p.dead = true; p.life = 0.35; }
    }
    if (p.dead) { p.life -= dt; if (p.life <= 0) return false; }
    const x = X(clamp(p.x, 0, end));
    const i = hopOf(p.x);
    const x0 = X(positions[i]), x1 = X(positions[i + 1]);
    const lift = axis - halfH - 30 - Math.min(i * 4, 26);
    const t = (x1 - x0) > 0.5 ? clamp((x - x0) / (x1 - x0), 0, 1) : 0;
    const base = axis - halfH - 6;
    const y = (1 - t) * (1 - t) * base + 2 * (1 - t) * t * lift + t * t * base;
    ctx.globalAlpha = p.dead ? clamp(p.life / 0.35, 0, 1) : 0.95;
    ctx.fillStyle = p.dead ? '#ff5f70' : (p.kind === 'video' ? '#3ddc97' : '#ffc857');
    const s = p.dead ? 4.5 : 3;
    ctx.fillRect(x - s / 2, y - s / 2, s, s);
    ctx.globalAlpha = 1;
    return true;
  });
  if (S.packets.length > 900) S.packets.splice(0, S.packets.length - 900);
}

/* ---------------------------------------------------------- zoomed view */

function drawZoom() {
  const cv = $('zoom');
  const { ctx, w, h } = fitCanvas(cv, 120);
  ctx.clearRect(0, 0, w, h);
  if (!S.pipe || !S.state) return;
  const st = S.state;
  const span = 90;
  const c = st.crawler.position_m;
  const lo = c - span * 0.65, hi = c + span * 0.35;
  const X = (m) => ((m - lo) / (hi - lo)) * w;
  const axis = 62, halfH = 30;

  const sec = S.pipe.sections.find((s) => c >= s.start_m && c <= s.end_m) || S.pipe.sections[0];
  const g = ctx.createLinearGradient(0, axis - halfH, 0, axis + halfH);
  g.addColorStop(0, '#2f3b73'); g.addColorStop(0.5, '#141c3d'); g.addColorStop(1, '#0c1230');
  ctx.fillStyle = g; ctx.fillRect(0, axis - halfH, w, halfH * 2);

  // joints every few metres, so motion is legible at this zoom
  const spacing = 5.5;
  ctx.strokeStyle = 'rgba(120,145,220,.35)'; ctx.lineWidth = 1;
  for (let m = Math.floor(lo / spacing) * spacing; m < hi; m += spacing) {
    const x = X(m);
    ctx.beginPath(); ctx.moveTo(x, axis - halfH); ctx.lineTo(x, axis + halfH); ctx.stroke();
  }
  if (sec.fill_fraction > 0.005) {
    const wh = sec.fill_fraction * halfH * 2;
    const wg = ctx.createLinearGradient(0, axis + halfH - wh, 0, axis + halfH);
    wg.addColorStop(0, 'rgba(70,195,235,.6)'); wg.addColorStop(1, 'rgba(18,70,120,.85)');
    ctx.fillStyle = wg; ctx.fillRect(0, axis + halfH - wh, w, wh);
  }

  S.pipe.features.forEach((f) => {
    if (f.position_m < lo || f.position_m > hi) return;
    const x = X(f.position_m);
    ctx.fillStyle = FEATURE_COLORS[f.kind] || '#6679b5';
    ctx.fillRect(x - 2, axis - halfH, 4, halfH * 2);
    ctx.font = '9px ui-monospace,monospace';
    ctx.fillText(`${f.label} (${f.loss_db} dB)`, x + 6, axis - halfH + 11);
  });

  st.nodes.forEach((n) => {
    if (n.position_m < lo || n.position_m > hi) return;
    const x = X(n.position_m);
    ctx.fillStyle = '#7b6cff';
    ctx.beginPath(); ctx.arc(x, axis + halfH - 8, 7, 0, Math.PI * 2); ctx.fill();
    ctx.fillStyle = '#cfc9ff'; ctx.font = '9px ui-monospace,monospace';
    ctx.fillText(`R${n.index}`, x - 7, axis + halfH - 20);
  });

  const cx = X(c);
  ctx.fillStyle = 'rgba(47,184,242,.2)';
  ctx.beginPath(); ctx.moveTo(cx, axis); ctx.lineTo(cx + 120, axis - 26);
  ctx.lineTo(cx + 120, axis + 26); ctx.closePath(); ctx.fill();
  ctx.fillStyle = '#2fb8f2'; ctx.fillRect(cx - 12, axis - 9, 24, 17);
  ctx.fillStyle = '#9fe6ff'; ctx.fillRect(cx - 12, axis + 8, 24, 3);

  const lastNode = st.nodes.length ? st.nodes[st.nodes.length - 1].position_m : 0;
  $('zoom-caption').textContent =
    `local view · ${sec.diameter_in}" ${sec.wall} · ${(sec.fill_fraction * 100).toFixed(0)}% full · ` +
    `${Math.round(c - lastNode)} m from last relay`;
}

/* -------------------------------------------------------------- camera */

const camOff = document.createElement('canvas');

function tunnelFrame(octx, w, h, posM, fill, feature) {
  const cxp = w / 2, cyp = h / 2;
  octx.fillStyle = '#05070e';
  octx.fillRect(0, 0, w, h);

  const rings = 26, spacing = 0.55;
  const phase = (posM % spacing) / spacing;
  for (let i = rings; i >= 1; i--) {
    const d = (i - phase) * spacing + 0.35;
    const r = (w * 0.62) / (1 + d * 1.25);
    const lum = clamp(1.15 / (1 + d * d * 0.35), 0, 1);
    const isJoint = Math.round((posM + (i - phase) * spacing) / 5.5) !==
                    Math.round((posM + (i - phase + 1) * spacing) / 5.5);
    octx.beginPath();
    octx.ellipse(cxp, cyp, r, r * 0.86, 0, 0, Math.PI * 2);
    octx.strokeStyle = `rgba(${Math.round(120 * lum + 25)},${Math.round(128 * lum + 28)},${Math.round(140 * lum + 34)},${0.5 * lum + 0.08})`;
    octx.lineWidth = isJoint ? 3 : 1.2;
    if (isJoint) octx.strokeStyle = `rgba(${Math.round(190 * lum)},${Math.round(185 * lum)},${Math.round(170 * lum)},${0.75 * lum})`;
    octx.stroke();
  }

  // wall shading from the crawler lights
  const grd = octx.createRadialGradient(cxp, cyp, w * 0.04, cxp, cyp, w * 0.72);
  grd.addColorStop(0, 'rgba(0,0,0,0.92)');
  grd.addColorStop(0.45, 'rgba(90,95,105,0.10)');
  grd.addColorStop(1, 'rgba(150,150,160,0.26)');
  octx.fillStyle = grd;
  octx.fillRect(0, 0, w, h);

  // water in the invert
  if (fill > 0.005) {
    const wy = cyp + (h * 0.43) * (1 - fill * 1.5);
    const wg = octx.createLinearGradient(0, wy, 0, h);
    wg.addColorStop(0, 'rgba(70,150,180,.45)');
    wg.addColorStop(1, 'rgba(10,40,70,.85)');
    octx.fillStyle = wg;
    octx.fillRect(0, wy, w, h - wy);
    octx.strokeStyle = 'rgba(170,235,255,.35)';
    octx.lineWidth = 1;
    octx.beginPath();
    for (let x = 0; x <= w; x += 4) {
      const y = wy + Math.sin((x * 0.05) + posM * 2.2) * 1.4;
      x === 0 ? octx.moveTo(x, y) : octx.lineTo(x, y);
    }
    octx.stroke();
  }

  // the defect you are driving up to
  if (feature && feature.dist < 6 && feature.dist > -1) {
    const scale = clamp(1.6 / (feature.dist + 0.6), 0.2, 2.2);
    const r = (w * 0.3) * scale;
    octx.save();
    octx.translate(cxp, cyp);
    if (feature.kind === 'debris') {
      octx.fillStyle = 'rgba(90,70,45,.75)';
      octx.beginPath();
      octx.ellipse(0, r * 0.45, r * 0.75, r * 0.3, 0, 0, Math.PI * 2);
      octx.fill();
    } else if (feature.kind === 'offset' || feature.kind === 'joint') {
      octx.strokeStyle = 'rgba(210,205,190,.7)'; octx.lineWidth = 3 * scale;
      octx.beginPath(); octx.ellipse(r * 0.08, 0, r * 0.7, r * 0.6, 0, 0, Math.PI * 2); octx.stroke();
    } else if (feature.kind === 'manhole') {
      octx.fillStyle = 'rgba(120,110,255,.18)';
      octx.fillRect(-r * 0.4, -r * 1.4, r * 0.8, r * 1.1);
    } else if (feature.kind === 'bend') {
      octx.fillStyle = 'rgba(10,10,16,.75)';
      octx.beginPath(); octx.ellipse(r * 0.5, 0, r * 0.7, r * 0.75, 0, 0, Math.PI * 2); octx.fill();
    }
    octx.restore();
  }

  // sensor grain
  const n = Math.round(w * h * 0.012);
  octx.fillStyle = 'rgba(255,255,255,.05)';
  for (let i = 0; i < n; i++) {
    octx.fillRect((Math.random() * w) | 0, (Math.random() * h) | 0, 1, 1);
  }
}

function drawCam() {
  const cv = $('cam');
  const ctx = cv.getContext('2d');
  ctx.imageSmoothingEnabled = false;
  if (!S.state) return;
  const st = S.state, v = st.video;

  // Live mode with a camera node: show the real MJPEG stream instead of the
  // simulated one. Everything else on the console is unchanged.
  const liveCam = st.live && st.live.camera_url;
  const img = $('cam-live');
  if (liveCam) {
    if (img.dataset.src !== st.live.camera_url) {
      img.dataset.src = st.live.camera_url;
      img.src = st.live.camera_url;
      img.className = 'on';
      $('cam').classList.add('hidden');
    }
    $('cam-tier').textContent = `live MJPEG · ${st.live.camera_url}`;
    $('cam-tier').className = 'cam-tier';
    $('cam-lost').className = 'cam-lost' + (st.chain.control_ok ? '' : ' on');
    renderOsd(st.crawler.position_m);
    return;
  }
  if (img.className === 'on') {
    img.className = ''; img.removeAttribute('src'); delete img.dataset.src;
    $('cam').classList.remove('hidden');
  }

  const tierEl = $('cam-tier');
  tierEl.textContent = v.name === 'no video'
    ? 'NO VIDEO' : `${v.name} · ${v.required_kbps} kbps · ${v.usable_for}`;
  tierEl.className = 'cam-tier ' + (v.name === 'no video' ? 'none' : (v.height >= 720 ? '' : 'low'));
  $('cam-lost').className = 'cam-lost' + (v.freeze ? ' on' : '');
  if (v.freeze) { renderOsd(); return; }

  const interval = 1000 / Math.max(v.fps, 0.05);
  const now = performance.now();
  if (now - (S.camLast || 0) < interval) { renderOsd(); return; }
  S.camLast = now;

  // Latency made literal: show the pipe as it was glass_to_glass_ms ago.
  const lagMs = isFinite(v.glass_to_glass_ms) ? v.glass_to_glass_ms : 0;
  const target = now - lagMs;
  let posM = st.crawler.position_m;
  for (let i = S.posHistory.length - 1; i >= 0; i--) {
    if (S.posHistory[i].t <= target) { posM = S.posHistory[i].pos; break; }
  }

  const ow = clamp(Math.round(640 * (v.width / 1920)) || 64, 48, 640);
  const oh = Math.round(ow * 9 / 16);
  camOff.width = ow; camOff.height = oh;
  const octx = camOff.getContext('2d');

  const feature = nearestFeature(posM);
  const sec = S.pipe && S.pipe.sections.find((s) => posM >= s.start_m && posM <= s.end_m);
  tunnelFrame(octx, ow, oh, posM, sec ? sec.fill_fraction : 0, feature);

  // packet loss shows up as macroblock smear, the way it does on real video
  const loss = v.loss_percent / 100 + (st.chain.hops.length
    ? st.chain.hops[st.chain.hops.length - 1].per * 0.18 : 0);
  if (loss > 0.002) {
    const blocks = Math.min(Math.round(loss * 220), 90);
    for (let i = 0; i < blocks; i++) {
      const bw = 8 + ((Math.random() * 14) | 0), bh = 6 + ((Math.random() * 10) | 0);
      const dx = (Math.random() * (ow - bw)) | 0, dy = (Math.random() * (oh - bh)) | 0;
      const sx = clamp(dx + ((Math.random() - 0.5) * 24) | 0, 0, ow - bw);
      const sy = clamp(dy + ((Math.random() - 0.5) * 18) | 0, 0, oh - bh);
      octx.drawImage(camOff, sx, sy, bw, bh, dx, dy, bw, bh);
    }
  }

  ctx.clearRect(0, 0, cv.width, cv.height);
  ctx.drawImage(camOff, 0, 0, ow, oh, 0, 0, cv.width, cv.height);
  renderOsd(posM);
}

function nearestFeature(posM) {
  if (!S.pipe) return null;
  let best = null;
  S.pipe.features.forEach((f) => {
    const d = f.position_m - posM;
    if (d > -1 && d < 8 && (!best || d < best.dist)) best = { kind: f.kind, dist: d, label: f.label };
  });
  return best;
}

function renderOsd(posM) {
  const st = S.state, v = st.video;
  $('osd-res').textContent = v.name === 'no video' ? '--' : `${v.width}x${v.height} @ ${v.fps} fps`;
  $('osd-lat').textContent = isFinite(v.glass_to_glass_ms)
    ? `lag ${Math.round(v.glass_to_glass_ms)} ms` : 'lag --';
  $('osd-loss').textContent = `loss ${v.loss_percent.toFixed(2)}% · FEC ${v.fec_overhead}×` +
    (posM !== undefined ? ` · ${Math.round(posM)} m` : '');
  const f = posM !== undefined ? nearestFeature(posM) : null;
  $('cam-note').textContent = v.freeze
    ? 'Video has no bandwidth left. Control and telemetry still flow on the spread-spectrum channel, so the crawler can be driven back out.'
    : (f ? `Approaching ${f.label} in ${f.dist.toFixed(1)} m.`
         : `Video is bid for whatever bandwidth is left after the control slice is reserved.`);
}

/* --------------------------------------------------------------- charts */

function drawChart() {
  const cv = $('chart');
  const { ctx, w, h } = fitCanvas(cv, 190);
  ctx.clearRect(0, 0, w, h);
  if (!S.pipe) return;
  const L = S.pipe.length_m, ml = 44, mr = 44, mt = 12, mb = 24;
  const X = (m) => ml + (m / L) * (w - ml - mr);
  const Ym = (db) => mt + (1 - clamp((db + 5) / 40, 0, 1)) * (h - mt - mb);
  const maxR = Math.max(4, ...S.chart.map((p) => p.mbps));
  const Yr = (r) => mt + (1 - clamp(r / maxR, 0, 1)) * (h - mt - mb);

  ctx.strokeStyle = 'rgba(80,96,160,.22)'; ctx.lineWidth = 1;
  [0, 10, 20, 30].forEach((db) => {
    const y = Ym(db);
    ctx.beginPath(); ctx.moveTo(ml, y); ctx.lineTo(w - mr, y); ctx.stroke();
    ctx.fillStyle = 'rgba(141,154,206,.6)'; ctx.font = '9px ui-monospace,monospace';
    ctx.fillText(`${db}`, 8, y + 3);
  });
  ctx.fillStyle = 'rgba(141,154,206,.6)';
  ctx.fillText('dB', 8, mt + 2);
  ctx.fillText(`${maxR.toFixed(0)}`, w - mr + 6, Yr(maxR) + 3);
  ctx.fillText('Mbps', w - mr + 6, mt + 2);

  if (S.chart.length > 1) {
    ctx.strokeStyle = '#3ddc97'; ctx.lineWidth = 1.6; ctx.beginPath();
    S.chart.forEach((p, i) => { const x = X(p.x), y = Yr(p.mbps); i ? ctx.lineTo(x, y) : ctx.moveTo(x, y); });
    ctx.stroke();
    ctx.strokeStyle = '#2fb8f2'; ctx.lineWidth = 1.6; ctx.beginPath();
    S.chart.forEach((p, i) => { const x = X(p.x), y = Ym(p.margin); i ? ctx.lineTo(x, y) : ctx.moveTo(x, y); });
    ctx.stroke();
  }

  if (S.state) {
    S.state.nodes.forEach((n) => {
      const x = X(n.position_m);
      ctx.strokeStyle = 'rgba(123,108,255,.45)';
      ctx.setLineDash([2, 3]);
      ctx.beginPath(); ctx.moveTo(x, mt); ctx.lineTo(x, h - mb); ctx.stroke();
      ctx.setLineDash([]);
    });
    const cx = X(S.state.crawler.position_m);
    ctx.strokeStyle = 'rgba(232,237,255,.5)';
    ctx.beginPath(); ctx.moveTo(cx, mt); ctx.lineTo(cx, h - mb); ctx.stroke();
  }
  ctx.fillStyle = 'rgba(141,154,206,.6)';
  ctx.fillText('0', ml, h - 8);
  ctx.fillText(`${Math.round(L)} m`, w - mr - 30, h - 8);
  ctx.fillText('dashed = relay drop point', ml + 70, h - 8);
}

/* -------------------------------------------------------------- controls */

const TIMESCALES = [1, 2, 5, 15, 40, 100, 200];

function syncControls() {
  const st = S.state;
  if (document.activeElement !== $('speed')) {
    $('speed').value = st.cruise_speed;
    $('speed-val').textContent = st.cruise_speed.toFixed(2);
  }
  $('mag-val').textContent = st.policy.magazine;
  if (document.activeElement !== $('magazine')) $('magazine').value = st.policy.magazine;
  if (document.activeElement !== $('policy')) $('policy').value = st.policy.key;
  $('freq').value = String(Math.round(st.radio.frequency_mhz));
  $('bw').value = String(Math.round(st.radio.bandwidth_mhz));
  $('txp').value = String(Math.round(st.radio.tx_power_dbm));
  $('relaybuild').value = String(st.radio.radios_per_node);

  // Radio settings are compiled into the firmware, and sim speed is
  // meaningless against a real robot. Lock them rather than lie.
  if (st.live && !S.lockedControls) {
    S.lockedControls = true;
    ['timescale', 'freq', 'bw', 'txp', 'relaybuild', 'policy'].forEach((id) => {
      const el = $(id);
      el.disabled = true;
      if (el.parentElement) el.parentElement.classList.add('disabled');
    });
    const auto = document.querySelector('[data-cmd="auto"]');
    if (auto) { auto.disabled = true; auto.classList.add('ghost'); auto.textContent = 'Auto (sim only)'; }
    const reset = document.querySelector('[data-cmd="reset"]');
    if (reset) reset.style.display = 'none';
  }
}

function wireControls() {
  document.querySelectorAll('[data-cmd]').forEach((b) => {
    b.addEventListener('click', () => {
      const cmd = b.dataset.cmd;
      if (cmd === 'drive') return post('drive', { speed_mps: parseFloat($('speed').value) });
      post(cmd);
    });
  });

  $('estop').addEventListener('click', () => {
    const t0 = performance.now();
    post('estop').then((r) => {
      const wall = (performance.now() - t0).toFixed(0);
      $('plan-out').innerHTML =
        `<b>E-STOP</b> acknowledged over <b>${r.hops}</b> hops in a modelled ` +
        `<b>${Math.round(r.rtt_ms)} ms</b> round trip (console round trip ${wall} ms).`;
    });
  });

  $('scenario').addEventListener('change', (e) => {
    post('reset', { scenario: e.target.value }).then(loadPipe);
  });
  $('speed').addEventListener('input', (e) => {
    $('speed-val').textContent = parseFloat(e.target.value).toFixed(2);
    post('set_speed', { mps: parseFloat(e.target.value) });
  });
  $('timescale').addEventListener('input', (e) => {
    const v = TIMESCALES[parseInt(e.target.value, 10)];
    $('ts-val').textContent = v;
    post('time_scale', { value: v });
  });
  $('magazine').addEventListener('input', (e) => {
    $('mag-val').textContent = e.target.value;
    post('set_magazine', { count: parseInt(e.target.value, 10) });
  });
  $('policy').addEventListener('change', (e) => post('set_policy', { policy: e.target.value }));
  $('freq').addEventListener('change', (e) =>
    post('set_radio', { frequency_mhz: parseFloat(e.target.value) }).then(loadPipe));
  $('bw').addEventListener('change', (e) =>
    post('set_radio', { bandwidth_mhz: parseFloat(e.target.value) }));
  $('txp').addEventListener('change', (e) =>
    post('set_radio', { tx_power_dbm: parseFloat(e.target.value) }));
  $('relaybuild').addEventListener('change', (e) => {
    const dual = e.target.value === '2';
    post('set_radio', { radios_per_node: dual ? 2 : 1, channel_count: dual ? 3 : 1 });
  });
  $('plan-btn').addEventListener('click', () => {
    $('plan-out').textContent = 'planning…';
    fetch('/api/plan').then((r) => r.json()).then((p) => {
      $('plan-out').innerHTML =
        `Covering <b>${Math.round(p.length_m)} m</b> at a <b>${p.margin_db.toFixed(0)} dB</b> drop ` +
        `threshold needs <b>${p.node_count}</b> relays in <b>${p.hops}</b> hops ` +
        `(mean hop <b>${Math.round(p.mean_hop_m)} m</b>).<br>Drop points: ` +
        (p.positions.length ? p.positions.map((x) => `${Math.round(x)} m`).join(', ') : 'none needed') +
        `<br><span style="color:var(--dimmer)">solved in ${p.compute_ms} ms</span>`;
    });
  });

  window.addEventListener('keydown', (e) => {
    if (e.target.tagName === 'INPUT' || e.target.tagName === 'SELECT') return;
    if (e.code === 'Space') { e.preventDefault(); post('estop'); }
    if (e.key === 'a') post('auto');
    if (e.key === 's') post('stop');
    if (e.key === 'd') post('deploy');
    if (e.key === 'r') post('return');
  });
}

/* ----------------------------------------------------------------- boot */

function frame() {
  drawPipe();
  drawZoom();
  drawCam();
  drawChart();
  requestAnimationFrame(frame);
}

loadScenarios()
  .then(loadPipe)
  .then(() => { wireControls(); connect(); requestAnimationFrame(frame); });
