// Coordinator UI: join QR code, devices, controls, the room map and results.
// Delivered as a hot-swappable module, so it can be changed without anyone
// reloading the coordinator page.

const esc = (s) => String(s == null ? "" : s).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c]);
const fmt = (v, d = 2) => (v == null || !isFinite(v) ? "-" : Number(v).toFixed(d));
const ago = (ts) => {
  const s = Math.round(Date.now() / 1000 - ts);
  return s < 60 ? `${s}s ago` : s < 3600 ? `${Math.round(s / 60)}m ago` : `${Math.round(s / 3600)}h ago`;
};

// best rotation(+optional reflection)+translation mapping points A onto B
function procrustes2d(A, B) {
  const n = A.length;
  const ca = [0, 0],
    cb = [0, 0];
  A.forEach((p) => ((ca[0] += p[0] / n), (ca[1] += p[1] / n)));
  B.forEach((p) => ((cb[0] += p[0] / n), (cb[1] += p[1] / n)));
  let best = null;
  for (const refl of [1, -1]) {
    let s = 0,
      c = 0;
    for (let i = 0; i < n; i++) {
      const ax = A[i][0] - ca[0],
        ay = (A[i][1] - ca[1]) * refl;
      const bx = B[i][0] - cb[0],
        by = B[i][1] - cb[1];
      s += ax * by - ay * bx;
      c += ax * bx + ay * by;
    }
    const th = Math.atan2(s, c);
    const f = (p) => {
      const x = p[0] - ca[0],
        y = (p[1] - ca[1]) * refl;
      return [Math.cos(th) * x - Math.sin(th) * y + cb[0], Math.sin(th) * x + Math.cos(th) * y + cb[1]];
    };
    let err = 0;
    for (let i = 0; i < n; i++) {
      const q = f(A[i]);
      err += (q[0] - B[i][0]) ** 2 + (q[1] - B[i][1]) ** 2;
    }
    if (!best || err < best.err) best = { err, f };
  }
  return best.f;
}

export default function setup(ctx) {
  const rt = ctx.rt;
  let state = null;
  const results = new Map(); // id -> {id, ts, kind, summary, data?}
  const logLines = [];
  let simTruth = null;
  let onsetCount = 0;
  let selected = null;

  const root = ctx.ui;
  root.innerHTML = `
  <style>
    .co { display: grid; grid-template-columns: minmax(280px, 380px) 1fr; gap: 16px; padding: 16px; }
    @media (max-width: 860px) { .co { grid-template-columns: 1fr; } }
    .co .card { background: var(--card); border: 1px solid var(--line); border-radius: 12px; padding: 12px 14px; margin-bottom: 14px; }
    .co h2 { font-size: 1em; margin: 0 0 8px; }
    .co .join { display: flex; gap: 12px; align-items: center; }
    .co .join img { width: 132px; height: 132px; background: white; border-radius: 6px; }
    .co .join a { word-break: break-all; }
    .co .row { display: flex; gap: 8px; flex-wrap: wrap; align-items: center; margin: 6px 0; }
    .co table { width: 100%; border-collapse: collapse; font-size: 0.9em; }
    .co td, .co th { text-align: left; padding: 4px 4px; border-bottom: 1px solid var(--line); vertical-align: top; }
    .co .sw { display: inline-block; width: 12px; height: 12px; border-radius: 50%; vertical-align: -1px; margin-right: 6px; }
    .co .dev-name { cursor: pointer; font-weight: 600; }
    .co .small { font-size: 0.82em; }
    .co .mapwrap { position: relative; }
    .co svg.map { width: 100%; height: auto; background: var(--card); border: 1px solid var(--line); border-radius: 12px; touch-action: none; }
    .co .log { max-height: 260px; }
    .co .pill { padding: 1px 7px; border-radius: 9px; background: var(--line); font-size: 0.8em; }
    .co .res { cursor: pointer; }
    .co .res.sel { background: rgba(120,160,255,0.15); }
    .co .progress { min-height: 1.3em; }
  </style>
  <div class="co">
    <div>
      <div class="card">
        <h2>Add a listener</h2>
        <div class="join"><img alt="QR code"><div><div class="muted small">Scan with each phone or tablet, or open:</div><a target="_blank"></a></div></div>
      </div>
      <div class="card">
        <h2>Controls</h2>
        <div class="row">
          <button class="primary" data-act="calibrate">Calibrate</button>
          <label><input type="checkbox" data-act="monitoring"> Listen for beeps</label>
        </div>
        <div class="row">
          <select data-test></select><button data-act="testbeep">Test beep</button>
          <button data-act="locate" title="Use if a beep just happened but wasn't detected">It just beeped!</button>
        </div>
        <div class="row small">
          Map: <button data-act="rot-">&#8634;</button><button data-act="rot+">&#8635;</button><button data-act="flip">Flip</button>
        </div>
        <div class="progress small muted"></div>
      </div>
      <div class="card"><h2>Devices</h2><div class="devices"></div></div>
    </div>
    <div>
      <div class="card mapwrap"><svg class="map" viewBox="0 0 800 600"></svg>
        <div class="small muted legend">Dots are listeners. Crosses are beep estimates (newest is largest). Shading shows where the latest beep could plausibly be.</div>
      </div>
      <div class="card"><h2>Beeps</h2><div class="results small"></div></div>
      <div class="card"><h2>Log</h2><div class="log small"></div></div>
    </div>
  </div>`;
  const q = (sel) => root.querySelector(sel);
  const svg = q("svg.map");

  function log(line) {
    logLines.push(`${new Date().toLocaleTimeString()} ${line}`);
    if (logLines.length > 300) logLines.shift();
    const el = q(".log");
    el.textContent = logLines.slice().reverse().join("\n");
  }

  async function cmd(name, args = {}) {
    q(".progress").textContent = `${name}...`;
    try {
      const r = await ctx.command(name, args, 120000);
      return r;
    } catch (e) {
      q(".progress").innerHTML = `<span class="bad">${esc(name)} failed: ${esc(e.message)}</span>`;
      log(`${name} failed: ${e.message}`);
      throw e;
    }
  }

  root.addEventListener("click", async (e) => {
    const b = e.target.closest("[data-act]");
    if (!b) return;
    const act = b.dataset.act;
    if (act === "calibrate") cmd("calibrate").catch(() => {});
    else if (act === "testbeep") cmd("testbeep", { device: q("[data-test]").value }).catch(() => {});
    else if (act === "locate") cmd("locate", { ago: 1.0, label: "manual" }).catch(() => {});
    else if (act === "identify") cmd("identify", { device: b.dataset.dev }).catch(() => {});
    else if (act === "rot-" || act === "rot+" || act === "flip") {
      const v = state.view || { rotation: 0, flip: false };
      const nv = { rotation: (v.rotation || 0) + (act === "rot-" ? -15 : act === "rot+" ? 15 : 0), flip: act === "flip" ? !v.flip : !!v.flip };
      state.view = nv;
      draw();
      cmd("view", nv).catch(() => {});
    }
  });
  q('[data-act="monitoring"]').addEventListener("change", (e) => cmd("monitoring", { on: e.target.checked }).catch(() => {}));
  root.addEventListener("dblclick", (e) => {
    const n = e.target.closest(".dev-name");
    if (!n) return;
    const name = prompt("Name for this device", n.textContent);
    if (name) cmd("rename", { device: n.dataset.dev, name }).catch(() => {});
  });

  // -- view transform ------------------------------------------------------
  function viewTf(p) {
    const v = (state && state.view) || { rotation: 0, flip: false };
    let x = p[0],
      y = p[1];
    if (v.flip) x = -x;
    const a = ((v.rotation || 0) * Math.PI) / 180;
    return [Math.cos(a) * x - Math.sin(a) * y, Math.sin(a) * x + Math.cos(a) * y];
  }

  function latestLocates(n = 25) {
    return Array.from(results.values())
      .filter((r) => r.kind === "locate" && r.summary && r.summary.x != null)
      .sort((a, b) => a.id - b.id)
      .slice(-n);
  }

  function draw() {
    if (!state) return;
    const L = state.layout;
    const W = 800,
      H = 600,
      pad = 50;
    let parts = [];
    if (!L) {
      svg.innerHTML = `<text x="400" y="300" text-anchor="middle" fill="currentColor" opacity="0.6" font-size="20">Connect 3+ listeners, then press Calibrate</text>`;
      return;
    }
    const ids = L.device_ids;
    const pos = L.positions_by_id;
    const pts = ids.map((id) => viewTf(pos[id]));
    const locs = latestLocates();
    const est = locs.map((r) => viewTf([r.summary.x, r.summary.y]));
    let truthPts = null;
    if (simTruth) {
      const common = ids.filter((id) => simTruth.devices[id]);
      if (common.length >= 3) {
        const f = procrustes2d(
          common.map((id) => simTruth.devices[id]),
          common.map((id) => pos[id])
        );
        truthPts = { devices: Object.fromEntries(common.map((id) => [id, viewTf(f(simTruth.devices[id]))])), beeper: simTruth.beeper ? viewTf(f(simTruth.beeper)) : null };
      }
    }
    const all = pts.concat(est.filter((p) => Math.abs(p[0]) < 30 && Math.abs(p[1]) < 30));
    if (truthPts && truthPts.beeper) all.push(truthPts.beeper);
    let x0 = Math.min(...all.map((p) => p[0])),
      x1 = Math.max(...all.map((p) => p[0]));
    let y0 = Math.min(...all.map((p) => p[1])),
      y1 = Math.max(...all.map((p) => p[1]));
    const span = Math.max(x1 - x0, y1 - y0, 1);
    x0 -= 0.15 * span;
    x1 += 0.15 * span;
    y0 -= 0.15 * span;
    y1 += 0.15 * span;
    const s = Math.min((W - 2 * pad) / (x1 - x0), (H - 2 * pad) / (y1 - y0));
    const ox = pad + ((W - 2 * pad) - s * (x1 - x0)) / 2;
    const oy = pad + ((H - 2 * pad) - s * (y1 - y0)) / 2;
    const X = (p) => ox + (p[0] - x0) * s;
    const Y = (p) => oy + (p[1] - y0) * s;

    // likelihood heatmap for the selected (or latest) result, if we have its grid
    const focus = selected != null ? results.get(selected) : locs[locs.length - 1];
    const grid = focus && focus.data && focus.data.estimate && focus.data.estimate.grid;
    if (grid) {
      // accumulate with other results of similar frequency on the same grid
      const acc = grid.loglik.slice();
      if (selected == null) {
        for (const r of locs) {
          const g2 = r.data && r.data.estimate && r.data.estimate.grid;
          if (!g2 || r === focus || g2.nx !== grid.nx || g2.x0 !== grid.x0 || g2.y1 !== grid.y1) continue;
          if (focus.summary.freq && r.summary.freq && Math.abs(focus.summary.freq - r.summary.freq) > 150) continue;
          for (let i = 0; i < acc.length; i++) acc[i] += g2.loglik[i];
        }
      }
      const mx = Math.max(...acc);
      const dx = (grid.x1 - grid.x0) / (grid.nx - 1),
        dy = (grid.y1 - grid.y0) / (grid.ny - 1);
      for (let j = 0; j < grid.ny; j++)
        for (let i = 0; i < grid.nx; i++) {
          const v = Math.exp(Math.max(-30, acc[j * grid.nx + i] - mx));
          if (v < 0.02) continue;
          const corners = [
            [grid.x0 + (i - 0.5) * dx, grid.y0 + (j - 0.5) * dy],
            [grid.x0 + (i + 0.5) * dx, grid.y0 + (j - 0.5) * dy],
            [grid.x0 + (i + 0.5) * dx, grid.y0 + (j + 0.5) * dy],
            [grid.x0 + (i - 0.5) * dx, grid.y0 + (j + 0.5) * dy],
          ].map(viewTf);
          parts.push(`<polygon points="${corners.map((c) => `${X(c).toFixed(1)},${Y(c).toFixed(1)}`).join(" ")}" fill="#e53e3e" fill-opacity="${(0.55 * v).toFixed(3)}"/>`);
        }
    }
    // 1m grid
    for (let gx = Math.ceil(x0); gx <= x1; gx++) parts.push(`<line x1="${X([gx, 0])}" y1="0" x2="${X([gx, 0])}" y2="${H}" stroke="currentColor" stroke-opacity="0.07"/>`);
    for (let gy = Math.ceil(y0); gy <= y1; gy++) parts.push(`<line x1="0" y1="${Y([0, gy])}" x2="${W}" y2="${Y([0, gy])}" stroke="currentColor" stroke-opacity="0.07"/>`);
    parts.push(`<line x1="20" y1="${H - 20}" x2="${20 + s}" y2="${H - 20}" stroke="currentColor" stroke-width="3"/><text x="${24 + s}" y="${H - 16}" font-size="13" fill="currentColor">1 m</text>`);
    // truth (simulation only)
    if (truthPts) {
      for (const [id, p] of Object.entries(truthPts.devices)) parts.push(`<circle cx="${X(p)}" cy="${Y(p)}" r="13" fill="none" stroke="currentColor" stroke-opacity="0.35" stroke-dasharray="3 3"/>`);
      if (truthPts.beeper) parts.push(`<rect x="${X(truthPts.beeper) - 9}" y="${Y(truthPts.beeper) - 9}" width="18" height="18" fill="none" stroke="currentColor" stroke-width="2" stroke-dasharray="4 3"/><text x="${X(truthPts.beeper) + 12}" y="${Y(truthPts.beeper) + 22}" font-size="12" fill="currentColor" opacity="0.6">true beeper (sim)</text>`);
    }
    // estimates
    est.forEach((p, k) => {
      const r = locs[k];
      const latest = k === est.length - 1;
      const sz = latest ? 11 : 6;
      const col = r.summary.label && r.summary.label.startsWith("testbeep") ? "#805ad5" : "#e53e3e";
      const op = latest ? 1 : 0.35 + (0.5 * k) / Math.max(1, est.length);
      parts.push(`<g opacity="${op.toFixed(2)}" class="est" data-id="${r.id}"><line x1="${X(p) - sz}" y1="${Y(p) - sz}" x2="${X(p) + sz}" y2="${Y(p) + sz}" stroke="${col}" stroke-width="${latest ? 4 : 2.5}"/><line x1="${X(p) - sz}" y1="${Y(p) + sz}" x2="${X(p) + sz}" y2="${Y(p) - sz}" stroke="${col}" stroke-width="${latest ? 4 : 2.5}"/></g>`);
    });
    // devices
    const live = new Map((state.devices || []).map((d) => [d.id, d]));
    ids.forEach((id, k) => {
      const p = pts[k];
      const d = live.get(id);
      const color = (L.colors && L.colors[id]) || (d && d.color) || "#888";
      const name = (d && d.name) || (L.names && L.names[id]) || id;
      const on = d && d.status && d.status.running;
      parts.push(`<circle cx="${X(p)}" cy="${Y(p)}" r="11" fill="${color}" fill-opacity="${on ? 1 : 0.3}" stroke="white" stroke-width="2"/>`);
      parts.push(`<text x="${X(p) + 15}" y="${Y(p) + 5}" font-size="15" font-weight="600" fill="currentColor">${esc(name)}</text>`);
    });
    svg.innerHTML = parts.join("");
  }

  function renderDevices() {
    if (!state) return;
    const devs = (state.devices || []).filter((d) => d.role === "listener");
    const L = state.layout;
    const latest = latestLocates(1)[0];
    const levels = {};
    if (latest && latest.data) for (const d of latest.data.devices || []) if (d.level_db != null) levels[d.device_id] = d.level_db;
    const rows = devs.map((d) => {
      const st = d.status || {};
      const lv = st.level ? `${fmt(st.level.rms, 0)} dB` : "";
      const flags = [];
      if (!st.running) flags.push('<span class="bad">mic off</span>');
      if (st.visibility === "hidden") flags.push('<span class="warn">tab hidden</span>');
      if (!d.sim && st.running && !st.wakeLock) flags.push('<span class="warn">may sleep</span>');
      if (st.gaps) flags.push(`<span class="warn">${st.gaps} gaps</span>`);
      if (st.lastError) flags.push(`<span class="bad" title="${esc(st.lastError)}">error</span>`);
      if (d.sim) flags.push('<span class="pill">sim</span>');
      const placed = L && L.positions_by_id[d.id] ? "" : '<span class="muted">not calibrated</span>';
      const heard = levels[d.id] != null ? `last beep ${fmt(levels[d.id], 0)} dB` : "";
      return `<tr><td><span class="sw" style="background:${d.color}"></span><span class="dev-name" data-dev="${esc(d.id)}" title="double-click to rename">${esc(d.name)}</span>
        <div class="small muted">${esc(st.sampleRate || "")}${st.sampleRate ? " Hz" : ""} ${lv} ${d.clock && d.clock.rtt_ms != null ? `· ${d.clock.rtt_ms} ms rtt` : ""}</div>
        <div class="small">${flags.join(" ")} ${placed} ${heard}</div></td>
        <td style="text-align:right"><button class="small" data-act="identify" data-dev="${esc(d.id)}">Identify</button></td></tr>`;
    });
    q(".devices").innerHTML = devs.length ? `<table>${rows.join("")}</table>` : '<div class="muted">No listeners yet. Scan the QR code with a phone.</div>';
    const sel = q("[data-test]");
    const cur = sel.value;
    sel.innerHTML = devs.map((d) => `<option value="${esc(d.id)}">${esc(d.name)}</option>`).join("");
    if (cur) sel.value = cur;
  }

  function renderResults() {
    const locs = Array.from(results.values())
      .filter((r) => r.kind === "locate" || r.kind === "calibration")
      .sort((a, b) => b.id - a.id)
      .slice(0, 30);
    const rows = locs.map((r) => {
      const s = r.summary || {};
      if (r.kind === "calibration")
        return `<tr class="muted"><td>${ago(r.ts)}</td><td colspan="3">calibrated ${s.devices} devices · ${s.pairs_measured}/${s.pairs_total} distances · fit ${fmt((s.rms_residual_m || 0) * 100, 1)} cm</td></tr>`;
      const where = s.x != null ? `(${fmt(s.x)}, ${fmt(s.y)}) m` : `<span class="muted">${esc(s.error || "no fix")}</span>`;
      const err = s.error_m != null ? ` · <b>${fmt(s.error_m * 100, 0)} cm</b> from source` : "";
      return `<tr class="res ${selected === r.id ? "sel" : ""}" data-id="${r.id}"><td>${ago(r.ts)}</td><td>${esc(s.label)}${s.freq ? ` <span class="muted">${fmt(s.freq, 0)} Hz</span>` : ""}</td>
        <td>${where}${s.rms_residual_m != null ? ` <span class="muted">±${fmt(s.rms_residual_m * 100, 0)} cm fit</span>` : ""}${err}</td>
        <td>${s.used || 0} devs${s.loudest_name ? ` · loudest ${esc(s.loudest_name)}` : ""}</td></tr>`;
    });
    q(".results").innerHTML = rows.length ? `<table>${rows.join("")}</table>` : '<div class="muted">Nothing yet.</div>';
  }
  q(".results").addEventListener("click", (e) => {
    const tr = e.target.closest(".res");
    if (!tr) return;
    const id = Number(tr.dataset.id);
    selected = selected === id ? null : id;
    renderResults();
    draw();
  });

  function renderAll() {
    if (!state) return;
    const join = state.public_url ? state.public_url.replace(/\/$/, "") + "/join" : location.origin + "/join";
    const img = q(".join img");
    if (img.dataset.url !== join) {
      img.src = "/qr.svg?data=" + encodeURIComponent(join);
      img.dataset.url = join;
      q(".join a").href = join;
      q(".join a").textContent = join;
    }
    q('[data-act="monitoring"]').checked = !!(state.settings && state.settings.monitoring);
    q('[data-act="calibrate"]').disabled = !!state.procedure;
    if (state.procedure) q(".progress").textContent = `running: ${state.procedure}...`;
    renderDevices();
    renderResults();
    draw();
  }

  const onState = (m) => {
    state = m.state;
    renderAll();
  };
  ctx.on("state", onState);
  const onHistory = (m) => {
    for (const r of m.results || []) results.set(r.id, r);
    for (const e of m.events || []) {
      if (e.topic === "progress") log(e.data.message);
      if (e.topic === "sim_truth") simTruth = e.data;
    }
    renderAll();
  };
  ctx.on("history", onHistory);
  // catch up on anything that arrived before this module was installed
  if (rt.last.history) onHistory(rt.last.history);
  if (rt.last.state) onState(rt.last.state);
  ctx.on("result", (m) => {
    results.set(m.id, { id: m.id, ts: Date.now() / 1000, kind: m.kind, summary: m.summary, data: m.data });
    selected = null;
    renderResults();
    renderDevices();
    draw();
  });
  ctx.on("progress", (m) => {
    q(".progress").textContent = m.message;
    log(m.message);
  });
  ctx.on("event", (m) => {
    if (m.topic === "sim_truth") {
      simTruth = m.data;
      draw();
    } else if (m.topic === "onset") {
      onsetCount++;
      log(`${m.name} heard a sound: ${m.data.freq} Hz, +${m.data.snr_db} dB, tonality ${m.data.prominence}`);
    } else if (m.topic === "log" && m.data && m.data.level !== "info") {
      log(`${m.name}: ${m.data.level}: ${m.data.message}`);
    } else if (m.topic === "module_error") {
      log(`${m.name}: module ${m.data.name} failed: ${m.data.error}`);
    }
  });
  ctx.setInterval(() => renderResults(), 15000);
}
