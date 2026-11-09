"""
Turn recordings into answers.

These functions are pure: they take recordings (numpy arrays plus the frame
number of their first sample in that device's stream) and approximate
expectations of where to look, and return JSON-friendly results. The server
procedures gather the recordings; the tests feed in simulated ones.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from . import dsp


@dataclass
class Recording:
    device_id: str
    sr: float
    start_frame: int
    samples: np.ndarray

    def local(self, frame: float) -> float:
        return frame - self.start_frame


def _window(rec: Recording, centre_frame: float, before: float, after: float):
    lo = int(rec.local(centre_frame) - before * rec.sr)
    hi = int(rec.local(centre_frame) + after * rec.sr)
    return lo, hi


def find_chirp(rec: Recording, template: np.ndarray, expected_frame: float, before: float, after: float):
    lo, hi = _window(rec, expected_frame, before, after)
    det = dsp.detect_chirp(rec.samples, template, lo, hi, sr=rec.sr)
    if det is None:
        return None
    return {
        "frame": rec.start_frame + det.index,
        "snr": det.snr,
        "peak": det.peak,
        "strongest_frame": rec.start_frame + det.strongest_index,
    }


def analyse_calibration(
    recordings: list[Recording],
    emitters: list[dict],
    c: float = dsp.SPEED_OF_SOUND,
    self_distance: float = dsp.DEFAULT_SELF_DISTANCE,
    chirp_params: dict | None = None,
    min_snr: float = 6.0,
    before: float = 0.35,
    after: float = 0.65,
) -> dict:
    """
    emitters: one entry per chirp played, in order:
        {"device_id": ..., "slot": k,
         "expected": {device_id: approximate frame where it should arrive}}
    The self-recording window is taken from expected[emitter] as well (the
    procedure should set it to the scheduled play frame).
    """
    chirp_params = chirp_params or {}
    ids = [r.device_id for r in recordings]
    index = {d: i for i, d in enumerate(ids)}
    n = len(ids)
    arrivals = np.full((n, n), np.nan)
    snrs = np.full((n, n), np.nan)
    detections = []
    for em in emitters:
        j = index.get(em["device_id"])
        if j is None:
            continue
        for rec in recordings:
            i = index[rec.device_id]
            exp = em["expected"].get(rec.device_id)
            if exp is None:
                continue
            template = dsp.chirp_for_slot(rec.sr, em["slot"], **chirp_params)
            if i == j:
                found = find_chirp(rec, template, exp, 0.05, 0.6)
            else:
                found = find_chirp(rec, template, exp, before, after)
            entry = {"emitter": em["device_id"], "receiver": rec.device_id, "expected": exp}
            if found:
                entry.update(found)
                if found["snr"] >= min_snr:
                    arrivals[i, j] = found["frame"]
                    snrs[i, j] = found["snr"]
            detections.append(entry)
    srs = [r.sr for r in recordings]
    D = dsp.pairwise_distances(arrivals, srs, c=c, self_distance=self_distance)
    result = {
        "device_ids": ids,
        "arrivals": _clean(arrivals),
        "snr": _clean(snrs),
        "distances": _clean(D),
        "detections": detections,
        "speed_of_sound": c,
    }
    return result


def combine_distance_matrices(mats: list) -> np.ndarray:
    """Median of several distance matrices, ignoring NaNs."""
    arr = np.array([[[np.nan if v is None else v for v in row] for row in m] for m in mats], dtype=np.float64)
    with np.errstate(all="ignore"):
        import warnings

        with warnings.catch_warnings():
            warnings.simplefilter("ignore", RuntimeWarning)
            med = np.nanmedian(arr, axis=0)
    return med


def layout_from_distances(device_ids: list[str], D) -> dict:
    D = np.array([[np.nan if v is None else v for v in row] for row in D], dtype=np.float64)
    lay = dsp.fit_layout(D)
    out = lay.as_dict()
    out["device_ids"] = list(device_ids)
    out["positions_by_id"] = {d: out["positions"][i] for i, d in enumerate(device_ids)}
    return out


def analyse_beep(
    recordings: list[Recording],
    positions: dict,
    sync: dict,
    beep_expected: dict,
    c: float = dsp.SPEED_OF_SOUND,
    self_distance: float = dsp.DEFAULT_SELF_DISTANCE,
    chirp_params: dict | None = None,
    freq_hint: float | None = None,
    min_chirp_snr: float = 6.0,
    min_onset_snr_db: float = 8.0,
) -> dict:
    """
    Locate a beep using a sync chirp heard by every device shortly after it.

    sync: {"device_id": emitter, "slot": k, "expected": {device_id: frame}}
    beep_expected: {device_id: approximate frame of the beep onset}
    positions: {device_id: [x, y]} from calibration

    For device i, the beep arrival time relative to the sync chirp's emission
    is (onset_i - chirp_i)/sr_i + dist(i, emitter)/c. Those times share a
    common origin across devices, so they are directly usable for TDOA.
    """
    chirp_params = chirp_params or {}
    emitter = sync["device_id"]
    pe = positions.get(emitter)
    per_device = []
    # 1. figure out the beep's frequency from the loudest recording
    freq = freq_hint
    best = None
    for rec in recordings:
        exp = beep_expected.get(rec.device_id)
        if exp is None:
            continue
        lo, hi = _window(rec, exp, 0.3, 0.3)
        lo, hi = max(lo, 0), min(hi, len(rec.samples))
        if hi - lo < 256:
            continue
        seg = rec.samples[lo:hi]
        level = dsp.rms_db(seg)
        if best is None or level > best[0]:
            best = (level, rec, lo, hi)
    if freq is None and best is not None:
        _, rec, lo, hi = best
        seg = rec.samples[lo:hi]
        # look at the loudest 60ms of the window
        env = dsp.moving_average(np.abs(seg), int(0.01 * rec.sr))
        p = int(np.argmax(env))
        a = max(0, p - int(0.03 * rec.sr))
        b = min(len(seg), p + int(0.03 * rec.sr))
        f, prom = dsp.dominant_frequency(seg[a:b], rec.sr)
        if f is not None and prom > 8:
            freq = f
    # 2. per-device sync chirp + beep onset
    times = {}
    for rec in recordings:
        info = {"device_id": rec.device_id}
        per_device.append(info)
        pi = positions.get(rec.device_id)
        if pi is None:
            info["error"] = "no position (not calibrated)"
            continue
        exp_c = sync["expected"].get(rec.device_id)
        exp_b = beep_expected.get(rec.device_id)
        if exp_c is None or exp_b is None:
            info["error"] = "missing expectations"
            continue
        template = dsp.chirp_for_slot(rec.sr, sync.get("slot", 0), **chirp_params)
        before, after = (0.05, 0.6) if rec.device_id == emitter else (0.35, 0.65)
        ch = find_chirp(rec, template, exp_c, before, after)
        if not ch or ch["snr"] < min_chirp_snr:
            info["error"] = "sync chirp not detected"
            info["chirp"] = ch
            continue
        info["chirp"] = ch
        lo, hi = _window(rec, exp_b, 0.4, 0.4)
        # never let the beep window run into the sync chirp
        hi = min(hi, int(rec.local(ch["frame"]) - 0.02 * rec.sr))
        on = dsp.detect_onset(
            rec.samples,
            rec.sr,
            f_center=freq,
            lo=lo,
            hi=hi,
            min_snr_db=min_onset_snr_db,
        )
        if on is None:
            info["error"] = "beep onset not detected"
            continue
        info["onset"] = on.as_dict()
        info["onset_frame"] = rec.start_frame + on.index
        info["level_db"] = 20 * np.log10(max(on.peak, 1e-9))
        info["snr_db"] = on.snr_db
        if rec.device_id == emitter:
            d_e = self_distance
        else:
            d_e = float(np.hypot(pi[0] - pe[0], pi[1] - pe[1])) if pe is not None else None
        if d_e is None:
            info["error"] = "sync emitter has no position"
            continue
        t = (info["onset_frame"] - ch["frame"]) / rec.sr + d_e / c
        info["time"] = t
        times[rec.device_id] = t
    ids = [d for d in times]
    result = {
        "freq": freq,
        "devices": per_device,
        "used": ids,
    }
    loudest = [p for p in per_device if "level_db" in p]
    if loudest:
        result["loudest"] = max(loudest, key=lambda p: p["level_db"])["device_id"]
    if len(ids) >= 3:
        P = np.array([positions[d] for d in ids])
        T = np.array([times[d] for d in ids])
        res = dsp.locate_tdoa(P, T, c=c)
        if res is not None:
            result["estimate"] = res.as_dict()
            # leave-one-out to flag a bad device when we have redundancy
            if len(ids) >= 5 and res.rms_residual * c > 0.15:
                best_alt = None
                for k in range(len(ids)):
                    keep = [m for m in range(len(ids)) if m != k]
                    r2 = dsp.locate_tdoa(P[keep], T[keep], c=c)
                    if r2 and (best_alt is None or r2.rms_residual < best_alt[1].rms_residual):
                        best_alt = (ids[k], r2)
                if best_alt and best_alt[1].rms_residual < res.rms_residual / 3:
                    result["estimate"] = best_alt[1].as_dict()
                    result["dropped"] = best_alt[0]
    else:
        result["error"] = f"need 3 devices with good timing, have {len(ids)}"
    return result


def _clean(a):
    a = np.asarray(a, dtype=np.float64)
    return [[None if not np.isfinite(v) else float(v) for v in row] for row in a]
