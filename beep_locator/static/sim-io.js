// A virtual room: simulated listeners that "hear" each other through a
// simple model of sound propagation. Each simulated device has its own
// sample rate, a clock that drifts (ppm), a random frame offset and unknown
// input/output latencies - the same nuisances real devices have - so the
// whole system (server, procedures, modules) can be exercised without
// hardware.

const C = 343.0;

function gauss() {
  // Box-Muller
  let u = 0,
    v = 0;
  while (u === 0) u = Math.random();
  while (v === 0) v = Math.random();
  return Math.sqrt(-2.0 * Math.log(u)) * Math.cos(2.0 * Math.PI * v);
}

export class VirtualRoom {
  constructor({ c = C, noise = 0.002, reflectionGain = 0.35, reflectionDelay = 0.004, selfDistance = 0.05 } = {}) {
    this.c = c;
    this.noise = noise;
    this.reflectionGain = reflectionGain;
    this.reflectionDelay = reflectionDelay;
    this.selfDistance = selfDistance;
    this.emissions = [];
    this.devices = [];
    this.beepers = [];
  }
  now() {
    return performance.now() / 1000;
  }
  addDevice(spec) {
    const io = new SimIO(this, spec);
    this.devices.push(io);
    return io;
  }
  emit(x, y, trueTime, samples, sr, gain = 1, label = "") {
    this.emissions.push({ x, y, t: trueTime, samples, sr, gain, dur: samples.length / sr, label });
    const cutoff = this.now() - 15;
    if (this.emissions.length > 50) this.emissions = this.emissions.filter((e) => e.t + e.dur > cutoff);
  }
  addBeeper({ x, y, freq = 3200, every = 10, duration = 0.1, gain = 0.5 }) {
    const sr = 48000;
    const n = Math.round(duration * sr);
    const s = new Float32Array(n);
    const rise = Math.round(0.002 * sr);
    for (let i = 0; i < n; i++) {
      const env = Math.min(1, i / rise, (n - i) / rise);
      s[i] = env * Math.sin((2 * Math.PI * freq * i) / sr);
    }
    const b = { x, y, freq, every, gain, samples: s, sr, count: 0, lastTime: null };
    b.timer = setInterval(() => {
      b.lastTime = this.now() + 0.2;
      b.count++;
      this.emit(x, y, b.lastTime, s, sr, gain, "beeper");
    }, every * 1000);
    this.beepers.push(b);
    return b;
  }
  render(dev, frameStart, n) {
    const out = new Float32Array(n);
    const sig = this.noise;
    for (let i = 0; i < n; i++) out[i] = sig * gauss();
    const tStart = dev.trueTime(frameStart) - dev.inLatency;
    const dt = 1 / dev.rate;
    for (const e of this.emissions) {
      let dist = Math.hypot(e.x - dev.x, e.y - dev.y);
      if (dist < 1e-6) dist = this.selfDistance;
      const att = e.gain / Math.max(dist, 0.05);
      const paths = [
        [dist / this.c, att],
        [dist / this.c + this.reflectionDelay, att * this.reflectionGain],
      ];
      for (const [delay, g] of paths) {
        const local0 = tStart - e.t - delay;
        if (local0 > e.dur || local0 + n * dt < 0) continue;
        const len = e.samples.length;
        for (let i = 0; i < n; i++) {
          const local = local0 + i * dt;
          if (local < 0 || local >= e.dur) continue;
          const idx = local * e.sr;
          const k = Math.floor(idx);
          const f = idx - k;
          const a = e.samples[k] || 0;
          const b = k + 1 < len ? e.samples[k + 1] : 0;
          out[i] += g * (a + (b - a) * f);
        }
      }
    }
    return out;
  }
}

export class SimIO {
  constructor(room, { x, y, sr = 48000, ppm = 0, inLatency = 0.02, outLatency = 0.03, gain = 1 }) {
    this.kind = "sim";
    this.room = room;
    this.x = x;
    this.y = y;
    this.sampleRate = sr;
    this.ppm = ppm;
    this.rate = sr * (1 + ppm * 1e-6);
    this.inLatency = inLatency;
    this.outLatency = outLatency;
    this.gain = gain;
    this.onChunk = null;
    this._running = false;
  }
  trueTime(frame) {
    return this.origin + frame / this.rate;
  }
  frameAt(t) {
    return (t - this.origin) * this.rate;
  }
  async start() {
    // every device starts its clock at a different moment
    this.origin = this.room.now() - Math.random() * 30;
    this.nextFrame = Math.floor(this.frameAt(this.room.now())) - 4096;
    this._running = true;
    this.timer = setInterval(() => this.pump(), 20);
  }
  get running() {
    return this._running;
  }
  currentFrame() {
    return Math.floor(this.frameAt(this.room.now()));
  }
  pump() {
    const target = this.currentFrame() - 1024;
    const chunk = 1024;
    while (this.nextFrame + chunk <= target) {
      const s = this.room.render(this, this.nextFrame, chunk);
      if (this.onChunk) this.onChunk(this.nextFrame, s);
      this.nextFrame += chunk;
    }
  }
  play(samples, frame) {
    const now = this.currentFrame();
    if (frame == null || frame < now + 0.01 * this.sampleRate) frame = now + Math.round(0.02 * this.sampleRate);
    frame = Math.round(frame);
    const t = this.trueTime(frame) + this.outLatency;
    this.room.emit(this.x, this.y, t, samples, this.sampleRate, this.gain, "device");
    return frame;
  }
  async resume() {}
  info() {
    return {
      kind: "sim",
      sampleRate: this.sampleRate,
      ppm: this.ppm,
      x: this.x,
      y: this.y,
      inLatency: this.inLatency,
      outLatency: this.outLatency,
    };
  }
}
