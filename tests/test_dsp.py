import numpy as np
import pytest

from beep_locator import analysis, dsp
from beep_locator.sim import Room, SimDevice


def make_room(seed=1, n=5, noise=0.002, reflection_gain=0.35):
    rng = np.random.default_rng(seed)
    pts = [(0.3, 0.4), (4.6, 0.6), (4.2, 3.7), (0.5, 3.3), (2.4, 2.0), (2.5, 0.2)][:n]
    devices = []
    for k, (x, y) in enumerate(pts):
        devices.append(
            SimDevice(
                x=x,
                y=y,
                sr=[48000.0, 44100.0][k % 2],
                ppm=float(rng.uniform(-40, 40)),
                t0=float(rng.uniform(-50, 50)),
                in_latency=float(rng.uniform(0.005, 0.08)),
                out_latency=float(rng.uniform(0.005, 0.12)),
                noise=noise,
            )
        )
    return Room(devices=devices, seed=seed, reflection_gain=reflection_gain)


def coarse(room, k, true_time, rng, err=0.08):
    """What the server's network clock sync would give: ~frame +- err seconds."""
    d = room.devices[k]
    return float(d.frame_at(true_time + rng.uniform(-err, err)))


def run_calibration(room, rng, ids, start=10.0, gap=1.0):
    emitters = []
    for j, dev in enumerate(room.devices):
        t_sched = start + j * gap
        play_frame = coarse(room, j, t_sched, rng, 0.0)
        room.play(j, play_frame, dsp.chirp_for_slot(dev.sr, j))
        expected = {ids[i]: coarse(room, i, t_sched, rng) for i in range(len(ids))}
        expected[ids[j]] = play_frame
        emitters.append({"device_id": ids[j], "slot": j, "expected": expected})
    recs = []
    for i, dev in enumerate(room.devices):
        f0 = int(dev.frame_at(start - 0.5))
        n = int((len(room.devices) * gap + 1.5) * dev.sr)
        recs.append(analysis.Recording(ids[i], dev.sr, f0, room.record(i, f0, n)))
    return analysis.analyse_calibration(recs, emitters)


def true_distances(room):
    P = np.array([[d.x, d.y] for d in room.devices])
    return np.sqrt(((P[:, None] - P[None]) ** 2).sum(-1)), P


def test_chirp_detection_exact():
    sr = 48000
    ch = dsp.make_chirp(sr)
    x = np.zeros(sr)
    x[12345 : 12345 + len(ch)] += ch
    det = dsp.detect_chirp(x, ch, sr=sr)
    assert abs(det.index - 12345) < 0.2


def test_two_way_ranging_cancels_latency_and_offsets():
    rng = np.random.default_rng(3)
    room = make_room(seed=3)
    ids = [f"d{i}" for i in range(len(room.devices))]
    res = run_calibration(room, rng, ids)
    D = np.array([[np.nan if v is None else v for v in row] for row in res["distances"]])
    T, _ = true_distances(room)
    iu = np.triu_indices(len(ids), 1)
    err = np.abs(D[iu] - T[iu])
    assert np.isfinite(D[iu]).all()
    # sub-3cm despite random latencies of up to 200ms round trip and +-40ppm clocks
    assert err.max() < 0.03, err


def test_layout_recovers_geometry():
    rng = np.random.default_rng(5)
    room = make_room(seed=5)
    ids = [f"d{i}" for i in range(len(room.devices))]
    res = run_calibration(room, rng, ids)
    lay = analysis.layout_from_distances(ids, res["distances"])
    _, P = true_distances(room)
    aligned, _ = dsp.procrustes_align(np.array(lay["positions"]), P)
    assert np.abs(aligned - P).max() < 0.05


def test_layout_rejects_one_bad_pair():
    _, P = true_distances(make_room(n=6))
    D = np.sqrt(((P[:, None] - P[None]) ** 2).sum(-1))
    D[1, 3] = D[3, 1] = D[1, 3] + 1.5
    lay = dsp.fit_layout(D)
    assert [1, 3] in lay.excluded_pairs
    aligned, _ = dsp.procrustes_align(lay.positions, P)
    assert np.abs(aligned - P).max() < 0.01


def test_tdoa_exact():
    P = np.array([[0, 0], [4, 0], [4, 3], [0, 3.0]])
    src = np.array([1.2, 2.1])
    t = np.sqrt(((P - src) ** 2).sum(1)) / dsp.SPEED_OF_SOUND + 5.0
    r = dsp.locate_tdoa(P, t)
    assert abs(r.x - src[0]) < 1e-3 and abs(r.y - src[1]) < 1e-3


@pytest.mark.parametrize("seed", [1, 2, 7])
def test_end_to_end_beep_location(seed):
    rng = np.random.default_rng(seed)
    room = make_room(seed=seed)
    ids = [f"d{i}" for i in range(len(room.devices))]
    cal = run_calibration(room, rng, ids)
    lay = analysis.layout_from_distances(ids, cal["distances"])
    # mystery beep somewhere in the room, 30s later
    src = (float(rng.uniform(0.8, 4.0)), float(rng.uniform(0.8, 3.0)))
    t_beep = 40.0
    room.emit_at(src[0], src[1], t_beep, dsp.make_beep(48000, 3150), 48000, gain=0.5)
    # sync chirp from device 0 ~1.2s later
    e = 0
    t_sync = t_beep + 1.2
    play = coarse(room, e, t_sync, rng, 0.0)
    room.play(e, play, dsp.chirp_for_slot(room.devices[e].sr, 0))
    sync = {"device_id": ids[e], "slot": 0, "expected": {ids[i]: coarse(room, i, t_sync, rng) for i in range(len(ids))}}
    sync["expected"][ids[e]] = play
    beep_expected = {ids[i]: coarse(room, i, t_beep, rng) for i in range(len(ids))}
    recs = []
    for i, dev in enumerate(room.devices):
        f0 = int(dev.frame_at(t_beep - 0.8))
        n = int(2.8 * dev.sr)
        recs.append(analysis.Recording(ids[i], dev.sr, f0, room.record(i, f0, n)))
    res = analysis.analyse_beep(recs, lay["positions_by_id"], sync, beep_expected)
    assert "estimate" in res, res
    assert abs(res["freq"] - 3150) < 30
    # compare in the true coordinate frame
    _, P = true_distances(room)
    _, transform = dsp.procrustes_align(np.array(lay["positions"]), P)
    est = transform(np.array([[res["estimate"]["x"], res["estimate"]["y"]]]))[0]
    err = float(np.hypot(est[0] - src[0], est[1] - src[1]))
    assert err < 0.2, (err, est, src)
