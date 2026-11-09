"""
A tiny acoustic room simulator, used by the tests to check the maths end to
end without real hardware.

Each simulated device has its own sample rate, a clock that runs slightly fast
or slow (ppm), an arbitrary frame offset, and unknown input/output latencies -
exactly the things that make naive time-of-flight measurement impossible.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from .dsp import SPEED_OF_SOUND


@dataclass
class SimDevice:
    x: float
    y: float
    sr: float = 48000.0
    ppm: float = 0.0  # clock error
    t0: float = 0.0  # true time at frame 0
    in_latency: float = 0.02
    out_latency: float = 0.03
    self_distance: float = 0.05
    noise: float = 0.002

    def true_time(self, frame):
        return self.t0 + np.asarray(frame, dtype=np.float64) / (self.sr * (1 + self.ppm * 1e-6))

    def frame_at(self, true_time):
        return (np.asarray(true_time, dtype=np.float64) - self.t0) * self.sr * (1 + self.ppm * 1e-6)


@dataclass
class Emission:
    true_time: float  # when the sound leaves the source
    signal: np.ndarray
    sr: float
    x: float
    y: float
    gain: float = 1.0


@dataclass
class Room:
    devices: list
    emissions: list = field(default_factory=list)
    c: float = SPEED_OF_SOUND
    reflection_gain: float = 0.35
    reflection_delay: float = 0.004  # extra path time for one strong reflection
    seed: int = 0

    def play(self, device_index: int, frame: float, signal: np.ndarray, gain: float = 1.0):
        """Device schedules ``signal`` to play at ``frame`` of its own clock."""
        d = self.devices[device_index]
        t = float(d.true_time(frame)) + d.out_latency
        self.emissions.append(Emission(t, np.asarray(signal, dtype=np.float64), d.sr, d.x, d.y, gain))
        return t

    def emit_at(self, x: float, y: float, true_time: float, signal: np.ndarray, sr: float, gain: float = 1.0):
        self.emissions.append(Emission(true_time, np.asarray(signal, dtype=np.float64), sr, x, y, gain))

    def record(self, device_index: int, start_frame: int, n: int) -> np.ndarray:
        d = self.devices[device_index]
        rng = np.random.default_rng(self.seed + device_index * 7919 + int(start_frame) % 100000)
        frames = np.arange(start_frame, start_frame + n)
        t = d.true_time(frames) - d.in_latency
        out = rng.normal(0, d.noise, n)
        for e in self.emissions:
            dist = float(np.hypot(e.x - d.x, e.y - d.y))
            if dist < 1e-6:
                dist = d.self_distance
            att = e.gain / max(dist, 0.05)
            dur = len(e.signal) / e.sr
            for delay, g in ((dist / self.c, att), (dist / self.c + self.reflection_delay, att * self.reflection_gain)):
                local = t - e.true_time - delay
                mask = (local >= 0) & (local < dur)
                if not mask.any():
                    continue
                src_idx = local[mask] * e.sr
                out[mask] += g * np.interp(src_idx, np.arange(len(e.signal)), e.signal)
        return out.astype(np.float32)
