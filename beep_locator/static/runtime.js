// beep-locator browser runtime.
//
// This file is the *stable* part of every page: it owns the audio input and
// output, a rolling buffer of everything the microphone heard, and the
// WebSocket to the server. Everything else - detection, UI, experiments - is
// delivered as *modules* over the WebSocket and hot-swapped in place, so new
// code can be pushed to a phone without reloading the page (which would need
// another tap to re-enable audio).

export const BOOT_VERSION = "2026-11-09.1";

const AsyncFunction = Object.getPrototypeOf(async function () {}).constructor;
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

// ---------------------------------------------------------------------------
// Rolling buffer of audio indexed by absolute frame number
// ---------------------------------------------------------------------------

export class RingBuffer {
  constructor(capacity) {
    this.cap = capacity;
    this.buf = new Float32Array(capacity);
    this.start = 0; // oldest frame still held
    this.end = 0; // one past newest frame
    this.started = false;
    this.gaps = [];
  }
  _put(frame, data) {
    let off = 0;
    while (off < data.length) {
      const pos = (frame + off) % this.cap;
      const n = Math.min(data.length - off, this.cap - pos);
      this.buf.set(data.subarray(off, off + n), pos);
      off += n;
    }
  }
  write(frame, data) {
    if (!this.started) {
      this.started = true;
      this.start = this.end = frame;
    }
    if (frame > this.end) {
      const missing = frame - this.end;
      this.gaps.push({ at: this.end, missing, t: Date.now() });
      if (this.gaps.length > 200) this.gaps.shift();
      const z = Math.min(missing, this.cap);
      this._put(frame - z, new Float32Array(z));
    } else if (frame < this.end) {
      const skip = this.end - frame;
      if (skip >= data.length) return;
      data = data.subarray(skip);
      frame = this.end;
    }
    if (data.length > this.cap) {
      frame += data.length - this.cap;
      data = data.subarray(data.length - this.cap);
    }
    this._put(frame, data);
    this.end = frame + data.length;
    this.start = Math.max(this.start, this.end - this.cap);
  }
  read(a, b) {
    a = Math.floor(a);
    b = Math.floor(b);
    const out = new Float32Array(Math.max(0, b - a));
    const lo = Math.max(a, this.start);
    const hi = Math.min(b, this.end);
    let f = lo;
    while (f < hi) {
      const pos = f % this.cap;
      const n = Math.min(hi - f, this.cap - pos);
      out.set(this.buf.subarray(pos, pos + n), f - a);
      f += n;
    }
    return { samples: out, lo, hi };
  }
  gapsBetween(a, b) {
    return this.gaps.filter((g) => g.at < b && g.at + g.missing > a);
  }
}

// ---------------------------------------------------------------------------
// Real audio via WebAudio
// ---------------------------------------------------------------------------

export class WebAudioIO {
  constructor() {
    this.kind = "webaudio";
    this.onChunk = null;
  }
  async start() {
    const AC = window.AudioContext || window.webkitAudioContext;
    this.ctx = new AC({ latencyHint: "interactive" });
    // Resume first, while we are still inside the user's tap.
    const resumed = this.ctx.resume();
    try {
      if (navigator.audioSession) navigator.audioSession.type = "play-and-record";
    } catch (e) {}
    this.stream = await navigator.mediaDevices.getUserMedia({
      audio: {
        echoCancellation: false,
        noiseSuppression: false,
        autoGainControl: false,
        channelCount: 1,
      },
      video: false,
    });
    await resumed;
    await this.ctx.audioWorklet.addModule("/static/capture-worklet.js?v=" + BOOT_VERSION);
    this.source = this.ctx.createMediaStreamSource(this.stream);
    this.node = new AudioWorkletNode(this.ctx, "beep-capture", {
      numberOfInputs: 1,
      numberOfOutputs: 1,
      outputChannelCount: [1],
    });
    this.sink = this.ctx.createGain();
    this.sink.gain.value = 0;
    this.source.connect(this.node);
    this.node.connect(this.sink);
    this.sink.connect(this.ctx.destination);
    this.node.port.onmessage = (e) => this.onChunk && this.onChunk(e.data.frame, e.data.samples);
    this.output = this.ctx.createGain();
    this.output.connect(this.ctx.destination);
    this.track = this.stream.getAudioTracks()[0];
    this.sampleRate = this.ctx.sampleRate;
    if (this.ctx.state !== "running") await this.ctx.resume();
  }
  get running() {
    return !!this.ctx && this.ctx.state === "running" && this.track && this.track.readyState === "live";
  }
  currentFrame() {
    return Math.round(this.ctx.currentTime * this.sampleRate);
  }
  play(samples, frame) {
    const buf = this.ctx.createBuffer(1, samples.length, this.sampleRate);
    buf.copyToChannel(samples, 0);
    const src = this.ctx.createBufferSource();
    src.buffer = buf;
    src.connect(this.output);
    const now = this.ctx.currentTime;
    let when = frame == null ? now + 0.05 : frame / this.sampleRate;
    if (when < now + 0.01) when = now + 0.02;
    src.start(when);
    return Math.round(when * this.sampleRate);
  }
  async resume() {
    if (this.ctx && this.ctx.state !== "running") await this.ctx.resume();
  }
  info() {
    const s = this.track ? this.track.getSettings() : {};
    return {
      kind: this.kind,
      sampleRate: this.sampleRate,
      state: this.ctx && this.ctx.state,
      baseLatency: this.ctx && this.ctx.baseLatency,
      outputLatency: this.ctx && this.ctx.outputLatency,
      track: {
        label: this.track && this.track.label,
        muted: this.track && this.track.muted,
        readyState: this.track && this.track.readyState,
        echoCancellation: s.echoCancellation,
        noiseSuppression: s.noiseSuppression,
        autoGainControl: s.autoGainControl,
        sampleRate: s.sampleRate,
        channelCount: s.channelCount,
        latency: s.latency,
      },
      audioSession: navigator.audioSession ? navigator.audioSession.type : null,
    };
  }
}

// ---------------------------------------------------------------------------
// Runtime: WebSocket, RPC, modules
// ---------------------------------------------------------------------------

function randomId() {
  const a = new Uint8Array(8);
  crypto.getRandomValues(a);
  return Array.from(a, (b) => b.toString(16).padStart(2, "0")).join("");
}

export function persistentId(key = "beep-locator-id") {
  let id = null;
  try {
    id = localStorage.getItem(key);
    if (!id) {
      id = randomId();
      localStorage.setItem(key, id);
    }
  } catch (e) {
    id = randomId();
  }
  return id;
}

function b64ToFloat32(b64) {
  const bin = atob(b64);
  const bytes = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
  return new Float32Array(bytes.buffer);
}

function jsonable(value) {
  return JSON.parse(
    JSON.stringify(value === undefined ? null : value, (k, v) => {
      if (ArrayBuffer.isView(v)) return Array.from(v);
      if (v instanceof Map) return Object.fromEntries(v);
      if (v instanceof Set) return Array.from(v);
      if (v instanceof Error) return { error: v.message, stack: v.stack };
      if (typeof v === "number" && !isFinite(v)) return null;
      return v;
    })
  );
}

export class Runtime {
  constructor({ role = "listener", io = null, id = null, name = null, sim = false, token = null, root = document.body, wsUrl = null, onState = null }) {
    this.role = role;
    this.io = io;
    this.id = id || persistentId(role === "coordinator" ? "beep-locator-coordinator-id" : "beep-locator-id");
    this.requestedName = name;
    this.sim = sim;
    this.token = token;
    this.root = root;
    this.wsUrl = wsUrl || (location.protocol === "https:" ? "wss://" : "ws://") + location.host + "/ws";
    this.onState = onState;
    this.ws = null;
    this.connected = false;
    this.welcome = null;
    this.settings = {};
    this.modules = new Map();
    this.methods = new Map();
    this.handlers = new Map(); // topic -> Set(fn)
    this.audioListeners = new Set();
    this.ring = null;
    this.pendingCommands = new Map();
    this.commandIds = 1;
    this.backoff = 500;
    this.stopped = false;
    this.wakeLock = null;
    this.logLines = [];
    this.lastError = null;
    this.moduleRoot = root.querySelector("[data-modules]") || root;
    this.evalCtx = null;
    this.last = {};
    this._installBuiltins();
  }

  // -- audio ----------------------------------------------------------------
  async startAudio() {
    await this.io.start();
    const sr = this.io.sampleRate;
    this.ring = new RingBuffer(Math.round(sr * 60));
    this.io.onChunk = (frame, samples) => {
      this.ring.write(frame, samples);
      for (const fn of this.audioListeners) {
        try {
          fn(frame, samples);
        } catch (e) {
          this.reportError("audio listener", e);
        }
      }
    };
    this.requestWakeLock();
    this.emitLocal("audio-started", {});
    this.sendStatus();
  }

  get audioRunning() {
    return !!(this.io && this.ring && this.io.running);
  }

  async requestWakeLock() {
    if (this.sim || !("wakeLock" in navigator)) return;
    try {
      this.wakeLock = await navigator.wakeLock.request("screen");
      this.wakeLock.addEventListener("release", () => {
        this.wakeLock = null;
      });
    } catch (e) {
      this.wakeLock = null;
    }
  }

  levelDb(seconds = 0.5) {
    if (!this.ring || !this.ring.started) return null;
    const n = Math.round(this.io.sampleRate * seconds);
    const { samples } = this.ring.read(this.ring.end - n, this.ring.end);
    let s = 0,
      peak = 0;
    for (let i = 0; i < samples.length; i++) {
      s += samples[i] * samples[i];
      const a = Math.abs(samples[i]);
      if (a > peak) peak = a;
    }
    return {
      rms: 10 * Math.log10(s / Math.max(1, samples.length) + 1e-12),
      peak: 20 * Math.log10(peak + 1e-9),
    };
  }

  // -- connection -------------------------------------------------------------
  connect() {
    if (this.stopped) return;
    const ws = new WebSocket(this.wsUrl);
    this.ws = ws;
    ws.onopen = () => {
      this.backoff = 500;
      this.sendRaw({
        type: "hello",
        id: this.id,
        role: this.role,
        token: this.token,
        name: this.requestedName,
        sim: this.sim,
        ua: navigator.userAgent,
        boot_version: BOOT_VERSION,
      });
    };
    ws.onmessage = (e) => {
      let msg;
      try {
        msg = JSON.parse(e.data);
      } catch (err) {
        return;
      }
      this.dispatch(msg);
    };
    ws.onclose = (e) => {
      this.connected = false;
      this.emitLocal("connection", { connected: false, code: e.code });
      if (e.code === 4000) {
        this.log("warn", "Another tab connected as this device; this one has stopped.");
        this.stopped = true;
        return;
      }
      if (e.code === 4401) {
        this.stopped = true;
        this.emitLocal("auth-failed", {});
        return;
      }
      setTimeout(() => this.connect(), this.backoff);
      this.backoff = Math.min(this.backoff * 1.7, 8000);
    };
    ws.onerror = () => {};
  }

  sendRaw(msg) {
    if (this.ws && this.ws.readyState === 1) {
      this.ws.send(JSON.stringify(msg));
      return true;
    }
    return false;
  }

  send(topic, data) {
    return this.sendRaw({ type: "event", topic, data: jsonable(data) });
  }

  command(name, args = {}, timeout = 60000) {
    const id = this.commandIds++;
    return new Promise((resolve, reject) => {
      this.pendingCommands.set(id, { resolve, reject });
      if (!this.sendRaw({ type: "command", id, name, args })) {
        this.pendingCommands.delete(id);
        reject(new Error("not connected"));
      }
      setTimeout(() => {
        if (this.pendingCommands.has(id)) {
          this.pendingCommands.delete(id);
          reject(new Error("timed out"));
        }
      }, timeout);
    });
  }

  log(level, ...args) {
    const message = args.map((a) => (typeof a === "string" ? a : JSON.stringify(jsonable(a)))).join(" ");
    const line = `${new Date().toLocaleTimeString()} ${level === "info" ? "" : level + ": "}${message}`;
    this.logLines.push(line);
    if (this.logLines.length > 200) this.logLines.shift();
    this.emitLocal("log", { level, message, line });
    this.sendRaw({ type: "log", level, message });
    if (level === "error") console.error(message);
    else console.log(message);
  }

  reportError(where, e) {
    this.lastError = `${where}: ${e && e.message ? e.message : e}`;
    this.log("error", this.lastError, e && e.stack ? e.stack.split("\n").slice(0, 4).join(" | ") : "");
  }

  on(topic, fn) {
    if (!this.handlers.has(topic)) this.handlers.set(topic, new Set());
    this.handlers.get(topic).add(fn);
    return () => this.handlers.get(topic) && this.handlers.get(topic).delete(fn);
  }

  emitLocal(topic, data) {
    const hs = this.handlers.get(topic);
    if (!hs) return;
    for (const fn of Array.from(hs)) {
      try {
        fn(data);
      } catch (e) {
        if (topic !== "log") this.reportError(`handler for ${topic}`, e);
      }
    }
  }

  async dispatch(msg) {
    switch (msg.type) {
      case "welcome":
        this.welcome = msg;
        this.connected = true;
        this.settings = msg.settings || {};
        this.emitLocal("welcome", msg);
        this.emitLocal("connection", { connected: true });
        this.sendStatus();
        if (!this.statusTimer) this.statusTimer = setInterval(() => this.sendStatus(), 1500);
        break;
      case "ping":
        this.sendRaw({
          type: "pong",
          t: msg.t,
          frame: this.io && this.io.sampleRate && this.ring ? this.io.currentFrame() : null,
          sr: this.io ? this.io.sampleRate : null,
          perf: performance.now(),
        });
        break;
      case "rpc":
        this.handleRpc(msg);
        break;
      case "module":
        this.installModule(msg);
        break;
      case "module_remove":
        this.removeModule(msg.name);
        break;
      case "settings":
        this.settings = msg.settings || {};
        this.emitLocal("settings", this.settings);
        break;
      case "msg":
        this.emitLocal(msg.topic, msg.data);
        break;
      case "command_result": {
        const p = this.pendingCommands.get(msg.id);
        if (p) {
          this.pendingCommands.delete(msg.id);
          if (msg.ok) p.resolve(msg.value);
          else p.reject(new Error(msg.error));
        }
        break;
      }
      case "reload":
        location.reload();
        break;
      case "error":
        this.log("error", msg.error);
        break;
      default:
        // state, event, result, progress, history (coordinators). Keep the
        // latest of each so modules installed later can catch up.
        this.last[msg.type] = msg;
        this.emitLocal(msg.type, msg);
    }
  }

  // -- status -------------------------------------------------------------
  sendStatus() {
    const lvl = this.audioRunning ? this.levelDb() : null;
    const status = {
      running: this.audioRunning,
      sampleRate: this.io ? this.io.sampleRate : null,
      frame: this.ring ? this.ring.end : null,
      level: lvl,
      visibility: document.visibilityState,
      wakeLock: !!this.wakeLock,
      gaps: this.ring ? this.ring.gaps.length : 0,
      lastGap: this.ring && this.ring.gaps.length ? this.ring.gaps[this.ring.gaps.length - 1] : null,
      ioState: this.io && this.io.ctx ? this.io.ctx.state : this.io ? (this.io.running ? "running" : "stopped") : null,
      lastError: this.lastError,
      modules: Object.fromEntries(Array.from(this.modules, ([k, v]) => [k, v.version])),
    };
    this.status = status;
    this.sendRaw({ type: "status", status });
    this.emitLocal("status", status);
  }

  // -- RPC ------------------------------------------------------------------
  async handleRpc(msg) {
    const fn = this.methods.get(msg.method);
    let reply;
    if (!fn) {
      reply = { type: "rpc_result", id: msg.id, ok: false, error: `no such method: ${msg.method}` };
    } else {
      try {
        const value = await fn(msg.args || {});
        reply = { type: "rpc_result", id: msg.id, ok: true, value: jsonable(value) };
      } catch (e) {
        reply = {
          type: "rpc_result",
          id: msg.id,
          ok: false,
          error: (e && e.message ? e.message : String(e)) + (e && e.stack ? "\n" + e.stack.split("\n").slice(0, 5).join("\n") : ""),
        };
      }
    }
    this.sendRaw(reply);
  }

  _installBuiltins() {
    this.methods.set("eval", async ({ code }) => {
      if (!this.evalCtx) this.evalCtx = this.moduleContext("__eval__");
      const fn = new AsyncFunction("ctx", "rt", code);
      return await fn(this.evalCtx, this);
    });
    this.methods.set("info", async () => this.info());
    this.methods.set("play", async ({ samples, frame = null, gain = 1 }) => {
      if (!this.audioRunning) throw new Error("audio not running");
      let s = b64ToFloat32(samples);
      if (gain !== 1) s = s.map((v) => v * gain);
      const scheduled = this.io.play(s, frame);
      this.emitLocal("played", { frame: scheduled, n: s.length });
      return { frame: scheduled, now: this.io.currentFrame(), requested: frame };
    });
    this.methods.set("clip", async ({ start, end, label = "clip", meta = {}, wait = 10 }) => this.uploadClip(start, end, label, meta, wait));
    this.methods.set("identify", async () => {
      this.emitLocal("identify", {});
      if (this.audioRunning) {
        const sr = this.io.sampleRate;
        const n = Math.round(sr * 0.15);
        const s = new Float32Array(n);
        for (let i = 0; i < n; i++) s[i] = 0.3 * Math.sin((2 * Math.PI * 880 * i) / sr) * Math.sin((Math.PI * i) / n);
        this.io.play(s, null);
      }
      if (navigator.vibrate) navigator.vibrate(200);
      return { ok: true };
    });
  }

  info() {
    return {
      id: this.id,
      role: this.role,
      boot_version: BOOT_VERSION,
      io: this.io ? (this.io.info ? this.io.info() : { kind: this.io.kind }) : null,
      ring: this.ring ? { start: this.ring.start, end: this.ring.end, gaps: this.ring.gaps.slice(-10) } : null,
      now: this.io && this.ring ? this.io.currentFrame() : null,
      modules: Object.fromEntries(Array.from(this.modules, ([k, v]) => [k, { version: v.version, error: v.error || null }])),
      visibility: document.visibilityState,
      wakeLock: !!this.wakeLock,
      userAgent: navigator.userAgent,
      screen: { w: screen.width, h: screen.height, dpr: devicePixelRatio },
      secure: window.isSecureContext,
    };
  }

  async uploadClip(start, end, label = "clip", meta = {}, wait = 10) {
    if (!this.ring) throw new Error("audio not running");
    start = Math.floor(start);
    end = Math.floor(end);
    if (end - start > this.io.sampleRate * 60) throw new Error("clip too long");
    const t0 = performance.now();
    while (this.ring.end < end && performance.now() - t0 < wait * 1000) await sleep(40);
    const { samples, lo, hi } = this.ring.read(start, end);
    const pcm = new Int16Array(samples.length);
    for (let i = 0; i < samples.length; i++) {
      const v = Math.max(-1, Math.min(1, samples[i]));
      pcm[i] = Math.round(v * 32767);
    }
    const sr = this.io.sampleRate;
    const q = new URLSearchParams({ device: this.id, key: this.welcome.upload_key, start: String(start), sr: String(sr), label });
    const gaps = this.ring.gapsBetween(start, end);
    const resp = await fetch(`/upload/clip?${q}`, {
      method: "POST",
      body: pcm.buffer,
      headers: { "Content-Type": "application/octet-stream", "X-Meta": JSON.stringify(jsonable({ ...meta, lo, hi, gaps })) },
    });
    const j = await resp.json();
    if (!resp.ok) throw new Error(j.error || resp.statusText);
    return { clip_id: j.clip_id, start, n: samples.length, sr, valid: [lo, hi], gaps };
  }

  // -- modules ------------------------------------------------------------
  moduleContext(name) {
    const rt = this;
    const cleanups = [];
    let ui = null;
    const ctx = {
      name,
      rt,
      get id() {
        return rt.id;
      },
      get role() {
        return rt.role;
      },
      get deviceName() {
        return rt.welcome ? rt.welcome.name : null;
      },
      get color() {
        return rt.welcome ? rt.welcome.color : null;
      },
      get settings() {
        return rt.settings;
      },
      get io() {
        return rt.io;
      },
      get audio() {
        return rt.io && rt.io.ctx;
      },
      get sampleRate() {
        return rt.io ? rt.io.sampleRate : null;
      },
      get ring() {
        return rt.ring;
      },
      get audioRunning() {
        return rt.audioRunning;
      },
      get ui() {
        if (!ui) {
          ui = document.createElement("div");
          ui.className = "module module-" + name.replace(/[^a-z0-9_-]/gi, "_");
          ui.dataset.module = name;
          rt.moduleRoot.appendChild(ui);
        }
        return ui;
      },
      frame: () => (rt.ring ? rt.ring.end : null),
      now: () => (rt.io && rt.ring ? rt.io.currentFrame() : null),
      samples: (a, b) => rt.ring.read(a, b).samples,
      play: (samples, frame = null) => rt.io.play(samples, frame),
      upload: (start, end, label, meta) => rt.uploadClip(start, end, label, meta),
      send: (topic, data) => rt.send(topic, data),
      command: (cmd, args, timeout) => rt.command(cmd, args, timeout),
      log: (...a) => rt.log("info", `[${name}]`, ...a),
      warn: (...a) => rt.log("warn", `[${name}]`, ...a),
      error: (...a) => rt.log("error", `[${name}]`, ...a),
      on(topic, fn) {
        const off = rt.on(topic, fn);
        cleanups.push(off);
        return off;
      },
      onAudio(fn) {
        rt.audioListeners.add(fn);
        const off = () => rt.audioListeners.delete(fn);
        cleanups.push(off);
        return off;
      },
      method(mname, fn) {
        rt.methods.set(mname, fn);
        const off = () => rt.methods.get(mname) === fn && rt.methods.delete(mname);
        cleanups.push(off);
        return off;
      },
      setInterval(fn, ms) {
        const h = setInterval(() => {
          try {
            fn();
          } catch (e) {
            rt.reportError(`${name} interval`, e);
          }
        }, ms);
        cleanups.push(() => clearInterval(h));
        return h;
      },
      setTimeout(fn, ms) {
        const h = setTimeout(fn, ms);
        cleanups.push(() => clearTimeout(h));
        return h;
      },
      raf(fn) {
        let alive = true;
        const loop = (t) => {
          if (!alive) return;
          try {
            fn(t);
          } catch (e) {
            rt.reportError(`${name} raf`, e);
          }
          requestAnimationFrame(loop);
        };
        requestAnimationFrame(loop);
        cleanups.push(() => (alive = false));
      },
      onCleanup(fn) {
        cleanups.push(fn);
      },
      store: {},
      cleanup() {
        while (cleanups.length) {
          try {
            cleanups.pop()();
          } catch (e) {}
        }
        if (ui) {
          ui.remove();
          ui = null;
        }
      },
    };
    return ctx;
  }

  async installModule({ name, code, version }) {
    const existing = this.modules.get(name);
    if (existing && existing.version === version && !existing.error) {
      this.sendRaw({ type: "module_status", name, version, ok: true });
      return;
    }
    let factory;
    try {
      const url = URL.createObjectURL(new Blob([code + `\n//# sourceURL=module:${name}.js`], { type: "text/javascript" }));
      try {
        const mod = await import(url);
        factory = mod.default;
      } finally {
        URL.revokeObjectURL(url);
      }
      if (typeof factory !== "function") throw new Error("module must `export default function setup(ctx) {...}`");
    } catch (e) {
      this.reportError(`loading module ${name}`, e);
      this.sendRaw({ type: "module_status", name, version, ok: false, error: String(e && e.message ? e.message : e) });
      return;
    }
    // tear down the old version, start the new one; fall back if it throws
    if (existing) this._teardown(existing);
    const ctx = this.moduleContext(name);
    try {
      const instance = (await factory(ctx)) || {};
      this.modules.set(name, { version, ctx, instance, factory });
      this.sendRaw({ type: "module_status", name, version, ok: true });
      this.log("info", `module ${name} ${version} running`);
    } catch (e) {
      ctx.cleanup();
      this.reportError(`starting module ${name}`, e);
      this.sendRaw({ type: "module_status", name, version, ok: false, error: String(e && e.message ? e.message : e) });
      if (existing && existing.factory) {
        const ctx2 = this.moduleContext(name);
        try {
          const inst2 = (await existing.factory(ctx2)) || {};
          this.modules.set(name, { ...existing, ctx: ctx2, instance: inst2, error: `newer version ${version} failed: ${e.message}` });
          this.log("warn", `module ${name}: reverted to ${existing.version}`);
        } catch (e2) {
          ctx2.cleanup();
          this.modules.delete(name);
        }
      } else {
        this.modules.set(name, { version, error: String(e.message || e) });
      }
    }
    this.sendStatus();
  }

  _teardown(m) {
    try {
      if (m.instance && typeof m.instance.teardown === "function") m.instance.teardown();
    } catch (e) {
      this.reportError(`teardown ${m.version}`, e);
    }
    if (m.ctx) m.ctx.cleanup();
  }

  removeModule(name) {
    const m = this.modules.get(name);
    if (m) this._teardown(m);
    this.modules.delete(name);
    this.sendStatus();
  }
}

// Report uncaught errors to the server so they show up remotely.
export function captureErrors(rt) {
  window.addEventListener("error", (e) => rt.log("error", "uncaught:", e.message, e.filename || "", e.lineno || ""));
  window.addEventListener("unhandledrejection", (e) => rt.log("error", "unhandled rejection:", String(e.reason && e.reason.message ? e.reason.message : e.reason)));
}
