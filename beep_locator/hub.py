"""
The hub: who is connected, how to talk to them, and what code they run.

Every browser (listener, coordinator or simulated listener) holds one
WebSocket to the hub. The hub can:

* call methods on a client and await the answer (``rpc``) - including
  ``eval``, which runs arbitrary JavaScript sent from here
* push *modules*: named chunks of JavaScript that clients install and keep
  running, hot-swapped without reloading the page (so audio keeps flowing)
* keep a rough mapping between each device's audio frame counter and server
  time (ping/pong), good to a few tens of milliseconds - enough to know where
  to look in a recording, not enough to measure anything with
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import itertools
import json
import math
import secrets
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from .store import Store

MODULES_DIR = Path(__file__).parent / "modules"

PALETTE = [
    ("Red", "#e6194b"),
    ("Blue", "#4363d8"),
    ("Green", "#3cb44b"),
    ("Orange", "#f58231"),
    ("Purple", "#911eb4"),
    ("Teal", "#469990"),
    ("Pink", "#f032e6"),
    ("Yellow", "#d4b100"),
    ("Brown", "#9a6324"),
    ("Navy", "#000075"),
    ("Lime", "#7fb800"),
    ("Maroon", "#800000"),
    ("Grey", "#808080"),
]

DEFAULT_SETTINGS = {
    "monitoring": True,
    "temperature_c": 20.0,
    "self_distance": 0.05,
    "chirp": {"f_lo": 2000.0, "f_hi": 7000.0, "duration": 0.08, "amplitude": 0.9},
    "calibration_rounds": 4,
    "calibration_gap": 1.0,
    "sync_gain": 1.0,
    # onset detector (runs on each listener)
    "detector": {"threshold_db": 12.0, "min_prominence": 6.0, "band_lo": 800.0, "band_hi": 10000.0},
    # server-side trigger logic
    "trigger": {"cooldown": 4.0, "cluster_window": 0.5, "min_devices": 1, "min_prominence": 6.0},
}


class RpcError(Exception):
    pass


@dataclass
class ClockSync:
    """Coarse mapping between a device's audio frames and server time."""

    samples: deque = field(default_factory=lambda: deque(maxlen=30))
    sr: float | None = None

    def add(self, t_send: float, t_recv: float, frame: float, sr: float):
        self.sr = sr
        rtt = t_recv - t_send
        mid = (t_send + t_recv) / 2
        self.samples.append((rtt, mid - frame / sr, t_recv))

    @property
    def ready(self) -> bool:
        return self.sr is not None and len(self.samples) >= 2

    def best(self):
        # min-RTT sample among recent ones; drop samples older than 60s if we can
        now = time.time()
        recent = [s for s in self.samples if now - s[2] < 60] or list(self.samples)
        return min(recent, key=lambda s: s[0])

    @property
    def offset(self) -> float:
        return self.best()[1]

    @property
    def rtt(self) -> float:
        return self.best()[0]

    def to_frame(self, server_time: float) -> float:
        return (server_time - self.offset) * self.sr

    def to_time(self, frame: float) -> float:
        return frame / self.sr + self.offset


class Conn:
    def __init__(self, ws, hello: dict, hub: "Hub"):
        self.ws = ws
        self.hub = hub
        self.id = hello["id"]
        self.role = hello.get("role", "listener")
        self.sim = bool(hello.get("sim"))
        self.ua = hello.get("ua", "")
        self.boot_version = hello.get("boot_version")
        self.name = None
        self.color = None
        self.upload_key = secrets.token_urlsafe(12)
        self.clock = ClockSync()
        self.status: dict = {}
        self.module_status: dict = {}
        self.pending: dict[int, asyncio.Future] = {}
        self.connected_at = time.time()
        self.last_seen = time.time()
        self._send_lock = asyncio.Lock()

    async def send(self, msg: dict):
        text = dumps(msg)
        async with self._send_lock:
            await self.ws.send_text(text)

    @property
    def sr(self):
        return self.status.get("sampleRate") or self.clock.sr

    @property
    def audio_running(self) -> bool:
        return bool(self.status.get("running"))

    def summary(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "color": self.color,
            "role": self.role,
            "sim": self.sim,
            "ua": self.ua,
            "boot_version": self.boot_version,
            "connected_at": self.connected_at,
            "last_seen": self.last_seen,
            "status": self.status,
            "modules": self.module_status,
            "clock": {
                "ready": self.clock.ready,
                "rtt_ms": round(self.clock.rtt * 1000, 1) if self.clock.ready else None,
            },
        }


class Hub:
    def __init__(self, store: Store, token: str, public_url: str | None = None):
        self.store = store
        self.token = token
        self.public_url = public_url
        self.conns: dict[str, Conn] = {}
        self._rpc_ids = itertools.count(1)
        self.settings = json.loads(json.dumps(DEFAULT_SETTINGS))
        for k, v in store.get_settings().items():
            if isinstance(v, dict) and isinstance(self.settings.get(k), dict):
                self.settings[k].update(v)
            else:
                self.settings[k] = v
        self.layout = None
        latest = store.latest_result("calibration")
        if latest and latest["data"].get("layout"):
            self.layout = latest["data"]["layout"]
            self.layout["result_id"] = latest["id"]
        # Sessions: a new one starts each time the devices are repositioned.
        # Each is marked by a result of kind "session"; a result belongs to
        # the session whose marker has the greatest id below its own.
        self.sessions = self._load_sessions()
        if len(self.sessions) > 1 and (self.layout or {}).get("result_id", 0) < self.sessions[-1]["start_id"]:
            self.layout = None  # devices were moved after the last calibration
        self.view = store.get_settings().get("view", {"rotation": 0, "flip": False})
        # device id -> {"ok", "ts", "how"}: did anything hear this device's
        # speaker the last time it was asked to play?
        self.speakers = store.get_settings().get("speakers", {}) or {}
        self.event_listeners: list = []
        self.suppress_windows: deque = deque(maxlen=50)
        self.procedure_lock = asyncio.Lock()
        self.current_procedure: str | None = None
        self._state_dirty = asyncio.Event()
        self._module_mtimes: dict = {}
        self.restart_hook = None  # set by the CLI when auto-update is enabled
        self.update_hook = None

    # -- auth --------------------------------------------------------------
    def check_token(self, token: str | None) -> bool:
        return bool(token) and secrets.compare_digest(token, self.token)

    # -- names and colours -------------------------------------------------
    def assign_identity(self, conn: Conn, requested_name: str | None = None):
        known = self.store.get_device(conn.id)
        if known and known["name"]:
            conn.name, conn.color = known["name"], known["color"]
        else:
            used = {c.color for c in self.conns.values()} | {d["color"] for d in self.store.all_devices()}
            for name, color in PALETTE:
                if color not in used:
                    conn.name, conn.color = name, color
                    break
            else:
                n, c = PALETTE[len(used) % len(PALETTE)]
                conn.name, conn.color = f"{n} {len(used)}", c
        if conn.role == "coordinator" and not (known and known["name"]):
            conn.name = "Coordinator"
        if requested_name:
            conn.name = requested_name
        self.store.upsert_device(conn.id, conn.name, conn.color, conn.role, conn.ua)

    def rename(self, device_id: str, name: str):
        conn = self.conns.get(device_id)
        dev = self.store.get_device(device_id)
        if conn:
            conn.name = name
            self.store.upsert_device(conn.id, conn.name, conn.color, conn.role, conn.ua)
        elif dev:
            self.store.upsert_device(device_id, name, dev["color"], dev["role"], dev["ua"])

    # -- connection lifecycle ------------------------------------------------
    async def register(self, conn: Conn):
        old = self.conns.get(conn.id)
        if old is not None and old is not conn:
            try:
                await old.ws.close(code=4000, reason="replaced by new connection")
            except Exception:
                pass
        self.conns[conn.id] = conn
        self.mark_dirty()

    def unregister(self, conn: Conn):
        if self.conns.get(conn.id) is conn:
            del self.conns[conn.id]
        for fut in conn.pending.values():
            if not fut.done():
                fut.set_exception(RpcError("disconnected"))
        self.mark_dirty()

    def listeners(self, running_only=True, with_clock=True) -> list[Conn]:
        out = []
        for c in self.conns.values():
            if c.role != "listener":
                continue
            if running_only and not c.audio_running:
                continue
            if with_clock and not c.clock.ready:
                continue
            out.append(c)
        return sorted(out, key=lambda c: c.connected_at)

    def coordinators(self) -> list[Conn]:
        return [c for c in self.conns.values() if c.role == "coordinator"]

    def resolve_targets(self, to) -> list[Conn]:
        if to in (None, "listeners"):
            return [c for c in self.conns.values() if c.role == "listener"]
        if to == "coordinators":
            return self.coordinators()
        if to == "all":
            return list(self.conns.values())
        if isinstance(to, str):
            to = [to]
        out = []
        for t in to:
            if t in self.conns:
                out.append(self.conns[t])
            else:
                # allow addressing by name, case-insensitive
                for c in self.conns.values():
                    if (c.name or "").lower() == str(t).lower():
                        out.append(c)
        return out

    # -- messaging ---------------------------------------------------------
    async def broadcast(self, msg: dict, role: str | None = None):
        targets = [c for c in list(self.conns.values()) if role is None or c.role == role]
        await asyncio.gather(*(self._safe_send(c, msg) for c in targets))

    async def _safe_send(self, conn: Conn, msg: dict):
        try:
            await conn.send(msg)
        except Exception:
            pass

    async def rpc(self, conn: Conn, method: str, args: dict | None = None, timeout: float = 10.0):
        rid = next(self._rpc_ids)
        fut = asyncio.get_running_loop().create_future()
        conn.pending[rid] = fut
        try:
            await conn.send({"type": "rpc", "id": rid, "method": method, "args": args or {}})
            return await asyncio.wait_for(fut, timeout)
        except asyncio.TimeoutError:
            raise RpcError(f"{conn.name}: {method} timed out after {timeout}s")
        finally:
            conn.pending.pop(rid, None)

    async def rpc_many(self, conns: list[Conn], method: str, args=None, timeout: float = 10.0) -> dict:
        async def one(c):
            try:
                return c.id, {"ok": True, "value": await self.rpc(c, method, args, timeout)}
            except Exception as e:
                return c.id, {"ok": False, "error": str(e)}

        pairs = await asyncio.gather(*(one(c) for c in conns))
        return dict(pairs)

    def handle_rpc_result(self, conn: Conn, msg: dict):
        fut = conn.pending.get(msg.get("id"))
        if fut is None or fut.done():
            return
        if msg.get("ok"):
            fut.set_result(msg.get("value"))
        else:
            fut.set_exception(RpcError(f"{conn.name}: {msg.get('error')}"))

    # -- state for coordinators -------------------------------------------
    def mark_dirty(self):
        self._state_dirty.set()

    def state(self) -> dict:
        return {
            "devices": [c.summary() for c in sorted(self.conns.values(), key=lambda c: c.connected_at)],
            "known_devices": self.store.all_devices(),
            "layout": self.layout,
            "settings": self.settings,
            "view": self.view,
            "speakers": self.speakers,
            "sessions": self.sessions,
            "phase": self.settings.get("phase") or "running",
            "public_url": self.public_url,
            "procedure": self.current_procedure,
            "server_time": time.time(),
            "modules": self.module_list(),
        }

    async def state_loop(self):
        while True:
            try:
                await asyncio.wait_for(self._state_dirty.wait(), timeout=3.0)
            except asyncio.TimeoutError:
                pass
            self._state_dirty.clear()
            if self.coordinators():
                await self.broadcast({"type": "state", "state": self.state()}, role="coordinator")
            await asyncio.sleep(0.3)

    async def progress(self, message: str, **extra):
        self.store.add_event("progress", {"message": message, **extra})
        await self.broadcast({"type": "progress", "message": message, **extra}, role="coordinator")

    async def publish_result(self, kind: str, data: dict, summary: dict | None = None):
        rid = self.store.add_result(kind, data, summary)
        await self.broadcast({"type": "result", "kind": kind, "id": rid, "summary": summary, "data": data}, role="coordinator")
        return rid

    # -- events from clients ---------------------------------------------
    async def handle_event(self, conn: Conn, topic: str, data):
        eid = self.store.add_event(topic, data, device_id=conn.id)
        msg = {"type": "event", "id": eid, "topic": topic, "device_id": conn.id, "name": conn.name, "data": data, "ts": time.time()}
        await self.broadcast(msg, role="coordinator")
        for cb in list(self.event_listeners):
            try:
                res = cb(conn, topic, data)
                if asyncio.iscoroutine(res):
                    asyncio.create_task(res)
            except Exception as e:  # pragma: no cover
                print("event listener error", e)

    def suppress(self, t_from: float, t_to: float, reason: str):
        self.suppress_windows.append((t_from, t_to, reason))

    def suppressed(self, t: float):
        for a, b, reason in self.suppress_windows:
            if a <= t <= b:
                return reason
        return None

    # -- modules -----------------------------------------------------------
    def disk_modules(self):
        out = {}
        if MODULES_DIR.exists():
            for path in MODULES_DIR.glob("*/*.js"):
                role = path.parent.name
                out[(role, path.stem)] = {"code": path.read_text(), "ts": path.stat().st_mtime, "source": "disk"}
        return out

    def modules_for(self, role: str | None = None) -> list[dict]:
        merged = self.disk_modules()
        for row in self.store.pushed_modules():
            key = (row["role"], row["name"])
            if key not in merged or row["ts"] >= merged[key]["ts"]:
                merged[key] = {"code": row["code"], "ts": row["ts"], "source": "pushed"}
        out = []
        for (r, name), m in sorted(merged.items()):
            if role and r != role:
                continue
            out.append(
                {
                    "role": r,
                    "name": name,
                    "code": m["code"],
                    "version": hashlib.sha1(m["code"].encode()).hexdigest()[:10],
                    "source": m["source"],
                    "ts": m["ts"],
                }
            )
        return out

    def module_list(self):
        return [{k: v for k, v in m.items() if k != "code"} for m in self.modules_for()]

    async def send_modules(self, conn: Conn):
        for m in self.modules_for(conn.role):
            await conn.send({"type": "module", "name": m["name"], "code": m["code"], "version": m["version"]})

    async def push_module(self, role: str, name: str, code: str):
        self.store.set_module(role, name, code)
        await self._broadcast_module(role, name)

    async def remove_module(self, role: str, name: str):
        self.store.delete_module(role, name)
        disk = self.disk_modules().get((role, name))
        if disk:
            await self._broadcast_module(role, name)
        else:
            await self.broadcast({"type": "module_remove", "name": name}, role=role)
        self.mark_dirty()

    async def _broadcast_module(self, role, name):
        for m in self.modules_for(role):
            if m["name"] == name:
                await self.broadcast({"type": "module", "name": name, "code": m["code"], "version": m["version"]}, role=role)
        self.mark_dirty()

    async def watch_disk_modules(self):
        """Re-push modules when their files change on disk (handy when hacking)."""
        while True:
            await asyncio.sleep(1.0)
            try:
                for path in MODULES_DIR.glob("*/*.js"):
                    m = path.stat().st_mtime
                    key = str(path)
                    if key in self._module_mtimes and self._module_mtimes[key] != m:
                        self._module_mtimes[key] = m
                        await self._broadcast_module(path.parent.name, path.stem)
                    self._module_mtimes[key] = m
            except Exception as e:  # pragma: no cover
                print("module watcher error", e)

    def _load_sessions(self) -> list:
        first = self.store.query("select min(ts) from results")["rows"]
        t0 = first[0][0] if first and first[0][0] else time.time()
        out = [{"n": 1, "start_id": 0, "ts": t0, "previous": None}]
        for r in self.store.results(kind="session", limit=10000, full=True):
            out[-1]["end_ts"] = r["ts"]
            out.append({"n": len(out) + 1, "start_id": r["id"], "ts": r["ts"], "previous": (r.get("data") or {}).get("previous")})
        return out

    def set_speaker(self, device_id: str, ok: bool, how: str):
        self.speakers[device_id] = {"ok": bool(ok), "ts": time.time(), "how": how}
        self.store.set_setting("speakers", self.speakers)
        self.mark_dirty()

    # -- settings ------------------------------------------------------------
    def update_settings(self, patch: dict):
        for k, v in patch.items():
            if isinstance(v, dict) and isinstance(self.settings.get(k), dict):
                self.settings[k].update(v)
            else:
                self.settings[k] = v
            self.store.set_setting(k, self.settings[k])
        self.mark_dirty()

    @property
    def speed_of_sound(self) -> float:
        from .dsp import speed_of_sound

        return speed_of_sound(float(self.settings.get("temperature_c", 20.0)))

    # -- ping loop ---------------------------------------------------------
    async def ping_loop(self, conn: Conn):
        n = 0
        while self.conns.get(conn.id) is conn:
            try:
                await conn.send({"type": "ping", "t": time.time()})
            except Exception:
                return
            n += 1
            # ping quickly at first so the clock mapping is usable fast
            await asyncio.sleep(0.25 if n < 8 else 2.0)

    def handle_pong(self, conn: Conn, msg: dict):
        t_recv = time.time()
        frame, sr = msg.get("frame"), msg.get("sr")
        if frame is None or not sr:
            return
        conn.clock.add(float(msg["t"]), t_recv, float(frame), float(sr))


def clean(o):
    """Replace NaN/inf with None so the result is valid JSON for browsers."""
    if isinstance(o, float):
        return o if math.isfinite(o) else None
    if isinstance(o, dict):
        return {k: clean(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [clean(v) for v in o]
    if isinstance(o, np.ndarray):
        return clean(o.tolist())
    if isinstance(o, np.floating):
        return clean(float(o))
    if isinstance(o, np.integer):
        return int(o)
    return o


def dumps(o) -> str:
    return json.dumps(clean(o), default=_default, allow_nan=False)


def encode_f32(samples: np.ndarray) -> str:
    return base64.b64encode(np.asarray(samples, dtype="<f4").tobytes()).decode()


def _default(o):
    if isinstance(o, np.floating):
        return float(o)
    if isinstance(o, np.integer):
        return int(o)
    if isinstance(o, np.ndarray):
        return o.tolist()
    raise TypeError(type(o))
