"""
Server-side orchestration: calibration, locating beeps, test beeps.

Each procedure talks to devices through ``hub.rpc`` using a handful of
methods every listener runtime provides:

    play  {samples (base64 float32), frame}  -> {frame: scheduled frame}
    clip  {start, end, label}                -> {clip_id, start, n, sr}
    identify {}                              -> flash + blip
"""

from __future__ import annotations

import asyncio
import time

import numpy as np

from . import analysis, dsp
from . import mystery
from .hub import Conn, Hub, RpcError, encode_f32

LATENCY_GUESS = 0.04  # typical output+input latency, used to centre search windows


class Busy(Exception):
    pass


def _chirp_params(hub: Hub) -> dict:
    ch = hub.settings.get("chirp", {})
    return {
        "f_lo": float(ch.get("f_lo", 2000)),
        "f_hi": float(ch.get("f_hi", 7000)),
        "duration": float(ch.get("duration", 0.08)),
        "amplitude": float(ch.get("amplitude", 0.9)),
    }


async def _sleep_until(t: float):
    delay = t - time.time()
    if delay > 0:
        await asyncio.sleep(delay)


async def _fetch_recordings(hub: Hub, devs: list[Conn], t_start: float, t_end: float, label: str) -> tuple[list, list]:
    async def one(c: Conn):
        args = {
            "start": int(c.clock.to_frame(t_start)),
            "end": int(c.clock.to_frame(t_end)),
            "label": label,
        }
        res = await hub.rpc(c, "clip", args, timeout=40)
        row, samples = hub.store.load_clip(res["clip_id"])
        return analysis.Recording(c.id, float(row["sr"]), int(row["start_frame"]), samples), res["clip_id"]

    pairs = await asyncio.gather(*(one(c) for c in devs), return_exceptions=True)
    recs, clip_ids = [], []
    for c, p in zip(devs, pairs):
        if isinstance(p, Exception):
            await hub.progress(f"Could not fetch audio from {c.name}: {p}")
            continue
        recs.append(p[0])
        clip_ids.append(p[1])
    return recs, clip_ids


async def calibrate(hub: Hub, rounds: int | None = None, gap: float | None = None, devices=None) -> dict:
    if hub.procedure_lock.locked():
        raise Busy(f"busy: {hub.current_procedure}")
    async with hub.procedure_lock:
        hub.current_procedure = "calibrate"
        hub.mark_dirty()
        try:
            return await _calibrate(hub, rounds, gap, devices)
        finally:
            hub.current_procedure = None
            hub.mark_dirty()


async def _calibrate(hub: Hub, rounds, gap, devices) -> dict:
    s = hub.settings
    rounds = int(rounds or s.get("calibration_rounds", 2))
    gap = float(gap or s.get("calibration_gap", 1.0))
    devs = hub.listeners()
    if devices:
        wanted = {c.id for c in hub.resolve_targets(devices)}
        devs = [c for c in devs if c.id in wanted]
    if len(devs) < 2:
        raise ValueError(f"need at least 2 listening devices, have {len(devs)}")
    params = _chirp_params(hub)
    c_sound = hub.speed_of_sound
    matrices, details, all_clips = [], [], []
    for r in range(rounds):
        await hub.progress(f"Calibration round {r + 1}/{rounds}: {len(devs)} devices chirp in turn")
        max_rtt = max(c.clock.rtt for c in devs)
        T0 = time.time() + 0.6 + max_rtt
        t_end = T0 + len(devs) * gap + 0.6
        hub.suppress(T0 - 0.4, t_end + 0.5, "calibration")
        plays = []
        for k, c in enumerate(devs):
            sig = dsp.chirp_for_slot(c.sr, k, **params)
            plays.append((c, k, T0 + k * gap, sig))
        replies = await asyncio.gather(
            *(hub.rpc(c, "play", {"samples": encode_f32(sig), "frame": c.clock.to_frame(t)}) for c, k, t, sig in plays),
            return_exceptions=True,
        )
        await _sleep_until(t_end)
        recs, clip_ids = await _fetch_recordings(hub, devs, T0 - 0.5, t_end, f"cal-r{r}")
        all_clips += clip_ids
        emitters = []
        for (c, k, t, sig), rep in zip(plays, replies):
            if isinstance(rep, Exception):
                await hub.progress(f"{c.name} failed to play: {rep}")
                continue
            sched = float(rep["frame"])
            t_actual = c.clock.to_time(sched)
            expected = {o.id: o.clock.to_frame(t_actual) + LATENCY_GUESS * o.sr for o in devs}
            expected[c.id] = sched
            emitters.append({"device_id": c.id, "slot": k, "expected": expected})
        res = analysis.analyse_calibration(
            recs,
            emitters,
            c=c_sound,
            self_distance=float(s.get("self_distance", dsp.DEFAULT_SELF_DISTANCE)),
            chirp_params=params,
        )
        # analyse_calibration orders matrices by recordings; re-map to devs order
        order = [res["device_ids"].index(c.id) if c.id in res["device_ids"] else None for c in devs]
        D = [[(res["distances"][a][b] if a is not None and b is not None else None) for b in order] for a in order]
        matrices.append(D)
        details.append(res)
        missing = sum(1 for d in res["detections"] if "frame" not in d)
        await hub.progress(f"Round {r + 1}: {len(res['detections']) - missing}/{len(res['detections'])} chirps detected")
    # speaker check: was each device's chirp heard by anything (itself included)?
    for c in devs:
        heard = False
        for res in details:
            if c.id in res["device_ids"]:
                j = res["device_ids"].index(c.id)
                heard = heard or any(row[j] is not None for row in res["snr"])
        hub.set_speaker(c.id, heard, "calibration")
    ids = [c.id for c in devs]
    D = analysis.combine_distance_matrices(matrices)
    rough = {d: p for d, p in (s.get("rough_positions") or {}).items() if d in ids}
    use_rough = len(rough) == len(ids)
    # a device nothing could range to has no measured position: keep it only
    # if the user placed it by hand, and say so
    ranged = [bool(np.isfinite(np.delete(D[k], k)).any()) for k in range(len(ids))]
    unlocated = [c.name for c, ok in zip(devs, ranged) if not ok]
    if not use_rough and unlocated:
        keep = [k for k, ok in enumerate(ranged) if ok]
        if len(keep) < 2:
            raise ValueError("no device heard any other device's chirp")
        devs = [devs[k] for k in keep]
        ids = [c.id for c in devs]
        D = D[np.ix_(keep, keep)]
    layout = analysis.layout_from_distances(ids, D.tolist(), rough if use_rough else None)
    layout["from_rough"] = use_rough
    layout["unlocated"] = [c.id for c, ok in zip(devs, ranged) if not ok] if use_rough else []
    if use_rough:
        hub.view = {"rotation": 0.0, "flip": False}
        hub.store.set_setting("view", hub.view)
    layout["names"] = {c.id: c.name for c in devs}
    layout["colors"] = {c.id: c.color for c in devs}
    layout["speed_of_sound"] = c_sound
    layout["ts"] = time.time()
    finite = D[np.triu_indices(len(ids), 1)]
    summary = {
        "devices": len(ids),
        "pairs_measured": int(np.isfinite(finite).sum()),
        "pairs_total": int(len(finite)),
        "rms_residual_m": layout["rms_residual"],
        "excluded_pairs": layout["excluded_pairs"],
        "unlocated": unlocated,
        "from_rough": use_rough,
    }
    data = {"layout": layout, "rounds": details, "clip_ids": all_clips, "chirp": params}
    rid = await hub.publish_result("calibration", data, summary)
    layout["result_id"] = rid
    hub.layout = layout
    hub.mark_dirty()
    await hub.progress(
        f"Calibrated {len(ids)} devices: {summary['pairs_measured']}/{summary['pairs_total']} distances, "
        f"fit error {layout['rms_residual'] * 100:.1f}cm"
        + (f"; ignored {len(layout['excluded_pairs'])} inconsistent distance(s)" if layout["excluded_pairs"] else "")
        + (f"; could not range to {', '.join(unlocated)}" + (" (left where you placed it)" if use_rough else " (left off the map)") if unlocated else "")
    )
    return {"result_id": rid, "summary": summary, "layout": layout}


def _room(hub: Hub):
    """[x0, y0, x1, y1] walls drawn by the user (map metres), or None."""
    r = hub.settings.get("room")
    if isinstance(r, (list, tuple)) and len(r) == 4 and abs(r[2] - r[0]) > 0.5 and abs(r[3] - r[1]) > 0.5:
        return [float(v) for v in r]
    return None


def _mystery_state(hub: Hub) -> dict:
    m = hub.settings.get("mystery") or {}
    return {
        "marked": dict(m.get("marked") or {}),  # result id -> signature, confirmed by the user
        "scores": dict(m.get("scores") or {}),  # result id -> 0..1 likeness to the confirmed ones
        "threshold": float(m.get("threshold", mystery.DEFAULT_THRESHOLD)),
        "estimate": m.get("estimate"),
    }


def _result_signature(hub: Hub, res: dict | None) -> dict | None:
    data = (res or {}).get("data") or {}
    if data.get("signature"):
        return data["signature"]
    recs = []
    for cid in data.get("clip_ids") or []:
        try:
            row, samples = hub.store.load_clip(cid)
        except Exception:
            continue
        recs.append(analysis.Recording(row["device_id"], float(row["sr"]), int(row["start_frame"]), samples))
    return mystery.signature(recs, data.get("devices") or [])


# Bump when the way sounds are timed changes: older results are then re-timed
# from their stored recordings before being pooled with newer ones.
TIMING_VERSION = 2
_REANALYSED: dict = {}


def _mystery_pitch(m: dict) -> float | None:
    """Pitch of the confirmed mystery beep, if it has one."""
    f = [sig["freq"] for sig in m["marked"].values() if sig.get("tonal") and sig.get("freq")]
    return float(np.median(f)) if f else None


def _reanalyse(hub: Hub, rid: int, data: dict, f0: float | None) -> dict | None:
    """
    Re-time one stored sound from its recordings with the current rules. With
    ``f0`` it looks specifically for a beep at that pitch: a located "sound"
    is often somebody talking with the beep a moment later in the same clip.
    Returns {"used", "times", "signature", "estimate"} or None.
    """
    layout = hub.layout or {}
    positions = layout.get("positions_by_id") or {}
    key = (rid, round(f0) if f0 else None, layout.get("result_id"))
    if key in _REANALYSED:
        return _REANALYSED[key]
    recs, exp_c, exp_b = [], {}, {}
    emitter = data.get("sync_device")
    by_dev = {d["device_id"]: d for d in data.get("devices", [])}
    for cid in data.get("clip_ids") or []:
        try:
            row, samples = hub.store.load_clip(cid)
        except Exception:
            continue
        d = by_dev.get(row["device_id"]) or {}
        if not d.get("chirp") or d.get("onset_frame") is None:
            continue
        sr = float(row["sr"])
        recs.append(analysis.Recording(row["device_id"], sr, int(row["start_frame"]), samples))
        exp_c[row["device_id"]] = d["chirp"]["strongest_frame"] - (0 if row["device_id"] == emitter else 0.02 * sr)
        exp_b[row["device_id"]] = d["onset_frame"]
    res = None
    if len(recs) >= 3 and emitter:
        try:
            out = analysis.analyse_beep(
                recs,
                positions,
                {"device_id": emitter, "slot": 0, "expected": exp_c},
                exp_b,
                c=layout.get("speed_of_sound", hub.speed_of_sound),
                self_distance=float(hub.settings.get("self_distance", dsp.DEFAULT_SELF_DISTANCE)),
                chirp_params=_chirp_params(hub),
                freq_hint=f0,
                tonal=True if f0 else data.get("kind", "beep") != "clap",
                bounds=_room(hub),
            )
            times = {d["device_id"]: d["time"] for d in out["devices"] if "time" in d}
            res = {
                "used": [d for d in out["used"] if d in times] if out.get("estimate") else [],
                "times": times,
                "signature": mystery.signature(recs, out["devices"]),
                "estimate": out.get("estimate"),
            }
        except Exception:
            res = None
    if len(_REANALYSED) > 500:
        _REANALYSED.clear()
    _REANALYSED[key] = res
    return res


def _mystery_refresh(hub: Hub, m: dict, rescore: bool = False) -> dict:
    """Re-score recent sounds (if asked) and pool every match into one position."""
    confirmed = list(m["marked"].values())
    f0 = _mystery_pitch(m)
    layout = hub.layout or {}
    positions = layout.get("positions_by_id") or {}
    if rescore:
        m["scores"] = {}
        if confirmed:
            for r in hub.store.results(kind="locate", limit=80):
                res = hub.store.result(r["id"])
                best = mystery.score(_result_signature(hub, res), confirmed)
                if f0 and best < m["threshold"]:
                    re = _reanalyse(hub, r["id"], res.get("data") or {}, f0)
                    if re:
                        best = max(best, mystery.score(re["signature"], confirmed))
                m["scores"][str(r["id"])] = round(best, 3)
    ids = set(m["marked"]) | {k for k, v in m["scores"].items() if v >= m["threshold"]}
    sets, used = [], []
    for rid in sorted(ids, key=int)[-40:]:
        data = (hub.store.result(int(rid)) or {}).get("data") or {}
        # only hearings timed against the current map can be pooled
        if data.get("calibration_id") != layout.get("result_id"):
            continue
        own_ok = data.get("timing_version") == TIMING_VERSION and mystery.score(data.get("signature"), confirmed) >= m["threshold"]
        if own_ok and data.get("estimate"):
            times = {d["device_id"]: d["time"] for d in data.get("devices", []) if "time" in d}
            devs = [d for d in data.get("used", []) if d in times and d in positions]
        else:
            re = _reanalyse(hub, int(rid), data, f0)
            if not re or mystery.score(re["signature"], confirmed) < m["threshold"]:
                continue
            devs, times = [d for d in re["used"] if d in positions], re["times"]
        if len(devs) >= 3:
            sets.append(([positions[d] for d in devs], [times[d] for d in devs]))
            used.append(int(rid))
    est = mystery.locate_many(sets, c=layout.get("speed_of_sound", hub.speed_of_sound), bounds=_room(hub)) if sets else None
    if est:
        est["result_ids"] = used
    m["estimate"] = est
    m["matches"] = len(ids)
    m["pitch"] = f0
    hub.update_settings({"mystery": m})
    hub.mark_dirty()
    return m


def _sync_emitter(hub: Hub, devs: list[Conn]) -> Conn:
    pref = hub.settings.get("sync_device")
    for c in devs:
        if pref and (c.id == pref or (c.name or "").lower() == str(pref).lower()):
            return c
    # prefer a non-simulated laptop-ish device with good clock; otherwise first
    return sorted(devs, key=lambda c: c.clock.rtt)[0]


async def locate(hub: Hub, t_beep: float, onsets: dict | None = None, source: str | None = None, label: str = "beep", trigger=None):
    if hub.procedure_lock.locked():
        raise Busy(f"busy: {hub.current_procedure}")
    async with hub.procedure_lock:
        hub.current_procedure = "locate"
        hub.mark_dirty()
        try:
            return await _locate(hub, t_beep, onsets or {}, source, label, trigger)
        finally:
            hub.current_procedure = None
            hub.mark_dirty()


async def _locate(hub: Hub, t_beep: float, onsets: dict, source, label, trigger):
    if not hub.layout:
        raise ValueError("not calibrated yet")
    positions = hub.layout["positions_by_id"]
    hand_placed = set(hub.layout.get("unlocated") or [])
    devs = [c for c in hub.listeners() if c.id in positions and c.id not in hand_placed]
    if len(devs) < 3:
        raise ValueError(f"need 3 calibrated listening devices, have {len(devs)}")
    params = _chirp_params(hub)
    emitter = _sync_emitter(hub, devs)
    t_sync = max(time.time() + 0.3 + emitter.clock.rtt, t_beep + 0.9)
    hub.suppress(t_sync - 0.3, t_sync + 0.8, "sync")
    sig = dsp.chirp_for_slot(emitter.sr, 0, **params) * float(hub.settings.get("sync_gain", 1.0))
    rep = await hub.rpc(emitter, "play", {"samples": encode_f32(sig), "frame": emitter.clock.to_frame(t_sync)})
    sched = float(rep["frame"])
    t_sync_actual = emitter.clock.to_time(sched)
    t_end = t_sync_actual + 0.8
    await _sleep_until(t_end)
    recs, clip_ids = await _fetch_recordings(hub, devs, t_beep - 0.6, t_end, label)
    sync = {
        "device_id": emitter.id,
        "slot": 0,
        "expected": {o.id: o.clock.to_frame(t_sync_actual) + LATENCY_GUESS * o.sr for o in devs},
    }
    sync["expected"][emitter.id] = sched
    beep_expected = {}
    for o in devs:
        if o.id in onsets:
            beep_expected[o.id] = float(onsets[o.id])
        else:
            beep_expected[o.id] = o.clock.to_frame(t_beep) + LATENCY_GUESS * o.sr
    # clap (broadband, no clear pitch) or beep (tonal)? The listeners report
    # how far the strongest frequency stood above the rest when they triggered.
    proms = sorted(float(r.get("prominence", 0)) for r in ((trigger or {}).get("reports") or {}).values())
    kind = None
    if label.startswith("testbeep"):
        kind = "beep"
    elif proms:
        kind = "beep" if proms[len(proms) // 2] >= float(hub.settings.get("trigger", {}).get("tonal_prominence", 30.0)) else "clap"
    out = analysis.analyse_beep(
        recs,
        positions,
        sync,
        beep_expected,
        tonal=None if kind is None else kind == "beep",
        bounds=_room(hub),
        c=hub.layout.get("speed_of_sound", hub.speed_of_sound),
        self_distance=float(hub.settings.get("self_distance", dsp.DEFAULT_SELF_DISTANCE)),
        chirp_params=params,
    )
    # If a mystery beep with a pitch has been confirmed, look for exactly that
    # in these recordings: it is often masked by speech or other noise that
    # set the trigger off, and would otherwise be timed on the wrong sound.
    mys = _mystery_state(hub)
    f0 = _mystery_pitch(mys)
    if f0 and not label.startswith("testbeep"):
        try:
            hinted = analysis.analyse_beep(
                recs,
                positions,
                sync,
                beep_expected,
                c=hub.layout.get("speed_of_sound", hub.speed_of_sound),
                self_distance=float(hub.settings.get("self_distance", dsp.DEFAULT_SELF_DISTANCE)),
                chirp_params=params,
                freq_hint=f0,
                tonal=True,
                bounds=_room(hub),
            )
            if mystery.score(mystery.signature(recs, hinted["devices"]), list(mys["marked"].values())) >= mys["threshold"]:
                out, kind = hinted, "beep"
        except Exception:
            pass
    names = {c.id: c.name for c in devs}
    for d in out["devices"]:
        d["name"] = names.get(d["device_id"])
        if d["device_id"] == emitter.id:
            # the sync chirp doubles as a speaker check for whoever played it
            hub.set_speaker(emitter.id, bool(d.get("chirp")) and "sync chirp" not in str(d.get("error", "")), "sync chirp")
    # what it sounded like, and whether that is the beep being hunted
    try:
        sig = mystery.signature(recs, out["devices"])
    except Exception:
        sig = None
    out["signature"] = sig
    out["timing_version"] = TIMING_VERSION
    mscore = round(mystery.score(sig, list(mys["marked"].values())), 3) if sig and mys["marked"] else None
    out["mystery_score"] = mscore
    out.update(
        {
            "t_beep": t_beep,
            "label": label,
            "source": source,
            "sync_device": emitter.id,
            "clip_ids": clip_ids,
            "trigger": trigger,
            "calibration_id": hub.layout.get("result_id"),
        }
    )
    if kind is None:
        kind = "beep" if out.get("freq") else "clap"
    out["kind"] = kind
    est = out.get("estimate")
    summary = {
        "label": label,
        "kind": kind,
        "t": t_beep,
        "freq": out.get("freq"),
        "used": len(out.get("used", [])),
        "loudest": out.get("loudest"),
        "loudest_name": names.get(out.get("loudest")),
    }
    if est:
        summary.update({"x": est["x"], "y": est["y"], "rms_residual_m": est["rms_residual_m"]})
        if source and source in positions:
            sx, sy = positions[source]
            summary["source"] = source
            summary["error_m"] = float(np.hypot(est["x"] - sx, est["y"] - sy))
            out["error_m"] = summary["error_m"]
    else:
        summary["error"] = out.get("error")
    is_mystery = mscore is not None and mscore >= mys["threshold"]
    summary["mystery_score"] = mscore
    summary["mystery"] = is_mystery
    # how loud each listener heard it: repeatable to about 1dB for the same
    # source, so moving one listener and watching its number is a usable
    # warmer/colder search even when the timing is poor
    summary["levels"] = {d["device_id"]: round(float(d["level_db"]), 1) for d in out["devices"] if d.get("level_db") is not None}
    rid = await hub.publish_result("locate", out, summary)
    if mscore is not None:
        mys["scores"][str(rid)] = mscore
        mys = _mystery_refresh(hub, mys)
    msg = f"{kind if label == 'beep' else label}: "
    if est:
        msg += f"estimate ({est['x']:.2f}, {est['y']:.2f}) from {summary['used']} devices, fit {est['rms_residual_m'] * 100:.0f}cm"
        if "error_m" in summary:
            msg += f", {summary['error_m'] * 100:.0f}cm from {names.get(source)}"
    else:
        msg += f"no estimate ({out.get('error')}); loudest at {summary.get('loudest_name')}"
    if is_mystery:
        msg += f" - sounds like the mystery beep ({mscore * 100:.0f}% match)"
    await hub.progress(msg, result_id=rid)
    pooled = mys.get("estimate")
    if is_mystery and pooled:
        await hub.progress(
            f"Mystery beep: {pooled['n']} hearings together put it at ({pooled['x']:.2f}, {pooled['y']:.2f}), within about {pooled['radius_m'] * 100:.0f}cm"
            + (" (at the edge of the searched area: it may be further out)" if pooled.get("at_edge") else "")
        )
    return {"result_id": rid, "summary": summary}


async def testbeep(hub: Hub, device: str, freq: float = 3200.0, duration: float = 0.1, gain: float = 0.8):
    """Make one listener play a fake 'mystery beep' and locate it."""
    targets = hub.resolve_targets(device)
    if not targets:
        raise ValueError(f"no such device: {device}")
    c = targets[0]
    if hub.procedure_lock.locked():
        raise Busy(f"busy: {hub.current_procedure}")
    async with hub.procedure_lock:
        hub.current_procedure = "testbeep"
        hub.mark_dirty()
        try:
            t = time.time() + 0.3 + c.clock.rtt
            hub.suppress(t - 0.3, t + 3.0, "testbeep")
            sig = dsp.make_beep(c.sr, freq=freq, duration=duration, amplitude=gain)
            rep = await hub.rpc(c, "play", {"samples": encode_f32(sig), "frame": c.clock.to_frame(t)})
            t_actual = c.clock.to_time(float(rep["frame"]))
            await hub.progress(f"Test beep from {c.name} at {freq:.0f}Hz")
            return await _locate(hub, t_actual + LATENCY_GUESS, {}, c.id, f"testbeep-{c.name}", {"kind": "testbeep"})
        finally:
            hub.current_procedure = None
            hub.mark_dirty()


async def selftest(hub: Hub, devices=None) -> dict:
    """
    Per-device loopback check, one device at a time: play a chirp, record it,
    and report whether the device hears itself clearly. Also reports what the
    browser says about echo cancellation / AGC (which must be off) and the
    round-trip audio latency. Run this first on new hardware.
    """
    if hub.procedure_lock.locked():
        raise Busy(f"busy: {hub.current_procedure}")
    async with hub.procedure_lock:
        hub.current_procedure = "selftest"
        hub.mark_dirty()
        try:
            devs = hub.listeners()
            if devices:
                wanted = {c.id for c in hub.resolve_targets(devices)}
                devs = [c for c in devs if c.id in wanted]
            if not devs:
                raise ValueError("no listening devices")
            params = _chirp_params(hub)
            rows = []
            for c in devs:
                row = {"device_id": c.id, "name": c.name}
                try:
                    info = await hub.rpc(c, "info", {})
                    track = ((info or {}).get("io") or {}).get("track") or {}
                    row["track"] = track
                    row["io"] = {k: v for k, v in ((info or {}).get("io") or {}).items() if k != "track"}
                    t = time.time() + 0.3 + c.clock.rtt
                    hub.suppress(t - 0.3, t + 1.0, "selftest")
                    sig = dsp.chirp_for_slot(c.sr, 0, **params)
                    rep = await hub.rpc(c, "play", {"samples": encode_f32(sig), "frame": c.clock.to_frame(t)})
                    sched = float(rep["frame"])
                    t_act = c.clock.to_time(sched)
                    await _sleep_until(t_act + 0.8)
                    recs, clip_ids = await _fetch_recordings(hub, [c], t_act - 0.4, t_act + 0.8, f"selftest-{c.name}")
                    if not recs:
                        row["error"] = "no audio"
                    else:
                        rec = recs[0]
                        row["clip_id"] = clip_ids[0]
                        x = rec.samples
                        pre = x[: max(1, int(rec.local(sched)))]
                        row["noise_db"] = dsp.rms_db(pre)
                        row["peak"] = float(np.max(np.abs(x))) if len(x) else 0.0
                        row["clipped"] = row["peak"] > 0.99
                        found = analysis.find_chirp(rec, dsp.chirp_for_slot(rec.sr, 0, **params), sched, 0.05, 0.6, own=True)
                        if found:
                            row["snr"] = found["snr"]
                            row["latency_ms"] = (found["frame"] - sched) / rec.sr * 1000
                        row["ok"] = bool(found and found["snr"] > 10)
                except Exception as e:
                    row["error"] = str(e)
                problems = []
                if row.get("track", {}).get("echoCancellation"):
                    problems.append("echo cancellation is ON")
                if row.get("track", {}).get("autoGainControl"):
                    problems.append("auto gain is ON")
                if row.get("clipped"):
                    problems.append("clipping (lower chirp amplitude?)")
                if not row.get("ok"):
                    problems.append("did not clearly hear its own chirp")
                row["problems"] = problems
                rows.append(row)
                if "error" not in row:
                    hub.set_speaker(c.id, bool(row.get("ok")), "self-test")
                await hub.progress(
                    f"Self-test {c.name}: "
                    + (f"SNR {row.get('snr', 0):.0f}, latency {row.get('latency_ms', float('nan')):.0f}ms, noise {row.get('noise_db', float('nan')):.0f}dB" if "snr" in row else row.get("error", "no chirp found"))
                    + (f" - {'; '.join(problems)}" if problems else " - OK")
                )
            summary = {"devices": len(rows), "ok": sum(1 for r in rows if not r["problems"]), "problems": {r["name"]: r["problems"] for r in rows if r["problems"]}}
            rid = await hub.publish_result("selftest", {"devices": rows}, summary)
            return {"result_id": rid, "summary": summary, "devices": rows}
        finally:
            hub.current_procedure = None
            hub.mark_dirty()


async def identify(hub: Hub, device: str):
    res = await hub.rpc_many(hub.resolve_targets(device), "identify", {}, timeout=5)
    return res


class Monitor:
    """
    Watches 'onset' events reported by listeners and, when something that
    looks like a beep happens, runs ``locate``.
    """

    def __init__(self, hub: Hub):
        self.hub = hub
        self.cluster = None
        self.last_fire = 0.0
        hub.event_listeners.append(self.on_event)

    def on_event(self, conn: Conn, topic: str, data):
        if topic != "onset" or not isinstance(data, dict):
            return
        hub = self.hub
        if not hub.settings.get("monitoring", True):
            return
        if not conn.clock.ready or "frame" not in data:
            return
        trig = hub.settings.get("trigger", {})
        t = conn.clock.to_time(float(data["frame"]))
        if hub.suppressed(t):
            return
        if float(data.get("prominence", 0)) < float(trig.get("min_prominence", 6.0)):
            return
        if time.time() - self.last_fire < float(trig.get("cooldown", 4.0)):
            return
        if hub.procedure_lock.locked():
            return
        if self.cluster is None:
            self.cluster = {"t": t, "onsets": {}, "data": {}}
            asyncio.create_task(self._fire_later(float(trig.get("cluster_window", 0.5))))
        cl = self.cluster
        if conn.id not in cl["onsets"]:
            cl["onsets"][conn.id] = float(data["frame"])
            cl["data"][conn.id] = data
            cl["t"] = min(cl["t"], t)

    async def _fire_later(self, window: float):
        await asyncio.sleep(window)
        cl, self.cluster = self.cluster, None
        if cl is None:
            return
        hub = self.hub
        trig = hub.settings.get("trigger", {})
        if len(cl["onsets"]) < int(trig.get("min_devices", 1)):
            return
        if not hub.layout or hub.procedure_lock.locked():
            return
        self.last_fire = time.time()
        try:
            await locate(hub, cl["t"], cl["onsets"], label="beep", trigger={"kind": "onset", "reports": cl["data"]})
        except Exception as e:
            await hub.progress(f"Locate failed: {e}")


async def run(hub: Hub, name: str, args: dict | None = None):
    args = args or {}
    if name == "calibrate":
        return await calibrate(hub, args.get("rounds"), args.get("gap"), args.get("devices"))
    if name == "testbeep":
        return await testbeep(hub, args["device"], float(args.get("freq", 3200)), float(args.get("duration", 0.1)), float(args.get("gain", 0.8)))
    if name == "locate":
        # manual trigger: "a beep happened about `ago` seconds ago"
        t = time.time() - float(args.get("ago", 0.5))
        return await locate(hub, t, {}, label=args.get("label", "manual"), trigger={"kind": "manual"})
    if name == "identify":
        return await identify(hub, args.get("device", "listeners"))
    if name == "selftest":
        return await selftest(hub, args.get("devices"))
    if name == "monitoring":
        hub.update_settings({"monitoring": bool(args.get("on", True))})
        return {"monitoring": hub.settings["monitoring"]}
    if name == "view":
        hub.view = {"rotation": float(args.get("rotation", 0)), "flip": bool(args.get("flip", False))}
        hub.store.set_setting("view", hub.view)
        hub.mark_dirty()
        return hub.view
    if name == "rename":
        hub.rename(args["device"], args["name"])
        hub.mark_dirty()
        return {"ok": True}
    if name == "settings":
        hub.update_settings(args)
        await hub.broadcast({"type": "settings", "settings": hub.settings})
        return hub.settings
    if name == "rough":
        # hand-placed positions (metres, as drawn on the map). Shown at once
        # as the layout; the next calibration refines them.
        pos = {str(d): [float(p[0]), float(p[1])] for d, p in (args.get("positions") or {}).items()}
        if not pos:
            hub.update_settings({"rough_positions": None})
            return {"ok": True}
        hub.update_settings({"rough_positions": pos})
        known = {d["id"]: d for d in hub.store.all_devices()}
        live = {c.id: c for c in hub.conns.values()}
        ids = list(pos)
        hub.layout = {
            "device_ids": ids,
            "positions": [pos[d] for d in ids],
            "positions_by_id": pos,
            "names": {d: (live[d].name if d in live else (known.get(d) or {}).get("name")) for d in ids},
            "colors": {d: (live[d].color if d in live else (known.get(d) or {}).get("color")) for d in ids},
            "rough": True,
            "rms_residual": 0.0,
            "excluded_pairs": [],
            "unlocated": [],
            "speed_of_sound": hub.speed_of_sound,
            "ts": time.time(),
        }
        hub.view = {"rotation": 0.0, "flip": False}
        hub.store.set_setting("view", hub.view)
        hub.mark_dirty()
        return {"ok": True, "devices": len(ids)}
    if name == "reposition_start":
        # Step 1: stop reacting to sounds while the devices are carried about.
        # The listeners stay connected and keep their microphones open.
        if hub.procedure_lock.locked():
            raise Busy(f"busy: {hub.current_procedure}")
        if (hub.settings.get("phase") or "running") == "running":
            hub.update_settings({"monitoring_before": bool(hub.settings.get("monitoring", True)), "monitoring": False, "phase": "repositioning"})
        await hub.broadcast({"type": "settings", "settings": hub.settings})
        await hub.progress("Listening paused: move the devices, then click Repositioned.")
        hub.mark_dirty()
        return {"phase": hub.settings["phase"]}
    if name == "reposition_cancel":
        # only possible before the old session has been closed
        if hub.settings.get("phase") == "repositioning":
            hub.update_settings({"monitoring": bool(hub.settings.get("monitoring_before", True)), "phase": "running"})
            await hub.broadcast({"type": "settings", "settings": hub.settings})
            await hub.progress("Repositioning cancelled: carrying on with the same session.")
        hub.mark_dirty()
        return {"phase": hub.settings.get("phase")}
    if name == "reposition_done":
        # Step 2: the devices are in new places. Close the old session
        # (keeping what is needed to view it later) and start a new one with
        # no map; the user now places the devices roughly.
        if hub.settings.get("phase") != "repositioning":
            raise ValueError("not repositioning")
        mys = _mystery_state(hub)
        previous = {
            "layout_result_id": (hub.layout or {}).get("result_id"),
            "layout": hub.layout if (hub.layout or {}).get("rough") else None,
            "mystery_estimate": mys.get("estimate"),
            "room": _room(hub),
            "view": hub.view,
            "ended": time.time(),
        }
        n = len(hub.sessions) + 1
        rid = await hub.publish_result("session", {"n": n, "previous": previous}, {"n": n})
        hub.sessions = hub._load_sessions()
        # start the rough placement from where things were drawn before, so
        # only the devices that actually moved need dragging far
        old_pos = (hub.layout or {}).get("positions_by_id") or {}
        if old_pos:
            a = np.deg2rad(float(hub.view.get("rotation", 0)))
            fx = -1.0 if hub.view.get("flip") else 1.0
            hub.update_settings(
                {"rough_positions": None}
            )
            hub.update_settings(
                {"rough_positions": {d: [float(np.cos(a) * fx * p[0] - np.sin(a) * p[1]), float(np.sin(a) * fx * p[0] + np.cos(a) * p[1])] for d, p in old_pos.items()}}
            )
        hub.layout = None
        # the confirmed beep is still the same beep; where it is must be
        # worked out afresh from the new positions
        mys["estimate"] = None
        mys["scores"] = {}
        hub.update_settings({"mystery": mys, "phase": "placing"})
        await hub.progress(f"Session {n} started. Drag each listener to roughly where it is now, then click Done.", result_id=rid)
        hub.mark_dirty()
        return {"phase": "placing", "session": n}
    if name == "reposition_finish":
        # Step 3: take the rough placement, measure, and start listening again.
        if hub.settings.get("phase") != "placing":
            raise ValueError("not placing devices")
        if args.get("room") is not None or "room" in args:
            hub.update_settings({"room": [float(v) for v in args["room"]] if args.get("room") else None})
        await run(hub, "rough", {"positions": args.get("positions") or {}})
        res = await calibrate(hub, args.get("rounds"), None, None)
        hub.update_settings({"monitoring": bool(hub.settings.get("monitoring_before", True)), "phase": "running"})
        await hub.broadcast({"type": "settings", "settings": hub.settings})
        await hub.progress("Listening again in the new positions." if hub.settings["monitoring"] else "New positions measured. Listening is off: tick Listen for beeps to start.")
        hub.mark_dirty()
        return res
    if name == "room":
        # walls: [x0, y0, x1, y1] in map metres, or nothing to remove them
        r = args.get("room")
        hub.update_settings({"room": [float(v) for v in r] if r else None})
        if (hub.settings.get("mystery") or {}).get("marked"):
            _mystery_refresh(hub, _mystery_state(hub))
        hub.mark_dirty()
        return {"room": hub.settings.get("room")}
    if name == "mystery_clear":
        # forget what the mystery beep sounds like (it is otherwise kept
        # across sessions, since moving the listeners doesn't change the beep)
        hub.update_settings({"mystery": {"marked": {}, "scores": {}, "estimate": None, "matches": 0, "pitch": None}})
        await hub.progress("Forgot the mystery beep's sound. Mark a beep again to teach it.")
        hub.mark_dirty()
        return {"ok": True}
    if name == "mark_mystery":
        # the user confirms (or un-confirms) a located sound as the beep they
        # are hunting; everything recent is then re-scored against it
        rid = int(args["result_id"])
        on = bool(args.get("on", True))
        mys = _mystery_state(hub)
        if on:
            res = hub.store.result(rid)
            if not res or res.get("kind") != "locate":
                raise ValueError(f"no located sound with id {rid}")
            sig = _result_signature(hub, res)
            f0 = _mystery_pitch(mys)
            if f0:
                # the beep may be hiding behind another sound in this clip:
                # learn from the beep itself if it is there
                re = _reanalyse(hub, rid, res.get("data") or {}, f0)
                if re and mystery.score(re["signature"], list(mys["marked"].values())) >= mys["threshold"]:
                    sig = re["signature"]
            if not sig:
                raise ValueError("no usable recording of that sound to learn from")
            mys["marked"][str(rid)] = sig
        else:
            mys["marked"].pop(str(rid), None)
        mys = _mystery_refresh(hub, mys, rescore=True)
        n_like = sum(1 for k, v in mys["scores"].items() if v >= mys["threshold"] and k not in mys["marked"])
        est = mys.get("estimate")
        await hub.progress(
            (f"Marked as the mystery beep. " if on else "Unmarked. ")
            + (f"{len(mys['marked'])} confirmed, {n_like} other recent sound(s) match. " if mys["marked"] else "Nothing is marked as the mystery beep now. ")
            + (f"Together they put it at ({est['x']:.2f}, {est['y']:.2f}), within about {est['radius_m'] * 100:.0f}cm" + (" (at the edge of the searched area: it may be further out)" if est.get("at_edge") else "") if est else ("No position yet: none of them could be located on the current map." if mys["marked"] else "")),
            result_id=rid,
        )
        return {"confirmed": len(mys["marked"]), "similar": n_like, "estimate": est}
    if name == "pause_listening":
        # Ignore everything the listeners hear for a while (used while the
        # coordinator replays recordings out loud). The microphones stay
        # open - reopening one on a phone needs a tap - but nothing heard in
        # the window can trigger a locate. seconds=0 resumes at once.
        seconds = max(0.0, min(120.0, float(args.get("seconds", 10))))
        reason = str(args.get("reason", "replay"))
        now = time.time()
        kept = [w for w in hub.suppress_windows if w[2] != reason]
        hub.suppress_windows.clear()
        hub.suppress_windows.extend(kept)
        if seconds:
            hub.suppress(now - 0.5, now + seconds, reason)
        return {"paused_for": seconds}
    if name == "clear_layout":
        hub.layout = None
        hub.mark_dirty()
        return {"ok": True}
    raise ValueError(f"unknown procedure: {name}")


__all__ = ["run", "calibrate", "locate", "testbeep", "selftest", "identify", "Monitor", "Busy", "RpcError"]
