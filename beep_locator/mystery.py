"""
Recognising "the mystery beep" and pooling every time it was heard.

The user confirms one or more located sounds as the beep they are hunting.
Each located sound gets a small signature (band spectrum, pitch, length);
later sounds are scored against the confirmed ones, and everything that
matches is solved together for a single position.
"""

from __future__ import annotations

import numpy as np

from . import dsp

BAND_EDGES = np.geomspace(400.0, 12000.0, 33)
TONAL_PROMINENCE = 25.0
DEFAULT_THRESHOLD = 0.75


def _band_power(x: np.ndarray, sr: float) -> np.ndarray:
    x = np.asarray(x, dtype=np.float64)
    w = np.hanning(len(x))
    p = np.abs(np.fft.rfft(x * w)) ** 2 / (np.sum(w**2) + 1e-12)
    f = np.fft.rfftfreq(len(x), 1.0 / sr)
    idx = np.searchsorted(BAND_EDGES, f) - 1
    ok = (idx >= 0) & (idx < len(BAND_EDGES) - 1)
    return np.bincount(idx[ok], weights=p[ok], minlength=len(BAND_EDGES) - 1)[: len(BAND_EDGES) - 1]


def _one_signature(samples: np.ndarray, sr: float, onset: int, limit: int | None):
    n = len(samples)
    limit = n if limit is None else max(0, min(n, int(limit)))
    onset = int(onset)
    seg = samples[onset : min(limit, onset + int(0.25 * sr))]
    if onset < 0 or len(seg) < int(0.03 * sr):
        return None
    pre = samples[max(0, onset - int(0.3 * sr)) : max(0, onset - int(0.03 * sr))]
    power = _band_power(seg, sr)
    if len(pre) >= 256:
        power = np.maximum(power - 2.0 * _band_power(pre, sr), 0.0)
    spec = np.sqrt(power)
    norm = float(np.linalg.norm(spec))
    if norm <= 0:
        return None
    freq, prom = dsp.dominant_frequency(seg[: int(0.1 * sr)], sr)
    # how long it stays above a quarter of its peak
    long = samples[onset : min(limit, onset + int(1.0 * sr))]
    env = dsp.moving_average(np.abs(dsp.bandpass(long, sr, 800.0, min(12000.0, sr / 2 - 200))), max(1, int(0.005 * sr)))
    above = np.flatnonzero(env >= 0.25 * env.max()) if len(env) and env.max() > 0 else []
    duration = float(above[-1] / sr) if len(above) else 0.0
    return spec / norm, freq, float(prom), duration


def signature(recordings, devices: list[dict]) -> dict | None:
    """
    recordings: analysis.Recording list; devices: the per-device entries of a
    locate result (need ``onset_frame``; ``chirp.frame`` bounds the sound).
    Averaged over every listener that timed the sound, so one microphone's
    colouring doesn't define it.
    """
    by_dev = {d.get("device_id"): d for d in devices}
    specs, freqs, proms, durs = [], [], [], []
    for rec in recordings:
        d = by_dev.get(rec.device_id) or {}
        if d.get("onset_frame") is None:
            continue
        limit = None
        if d.get("chirp") and d["chirp"].get("frame") is not None:
            limit = rec.local(d["chirp"]["frame"]) - 0.06 * rec.sr
        one = _one_signature(rec.samples, rec.sr, rec.local(d["onset_frame"]), limit)
        if one is None:
            continue
        specs.append(one[0])
        if one[1] is not None:
            freqs.append(one[1])
        proms.append(one[2])
        durs.append(one[3])
    if not specs:
        return None
    spec = np.mean(specs, axis=0)
    spec = spec / (np.linalg.norm(spec) + 1e-12)
    prom = float(np.median(proms))
    return {
        "spec": [round(float(v), 4) for v in spec],
        "freq": float(np.median(freqs)) if freqs else None,
        "prominence": prom,
        "tonal": prom >= TONAL_PROMINENCE,
        "duration": float(np.median(durs)),
    }


def similarity(a: dict | None, b: dict | None) -> float:
    """0..1: how alike two signatures are."""
    if not a or not b:
        return 0.0
    s = float(np.clip(np.dot(a["spec"], b["spec"]), 0.0, 1.0))
    if a.get("tonal") != b.get("tonal"):
        s *= 0.5
    elif a.get("tonal") and a.get("freq") and b.get("freq"):
        # two beepers a few percent apart in pitch are different beepers
        if abs(a["freq"] - b["freq"]) / b["freq"] > 0.025:
            s *= 0.3
    da, db = a.get("duration") or 0.0, b.get("duration") or 0.0
    if min(da, db) > 0 and max(da, db) / min(da, db) > 2.5:
        s *= 0.6
    return s


def score(sig: dict | None, confirmed: list[dict]) -> float:
    """Best match against any confirmed example."""
    return max((similarity(sig, c) for c in confirmed), default=0.0)


def locate_many(sets, c: float = dsp.SPEED_OF_SOUND, margin: float = 3.0, step: float = 0.05, sigma: float = 0.0006, bounds=None) -> dict | None:
    """
    One position for several hearings of the same source.

    sets: [(positions (n,2), times (n,)), ...] - each hearing has its own
    unknown emission time but they all share (x, y). Each hearing's misfit is
    capped so one mis-timed beep cannot drag the answer. ``sigma`` is the
    expected timing error per listener (0.6ms is about 20cm).
    """
    sets = [(np.asarray(P, dtype=np.float64), np.asarray(T, dtype=np.float64)) for P, T in sets if len(T) >= 3]
    if not sets:
        return None
    allP = np.concatenate([P for P, _ in sets])
    xmin, ymin = allP.min(0) - margin
    xmax, ymax = allP.max(0) + margin
    if bounds is not None:
        xmin, xmax = sorted((float(bounds[0]), float(bounds[2])))
        ymin, ymax = sorted((float(bounds[1]), float(bounds[3])))
    xs = np.arange(xmin, xmax + step / 2, step)
    ys = np.arange(ymin, ymax + step / 2, step)
    GX, GY = np.meshgrid(xs, ys)
    pts = np.stack([GX.ravel(), GY.ravel()], 1)
    total = np.zeros(len(pts))
    for P, T in sets:
        tau = np.sqrt(((pts[:, None, :] - P[None, :, :]) ** 2).sum(-1)) / c
        r = T[None, :] - tau
        r -= r.mean(1, keepdims=True)
        total += np.minimum((r**2).sum(1) / sigma**2, 9.0 * len(T))
    k = int(np.argmin(total))
    # everything within the ~95% contour for two unknowns
    near = pts[total <= total[k] + 6.0]
    radius = float(np.sqrt(((near - pts[k]) ** 2).sum(1)).max()) if len(near) else 0.0
    edge = bounds is None and bool(pts[k, 0] <= xmin + step or pts[k, 0] >= xmax - step or pts[k, 1] <= ymin + step or pts[k, 1] >= ymax - step)
    return {"x": float(pts[k, 0]), "y": float(pts[k, 1]), "radius_m": max(radius, step), "n": len(sets), "at_edge": edge}
