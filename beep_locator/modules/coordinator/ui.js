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
  // id -> {id, ts, kind, summary, data?}. Kept on the runtime so results that
  // arrived live survive this module being hot-swapped.
  const liveResults = rt.__coResults || (rt.__coResults = new Map());
  let results = liveResults; // what is on screen: live, or a past session being viewed
  // -- sessions --------------------------------------------------------------
  // A new session starts each time the devices are repositioned. `viewing` is
  // set while looking (read-only) at an earlier one.
  let viewing = null; // {n, results, layout, room, view, estimate, since}
  let wizardRough = false; // rough mode was switched on by the reposition steps
  const sessions = () => (state && state.sessions && state.sessions.length ? state.sessions : [{ n: 1, start_id: 0, ts: 0 }]);
  const curSession = () => sessions()[sessions().length - 1];
  const inScope = (r) => !!viewing || r.id > curSession().start_id;
  const LAY = () => (viewing ? viewing.layout : state && state.layout);
  const VIEW = () => (viewing ? viewing.view : state && state.view) || { rotation: 0, flip: false };
  const ROOM = () => (viewing ? viewing.room : state && state.settings && state.settings.room);
  const phase = () => (state && state.phase) || "running";
  const logLines = [];
  let simTruth = null;
  let onsetCount = 0;
  let selected = null;
  // rough mode: the user drags each listener to roughly where it is; leaving
  // rough mode calibrates, starting from (and oriented by) that placement
  let roughMode = false;
  let roughPos = {}; // id -> [x, y] metres, as drawn
  let frozen = null; // map transform, held still while dragging
  let drag = null;
  let roomEdit = null; // [x0, y0, x1, y1] walls while editing in rough mode

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
    .co svg.map { width: 100%; height: auto; background: var(--card); border: 1px solid var(--line); border-radius: 12px; touch-action: none; user-select: none; -webkit-user-select: none; }
    .co svg.map.rough { border: 2px dashed var(--warn, #d69e2e); }
    .co svg.map.rough .dev { cursor: grab; }
    .co svg.map.rough.dragging, .co svg.map.rough.dragging .dev { cursor: grabbing; }
    .co .log { max-height: 260px; overflow: auto; font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 0.8em; }
    .co .logline { white-space: pre-wrap; padding: 1px 0; }
    .co .logline.sound { font-weight: 600; }
    .co .playbtn { padding: 0 6px; min-width: 0; line-height: 1.5; font-size: 0.85em; border-radius: 6px; cursor: pointer; }
    .co .loud table td { vertical-align: middle; padding: 6px 4px; }
    .co .loud .db { font-size: 1.5em; font-weight: 700; font-variant-numeric: tabular-nums; white-space: nowrap; }
    .co .loud .delta { font-weight: 700; white-space: nowrap; font-variant-numeric: tabular-nums; }
    .co .loud .bar { height: 12px; border-radius: 6px; background: var(--line); overflow: hidden; min-width: 80px; }
    .co .loud .bar > div { height: 100%; border-radius: 6px; }
    .co .loud .hist { font-variant-numeric: tabular-nums; white-space: nowrap; }
    .co .wizard .step { border: 2px solid var(--warn, #d69e2e); border-radius: 10px; padding: 10px 12px; margin-bottom: 8px; }
    .co .wizard .step b.t { display: block; font-size: 1.1em; margin-bottom: 4px; }
    .co .viewbar { display: none; border: 2px solid var(--warn, #d69e2e); border-radius: 10px; padding: 8px 12px; margin-bottom: 8px; }
    .co .viewbar.on { display: flex; justify-content: space-between; align-items: center; gap: 8px; flex-wrap: wrap; }
    .co .mysbtn.on { background: #c026d3; color: white; border-color: #c026d3; }
    .co .replay { display: none; margin: 0 0 10px; padding: 10px 12px; border-radius: 10px; border: 2px solid var(--warn, #d69e2e); }
    .co .replay.on { display: block; }
    .co .replay .who { font-size: 1.5em; font-weight: 700; margin: 4px 0; }
    .co .replay .who .sw { width: 18px; height: 18px; vertical-align: -2px; }
    .co .replay ol { margin: 6px 0 0; padding-left: 20px; }
    .co .replay li.now { font-weight: 700; }
    .co .replay li.done { opacity: 0.5; }
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
        <div class="wizard"></div>
        <div class="row">
          <button class="primary" data-act="calibrate">Calibrate</button>
          <label><input type="checkbox" data-act="monitoring"> Listen for beeps</label>
        </div>
        <div class="row">
          <select data-test></select><button data-act="testbeep">Test beep</button>
          <button data-act="locate" title="Use if a beep just happened but wasn't detected">It just beeped!</button>
        </div>
        <div class="row">
          <label title="Drag each listener to roughly where it is in the room. Untick to measure accurately from there."><input type="checkbox" data-act="rough"> Rough mode: drag listeners into place</label>
        </div>
        <div class="row small">
          Map: <button data-act="rot-">&#8634;</button><button data-act="rot+">&#8635;</button><button data-act="flip">Flip</button>
        </div>
        <div class="row small mysinfo"></div>
        <div class="progress small muted"></div>
      </div>
      <div class="card"><h2>Devices</h2><div class="devices"></div></div>
    </div>
    <div>
      <div class="card mapwrap">
        <div class="viewbar"></div>
        <div class="row small sessbar" style="margin-top:0"><label>Session <select data-session></select></label> <span class="muted sessnote"></span></div>
        <svg class="map" viewBox="0 0 800 600"></svg>
        <div class="since small" style="margin: 8px 0 6px">
          <input type="range" min="0" max="0" step="1" value="0" style="display:block; width:100%; margin:0" aria-label="Show sounds on the map from this time onwards" title="Slide right to hide older sounds from the map, left to bring them back">
          <div class="row" style="justify-content:space-between; margin-top:4px">
            <span>Showing on the map: <b class="since-label" style="font-variant-numeric: tabular-nums"></b></span>
            <span><button data-act="since-now" title="Hide everything so far; new sounds will still appear">Clear map</button> <button data-act="since-all">Show all</button></span>
          </div>
          <div class="muted">Slide right to hide older sounds, left to bring them back. Nothing is deleted.</div>
        </div>
        <div class="small muted legend">Dots are listeners. Crosses are beep estimates (newest is largest). Shading shows where the latest beep could plausibly be.</div>
      </div>
      <div class="card loudcard" style="display:none"><h2><span style="color:#c026d3">&#9678;</span> Mystery beep loudness <span class="muted small" style="font-weight:400">warmer / colder</span></h2><div class="loud"></div></div>
      <div class="card"><h2>Sounds located</h2><div class="replay"></div><div class="results small"></div></div>
      <div class="card"><h2>Log</h2><div class="log small"></div></div>
    </div>
  </div>`;
  const q = (sel) => root.querySelector(sel);
  const svg = q("svg.map");

  const lastHtml = new WeakMap();
  function setHtml(el, html) {
    if (lastHtml.get(el) === html) return;
    lastHtml.set(el, html);
    el.innerHTML = html;
  }
  const hms = (ts) => new Date(ts * 1000).toLocaleTimeString("en-GB", { hour12: false });
  // line: text; opts.ts: when it happened (seconds); opts.rid: result it refers to
  function log(line, opts = {}) {
    logLines.push({ ts: opts.ts || Date.now() / 1000, line, rid: opts.rid || null });
    if (logLines.length > 300) logLines.shift();
    renderLog();
  }
  function renderLog() {
    const html = logLines
      .slice()
      .reverse()
      .map((l) => {
        const r = l.rid != null ? liveResults.get(l.rid) : null;
        const sound = r && r.kind === "locate";
        const k = sound ? kindOf(r) : null;
        const btn = sound ? `<button class="playbtn" data-act="replay" data-id="${r.id}" title="Replay what each listener recorded">${replay.rid === r.id ? "&#9632;" : "&#9654;"}</button> ` : "";
        const mark = sound ? `<span style="color:${KIND_COLOR[k]}">${k === "clap" ? "&#9671;" : "&#10005;"}</span> ` : "";
        return `<div class="logline${sound ? " sound" : ""}"><span class="muted">${sound ? clock(r) : hms(l.ts)}</span> ${btn}${mark}${esc(l.line)}</div>`;
      })
      .join("");
    setHtml(q(".log"), html);
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
    else if (act === "rename") {
      const name = (prompt("Name for this device (e.g. where it is)", b.dataset.name) || "").trim();
      if (name && name !== b.dataset.name) cmd("rename", { device: b.dataset.dev, name }).catch(() => {});
    }
    else if (act === "repos-start") cmd("reposition_start").catch(() => {});
    else if (act === "repos-cancel") cmd("reposition_cancel").catch(() => {});
    else if (act === "repos-done") cmd("reposition_done").catch(() => {});
    else if (act === "repos-finish") {
      if (Object.keys(roughPos).length < 2) q(".progress").textContent = "Need at least two listeners connected.";
      else cmd("reposition_finish", { positions: roughPos, room: roomEdit }).catch(() => {});
    } else if (act === "view-live") viewSession(curSession().n);
    else if (act === "mystery-clear") {
      if (confirm("Forget what the mystery beep sounds like? You would need to mark a beep again.")) cmd("mystery_clear").catch(() => {});
    } else if (act === "since-now") setSince(Date.now() / 1000);
    else if (act === "since-all") setSince(0);
    else if (act === "replay") toggleReplay(Number(b.dataset.id));
    else if (act === "replay-stop") stopReplay("stopped");
    else if (act === "mystery")
      cmd("mark_mystery", { result_id: Number(b.dataset.id), on: b.dataset.on === "1" })
        .then(() => replay.rerender && replay.rerender())
        .catch(() => {});
    else if (act === "walls-add") {
      const pts = Object.values(roughPos);
      roomEdit = [Math.min(...pts.map((p) => p[0])) - 1, Math.min(...pts.map((p) => p[1])) - 1, Math.max(...pts.map((p) => p[0])) + 1, Math.max(...pts.map((p) => p[1])) + 1];
      cmd("room", { room: roomEdit }).catch(() => {});
      draw();
    } else if (act === "walls-remove") {
      roomEdit = null;
      cmd("room", { room: null }).catch(() => {});
      draw();
    }
    else if (act === "zoom-") zoomRough(1.25);
    else if (act === "zoom+") zoomRough(0.8);
    else if ((act === "rot-" || act === "rot+" || act === "flip") && !viewing) {
      const v = state.view || { rotation: 0, flip: false };
      const nv = { rotation: (v.rotation || 0) + (act === "rot-" ? -15 : act === "rot+" ? 15 : 0), flip: act === "flip" ? !v.flip : !!v.flip };
      state.view = nv;
      draw();
      cmd("view", nv).catch(() => {});
    }
  });
  q('[data-act="monitoring"]').addEventListener("change", (e) => cmd("monitoring", { on: e.target.checked }).catch(() => {}));

  // -- replay --------------------------------------------------------------
  // Plays, one listener after another, what each recorded around a located
  // sound. While it plays the server ignores everything the listeners hear,
  // so the replay can't be mistaken for a new beep.
  const replay = { rid: null, token: 0, ctx: null, src: null, rerender: null };
  // -- the mystery beep ----------------------------------------------------
  const MYS = "#c026d3";
  const mys = () => (state && state.settings && state.settings.mystery) || {};
  const isMarked = (id) => !!(mys().marked && mys().marked[id]);
  const mScore = (id) => (mys().scores || {})[id];
  const isLikely = (id) => !isMarked(id) && mScore(id) != null && mScore(id) >= (mys().threshold || 0.75);
  const mysteryButton = (id, big) =>
    viewing
      ? isMarked(id)
        ? `<span style="color:${MYS}">&#9678; mystery beep</span>`
        : ""
      : isMarked(id)
      ? `<button class="playbtn mysbtn on" data-act="mystery" data-id="${id}" data-on="0" title="Click to undo">&#9678; ${big ? "Marked as the mystery beep (undo)" : "Mystery beep &#10003;"}</button>`
      : `${isLikely(id) ? `<span style="color:${MYS}" title="Sounds like the beep you confirmed">&#9678; likely ${Math.round(mScore(id) * 100)}%</span> ` : ""}<button class="playbtn mysbtn" data-act="mystery" data-id="${id}" data-on="1" title="Tell the system this is the beep you are hunting. Replay it first to check.">${big ? "&#9678; This is the mystery beep" : isLikely(id) ? "Confirm" : "This is the mystery beep"}</button>`;
  const GAP = 0.6; // silence between listeners, seconds
  async function api(path) {
    const r = await fetch(path, { headers: { Authorization: "Bearer " + rt.token } });
    if (!r.ok) throw new Error(`${path}: HTTP ${r.status}`);
    return r;
  }
  function replayPanel(html) {
    const el = q(".replay");
    el.classList.toggle("on", !!html);
    el.innerHTML = html || "";
  }
  function refreshPlayButtons() {
    renderResults();
    renderLog();
  }
  function stopReplay(why) {
    if (replay.rid == null) return;
    replay.token++;
    try {
      if (replay.src) replay.src.stop();
    } catch (e) {}
    replay.src = null;
    const rid = replay.rid;
    replay.rid = null;
    ctx.command("pause_listening", { seconds: 0 }, 10000).catch(() => {});
    const t = replay.token;
    const done = () => replayPanel(`<div class="row" style="justify-content:space-between"><span class="muted">${esc(why || "Replay finished")}. Listening for new sounds again.</span><span>${rid != null && results.get(rid) ? `Was that it? ${mysteryButton(rid, true)}` : ""}</span></div>`);
    replay.rerender = why === "stopped" ? null : () => replay.rid == null && replay.token === t && done();
    if (why === "stopped") replayPanel("");
    else {
      done();
      setTimeout(() => replay.rid == null && replay.token === t && (replayPanel(""), (replay.rerender = null)), 30000);
    }
    refreshPlayButtons();
  }
  function toggleReplay(rid) {
    if (replay.rid === rid) return stopReplay("stopped");
    if (replay.rid != null) stopReplay("stopped");
    startReplay(rid).catch((e) => {
      log(`replay failed: ${e.message}`);
      stopReplay(`Replay failed: ${e.message}`);
    });
  }
  async function startReplay(rid) {
    const token = ++replay.token;
    replay.rid = rid;
    refreshPlayButtons();
    const AC = window.AudioContext || window.webkitAudioContext;
    if (!replay.ctx) replay.ctx = new AC();
    const ac = replay.ctx;
    if (ac.state !== "running") await ac.resume();
    replayPanel(`<span class="muted">Loading recordings…</span>`);
    const res = await (await api(`/api/results/${rid}`)).json();
    const data = res.data || {};
    const sum = res.summary || {};
    const title = `${clock(res)} ${sum.label === "beep" || !sum.label ? data.kind || kindOf({ summary: sum }) : sum.label}`;
    const live = new Map(((state && state.devices) || []).map((d) => [d.id, d]));
    const L = state && state.layout;
    const byDev = new Map((data.devices || []).map((d) => [d.device_id, d]));
    // fetch and cut every listener's snippet before playing anything
    const items = [];
    for (const cid of data.clip_ids || []) {
      const r = await api(`/api/clips/${cid}.wav`);
      const dev = r.headers.get("X-Device-Id");
      const start = Number(r.headers.get("X-Start-Frame"));
      const buf = await ac.decodeAudioData(await r.arrayBuffer());
      if (token !== replay.token) return;
      const sr = buf.sampleRate;
      const info = byDev.get(dev) || {};
      const total = buf.length;
      const chirp = info.chirp && info.chirp.frame != null ? info.chirp.frame - start : null;
      // stop short of the sync chirp the system played after the sound
      const limit = chirp != null ? Math.max(0, chirp - 0.06 * sr) : total;
      let from, to;
      if (info.onset_frame != null) {
        from = info.onset_frame - start - 0.3 * sr;
        to = info.onset_frame - start + 0.9 * sr;
      } else {
        from = limit - 1.3 * sr;
        to = limit;
      }
      from = Math.max(0, Math.round(from));
      to = Math.min(Math.round(limit), Math.round(to), total);
      if (to - from < 0.05 * sr) continue;
      const x = buf.getChannelData(0);
      let peak = 1e-6;
      for (let i = from; i < to; i++) peak = Math.max(peak, Math.abs(x[i]));
      const d = live.get(dev);
      items.push({
        buf,
        offset: from / sr,
        dur: (to - from) / sr,
        gain: Math.min(0.7 / peak, 400),
        boost: 20 * Math.log10(Math.min(0.7 / peak, 400)),
        name: (d && d.name) || info.name || (L && L.names && L.names[dev]) || dev,
        color: (d && d.color) || (L && L.colors && L.colors[dev]) || "#888",
        heard: info.onset_frame != null,
        level: info.level_db,
      });
    }
    if (!items.length) throw new Error("no recordings stored for this sound");
    const totalSecs = items.reduce((a, it) => a + it.dur + GAP, 0);
    await ctx.command("pause_listening", { seconds: totalSecs + 1.5 }, 10000);
    if (token !== replay.token) return;
    const show = (k) =>
      replayPanel(`<div class="row" style="justify-content:space-between"><span><b>Replaying ${esc(title)}</b> <span class="muted">· listening for new sounds is paused</span></span><span>${mysteryButton(rid, true)} <button data-act="replay-stop">&#9632; Stop</button></span></div>
        <div class="who"><span class="sw" style="background:${items[k].color}"></span>${esc(items[k].name)}'s recording <span class="muted" style="font-size:0.6em;font-weight:400">${k + 1} of ${items.length}</span></div>
        <ol>${items
          .map(
            (it, i) =>
              `<li class="${i === k ? "now" : i < k ? "done" : ""}"><span class="sw" style="background:${it.color}"></span>${esc(it.name)} <span class="muted">${it.level != null ? `heard it at ${fmt(it.level, 0)} dB` : it.heard ? "" : "no clear onset found"} · played ${fmt(it.boost, 0)} dB louder</span></li>`
          )
          .join("")}</ol>
        <div class="muted" style="margin-top:6px">Each recording is turned up to the same loudness so the quiet ones are audible. The sync chirp that followed is cut off.</div>`);
    for (let k = 0; k < items.length; k++) {
      if (token !== replay.token) return;
      const it = items[k];
      show(k);
      replay.rerender = () => replay.token === token && show(k);
      await new Promise((done) => {
        const src = ac.createBufferSource();
        const g = ac.createGain();
        g.gain.value = it.gain;
        src.buffer = it.buf;
        src.connect(g);
        g.connect(ac.destination);
        src.onended = done;
        replay.src = src;
        src.start(ac.currentTime + 0.05, it.offset, it.dur);
      });
      if (token !== replay.token) return;
      await new Promise((done) => setTimeout(done, GAP * 1000));
    }
    if (token === replay.token) stopReplay("Replay finished");
  }

  // -- mystery beep loudness (warmer / colder) -------------------------------
  // Each listener's level for successive hearings of the mystery beep. The
  // same source at the same distance repeats to about 1 dB, so moving one
  // listener and watching its number change finds the beeper by loudness.
  function mysteryHearings() {
    return Array.from(liveResults.values())
      .filter((r) => r.kind === "locate" && r.summary && r.summary.levels && (r.summary.mystery || isMarked(r.id)) && r.id > curSession().start_id)
      .sort((a, b) => a.id - b.id);
  }
  function beepPeriod(hs) {
    const gaps = [];
    for (let i = 1; i < hs.length; i++) {
      const g = rTime(hs[i]) - rTime(hs[i - 1]);
      if (g > 20 && g < 600) gaps.push(g);
    }
    if (gaps.length < 2) return null;
    gaps.sort((a, b) => a - b);
    return gaps[Math.floor(gaps.length / 2)];
  }
  function renderLoudness() {
    const card = q(".loudcard");
    const nMarked = Object.keys(mys().marked || {}).length;
    card.style.display = nMarked && !viewing ? "" : "none";
    if (!nMarked || viewing) return;
    const hs = mysteryHearings().slice(-8);
    if (!hs.length) {
      setHtml(q(".loud"), `<div class="muted">Waiting for the next mystery beep in this session. Each time it sounds, this shows how loud every listener heard it.</div><div class="small muted next"></div>`);
      return;
    }
    const last = hs[hs.length - 1],
      prev = hs[hs.length - 2];
    const devs = ((state && state.devices) || []).filter((d) => d.role === "listener");
    const rows = devs.map((d) => {
      const cur = last.summary.levels[d.id];
      const was = prev ? prev.summary.levels[d.id] : null;
      const delta = cur != null && was != null ? cur - was : null;
      const dtxt =
        delta == null
          ? ""
          : Math.abs(delta) < 1.5
            ? `<span class="delta muted">same (${delta >= 0 ? "+" : ""}${fmt(delta, 1)})</span>`
            : delta > 0
              ? `<span class="delta" style="color:#dd6b20">&#9650; +${fmt(delta, 1)} dB warmer</span>`
              : `<span class="delta" style="color:#3182ce">&#9660; ${fmt(delta, 1)} dB colder</span>`;
      const pct = cur == null ? 0 : Math.max(2, Math.min(100, ((cur + 70) / 60) * 100));
      const hist = hs.map((h) => (h.summary.levels[d.id] != null ? fmt(h.summary.levels[d.id], 0) : "–")).join(" ");
      return `<tr><td><span class="sw" style="background:${d.color}"></span><b>${esc(d.name)}</b></td>
        <td class="db">${cur != null ? fmt(cur, 1) + " dB" : '<span class="muted" style="font-size:0.6em">not heard</span>'}</td>
        <td>${dtxt}</td>
        <td style="width:30%"><div class="bar"><div style="width:${pct}%;background:${d.color}"></div></div></td>
        <td class="hist muted small" title="This listener's level for the last ${hs.length} hearings, oldest first">${hist}</td></tr>`;
    });
    setHtml(
      q(".loud"),
      `<div class="small">Last heard <b>${clock(last)}</b> <span class="next muted"></span></div>
      <table>${rows.join("")}</table>
      <div class="small muted">Move or hold one listener somewhere new, wait for the next beep, and watch its own number: about 6 dB louder means roughly half the distance. Only compare a listener with itself, since each device's microphone is different. Moving a listener makes the map position wrong until you recalibrate.</div>`
    );
    tickLoudness();
  }
  function tickLoudness() {
    const el = q(".loud .next");
    if (!el) return;
    const hs = mysteryHearings();
    const period = beepPeriod(hs);
    if (!hs.length || !period) {
      el.textContent = "";
      return;
    }
    let due = rTime(hs[hs.length - 1]) + period - Date.now() / 1000;
    let missed = 0;
    while (due < -5 && missed < 50) {
      due += period;
      missed++;
    }
    el.textContent = `· it repeats about every ${Math.round(period)} s · next expected in ~${Math.max(0, Math.round(due))} s${missed ? ` (${missed} since then not caught)` : ""}`;
  }
  ctx.setInterval(tickLoudness, 1000);

  // -- viewing an earlier session (read-only) --------------------------------
  async function viewSession(n) {
    const ss = sessions();
    if (n >= ss.length || n < 1) {
      viewing = null;
      results = liveResults;
      selected = null;
      renderAll();
      return;
    }
    const sx = ss[n - 1],
      next = ss[n];
    q(".sessnote").textContent = "loading…";
    try {
      const rows = (await (await api(`/api/results?since_id=${sx.start_id}&limit=5000`)).json()).results.filter((r) => r.id < next.start_id);
      const prev = next.previous || {};
      let layout = prev.layout || null;
      const cals = rows.filter((r) => r.kind === "calibration");
      const calId = prev.layout_result_id || (cals.length ? cals[cals.length - 1].id : null);
      if (!layout && calId) {
        const full = await (await api(`/api/results/${calId}`)).json();
        layout = full.data && full.data.layout;
        if (layout) layout.result_id = calId;
      }
      viewing = { n, results: new Map(rows.map((r) => [r.id, r])), layout, room: prev.room || null, view: prev.view || null, estimate: prev.mystery_estimate || null, since: 0, ts: sx.ts, end: next.ts };
      results = viewing.results;
      selected = null;
      if (roughMode && !wizardRough) {
        roughMode = false;
        frozen = null;
        q('[data-act="rough"]').checked = false;
      }
    } catch (e) {
      log(`could not load session ${n}: ${e.message}`);
      viewing = null;
      results = liveResults;
    }
    renderAll();
  }
  root.addEventListener("change", (e) => {
    if (e.target.matches("[data-session]")) viewSession(Number(e.target.value));
  });

  // the reposition steps, the session picker and what is disabled when
  function renderSession() {
    const ph = phase();
    // rough mode follows the "place them" step
    if (ph === "placing" && !viewing) {
      if (!roughMode) {
        seedRough();
        if (Object.keys(roughPos).length) {
          freezeView();
          roomEdit = (state.settings && state.settings.room && state.settings.room.slice()) || null;
          roughMode = true;
          wizardRough = true;
        }
      }
    } else if (wizardRough && ph !== "placing") {
      wizardRough = false;
      roughMode = false;
      frozen = null;
      drag = null;
    }
    const busy = !!state.procedure;
    let w = "";
    if (viewing) w = "";
    else if (ph === "repositioning")
      w = `<div class="step"><b class="t">Step 1 of 3 · Move the devices</b>Listening is paused, so nothing you do now is located or recorded as a sound. Leave each listener's page open with its screen on. When every device is in its new place:
        <div class="row"><button class="primary" data-act="repos-done">Repositioned</button><button data-act="repos-cancel" title="Carry on with the same session: only if nothing was moved">Cancel, nothing moved</button></div></div>`;
    else if (ph === "placing")
      w = busy
        ? `<div class="step"><b class="t">Step 3 of 3 · Measuring</b>The listeners are chirping to measure their exact positions…</div>`
        : `<div class="step"><b class="t">Step 2 of 3 · Roughly place them</b>Session ${curSession().n} has started. On the map, drag each dot to roughly where that listener is now (Identify makes one chirp so you can tell which is which).${roomEdit ? " The walls are where you left them." : ""}
        <div class="row"><button class="primary" data-act="repos-finish">Done, measure positions</button></div></div>`;
    else w = `<div class="row"><button data-act="repos-start" ${busy ? "disabled" : ""} title="Pause, move the devices, then start a new session. The listeners stay connected.">Reposition the devices</button></div>`;
    setHtml(q(".wizard"), w);
    // session picker
    const ss = sessions();
    const span = (x, i) => `${hms(x.ts)}${i < ss.length - 1 ? "–" + hms(ss[i + 1].ts) : " onwards"}`;
    setHtml(
      q("[data-session]"),
      ss
        .map((x, i) => `<option value="${x.n}">${x.n}${i === ss.length - 1 ? " (live)" : ""} · ${span(x, i)}</option>`)
        .reverse()
        .join("")
    );
    q("[data-session]").value = String(viewing ? viewing.n : curSession().n);
    q(".sessnote").textContent = ss.length > 1 ? "earlier sessions can be viewed read-only" : "a new session starts when you reposition the devices";
    const vb = q(".viewbar");
    vb.classList.toggle("on", !!viewing);
    setHtml(vb, viewing ? `<span><b>Viewing session ${viewing.n}</b> (${hms(viewing.ts)}–${hms(viewing.end)}) · read-only: the map, sounds and replays are as they were. The live session is still running.</span><button class="primary" data-act="view-live">Back to live</button>` : "");
    // nothing that changes the live session while viewing or mid-reposition
    const lock = !!viewing || ph !== "running";
    for (const sel of ['[data-act="calibrate"]', '[data-act="rough"]', '[data-act="testbeep"]', '[data-act="locate"]', '[data-act="monitoring"]', '[data-act="rot-"]', '[data-act="rot+"]', '[data-act="flip"]']) {
      const el = q(sel);
      if (el) el.disabled = lock || (busy && sel === '[data-act="calibrate"]');
    }
    q('[data-act="rough"]').checked = roughMode && !wizardRough;
    // what the system knows about the mystery beep
    const m = mys();
    const nMarked = Object.keys(m.marked || {}).length;
    setHtml(
      q(".mysinfo"),
      nMarked
        ? `<span style="color:${MYS}">&#9678; Mystery beep</span> <span>${m.pitch ? `${Math.round(m.pitch)} Hz · ` : ""}${nMarked} confirmed · remembered across sessions</span> <button data-act="mystery-clear" ${viewing ? "disabled" : ""} title="Forget what it sounds like">Forget this sound</button>`
        : `<span class="muted">&#9678; No mystery beep marked yet: replay a located sound and click "This is the mystery beep".</span>`
    );
  }

  // -- rough mode ----------------------------------------------------------
  const listenerIds = () => ((state && state.devices) || []).filter((d) => d.role === "listener").map((d) => d.id);
  function seedRough() {
    const L = state && state.layout;
    const saved = (state && state.settings && state.settings.rough_positions) || {};
    const ids = listenerIds();
    const placed = {};
    for (const id of ids) {
      const hand = L && (L.unlocated || []).includes(id);
      if (L && L.positions_by_id && L.positions_by_id[id] && !hand) placed[id] = viewTf(L.positions_by_id[id]);
      else if (saved[id]) placed[id] = saved[id].slice();
    }
    // anything never placed starts in a row above the others
    const ys = Object.values(placed).map((p) => p[1]);
    const xs = Object.values(placed).map((p) => p[0]);
    const top = ys.length ? Math.min(...ys) - 1 : 0;
    let x = xs.length ? Math.min(...xs) : -(ids.length - 1) / 2;
    for (const id of ids)
      if (!placed[id]) {
        placed[id] = [x, top];
        x += 1;
      }
    roughPos = placed;
  }
  function freezeView() {
    const pts = Object.values(roughPos);
    const W = 800,
      H = 600,
      pad = 50;
    let x0 = Math.min(...pts.map((p) => p[0])) - 2,
      x1 = Math.max(...pts.map((p) => p[0])) + 2;
    let y0 = Math.min(...pts.map((p) => p[1])) - 2,
      y1 = Math.max(...pts.map((p) => p[1])) + 2;
    // at least 8m x 6m, centred
    const gx = Math.max(0, 8 - (x1 - x0)) / 2,
      gy = Math.max(0, 6 - (y1 - y0)) / 2;
    x0 -= gx;
    x1 += gx;
    y0 -= gy;
    y1 += gy;
    const s = Math.min((W - 2 * pad) / (x1 - x0), (H - 2 * pad) / (y1 - y0));
    frozen = { x0, x1, y0, y1, s, ox: pad + (W - 2 * pad - s * (x1 - x0)) / 2, oy: pad + (H - 2 * pad - s * (y1 - y0)) / 2 };
  }
  function zoomRough(f) {
    if (!frozen) return;
    const cx = (frozen.x0 + frozen.x1) / 2,
      cy = (frozen.y0 + frozen.y1) / 2;
    const hx = ((frozen.x1 - frozen.x0) / 2) * f,
      hy = ((frozen.y1 - frozen.y0) / 2) * f;
    const W = 800,
      H = 600,
      pad = 50;
    const s = Math.min((W - 2 * pad) / (2 * hx), (H - 2 * pad) / (2 * hy));
    frozen = { x0: cx - hx, x1: cx + hx, y0: cy - hy, y1: cy + hy, s, ox: pad + (W - 2 * pad - s * 2 * hx) / 2, oy: pad + (H - 2 * pad - s * 2 * hy) / 2 };
    draw();
  }
  function setRough(on) {
    if (!state) return;
    if (on) {
      seedRough();
      if (!Object.keys(roughPos).length) {
        q('[data-act="rough"]').checked = false;
        q(".progress").textContent = "Connect some listeners first.";
        return;
      }
      freezeView();
      roomEdit = (state.settings && state.settings.room && state.settings.room.slice()) || null;
      roughMode = true;
      draw();
      return;
    }
    roughMode = false;
    frozen = null;
    drag = null;
    const positions = roughPos;
    draw();
    if (Object.keys(positions).length < 2) return;
    cmd("rough", { positions })
      .then(() => cmd("calibrate"))
      .catch(() => {});
  }
  q('[data-act="rough"]').addEventListener("change", (e) => setRough(e.target.checked));
  q(".since input").addEventListener("input", (e) => {
    const all = allLocates();
    const v = Number(e.target.value);
    // far right: nothing so far, only sounds from now on
    setSince(v >= all.length ? Date.now() / 1000 : v <= 0 ? 0 : rTime(all[v]));
  });
  function svgPoint(e) {
    const r = svg.getBoundingClientRect();
    return [((e.clientX - r.left) / r.width) * 800, ((e.clientY - r.top) / r.height) * 600];
  }
  svg.addEventListener("pointerdown", (e) => {
    if (!roughMode || !frozen) return;
    const g = e.target.closest("[data-dev]");
    if (!g) return;
    const id = g.dataset.dev;
    const p = svgPoint(e);
    const cur = id.startsWith("wall:") ? [roomEdit[Number(id.slice(5))], roomEdit[Number(id.slice(5)) + 1]] : roughPos[id];
    drag = { id, dx: frozen.ox + (cur[0] - frozen.x0) * frozen.s - p[0], dy: frozen.oy + (cur[1] - frozen.y0) * frozen.s - p[1] };
    try {
      svg.setPointerCapture(e.pointerId);
    } catch (err) {}
    svg.classList.add("dragging");
    e.preventDefault();
  });
  svg.addEventListener("pointermove", (e) => {
    if (!drag || !frozen) return;
    const p = svgPoint(e);
    const sx = Math.max(12, Math.min(788, p[0] + drag.dx)),
      sy = Math.max(12, Math.min(588, p[1] + drag.dy));
    const w = [(sx - frozen.ox) / frozen.s + frozen.x0, (sy - frozen.oy) / frozen.s + frozen.y0];
    if (drag.id.startsWith("wall:")) {
      const k = Number(drag.id.slice(5));
      roomEdit[k] = w[0];
      roomEdit[k + 1] = w[1];
    } else roughPos[drag.id] = w;
    draw();
  });
  const endDrag = () => {
    if (!drag) return;
    const wasWall = drag.id.startsWith("wall:");
    drag = null;
    svg.classList.remove("dragging");
    if (wasWall) {
      ctx.command("room", { room: roomEdit }, 10000).catch(() => {});
      return;
    }
    // keep the server's copy current so a reload doesn't lose the placement
    ctx.command("settings", { rough_positions: roughPos }, 10000).catch(() => {});
  };
  svg.addEventListener("pointerup", endDrag);
  svg.addEventListener("pointercancel", endDrag);
  root.addEventListener("dblclick", (e) => {
    const n = e.target.closest(".dev-name");
    if (!n) return;
    const name = prompt("Name for this device", n.textContent);
    if (name) cmd("rename", { device: n.dataset.dev, name }).catch(() => {});
  });

  // -- view transform ------------------------------------------------------
  function viewTf(p) {
    const v = VIEW();
    let x = p[0],
      y = p[1];
    if (v.flip) x = -x;
    const a = ((v.rotation || 0) * Math.PI) / 180;
    return [Math.cos(a) * x - Math.sin(a) * y, Math.sin(a) * x + Math.cos(a) * y];
  }

  // tonal beep or broadband clap? (older results have no kind: guess from pitch)
  const kindOf = (r) => {
    const s = r.summary || {};
    if (s.label && s.label.startsWith("testbeep")) return "test";
    return s.kind || (s.freq ? "beep" : "clap");
  };
  // HH:MM:SS the sound happened (falls back to when the result was stored)
  const clock = (r) => new Date(((r.summary && r.summary.t) || (r.data && r.data.t_beep) || r.ts) * 1000).toLocaleTimeString("en-GB", { hour12: false });
  const KIND_COLOR = { beep: "#e53e3e", clap: "#0d9488", test: "#805ad5" };

  // -- "show sounds from" filter ---------------------------------------------
  // sinceTs: null = the latest 25 (default), otherwise only sounds at or after
  // that time are drawn. Nothing is deleted: slide back to see older ones.
  let sinceTs = null;
  try {
    const v = localStorage.getItem("beep.since");
    if (v != null && v !== "") sinceTs = Number(v);
  } catch (e) {}
  const rTime = (r) => (r.summary && r.summary.t) || (r.data && r.data.t_beep) || r.ts;
  function allLocates() {
    return Array.from(results.values())
      .filter((r) => r.kind === "locate" && r.summary && r.summary.x != null && inScope(r))
      .sort((a, b) => a.id - b.id);
  }
  function latestLocates(n = 25) {
    const all = allLocates();
    if (viewing) return all.filter((r) => rTime(r) >= viewing.since);
    return sinceTs == null ? all.slice(-n) : all.filter((r) => rTime(r) >= sinceTs);
  }
  function setSince(ts) {
    if (viewing) {
      viewing.since = ts || 0;
      selected = null;
      draw();
      return;
    }
    sinceTs = ts;
    try {
      if (ts == null) localStorage.removeItem("beep.since");
      else localStorage.setItem("beep.since", String(ts));
    } catch (e) {}
    selected = null;
    draw();
  }
  const sinceTsLive = () => sinceTs;
  function updateSince() {
    const all = allLocates();
    const slider = q(".since input");
    const shown = latestLocates().length;
    slider.max = all.length;
    const sinceTs = viewing ? viewing.since : sinceTsLive();
    slider.value = sinceTs == null ? Math.max(0, all.length - 25) : all.filter((r) => rTime(r) < sinceTs).length;
    slider.disabled = !all.length;
    q(".since-label").textContent = !all.length
      ? "no sounds located yet"
      : sinceTs == null
        ? `latest ${shown} of ${all.length} sounds`
        : shown === all.length
          ? `all ${all.length} sounds`
          : shown
            ? `from ${hms(rTime(latestLocates()[0]))} onwards · ${shown} of ${all.length} sounds`
            : `cleared at ${hms(sinceTs)} · ${all.length} older sound${all.length === 1 ? "" : "s"} hidden · new ones will appear`;
  }

  function draw() {
    if (!state) return;
    const L = LAY();
    const W = 800,
      H = 600,
      pad = 50;
    let parts = [];
    updateSince();
    svg.classList.toggle("rough", roughMode);
    q(".legend").innerHTML = roughMode
      ? `<b>Rough mode.</b> Drag each dot to roughly where that listener is: each square is 1 m. Use Identify if you're not sure which is which. <button data-act="zoom-">Zoom out</button> <button data-act="zoom+">Zoom in</button> ${roomEdit ? '<button data-act="walls-remove">Remove walls</button> Drag the two square handles to the room\'s corners.' : '<button data-act="walls-add">Add walls</button>'} ${wizardRough ? "Click <b>Done, measure positions</b> when they are all placed." : "Untick rough mode when done and the listeners will chirp to measure their exact positions."}`
      : L && L.rough
        ? "These positions are your rough placement, not yet measured. Press Calibrate to measure."
        : `Dots are listeners. <span style="color:${KIND_COLOR.beep}">&#10005;</span> beep, <span style="color:${KIND_COLOR.clap}">&#9671;</span> clap, <span style="color:${KIND_COLOR.test}">&#10005;</span> test beep (newest is largest). <span style="color:${MYS}">&#9678;</span> the mystery beep: ringed symbols are hearings of it, the bullseye is where they jointly point. <span style="color:#2563eb">&#8853;</span> a suggested place for one more listener. Shading shows where the latest sound could plausibly be.`;
    if (roughMode) {
      // listeners that joined after rough mode started
      for (const id of listenerIds())
        if (!roughPos[id]) {
          const ys = Object.values(roughPos).map((p) => p[1]);
          roughPos[id] = [frozen ? (frozen.x0 + frozen.x1) / 2 : 0, ys.length ? Math.min(...ys) - 0.7 : 0];
        }
    }
    if (!L && !roughMode) {
      svg.innerHTML = viewing
        ? `<text x="400" y="300" text-anchor="middle" fill="currentColor" opacity="0.6" font-size="20">Session ${viewing.n} was never calibrated</text>`
        : phase() === "repositioning"
          ? `<text x="400" y="300" text-anchor="middle" fill="currentColor" opacity="0.6" font-size="20">Paused: move the devices, then click Repositioned</text>`
          : `<text x="400" y="300" text-anchor="middle" fill="currentColor" opacity="0.6" font-size="20">Connect 3+ listeners, then press Calibrate</text><text x="400" y="330" text-anchor="middle" fill="currentColor" opacity="0.45" font-size="15">or tick Rough mode to place them by hand first</text>`;
      return;
    }
    const ids = roughMode ? Object.keys(roughPos) : L.device_ids;
    const pos = roughMode ? roughPos : L.positions_by_id;
    const pts = ids.map((id) => (roughMode ? pos[id] : viewTf(pos[id])));
    const locs = roughMode ? [] : latestLocates();
    const est = locs.map((r) => viewTf([r.summary.x, r.summary.y]));
    let truthPts = null;
    if (simTruth && !roughMode) {
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
    let s = Math.min((W - 2 * pad) / (x1 - x0), (H - 2 * pad) / (y1 - y0));
    let ox = pad + ((W - 2 * pad) - s * (x1 - x0)) / 2;
    let oy = pad + ((H - 2 * pad) - s * (y1 - y0)) / 2;
    if (roughMode && frozen) ({ x0, x1, y0, y1, s, ox, oy } = frozen);
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
    const gridOp = roughMode ? 0.16 : 0.07;
    const gx0 = x0 - ox / s,
      gx1 = x0 + (W - ox) / s,
      gy0 = y0 - oy / s,
      gy1 = y0 + (H - oy) / s;
    for (let gx = Math.ceil(gx0); gx <= gx1; gx++) parts.push(`<line x1="${X([gx, 0])}" y1="0" x2="${X([gx, 0])}" y2="${H}" stroke="currentColor" stroke-opacity="${gridOp}"/>`);
    for (let gy = Math.ceil(gy0); gy <= gy1; gy++) parts.push(`<line x1="0" y1="${Y([0, gy])}" x2="${W}" y2="${Y([0, gy])}" stroke="currentColor" stroke-opacity="${gridOp}"/>`);
    parts.push(`<line x1="20" y1="${H - 20}" x2="${20 + s}" y2="${H - 20}" stroke="currentColor" stroke-width="3"/><text x="${24 + s}" y="${H - 16}" font-size="13" fill="currentColor">1 m</text>`);
    // truth (simulation only)
    if (truthPts) {
      for (const [id, p] of Object.entries(truthPts.devices)) parts.push(`<circle cx="${X(p)}" cy="${Y(p)}" r="13" fill="none" stroke="currentColor" stroke-opacity="0.35" stroke-dasharray="3 3"/>`);
      if (truthPts.beeper) parts.push(`<rect x="${X(truthPts.beeper) - 9}" y="${Y(truthPts.beeper) - 9}" width="18" height="18" fill="none" stroke="currentColor" stroke-width="2" stroke-dasharray="4 3"/><text x="${X(truthPts.beeper) + 12}" y="${Y(truthPts.beeper) + 22}" font-size="12" fill="currentColor" opacity="0.6">true beeper (sim)</text>`);
    }
    // walls, if drawn
    const room = roughMode ? roomEdit : ROOM();
    if (room) {
      const cs = [
        [room[0], room[1]],
        [room[2], room[1]],
        [room[2], room[3]],
        [room[0], room[3]],
      ].map((p) => (roughMode ? p : viewTf(p)));
      parts.push(`<polygon points="${cs.map((p) => `${X(p).toFixed(1)},${Y(p).toFixed(1)}`).join(" ")}" fill="none" stroke="currentColor" stroke-opacity="0.55" stroke-width="4" stroke-linejoin="round"/>`);
      if (roughMode)
        [0, 2].forEach((k) => {
          const p = cs[k];
          parts.push(`<g class="dev" data-dev="wall:${k}"><rect x="${X(p) - 11}" y="${Y(p) - 11}" width="22" height="22" rx="4" fill="var(--card)" stroke="currentColor" stroke-width="3"/><text x="${X(p) + 16}" y="${Y(p) - 14}" font-size="12" fill="currentColor" opacity="0.7">drag: wall corner</text></g>`);
        });
    }
    // where all hearings of the mystery beep, taken together, put it
    const pooled = !roughMode && (viewing ? viewing.estimate : mys().estimate);
    if (pooled) {
      const p = viewTf([pooled.x, pooled.y]);
      const rr = Math.max(14, pooled.radius_m * s);
      parts.push(`<circle cx="${X(p)}" cy="${Y(p)}" r="${rr}" fill="${MYS}" fill-opacity="0.12" stroke="${MYS}" stroke-opacity="0.6" stroke-dasharray="5 4"/>`);
      parts.push(`<circle cx="${X(p)}" cy="${Y(p)}" r="10" fill="none" stroke="${MYS}" stroke-width="3"/><circle cx="${X(p)}" cy="${Y(p)}" r="3.5" fill="${MYS}"/>`);
      parts.push(`<text x="${X(p) + 15}" y="${Y(p) - 12}" font-size="14" font-weight="700" fill="${MYS}" stroke="var(--card)" stroke-width="3" paint-order="stroke">mystery beep · ${pooled.n} hearing${pooled.n === 1 ? "" : "s"} · within ~${pooled.radius_m >= 1 ? fmt(pooled.radius_m, 1) + " m" : Math.round(pooled.radius_m * 100) + " cm"}${pooled.at_edge ? " · may be further out" : ""}</text>`);
    }
    // where one more listener would help most
    const sug = !roughMode && !viewing && state.settings && state.settings.suggestion;
    if (sug && sug.gain >= 0.05) {
      const p = viewTf([sug.x, sug.y]);
      const SUG = "#2563eb";
      const x = X(p),
        y = Y(p);
      const why = sug.basis === "mystery" ? `could tighten the mystery beep's position by ~${Math.round(sug.gain * 100)}%` : `would improve accuracy across the room by ~${Math.round(sug.gain * 100)}%`;
      parts.push(`<g class="suggest"><title>Suggested place for one more listener. ${sug.basis === "mystery" ? `Worked out from where the ${sug.hearings} hearings of the mystery beep allow it to be and the directions the current listeners already cover.` : "Worked out from the directions the current listeners already cover."}</title>
        <circle cx="${x}" cy="${y}" r="13" fill="${SUG}" fill-opacity="0.1" stroke="${SUG}" stroke-width="2.5" stroke-dasharray="5 4"/>
        <line x1="${x - 6}" y1="${y}" x2="${x + 6}" y2="${y}" stroke="${SUG}" stroke-width="2.5"/><line x1="${x}" y1="${y - 6}" x2="${x}" y2="${y + 6}" stroke="${SUG}" stroke-width="2.5"/>
        <text x="${x + 18}" y="${y + 4}" font-size="13" font-weight="600" fill="${SUG}" stroke="var(--card)" stroke-width="3" paint-order="stroke">add a listener here?</text>
        <text x="${x + 18}" y="${y + 19}" font-size="11" fill="${SUG}" stroke="var(--card)" stroke-width="3" paint-order="stroke">${why}</text></g>`);
    }
    // estimates
    est.forEach((p, k) => {
      const r = locs[k];
      const latest = k === est.length - 1;
      const sz = latest ? 11 : 6;
      const kind = kindOf(r);
      const col = KIND_COLOR[kind];
      const op = latest ? 1 : 0.35 + (0.5 * k) / Math.max(1, est.length);
      const sw = latest ? 4 : 2.5;
      const x = X(p),
        y = Y(p);
      // beeps are crosses; claps are open diamonds with a centre dot
      const mark =
        kind === "clap"
          ? `<polygon points="${x},${y - sz * 1.3} ${x + sz * 1.3},${y} ${x},${y + sz * 1.3} ${x - sz * 1.3},${y}" fill="none" stroke="${col}" stroke-width="${sw - 1}"/><circle cx="${x}" cy="${y}" r="${latest ? 3 : 2}" fill="${col}"/>`
          : `<line x1="${x - sz}" y1="${y - sz}" x2="${x + sz}" y2="${y + sz}" stroke="${col}" stroke-width="${sw}"/><line x1="${x - sz}" y1="${y + sz}" x2="${x + sz}" y2="${y - sz}" stroke="${col}" stroke-width="${sw}"/>`;
      const when = clock(r);
      const label = `<text x="${x + sz * 1.3 + 4}" y="${y + 4}" font-size="${latest ? 13 : 11}" font-weight="${latest ? 600 : 400}" fill="${col}" stroke="var(--card)" stroke-width="3" paint-order="stroke" style="font-variant-numeric: tabular-nums">${when}</text>`;
      const ring = isMarked(r.id) || isLikely(r.id) ? `<circle cx="${x}" cy="${y}" r="${sz * 1.9}" fill="none" stroke="${MYS}" stroke-width="2" ${isMarked(r.id) ? "" : 'stroke-dasharray="3 3"'}/>` : "";
      parts.push(`<g opacity="${op.toFixed(2)}" class="est" data-id="${r.id}">${ring}${mark}${label}</g>`);
    });
    // devices
    const live = new Map((state.devices || []).map((d) => [d.id, d]));
    ids.forEach((id, k) => {
      const p = pts[k];
      const d = live.get(id);
      const color = (d && d.color) || (L && L.colors && L.colors[id]) || "#888";
      const name = (d && d.name) || (L && L.names && L.names[id]) || id;
      const on = d && d.status && d.status.running;
      // not measured: placed by hand (rough mode, or nothing could range to it)
      const hand = roughMode || (L && (L.rough || (L.unlocated || []).includes(id)));
      const r = roughMode ? 16 : 11;
      parts.push(`<g class="dev" data-dev="${esc(id)}">`);
      if (roughMode) parts.push(`<circle cx="${X(p)}" cy="${Y(p)}" r="26" fill="${color}" fill-opacity="0.12"/>`);
      parts.push(`<circle cx="${X(p)}" cy="${Y(p)}" r="${r}" fill="${color}" fill-opacity="${on ? (hand && !roughMode ? 0.45 : 1) : 0.3}" stroke="${hand && !roughMode ? color : "white"}" stroke-width="2" ${hand && !roughMode ? 'stroke-dasharray="4 3"' : ""}/>`);
      parts.push(`<text x="${X(p) + r + 4}" y="${Y(p) + 5}" font-size="15" font-weight="600" fill="currentColor">${esc(name)}${hand && !roughMode ? ' <tspan font-weight="400" opacity="0.6">(placed by hand)</tspan>' : ""}</text>`);
      parts.push(`</g>`);
    });
    svg.innerHTML = parts.join("");
  }

  function renderDevices() {
    if (!state) return;
    const devs = (state.devices || []).filter((d) => d.role === "listener");
    const L = state.layout;
    const latest = allLocates().slice(-1)[0];
    const levels = {};
    if (latest && latest.data) for (const d of latest.data.devices || []) if (d.level_db != null) levels[d.device_id] = d.level_db;
    const rows = devs.map((d) => {
      const st = d.status || {};
      const lv = st.level ? `${fmt(st.level.rms, 0)} dB` : "";
      const flags = [];
      const rms = st.level && st.level.rms;
      const mic = !st.running
        ? '<span class="bad" title="The page is not capturing audio: reload it on the device and tap Start listening">&#10007; mic off</span>'
        : rms != null && rms <= -100
          ? '<span class="bad" title="The microphone is open but delivering pure silence (muted by the device?)">&#10007; mic silent</span>'
          : '<span style="color:var(--ok)">&#10003; mic working</span>';
      const sp = (state.speakers || {})[d.id];
      const spk = !sp
        ? '<span class="muted" title="Run Calibrate to check">? speaker not tested</span>'
        : sp.ok
          ? `<span style="color:var(--ok)" title="Its chirp was heard (${esc(sp.how)}, ${hms(sp.ts)})">&#10003; speaker working</span>`
          : `<span class="bad" title="Nothing heard its chirp (${esc(sp.how)}, ${hms(sp.ts)}). Muted, volume down, or playing through headphones/Bluetooth?">&#10007; speaker not heard</span>`;
      if (st.visibility === "hidden") flags.push('<span class="warn">tab hidden</span>');
      if (!d.sim && st.running && !st.wakeLock) flags.push('<span class="warn">may sleep</span>');
      if (st.gaps) flags.push(`<span class="warn">${st.gaps} gaps</span>`);
      if (st.lastError) flags.push(`<span class="bad" title="${esc(st.lastError)}">error</span>`);
      if (d.sim) flags.push('<span class="pill">sim</span>');
      const placed = L && L.positions_by_id[d.id] ? "" : '<span class="muted">not calibrated</span>';
      const heard = levels[d.id] != null ? `last beep ${fmt(levels[d.id], 0)} dB` : "";
      return `<tr><td><span class="sw" style="background:${d.color}"></span><span class="dev-name" data-dev="${esc(d.id)}" title="double-click to rename">${esc(d.name)}</span>
        <div class="small muted">${esc(st.sampleRate || "")}${st.sampleRate ? " Hz" : ""} ${lv} ${d.clock && d.clock.rtt_ms != null ? `· ${d.clock.rtt_ms} ms rtt` : ""}</div>
        <div class="small">${mic} · ${spk}</div>
        <div class="small">${flags.join(" ")} ${placed} ${heard}</div></td>
        <td style="text-align:right; white-space:nowrap"><button class="small" data-act="rename" data-dev="${esc(d.id)}" data-name="${esc(d.name)}" title="Give this device your own name, e.g. where it is">Rename</button> <button class="small" data-act="identify" data-dev="${esc(d.id)}">Identify</button></td></tr>`;
    });
    q(".devices").innerHTML = devs.length ? `<table>${rows.join("")}</table>` : '<div class="muted">No listeners yet. Scan the QR code with a phone.</div>';
    const sel = q("[data-test]");
    const cur = sel.value;
    sel.innerHTML = devs.map((d) => `<option value="${esc(d.id)}">${esc(d.name)}</option>`).join("");
    if (cur) sel.value = cur;
  }

  function renderResults() {
    const locs = Array.from(results.values())
      .filter((r) => (r.kind === "locate" || r.kind === "calibration") && inScope(r))
      .sort((a, b) => b.id - a.id)
      .slice(0, 30);
    const rows = locs.map((r) => {
      const s = r.summary || {};
      if (r.kind === "calibration")
        return `<tr class="muted"><td style="font-variant-numeric: tabular-nums; white-space: nowrap">${hms(r.ts)}</td><td colspan="4">calibrated ${s.devices} devices · ${s.pairs_measured}/${s.pairs_total} distances · fit ${fmt((s.rms_residual_m || 0) * 100, 1)} cm${s.from_rough ? " · from rough placement" : ""}${(s.excluded_pairs || []).length ? ` · <span class="warn">${s.excluded_pairs.length} distance(s) ignored</span>` : ""}${(s.unlocated || []).length ? ` · <span class="bad">couldn't range to ${esc(s.unlocated.join(", "))}</span>` : ""}</td></tr>`;
      const where = s.x != null ? `(${fmt(s.x)}, ${fmt(s.y)}) m` : `<span class="muted">${esc(s.error || "no fix")}</span>`;
      const err = s.error_m != null ? ` · <b>${fmt(s.error_m * 100, 0)} cm</b> from source` : "";
      return `<tr class="res ${selected === r.id ? "sel" : ""}" data-id="${r.id}"><td style="font-variant-numeric: tabular-nums; white-space: nowrap"><button class="playbtn" data-act="replay" data-id="${r.id}" title="Replay what each listener recorded">${replay.rid === r.id ? "&#9632;" : "&#9654;"}</button> ${clock(r)}</td><td><span style="color:${KIND_COLOR[kindOf(r)]}">${kindOf(r) === "clap" ? "&#9671;" : "&#10005;"}</span> ${esc(s.label === "beep" ? kindOf(r) : s.label)}${s.freq && kindOf(r) !== "clap" ? ` <span class="muted">${fmt(s.freq, 0)} Hz</span>` : ""}</td>
        <td>${where}${s.rms_residual_m != null ? ` <span class="muted">±${fmt(s.rms_residual_m * 100, 0)} cm fit</span>` : ""}${err}</td>
        <td>${s.used || 0} devs${s.loudest_name ? ` · loudest ${esc(s.loudest_name)}` : ""}</td>
        <td style="text-align:right; white-space:nowrap">${mysteryButton(r.id, false)}</td></tr>`;
    });
    // only touch the DOM when something changed, so a click on a play
    // button isn't lost to a re-render between press and release
    setHtml(q(".results"), rows.length ? `<table>${rows.join("")}</table>` : '<div class="muted">Nothing yet.</div>');
  }
  q(".results").addEventListener("click", (e) => {
    const tr = e.target.closest(".res");
    if (!tr || e.target.closest("[data-act]")) return;
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
    renderSession();
    if (state.procedure) q(".progress").textContent = `running: ${state.procedure}...`;
    renderDevices();
    renderResults();
    renderLoudness();
    draw();
  }

  const onState = (m) => {
    state = m.state;
    renderAll();
  };
  ctx.on("state", onState);
  const onHistory = (m) => {
    for (const r of m.results || []) liveResults.set(r.id, r);
    for (const e of m.events || []) {
      if (e.topic === "progress") log(e.data.message, { ts: e.ts, rid: e.data.result_id });
      if (e.topic === "sim_truth") simTruth = e.data;
    }
    renderAll();
  };
  ctx.on("history", onHistory);
  // catch up on anything that arrived before this module was installed
  if (rt.last.history) onHistory(rt.last.history);
  if (rt.last.state) onState(rt.last.state);
  ctx.on("result", (m) => {
    liveResults.set(m.id, { id: m.id, ts: Date.now() / 1000, kind: m.kind, summary: m.summary, data: m.data });
    if (!viewing) selected = null;
    renderLoudness();
    renderResults();
    renderDevices();
    draw();
  });
  ctx.on("progress", (m) => {
    q(".progress").textContent = m.message;
    log(m.message, { rid: m.result_id });
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
