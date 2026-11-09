"""SQLite + files on disk for everything the server records."""

from __future__ import annotations

import io
import json
import sqlite3
import time
import wave
from pathlib import Path

import numpy as np

SCHEMA = """
create table if not exists devices (
    id text primary key,
    name text,
    color text,
    role text,
    ua text,
    first_seen real,
    last_seen real
);
create table if not exists events (
    id integer primary key,
    ts real,
    device_id text,
    topic text,
    data text
);
create index if not exists events_topic on events(topic, id);
create table if not exists clips (
    id integer primary key,
    ts real,
    device_id text,
    label text,
    sr real,
    start_frame integer,
    n integer,
    path text,
    meta text
);
create table if not exists modules (
    role text,
    name text,
    code text,
    ts real,
    primary key (role, name)
);
create table if not exists results (
    id integer primary key,
    ts real,
    kind text,
    summary text,
    data text
);
create index if not exists results_kind on results(kind, id);
create table if not exists settings (
    key text primary key,
    value text
);
"""


class Store:
    def __init__(self, data_dir: str | Path):
        self.dir = Path(data_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        (self.dir / "clips").mkdir(exist_ok=True)
        self.db_path = self.dir / "beep.db"
        self.db = sqlite3.connect(self.db_path, check_same_thread=False, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("pragma journal_mode=wal")
        self.db.executescript(SCHEMA)

    # -- devices ---------------------------------------------------------
    def upsert_device(self, id, name, color, role, ua):
        now = time.time()
        self.db.execute(
            """insert into devices (id, name, color, role, ua, first_seen, last_seen)
               values (?, ?, ?, ?, ?, ?, ?)
               on conflict(id) do update set name=excluded.name, color=excluded.color,
               role=excluded.role, ua=excluded.ua, last_seen=excluded.last_seen""",
            (id, name, color, role, ua, now, now),
        )

    def get_device(self, id):
        row = self.db.execute("select * from devices where id = ?", (id,)).fetchone()
        return dict(row) if row else None

    def all_devices(self):
        return [dict(r) for r in self.db.execute("select * from devices order by first_seen")]

    # -- events ----------------------------------------------------------
    def add_event(self, topic, data, device_id=None, ts=None):
        cur = self.db.execute(
            "insert into events (ts, device_id, topic, data) values (?, ?, ?, ?)",
            (ts or time.time(), device_id, topic, _dumps(data)),
        )
        return cur.lastrowid

    def events(self, since_id=0, topic=None, limit=200, device_id=None):
        sql = "select * from events where id > ?"
        args = [since_id]
        if topic:
            sql += " and topic = ?"
            args.append(topic)
        if device_id:
            sql += " and device_id = ?"
            args.append(device_id)
        sql += " order by id desc limit ?"
        args.append(limit)
        rows = [dict(r) for r in self.db.execute(sql, args)]
        for r in rows:
            r["data"] = json.loads(r["data"]) if r["data"] else None
        return rows[::-1]

    # -- clips -----------------------------------------------------------
    def add_clip(self, device_id, label, sr, start_frame, samples_int16: bytes, meta=None):
        now = time.time()
        cur = self.db.execute(
            "insert into clips (ts, device_id, label, sr, start_frame, n, path, meta) values (?, ?, ?, ?, ?, ?, ?, ?)",
            (now, device_id, label, sr, start_frame, len(samples_int16) // 2, "", json.dumps(meta or {})),
        )
        clip_id = cur.lastrowid
        safe = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in (label or "clip"))[:40]
        path = self.dir / "clips" / f"{clip_id:06d}-{safe}-{device_id[:12]}.wav"
        with wave.open(str(path), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(int(round(sr)))
            w.writeframes(samples_int16)
        self.db.execute("update clips set path = ? where id = ?", (str(path), clip_id))
        return clip_id

    def clip_row(self, clip_id):
        row = self.db.execute("select * from clips where id = ?", (clip_id,)).fetchone()
        if not row:
            return None
        d = dict(row)
        d["meta"] = json.loads(d["meta"]) if d["meta"] else {}
        return d

    def clips(self, since_id=0, label=None, limit=200):
        sql = "select * from clips where id > ?"
        args = [since_id]
        if label:
            sql += " and label like ?"
            args.append(label)
        sql += " order by id desc limit ?"
        args.append(limit)
        rows = [dict(r) for r in self.db.execute(sql, args)]
        for r in rows:
            r["meta"] = json.loads(r["meta"]) if r["meta"] else {}
        return rows[::-1]

    def load_clip(self, clip_id) -> tuple[dict, np.ndarray]:
        row = self.clip_row(clip_id)
        if row is None:
            raise KeyError(clip_id)
        with wave.open(row["path"], "rb") as w:
            raw = w.readframes(w.getnframes())
        samples = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32767.0
        return row, samples

    def clip_wav_bytes(self, clip_id) -> bytes:
        row = self.clip_row(clip_id)
        return Path(row["path"]).read_bytes()

    # -- modules ---------------------------------------------------------
    def set_module(self, role, name, code):
        self.db.execute(
            "insert or replace into modules (role, name, code, ts) values (?, ?, ?, ?)",
            (role, name, code, time.time()),
        )

    def delete_module(self, role, name):
        self.db.execute("delete from modules where role = ? and name = ?", (role, name))

    def pushed_modules(self):
        return [dict(r) for r in self.db.execute("select * from modules")]

    # -- results ---------------------------------------------------------
    def add_result(self, kind, data, summary=None):
        cur = self.db.execute(
            "insert into results (ts, kind, summary, data) values (?, ?, ?, ?)",
            (time.time(), kind, _dumps(summary or {}), _dumps(data)),
        )
        return cur.lastrowid

    def results(self, kind=None, limit=20, since_id=0, full=False):
        cols = "id, ts, kind, summary" + (", data" if full else "")
        sql = f"select {cols} from results where id > ?"
        args = [since_id]
        if kind:
            sql += " and kind = ?"
            args.append(kind)
        sql += " order by id desc limit ?"
        args.append(limit)
        rows = [dict(r) for r in self.db.execute(sql, args)]
        for r in rows:
            r["summary"] = json.loads(r["summary"]) if r["summary"] else None
            if full:
                r["data"] = json.loads(r["data"]) if r["data"] else None
        return rows[::-1]

    def result(self, id):
        row = self.db.execute("select * from results where id = ?", (id,)).fetchone()
        if not row:
            return None
        d = dict(row)
        d["summary"] = json.loads(d["summary"]) if d["summary"] else None
        d["data"] = json.loads(d["data"]) if d["data"] else None
        return d

    def latest_result(self, kind):
        row = self.db.execute("select id from results where kind = ? order by id desc limit 1", (kind,)).fetchone()
        return self.result(row["id"]) if row else None

    # -- settings --------------------------------------------------------
    def get_settings(self):
        return {r["key"]: json.loads(r["value"]) for r in self.db.execute("select * from settings")}

    def set_setting(self, key, value):
        self.db.execute("insert or replace into settings (key, value) values (?, ?)", (key, json.dumps(value)))

    # -- read-only SQL for poking around remotely -------------------------
    def query(self, sql, params=None, limit=1000):
        ro = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True)
        ro.row_factory = sqlite3.Row
        try:
            cur = ro.execute(sql, params or [])
            cols = [c[0] for c in cur.description] if cur.description else []
            rows = [list(r) for r in cur.fetchmany(limit)]
            return {"columns": cols, "rows": rows}
        finally:
            ro.close()


def _dumps(o):
    from .hub import dumps

    return dumps(o)


def _json_default(o):
    if isinstance(o, (np.floating,)):
        return float(o)
    if isinstance(o, (np.integer,)):
        return int(o)
    if isinstance(o, np.ndarray):
        return o.tolist()
    raise TypeError(f"not JSON serialisable: {type(o)}")


def wav_bytes(samples: np.ndarray, sr: float) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(int(round(sr)))
        w.writeframes((np.clip(samples, -1, 1) * 32767).astype("<i2").tobytes())
    return buf.getvalue()
