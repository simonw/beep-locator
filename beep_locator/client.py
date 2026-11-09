"""
A small client for the admin API, for driving experiments from anywhere
(including a Claude session in the cloud) once the server is reachable.

    from beep_locator.client import Client
    c = Client.from_env()          # BEEP_URL / BEEP_TOKEN or ~/.beep-locator.json
    c.state()
    c.eval("return ctx.rt.info()", to="listeners")
    c.push_module("listener", "experiment", open("x.js").read())
    c.run("calibrate")
    rec = c.clip(123)              # numpy samples + metadata
"""

from __future__ import annotations

import io
import json
import os
import urllib.error
import urllib.parse
import urllib.request
import wave
from pathlib import Path

CONFIG = Path.home() / ".beep-locator.json"


class ApiError(Exception):
    def __init__(self, status, body):
        self.status = status
        self.body = body
        super().__init__(f"HTTP {status}: {body}")


class Client:
    def __init__(self, url: str, token: str, timeout: float = 180.0):
        self.url = url.rstrip("/")
        self.token = token
        self.timeout = timeout

    @classmethod
    def from_env(cls, url=None, token=None):
        cfg = {}
        if CONFIG.exists():
            try:
                cfg = json.loads(CONFIG.read_text())
            except ValueError:
                cfg = {}
        url = url or os.environ.get("BEEP_URL") or cfg.get("url") or "http://localhost:8765"
        token = token or os.environ.get("BEEP_TOKEN") or cfg.get("token")
        if not token:
            # a server started from this directory saves its token here
            f = Path(os.environ.get("BEEP_DATA_DIR", "beep-data")) / "token.txt"
            if f.exists():
                token = f.read_text().strip()
        if not token:
            raise SystemExit("No token: run from the directory containing beep-data/, set BEEP_TOKEN, or `beep-locator ctl login URL TOKEN`")
        return cls(url, token)

    # -- raw HTTP -----------------------------------------------------------
    def request(self, method, path, body=None, params=None, raw=False, content_type="application/json"):
        url = self.url + path
        if params:
            url += "?" + urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
        data = None
        headers = {"Authorization": f"Bearer {self.token}", "User-Agent": "beep-locator-client"}
        if body is not None:
            if isinstance(body, (bytes, str)) and content_type != "application/json":
                data = body.encode() if isinstance(body, str) else body
            else:
                data = json.dumps(body).encode()
            headers["Content-Type"] = content_type
        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                payload = resp.read()
                if raw:
                    return payload, dict(resp.headers)
                return json.loads(payload) if payload else None
        except urllib.error.HTTPError as e:
            text = e.read().decode(errors="replace")
            try:
                text = json.loads(text)
            except ValueError:
                pass
            raise ApiError(e.code, text) from None

    def get(self, path, **params):
        return self.request("GET", path, params=params)

    def post(self, path, body=None, **params):
        return self.request("POST", path, body=body if body is not None else {}, params=params)

    # -- helpers --------------------------------------------------------------
    def state(self):
        return self.get("/api/state")

    def eval(self, code: str, to="listeners", timeout: float = 15):
        return self.post("/api/eval", {"to": to, "code": code, "timeout": timeout})["results"]

    def rpc(self, method: str, args=None, to="listeners", timeout: float = 15):
        return self.post("/api/rpc", {"to": to, "method": method, "args": args or {}, "timeout": timeout})["results"]

    def modules(self):
        return self.get("/api/modules")["modules"]

    def module_source(self, role, name):
        body, _ = self.request("GET", f"/api/modules/{role}/{name}", raw=True)
        return body.decode()

    def push_module(self, role: str, name: str, code: str):
        return self.request("PUT", f"/api/modules/{role}/{name}", body=code, content_type="text/javascript")

    def remove_module(self, role: str, name: str):
        return self.request("DELETE", f"/api/modules/{role}/{name}")

    def run(self, name: str, **args):
        return self.post(f"/api/run/{name}", args)

    def events(self, since_id=0, topic=None, limit=200, device_id=None):
        return self.get("/api/events", since_id=since_id, topic=topic, limit=limit, device_id=device_id)["events"]

    def clips(self, since_id=0, label=None, limit=200):
        return self.get("/api/clips", since_id=since_id, label=label, limit=limit)["clips"]

    def clip_wav(self, clip_id: int) -> tuple[bytes, dict]:
        return self.request("GET", f"/api/clips/{clip_id}.wav", raw=True)

    def clip(self, clip_id: int):
        """Return a beep_locator.analysis.Recording for a stored clip."""
        import numpy as np

        from .analysis import Recording

        body, headers = self.clip_wav(clip_id)
        with wave.open(io.BytesIO(body)) as w:
            raw = w.readframes(w.getnframes())
        samples = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32767.0
        h = {k.lower(): v for k, v in headers.items()}
        return Recording(h.get("x-device-id"), float(h.get("x-sample-rate")), int(h.get("x-start-frame")), samples)

    def results(self, kind=None, limit=20, since_id=0, full=False):
        return self.get("/api/results", kind=kind, limit=limit, since_id=since_id, full=1 if full else None)["results"]

    def result(self, rid: int):
        return self.get(f"/api/results/{rid}")

    def settings(self, patch=None):
        if patch:
            return self.post("/api/settings", patch)
        return self.get("/api/settings")

    def query(self, sql: str, params=None):
        return self.post("/api/query", {"sql": sql, "params": params or []})

    def reload(self, to="listeners"):
        return self.post("/api/reload", {"to": to})

    def update(self):
        return self.post("/api/update")

    def restart(self):
        return self.post("/api/restart")
