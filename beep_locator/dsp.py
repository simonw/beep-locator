"""
Signal processing and geometry for beep-locator.

Conventions used throughout:

* Every device has its own audio clock, measured in *frames* (samples) of its
  own recording stream. We never compare frame numbers between devices
  directly, only intervals within one device's stream.
* ``arrivals[i][j]`` is the frame (in device i's stream, fractional) at which
  the chirp emitted by device j arrived at device i's microphone.
* Distances are metres, times are seconds.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np

SPEED_OF_SOUND = 343.0  # m/s, dry air at ~20C
DEFAULT_SELF_DISTANCE = 0.05  # metres between a device's own speaker and mic


def speed_of_sound(temp_c: float = 20.0) -> float:
    return 331.3 * math.sqrt(1 + temp_c / 273.15)


# ---------------------------------------------------------------------------
# Signals
# ---------------------------------------------------------------------------


def tukey(n: int, alpha: float = 0.15) -> np.ndarray:
    if n <= 1:
        return np.ones(n)
    if alpha <= 0:
        return np.ones(n)
    w = np.ones(n)
    edge = int(math.floor(alpha * (n - 1) / 2.0))
    if edge > 0:
        k = np.arange(edge)
        ramp = 0.5 * (1 - np.cos(np.pi * k / edge))
        w[:edge] = ramp
        w[-edge:] = ramp[::-1]
    return w


def make_chirp(
    sr: float,
    f0: float = 2000.0,
    f1: float = 7000.0,
    duration: float = 0.08,
    taper: float = 0.15,
    amplitude: float = 0.9,
) -> np.ndarray:
    """Linear chirp from f0 to f1 (use f0 > f1 for a down-chirp)."""
    n = int(round(duration * sr))
    t = np.arange(n) / sr
    k = (f1 - f0) / duration
    phase = 2 * np.pi * (f0 * t + 0.5 * k * t * t)
    return (amplitude * np.sin(phase) * tukey(n, taper)).astype(np.float32)


def chirp_for_slot(sr: float, slot: int, **kwargs) -> np.ndarray:
    """Alternate up and down chirps so adjacent emitters can't be confused."""
    f_lo = kwargs.pop("f_lo", 2000.0)
    f_hi = kwargs.pop("f_hi", 7000.0)
    if slot % 2 == 0:
        return make_chirp(sr, f_lo, f_hi, **kwargs)
    return make_chirp(sr, f_hi, f_lo, **kwargs)


def make_beep(
    sr: float,
    freq: float = 3200.0,
    duration: float = 0.1,
    rise: float = 0.002,
    amplitude: float = 0.8,
) -> np.ndarray:
    """A smoke-alarm-ish tone burst, used for test beeps."""
    n = int(round(duration * sr))
    t = np.arange(n) / sr
    env = np.ones(n)
    nr = max(1, int(round(rise * sr)))
    env[:nr] = np.linspace(0, 1, nr)
    env[-nr:] = np.linspace(1, 0, nr)
    return (amplitude * env * np.sin(2 * np.pi * freq * t)).astype(np.float32)


# ---------------------------------------------------------------------------
# Basic DSP helpers
# ---------------------------------------------------------------------------


def _next_pow2(n: int) -> int:
    return 1 << (int(n) - 1).bit_length()


def analytic_envelope(x: np.ndarray) -> np.ndarray:
    """Magnitude of the analytic signal (Hilbert envelope)."""
    x = np.asarray(x, dtype=np.float64)
    n = len(x)
    if n == 0:
        return x
    X = np.fft.fft(x)
    h = np.zeros(n)
    if n % 2 == 0:
        h[0] = h[n // 2] = 1
        h[1 : n // 2] = 2
    else:
        h[0] = 1
        h[1 : (n + 1) // 2] = 2
    return np.abs(np.fft.ifft(X * h))


def correlate_valid(x: np.ndarray, template: np.ndarray) -> np.ndarray:
    """c[k] = sum_m x[k+m] * template[m] for k in 0..len(x)-len(template)."""
    x = np.asarray(x, dtype=np.float64)
    t = np.asarray(template, dtype=np.float64)
    if len(x) < len(t):
        return np.zeros(0)
    n = _next_pow2(len(x) + len(t))
    c = np.fft.irfft(np.fft.rfft(x, n) * np.conj(np.fft.rfft(t, n)), n)
    return c[: len(x) - len(t) + 1]


def bandpass(x: np.ndarray, sr: float, f_lo: float, f_hi: float, edge: float = 150.0) -> np.ndarray:
    """Zero-phase FFT band-pass with raised-cosine edges."""
    x = np.asarray(x, dtype=np.float64)
    n = len(x)
    if n == 0:
        return x
    X = np.fft.rfft(x)
    f = np.fft.rfftfreq(n, 1.0 / sr)
    mask = np.zeros_like(f)
    inside = (f >= f_lo) & (f <= f_hi)
    mask[inside] = 1.0
    lo_edge = (f >= f_lo - edge) & (f < f_lo)
    mask[lo_edge] = 0.5 * (1 - np.cos(np.pi * (f[lo_edge] - (f_lo - edge)) / edge))
    hi_edge = (f > f_hi) & (f <= f_hi + edge)
    mask[hi_edge] = 0.5 * (1 + np.cos(np.pi * (f[hi_edge] - f_hi) / edge))
    return np.fft.irfft(X * mask, n)


def moving_average(x: np.ndarray, n: int) -> np.ndarray:
    if n <= 1:
        return np.asarray(x, dtype=np.float64)
    k = np.ones(n) / n
    return np.convolve(x, k, mode="same")


def parabolic_peak(y: np.ndarray, i: int) -> float:
    """Sub-sample position of a peak at index i using a parabola through 3 points."""
    if i <= 0 or i >= len(y) - 1:
        return float(i)
    a, b, c = y[i - 1], y[i], y[i + 1]
    denom = a - 2 * b + c
    if denom == 0:
        return float(i)
    delta = 0.5 * (a - c) / denom
    return float(i) + float(np.clip(delta, -1, 1))


def rms_db(x: np.ndarray) -> float:
    x = np.asarray(x, dtype=np.float64)
    if len(x) == 0:
        return -120.0
    r = math.sqrt(float(np.mean(x * x)))
    return 20 * math.log10(max(r, 1e-6))


def dominant_frequency(x: np.ndarray, sr: float, f_min: float = 500.0, f_max: float = 12000.0):
    """Return (frequency, prominence) of the strongest spectral peak."""
    x = np.asarray(x, dtype=np.float64)
    if len(x) < 64:
        return None, 0.0
    w = np.hanning(len(x))
    spec = np.abs(np.fft.rfft(x * w))
    f = np.fft.rfftfreq(len(x), 1.0 / sr)
    band = (f >= f_min) & (f <= min(f_max, sr / 2 - 1))
    if not band.any():
        return None, 0.0
    idx = np.flatnonzero(band)
    s = spec[idx]
    k = int(np.argmax(s))
    peak = s[k]
    prominence = float(peak / (np.median(s) + 1e-12))
    kk = idx[k]
    # parabolic interpolation in log magnitude for a better estimate
    if 0 < kk < len(spec) - 1:
        ls = np.log(spec[kk - 1 : kk + 2] + 1e-12)
        pos = parabolic_peak(ls, 1) - 1
        freq = (kk + pos) * sr / len(x)
    else:
        freq = f[kk]
    return float(freq), prominence


# ---------------------------------------------------------------------------
# Chirp detection
# ---------------------------------------------------------------------------


@dataclass
class Detection:
    index: float  # fractional sample index within the analysed array
    peak: float
    snr: float
    strongest_index: float

    def as_dict(self):
        return {
            "index": self.index,
            "peak": self.peak,
            "snr": self.snr,
            "strongest_index": self.strongest_index,
        }


_SIDELOBE_CACHE: dict = {}


def _sidelobe_profile(template: np.ndarray, max_lag: int = 120) -> np.ndarray:
    """
    Envelope of the template's autocorrelation at lags 1..max_lag, relative to
    its peak: how large a sidelobe an arrival produces k samples before itself.
    """
    key = (len(template), float(template[len(template) // 3]), float(np.abs(template).sum()))
    prof = _SIDELOBE_CACHE.get(key)
    if prof is None:
        t = np.asarray(template, dtype=np.float64)
        x = np.concatenate([np.zeros(max_lag), t, np.zeros(max_lag)])
        e = analytic_envelope(correlate_valid(x, t))
        e = e / e[max_lag]
        prof = np.maximum(e[max_lag - 1 :: -1], e[max_lag + 1 :])[:max_lag]
        # ignore the main lobe itself (up to its first null)
        rising = np.nonzero(np.diff(prof) > 0)[0]
        prof[: (int(rising[0]) if len(rising) else 0)] = 0.0
        # sidelobes of real speakers/mics are smeared: use a running maximum
        # over neighbouring lags rather than the exact nulls
        prof = np.array([prof[max(0, k - 3) : k + 4].max() for k in range(len(prof))])
        if len(_SIDELOBE_CACHE) > 64:
            _SIDELOBE_CACHE.clear()
        _SIDELOBE_CACHE[key] = prof
    return prof


def detect_chirp(
    x: np.ndarray,
    template: np.ndarray,
    lo: int | None = None,
    hi: int | None = None,
    sr: float = 48000.0,
    # 0.15 rather than 0.5: in real rooms the direct path is often much weaker
    # than a later reflection. On recorded calibrations 0.5 flipped between
    # peaks from round to round (metre-sized errors); 0.15 gave consistent
    # distances and a timing closure error several times smaller.
    first_arrival_frac: float = 0.15,
    max_early: float = 0.015,
) -> Detection | None:
    """
    Find where ``template`` starts within ``x``, searching start positions in
    [lo, hi). Returns the *first* strong arrival (the direct path), not just
    the strongest, to be robust against reflections.
    """
    x = np.asarray(x, dtype=np.float64)
    nt = len(template)
    lo = 0 if lo is None else max(0, int(lo))
    hi = len(x) - nt + 1 if hi is None else min(len(x) - nt + 1, int(hi))
    if hi - lo < 4:
        return None
    seg = x[lo : hi + nt - 1]
    c = correlate_valid(seg, template)
    env = analytic_envelope(c)
    p = int(np.argmax(env))
    peak = float(env[p])
    noise = float(np.median(env)) + 1e-12
    snr = peak / noise
    # earliest local maximum within max_early before the peak that is at least
    # first_arrival_frac of the peak, and is not just a correlation sidelobe
    # of a stronger arrival shortly after it
    start = max(1, p - int(max_early * sr))
    first = p
    thresh = first_arrival_frac * peak
    side = _sidelobe_profile(template)
    for i in range(start, p):
        if env[i] >= thresh and env[i] >= env[i - 1] and env[i] >= env[i + 1]:
            later = env[i + 1 : i + 1 + len(side)]
            if env[i] > 2.0 * float(np.max(later * side[: len(later)])):
                first = i
                break
    idx = parabolic_peak(env, first)
    strongest = parabolic_peak(env, p)
    return Detection(index=lo + idx, peak=peak, snr=snr, strongest_index=lo + strongest)


# ---------------------------------------------------------------------------
# Onset detection for unknown beeps
# ---------------------------------------------------------------------------


@dataclass
class Onset:
    index: float
    peak: float
    floor: float
    snr_db: float
    freq: float | None

    def as_dict(self):
        return {
            "index": self.index,
            "peak": self.peak,
            "floor": self.floor,
            "snr_db": self.snr_db,
            "freq": self.freq,
        }


def detect_onset(
    x: np.ndarray,
    sr: float,
    f_center: float | None = None,
    bandwidth: float = 600.0,
    lo: int | None = None,
    hi: int | None = None,
    smooth: float = 0.0005,
    min_snr_db: float = 8.0,
    frac: float = 0.5,
    impulsive: bool = False,
    above_floor_db: float | None = None,
) -> Onset | None:
    """
    Find the onset (leading edge) of the strongest burst in x[lo:hi].

    Two stage: a low threshold finds the start of the burst, then the onset is
    placed where the envelope first reaches ``frac`` of the first local
    maximum after that (the direct sound) rather than the global maximum
    (which reverberation can inflate).

    impulsive=True is for claps and knocks: the onset is simply the first
    sustained crossing of the low threshold. On recorded claps (50dB+ above
    the room) that was consistent between listeners to a few centimetres,
    where the two-stage rule was out by a metre or more.
    """
    x = np.asarray(x, dtype=np.float64)
    lo = 0 if lo is None else max(0, int(lo))
    hi = len(x) if hi is None else min(len(x), int(hi))
    if hi - lo < int(0.02 * sr):
        return None
    if f_center is not None:
        y = bandpass(x, sr, f_center - bandwidth / 2, f_center + bandwidth / 2)
    else:
        y = bandpass(x, sr, 800.0, min(12000.0, sr / 2 - 200))
    env = moving_average(analytic_envelope(y), max(1, int(smooth * sr)))
    seg = env[lo:hi]
    p = int(np.argmax(seg))
    peak = float(seg[p])
    # noise floor: 20th percentile of the part of the window before the peak,
    # excluding the last 20ms which may contain the rise
    pre = seg[: max(1, p - int(0.02 * sr))]
    floor = float(np.percentile(pre, 20)) if len(pre) > 10 else float(np.percentile(seg, 10))
    floor = max(floor, 1e-9)
    snr_db = 20 * math.log10(peak / floor)
    if snr_db < min_snr_db:
        return None
    # stage 1: first sustained crossing of a low threshold, searching backwards
    # no further than 300ms before the peak
    low = floor + 0.15 * (peak - floor)
    start = max(0, p - int(0.3 * sr))
    hold = max(1, int(0.001 * sr))
    above = seg >= low
    i1 = None
    i = start
    while i <= p:
        if above[i]:
            j = min(len(seg), i + hold)
            if above[i:j].all():
                i1 = i
                break
            i = j
        else:
            i += 1
    if i1 is None:
        i1 = p
    if above_floor_db is not None:
        # Tonal beeps: the first moment the tone clearly rises out of the
        # background, rather than a fraction of a peak that reverberation
        # sets differently at every listener. On repeated hearings of a real
        # smoke-alarm chirp this cut the misfit between listeners from about
        # a metre to about 10cm.
        q0, q1 = max(0, p - int(0.5 * sr)), max(1, p - int(0.1 * sr))
        noise = float(np.percentile(seg[q0:q1], 95)) if q1 - q0 > 50 else floor
        th = min(max(noise, 1e-9) * 10 ** (above_floor_db / 20), floor + 0.5 * (peak - floor))
        hold2 = max(1, int(0.002 * sr))
        ok = seg >= th
        i = max(1, p - int(0.15 * sr))
        while i < p and not (ok[i] and ok[i : i + hold2].all()):
            i += 1
        if seg[i] != seg[i - 1] and seg[i - 1] < th:
            idx = (i - 1) + (th - seg[i - 1]) / (seg[i] - seg[i - 1])
        else:
            idx = float(i)
        return Onset(index=lo + idx, peak=peak, floor=floor, snr_db=snr_db, freq=f_center)
    if impulsive:
        if i1 > 0 and seg[i1] != seg[i1 - 1] and seg[i1 - 1] < low:
            idx = (i1 - 1) + (low - seg[i1 - 1]) / (seg[i1] - seg[i1 - 1])
        else:
            idx = float(i1)
        return Onset(index=lo + idx, peak=peak, floor=floor, snr_db=snr_db, freq=None)
    # stage 2: local max within 8ms after i1
    w_end = min(len(seg), i1 + int(0.008 * sr))
    local = i1 + int(np.argmax(seg[i1:w_end])) if w_end > i1 else i1
    lpeak = float(seg[local])
    target = floor + frac * (lpeak - floor)
    k = local
    while k > 0 and seg[k - 1] >= target:
        k -= 1
    # linear interpolation between k-1 (below) and k (above)
    if k > 0 and seg[k] != seg[k - 1]:
        idx = (k - 1) + (target - seg[k - 1]) / (seg[k] - seg[k - 1])
    else:
        idx = float(k)
    freq = None
    if f_center is not None:
        freq = f_center
    return Onset(index=lo + idx, peak=peak, floor=floor, snr_db=snr_db, freq=freq)


# ---------------------------------------------------------------------------
# Ranging (BeepBeep two-way)
# ---------------------------------------------------------------------------


def pairwise_distances(
    arrivals: np.ndarray,
    sample_rates,
    c: float = SPEED_OF_SOUND,
    self_distance=DEFAULT_SELF_DISTANCE,
) -> np.ndarray:
    """
    BeepBeep two-way ranging.

    arrivals[i, j] = frame in device i's stream where emitter j's chirp
    arrived (NaN if not detected). Returns an NxN distance matrix (NaN where
    unknown).

        dist(A,B) = c/2 * (dA - dB) + (sAA + sBB)/2
        dA = (arr[A,B] - arr[A,A]) / srA   (A's recording)
        dB = (arr[B,B] - arr[B,A]) / srB   (B's recording)
    """
    arrivals = np.asarray(arrivals, dtype=np.float64)
    n = arrivals.shape[0]
    sr = np.asarray(sample_rates, dtype=np.float64)
    selfd = np.broadcast_to(np.asarray(self_distance, dtype=np.float64), (n,))
    D = np.full((n, n), np.nan)
    np.fill_diagonal(D, 0.0)
    for a in range(n):
        for b in range(a + 1, n):
            vals = (arrivals[a, a], arrivals[a, b], arrivals[b, b], arrivals[b, a])
            if not all(np.isfinite(v) for v in vals):
                continue
            dA = (arrivals[a, b] - arrivals[a, a]) / sr[a]
            dB = (arrivals[b, b] - arrivals[b, a]) / sr[b]
            d = c / 2 * (dA - dB) + (selfd[a] + selfd[b]) / 2
            D[a, b] = D[b, a] = d
    return D


# ---------------------------------------------------------------------------
# Layout (distances -> 2D positions)
# ---------------------------------------------------------------------------


def _fill_missing(D: np.ndarray) -> np.ndarray:
    """Fill NaNs with shortest-path estimates (Floyd-Warshall) or the mean."""
    D = D.copy()
    n = len(D)
    G = np.where(np.isfinite(D), D, np.inf)
    np.fill_diagonal(G, 0)
    for k in range(n):
        G = np.minimum(G, G[:, [k]] + G[[k], :])
    finite = D[np.isfinite(D) & (D > 0)]
    fallback = float(np.mean(finite)) if len(finite) else 1.0
    out = np.where(np.isfinite(D), D, np.where(np.isfinite(G), G, fallback))
    np.fill_diagonal(out, 0)
    return out


def classical_mds(D: np.ndarray, dim: int = 2) -> np.ndarray:
    n = len(D)
    J = np.eye(n) - np.ones((n, n)) / n
    B = -0.5 * J @ (D**2) @ J
    vals, vecs = np.linalg.eigh(B)
    order = np.argsort(vals)[::-1][:dim]
    L = np.sqrt(np.maximum(vals[order], 0))
    X = vecs[:, order] * L
    if X.shape[1] < dim:
        X = np.hstack([X, np.zeros((n, dim - X.shape[1]))])
    return X


def smacof(D: np.ndarray, W: np.ndarray, X0: np.ndarray, iters: int = 500, tol: float = 1e-10) -> np.ndarray:
    n = len(D)
    W = W.copy()
    np.fill_diagonal(W, 0)
    V = -W.copy()
    np.fill_diagonal(V, W.sum(axis=1))
    Vp = np.linalg.pinv(V)
    X = X0.copy()
    Dz = np.where(np.isfinite(D), D, 0)
    prev = None
    for _ in range(iters):
        diff = X[:, None, :] - X[None, :, :]
        dist = np.sqrt((diff**2).sum(-1))
        with np.errstate(divide="ignore", invalid="ignore"):
            ratio = np.where(dist > 1e-12, Dz / dist, 0.0)
        B = -W * ratio
        np.fill_diagonal(B, 0)
        np.fill_diagonal(B, -B.sum(axis=1))
        X = Vp @ B @ X
        stress = float((W * (dist - Dz) ** 2).sum()) / 2
        if prev is not None and abs(prev - stress) < tol:
            break
        prev = stress
    return X


def canonicalize(X: np.ndarray) -> np.ndarray:
    """Centre on the centroid, put device 0 -> device 1 along +x, device 2 above."""
    X = np.asarray(X, dtype=np.float64).copy()
    n = len(X)
    if n == 0:
        return X
    X -= X.mean(axis=0)
    if n >= 2:
        v = X[1] - X[0]
        ang = math.atan2(v[1], v[0])
        ca, sa = math.cos(-ang), math.sin(-ang)
        R = np.array([[ca, -sa], [sa, ca]])
        X = X @ R.T
    if n >= 3:
        # reflect so that device 2 is on the positive-y side of the 0->1 line
        if X[2, 1] - X[0, 1] < 0:
            X[:, 1] *= -1
    return X


@dataclass
class Layout:
    positions: np.ndarray
    distances: np.ndarray  # measured (NaN where unknown)
    fitted: np.ndarray  # distances implied by positions
    residuals: np.ndarray  # fitted - measured
    rms_residual: float
    excluded_pairs: list = field(default_factory=list)

    def as_dict(self):
        def clean(a):
            return [[None if not np.isfinite(v) else float(v) for v in row] for row in a]

        return {
            "positions": [[float(p[0]), float(p[1])] for p in self.positions],
            "distances": clean(self.distances),
            "fitted": clean(self.fitted),
            "residuals": clean(self.residuals),
            "rms_residual": float(self.rms_residual),
            "excluded_pairs": self.excluded_pairs,
        }


def fit_layout(D: np.ndarray, W: np.ndarray | None = None, reject_outliers: bool = True) -> Layout:
    D = np.asarray(D, dtype=np.float64)
    n = len(D)
    if n == 1:
        P = np.zeros((1, 2))
        return Layout(P, D, np.zeros((1, 1)), np.zeros((1, 1)), 0.0)
    if n == 2:
        d = D[0, 1] if np.isfinite(D[0, 1]) else 1.0
        P = np.array([[-d / 2, 0.0], [d / 2, 0.0]])
        F = np.array([[0, d], [d, 0]])
        return Layout(P, D, F, F - np.nan_to_num(D), 0.0)
    if W is None:
        W = np.where(np.isfinite(D), 1.0, 0.0)
    W = W.copy()
    np.fill_diagonal(W, 0)
    excluded = []

    def solve(Wk):
        Dk = np.where(Wk > 0, D, np.nan)
        Xk = smacof(Dk, Wk, classical_mds(_fill_missing(Dk)))
        Fk = np.sqrt(((Xk[:, None, :] - Xk[None, :, :]) ** 2).sum(-1))
        used = Wk > 0
        rk = float(np.sqrt(np.mean((Fk - np.nan_to_num(D))[used] ** 2))) if used.any() else 0.0
        return Xk, Fk, rk

    X, F, rms = solve(W)
    # Leave-one-pair-out outlier rejection: if dropping a single measurement
    # makes everything else fit far better, that measurement was bad
    # (typically a reflection detected instead of the direct path).
    while reject_outliers and rms > 0.05 and len(excluded) < max(0, n - 3):
        links = (W > 0).sum(axis=1)
        best = None
        for i, j in zip(*np.triu_indices(n, 1)):
            if W[i, j] <= 0 or links[i] <= 2 or links[j] <= 2:
                continue
            W2 = W.copy()
            W2[i, j] = W2[j, i] = 0
            X2, F2, r2 = solve(W2)
            if best is None or r2 < best[0]:
                best = (r2, i, j, X2, F2)
        if best is None or best[0] > rms / 3:
            break
        r2, i, j, X, F = best
        if abs(F[i, j] - D[i, j]) < 0.15:
            break
        W[i, j] = W[j, i] = 0
        excluded.append([int(i), int(j)])
        rms = r2
    X = canonicalize(X)
    F = np.sqrt(((X[:, None, :] - X[None, :, :]) ** 2).sum(-1))
    R = np.where(np.isfinite(D), F - np.nan_to_num(D), np.nan)
    np.fill_diagonal(R, 0)
    used = (W > 0) & np.isfinite(R)
    rms = float(np.sqrt(np.mean(R[used] ** 2))) if used.any() else 0.0
    return Layout(X, D, F, R, rms, excluded)


def refine_layout(
    D: np.ndarray,
    X0: np.ndarray,
    sigma_meas: float = 0.05,
    sigma_prior: float = 1.0,
    outlier: float = 0.15,
) -> Layout:
    """
    Fit positions to measured distances, starting from (and weakly held to) a
    rough placement X0 supplied by the user.

    The rough placement does three jobs: it fixes rotation, translation and
    mirror image, so the result is already in the user's frame; it gives a
    position to devices nothing could range to; and it says *which*
    measurement is wrong when they are inconsistent. (With few devices the
    measurements alone can show that one is an echo but not which one: every
    choice fits. The right choice is the one that moves the devices least
    from where the user put them.)
    """
    from itertools import combinations

    D = np.asarray(D, dtype=np.float64)
    X0 = np.asarray(X0, dtype=np.float64)
    n = len(D)
    all_pairs = [(i, j) for i in range(n) for j in range(i + 1, n) if np.isfinite(D[i, j])]
    wp = sigma_meas / sigma_prior

    def solve(pairs):
        X = X0.copy()
        m = len(pairs)

        def rj(X):
            r = np.zeros(m + 2 * n)
            J = np.zeros((m + 2 * n, 2 * n))
            for k, (i, j) in enumerate(pairs):
                v = X[i] - X[j]
                d = float(np.hypot(*v)) + 1e-9
                r[k] = d - D[i, j]
                J[k, 2 * i : 2 * i + 2] = v / d
                J[k, 2 * j : 2 * j + 2] = -v / d
            r[m:] = wp * (X - X0).ravel()
            J[m:] = wp * np.eye(2 * n)
            return r, J

        lam = 1e-3
        r, J = rj(X)
        for _ in range(200):
            step = np.linalg.solve(J.T @ J + lam * np.eye(2 * n), -J.T @ r)
            X2 = X + step.reshape(n, 2)
            r2, J2 = rj(X2)
            if float(r2 @ r2) <= float(r @ r):
                X, r, J = X2, r2, J2
                lam = max(lam / 3, 1e-9)
                if np.abs(step).max() < 1e-7:
                    break
            else:
                lam *= 10
                if lam > 1e9:
                    break
        res = r[:m]
        worst = float(np.abs(res).max()) if m else 0.0
        return X, res, worst, float(((X - X0) ** 2).sum())

    X, res, worst, _ = solve(all_pairs)
    excluded: list = []
    if worst > outlier:
        max_drop = max(1, len(all_pairs) // 4)
        for k in range(1, max_drop + 1):
            best = None
            for drop in combinations(range(len(all_pairs)), k):
                keep = [p for q, p in enumerate(all_pairs) if q not in drop]
                Xk, resk, worstk, moved = solve(keep)
                if worstk <= outlier and (best is None or moved < best[0]):
                    best = (moved, Xk, resk, drop)
            if best is not None:
                _, X, res, drop = best
                excluded = [[int(all_pairs[q][0]), int(all_pairs[q][1])] for q in drop]
                break
    F = np.sqrt(((X[:, None, :] - X[None, :, :]) ** 2).sum(-1))
    rms = float(np.sqrt(np.mean(np.square(res)))) if len(res) else 0.0
    return Layout(X, D, F, np.where(np.isfinite(D), F - D, 0.0), rms, excluded)


def procrustes_align(X: np.ndarray, Y: np.ndarray):
    """
    Find rotation/reflection + translation mapping X onto Y (no scaling).
    Returns (aligned X, transform function).
    """
    X = np.asarray(X, dtype=np.float64)
    Y = np.asarray(Y, dtype=np.float64)
    mx, my = X.mean(0), Y.mean(0)
    A, B = X - mx, Y - my
    U, _, Vt = np.linalg.svd(A.T @ B)
    R = U @ Vt

    def transform(P):
        return (np.asarray(P, dtype=np.float64) - mx) @ R + my

    return transform(X), transform


# ---------------------------------------------------------------------------
# Time difference of arrival localisation
# ---------------------------------------------------------------------------


@dataclass
class TdoaResult:
    x: float
    y: float
    t0: float
    rms_residual: float  # seconds
    residuals: list
    grid: dict | None = None

    def as_dict(self, include_grid=True):
        d = {
            "x": self.x,
            "y": self.y,
            "t0": self.t0,
            "rms_residual": self.rms_residual,
            "rms_residual_m": self.rms_residual * SPEED_OF_SOUND,
            "residuals": self.residuals,
        }
        if include_grid and self.grid is not None:
            d["grid"] = self.grid
        return d


def locate_tdoa(
    positions: np.ndarray,
    times: np.ndarray,
    c: float = SPEED_OF_SOUND,
    margin: float = 3.0,
    step: float = 0.05,
    sigma: float = 0.0005,
    grid_size: int = 48,
    bounds=None,
) -> TdoaResult | None:
    """
    Find (x, y, t0) minimising sum (t_i - t0 - |p - p_i|/c)^2.

    A grid search over the bounding box of the listeners (plus margin) avoids
    local minima, then Gauss-Newton refines the best cell. The answer never
    leaves the searched area: with inconsistent timings the refinement would
    otherwise run off to infinity. Also returns a coarse likelihood grid for
    display.
    """
    P = np.asarray(positions, dtype=np.float64)
    t = np.asarray(times, dtype=np.float64)
    ok = np.isfinite(t)
    P, t = P[ok], t[ok]
    if len(t) < 3:
        return None
    xmin, ymin = P.min(0) - margin
    xmax, ymax = P.max(0) + margin
    if bounds is not None:
        # the room's walls, if the user has drawn them: nothing is outside
        xmin, xmax = sorted((float(bounds[0]), float(bounds[2])))
        ymin, ymax = sorted((float(bounds[1]), float(bounds[3])))
    xs = np.arange(xmin, xmax + step / 2, step)
    ys = np.arange(ymin, ymax + step / 2, step)
    GX, GY = np.meshgrid(xs, ys)
    pts = np.stack([GX.ravel(), GY.ravel()], 1)
    tau = np.sqrt(((pts[:, None, :] - P[None, :, :]) ** 2).sum(-1)) / c
    resid = t[None, :] - tau
    t0 = resid.mean(1)
    cost = ((resid - t0[:, None]) ** 2).sum(1)
    k = int(np.argmin(cost))
    x, y, tt0 = pts[k, 0], pts[k, 1], t0[k]
    gx0, gy0, gt0 = float(x), float(y), float(tt0)
    # Gauss-Newton refinement
    for _ in range(30):
        d = np.sqrt((x - P[:, 0]) ** 2 + (y - P[:, 1]) ** 2) + 1e-9
        r = t - tt0 - d / c
        J = np.stack([(x - P[:, 0]) / (c * d), (y - P[:, 1]) / (c * d), np.ones_like(d)], 1)
        try:
            delta, *_ = np.linalg.lstsq(J, r, rcond=None)
        except np.linalg.LinAlgError:
            break
        x, y, tt0 = x + delta[0], y + delta[1], tt0 + delta[2]
        if np.abs(delta[:2]).max() < 1e-5:
            break
        if not (np.isfinite(x) and np.isfinite(y)) or abs(x - gx0) > 2 * step or abs(y - gy0) > 2 * step:
            # left the grid cell it started in: the refinement is diverging
            x, y, tt0 = gx0, gy0, gt0
            break
    d = np.sqrt((x - P[:, 0]) ** 2 + (y - P[:, 1]) ** 2)
    r = t - tt0 - d / c
    dof = max(1, len(t) - 3)
    rms = float(np.sqrt((r**2).sum() / dof)) if len(t) > 3 else float(np.sqrt(np.mean(r**2)))
    # coarse likelihood grid for display
    gx = np.linspace(xmin, xmax, grid_size)
    gy = np.linspace(ymin, ymax, grid_size)
    GX2, GY2 = np.meshgrid(gx, gy)
    pts2 = np.stack([GX2.ravel(), GY2.ravel()], 1)
    tau2 = np.sqrt(((pts2[:, None, :] - P[None, :, :]) ** 2).sum(-1)) / c
    res2 = t[None, :] - tau2
    res2 = res2 - res2.mean(1, keepdims=True)
    cost2 = (res2**2).sum(1)
    loglik = -cost2 / (2 * sigma**2)
    grid = {
        "x0": float(xmin),
        "y0": float(ymin),
        "x1": float(xmax),
        "y1": float(ymax),
        "nx": grid_size,
        "ny": grid_size,
        "loglik": [round(float(v), 2) for v in np.maximum(loglik - loglik.max(), -50)],
    }
    return TdoaResult(float(x), float(y), float(tt0), rms, [float(v) for v in r], grid)
