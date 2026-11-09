// Onset detector: watches the microphone for sudden, tonal sounds (beeps)
// and reports them to the server as "onset" events. The server decides
// whether enough devices heard it and then runs the precise analysis on
// uploaded audio, so this only needs to be roughly right.
//
// Hot-swappable: push a new version with
//   beep-locator ctl push listener detector path/to/detector.js

function fft(re, im) {
  const n = re.length;
  for (let i = 1, j = 0; i < n; i++) {
    let bit = n >> 1;
    for (; j & bit; bit >>= 1) j ^= bit;
    j ^= bit;
    if (i < j) {
      [re[i], re[j]] = [re[j], re[i]];
      [im[i], im[j]] = [im[j], im[i]];
    }
  }
  for (let len = 2; len <= n; len <<= 1) {
    const ang = (-2 * Math.PI) / len;
    const wlr = Math.cos(ang),
      wli = Math.sin(ang);
    const half = len >> 1;
    for (let i = 0; i < n; i += len) {
      let wr = 1,
        wi = 0;
      for (let j = 0; j < half; j++) {
        const a = i + j,
          b = a + half;
        const vr = re[b] * wr - im[b] * wi;
        const vi = re[b] * wi + im[b] * wr;
        re[b] = re[a] - vr;
        im[b] = im[a] - vi;
        re[a] += vr;
        im[a] += vi;
        const t = wr * wlr - wi * wli;
        wi = wr * wli + wi * wlr;
        wr = t;
      }
    }
  }
}

function highpass(fc, sr) {
  const w0 = (2 * Math.PI * fc) / sr;
  const cw = Math.cos(w0);
  const alpha = Math.sin(w0) / (2 * Math.SQRT1_2);
  const a0 = 1 + alpha;
  return {
    b0: (1 + cw) / 2 / a0,
    b1: -(1 + cw) / a0,
    b2: (1 + cw) / 2 / a0,
    a1: (-2 * cw) / a0,
    a2: (1 - alpha) / a0,
    x1: 0,
    x2: 0,
    y1: 0,
    y2: 0,
  };
}

function percentile(arr, p) {
  const s = Array.from(arr).sort((a, b) => a - b);
  return s[Math.min(s.length - 1, Math.floor(p * s.length))];
}

export default function setup(ctx) {
  const cfg = () => Object.assign({ threshold_db: 12, min_prominence: 6, band_lo: 800, band_hi: 10000 }, ctx.settings.detector || {});
  let sr = null;
  let hp = null;
  const HOP = 512;
  let hopBuf = new Float32Array(HOP);
  let hopN = 0;
  let hopFrame = null;
  const hist = [];
  let floor = null;
  let lastTrigger = -Infinity;
  const pending = [];
  let count = 0;
  let last = null;
  let bandDb = -100;

  function init() {
    sr = ctx.sampleRate;
    hp = highpass(cfg().band_lo, sr);
  }

  function processHop(frame, x) {
    // band-limited energy
    let e = 0;
    const f = hp;
    for (let i = 0; i < x.length; i++) {
      const y = f.b0 * x[i] + f.b1 * f.x1 + f.b2 * f.x2 - f.a1 * f.y1 - f.a2 * f.y2;
      f.x2 = f.x1;
      f.x1 = x[i];
      f.y2 = f.y1;
      f.y1 = y;
      e += y * y;
    }
    const db = 10 * Math.log10(e / x.length + 1e-12);
    bandDb = db;
    hist.push(db);
    if (hist.length > 400) hist.shift();
    if (hist.length % 8 === 0 || floor === null) floor = hist.length > 40 ? percentile(hist, 0.3) : null;
    const c = cfg();
    const t = frame / sr;
    if (floor !== null && db > floor + c.threshold_db && t - lastTrigger > 1.0) {
      lastTrigger = t;
      pending.push({ frame, db, floor });
    }
  }

  function analysePending() {
    const end = ctx.frame();
    while (pending.length && end >= pending[0].frame + 2048 + HOP) {
      const p = pending.shift();
      const N = 2048;
      const s = ctx.samples(p.frame - 256, p.frame - 256 + N);
      const re = new Float64Array(N),
        im = new Float64Array(N);
      for (let i = 0; i < N; i++) re[i] = s[i] * (0.5 - 0.5 * Math.cos((2 * Math.PI * i) / (N - 1)));
      fft(re, im);
      const c = cfg();
      const lo = Math.max(1, Math.floor((c.band_lo * N) / sr));
      const hi = Math.min(N / 2 - 1, Math.ceil((c.band_hi * N) / sr));
      const mags = [];
      let best = lo,
        bestMag = 0;
      for (let k = lo; k <= hi; k++) {
        const m = Math.hypot(re[k], im[k]);
        mags.push(m);
        if (m > bestMag) {
          bestMag = m;
          best = k;
        }
      }
      const med = percentile(mags, 0.5) + 1e-12;
      const prominence = bestMag / med;
      // parabolic interpolation of the peak in log magnitude
      let freq = (best * sr) / N;
      if (best > lo && best < hi) {
        const a = Math.log(Math.hypot(re[best - 1], im[best - 1]) + 1e-12);
        const b = Math.log(bestMag + 1e-12);
        const cc = Math.log(Math.hypot(re[best + 1], im[best + 1]) + 1e-12);
        const d = a - 2 * b + cc;
        if (d !== 0) freq = ((best + (0.5 * (a - cc)) / d) * sr) / N;
      }
      const ev = {
        frame: p.frame,
        db: +p.db.toFixed(1),
        floor_db: +p.floor.toFixed(1),
        snr_db: +(p.db - p.floor).toFixed(1),
        freq: Math.round(freq),
        prominence: +prominence.toFixed(1),
      };
      count++;
      last = ev;
      ctx.send("onset", ev);
      render();
    }
  }

  function onAudio(frame, samples) {
    if (!sr) init();
    let i = 0;
    while (i < samples.length) {
      if (hopN === 0) hopFrame = frame + i;
      const n = Math.min(HOP - hopN, samples.length - i);
      hopBuf.set(samples.subarray(i, i + n), hopN);
      hopN += n;
      i += n;
      if (hopN === HOP) {
        processHop(hopFrame, hopBuf);
        hopN = 0;
      }
    }
    analysePending();
  }

  const el = ctx.ui;
  el.innerHTML = `<div class="muted" style="margin:8px 0 4px">Beep detector</div>
    <div class="meter"><div class="band"></div></div>
    <div class="muted small last" style="font-size:0.85em;margin-top:4px">no beeps heard yet</div>`;
  const bar = el.querySelector(".band");
  const lastEl = el.querySelector(".last");
  function render() {
    if (last) lastEl.textContent = `${count} sounds; last ${last.freq}Hz, ${last.snr_db}dB above background, tonality ${last.prominence}`;
  }
  ctx.setInterval(() => {
    const above = floor === null ? 0 : bandDb - floor;
    bar.style.width = Math.max(0, Math.min(100, (above / (cfg().threshold_db * 2)) * 100)) + "%";
    bar.style.background = above > cfg().threshold_db ? "var(--bad)" : "var(--ok)";
  }, 200);

  ctx.on("settings", () => {
    if (sr) hp = highpass(cfg().band_lo, sr);
  });
  ctx.method("detector.state", () => ({ floor, bandDb, count, last, hist: hist.slice(-50) }));
  ctx.onAudio(onAudio);
}
