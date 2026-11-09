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
    ids = [c.id for c in devs]
    D = analysis.combine_distance_matrices(matrices)
    layout = analysis.layout_from_distances(ids, D.tolist())
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
    }
    data = {"layout": layout, "rounds": details, "clip_ids": all_clips, "chirp": params}
    rid = await hub.publish_result("calibration", data, summary)
    layout["result_id"] = rid
    hub.layout = layout
    hub.mark_dirty()
    await hub.progress(
        f"Calibrated {len(ids)} devices: {summary['pairs_measured']}/{summary['pairs_total']} distances, "
        f"fit error {layout['rms_residual'] * 100:.1f}cm"
    )
    return {"result_id": rid, "summary": summary, "layout": layout}


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
    devs = [c for c in hub.listeners() if c.id in positions]
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
    out = analysis.analyse_beep(
        recs,
        positions,
        sync,
        beep_expected,
        c=hub.layout.get("speed_of_sound", hub.speed_of_sound),
        self_distance=float(hub.settings.get("self_distance", dsp.DEFAULT_SELF_DISTANCE)),
        chirp_params=params,
    )
    names = {c.id: c.name for c in devs}
    for d in out["devices"]:
        d["name"] = names.get(d["device_id"])
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
    est = out.get("estimate")
    summary = {
        "label": label,
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
    rid = await hub.publish_result("locate", out, summary)
    msg = f"{label}: "
    if est:
        msg += f"estimate ({est['x']:.2f}, {est['y']:.2f}) from {summary['used']} devices, fit {est['rms_residual_m'] * 100:.0f}cm"
        if "error_m" in summary:
            msg += f", {summary['error_m'] * 100:.0f}cm from {names.get(source)}"
    else:
        msg += f"no estimate ({out.get('error')}); loudest at {summary.get('loudest_name')}"
    await hub.progress(msg, result_id=rid)
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
    if name == "clear_layout":
        hub.layout = None
        hub.mark_dirty()
        return {"ok": True}
    raise ValueError(f"unknown procedure: {name}")


__all__ = ["run", "calibrate", "locate", "testbeep", "identify", "Monitor", "Busy", "RpcError"]
