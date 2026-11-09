// Captures microphone samples together with the AudioContext frame number of
// each sample. That frame counter is the device's clock: every measurement we
// make is an interval between two frame numbers in the same stream.
class Capture extends AudioWorkletProcessor {
  constructor() {
    super();
    this.chunk = 2048;
    this.buf = new Float32Array(this.chunk + 256);
    this.n = 0;
    this.startFrame = null;
    this.lastFrame = null;
    this.lastLen = 128;
  }
  flush() {
    if (this.n === 0) return;
    const out = this.buf.slice(0, this.n);
    this.port.postMessage({ frame: this.startFrame, samples: out }, [out.buffer]);
    this.startFrame += this.n;
    this.n = 0;
  }
  process(inputs) {
    const input = inputs[0];
    const ch = input && input.length ? input[0] : null;
    const len = ch ? ch.length : 128;
    const frame = currentFrame; // eslint-disable-line no-undef
    if (this.startFrame === null) this.startFrame = frame;
    if (this.lastFrame !== null && frame !== this.lastFrame + this.lastLen) {
      // a discontinuity in the render clock: flush and restart the chunk so
      // the frame numbers stay truthful; the ring buffer records the gap
      this.flush();
      this.startFrame = frame;
    }
    if (ch) this.buf.set(ch, this.n);
    else this.buf.fill(0, this.n, this.n + len);
    this.n += len;
    this.lastFrame = frame;
    this.lastLen = len;
    if (this.n >= this.chunk) this.flush();
    return true;
  }
}
registerProcessor("beep-capture", Capture);
