import json
import threading

import numpy as np
from starlette.testclient import TestClient

from beep_locator.server import create_app

TOKEN = "t0ken"


def make_client(tmp_path):
    app = create_app(tmp_path / "data", TOKEN)
    return TestClient(app)


def auth():
    return {"Authorization": f"Bearer {TOKEN}"}


def hello(ws, **kw):
    ws.send_text(json.dumps({"type": "hello", "id": "dev1", "role": "listener", "ua": "test", **kw}))
    msgs = []
    while True:
        m = json.loads(ws.receive_text())
        msgs.append(m)
        # welcome, then one "module" message per listener module
        if m["type"] == "module":
            break
    return msgs


def test_pages_and_auth(tmp_path):
    with make_client(tmp_path) as c:
        assert c.get("/join").status_code == 200
        assert c.get("/").status_code == 200
        assert c.get("/sim").status_code == 200
        assert c.get("/static/runtime.js").status_code == 200
        assert c.get("/qr.svg?data=https://example.com/join").headers["content-type"].startswith("image/svg")
        assert c.get("/api/state").status_code == 401
        assert c.get("/api/state", headers=auth()).status_code == 200


def test_listener_gets_modules_and_answers_eval(tmp_path):
    with make_client(tmp_path) as c:
        with c.websocket_connect("/ws") as ws:
            msgs = hello(ws)
            welcome = msgs[0]
            assert welcome["type"] == "welcome" and welcome["name"] == "Red"
            assert any(m["type"] == "module" and m["name"] == "detector" for m in msgs)

            result = {}

            def call():
                result["r"] = c.post("/api/eval", headers=auth(), json={"to": "listeners", "code": "return 1+1"}).json()

            t = threading.Thread(target=call)
            t.start()
            while True:
                m = json.loads(ws.receive_text())
                if m["type"] == "rpc":
                    assert m["method"] == "eval" and m["args"]["code"] == "return 1+1"
                    ws.send_text(json.dumps({"type": "rpc_result", "id": m["id"], "ok": True, "value": 2}))
                    break
            t.join(5)
            assert result["r"]["results"]["dev1"]["value"] == 2


def test_push_module_broadcasts(tmp_path):
    with make_client(tmp_path) as c:
        with c.websocket_connect("/ws") as ws:
            hello(ws)
            code = "export default function setup(ctx) { ctx.log('hi') }"
            done = {}

            def push():
                done["r"] = c.put("/api/modules/listener/hello?wait=0.2", headers=auth(), content=code).json()

            t = threading.Thread(target=push)
            t.start()
            while True:
                m = json.loads(ws.receive_text())
                if m["type"] == "module" and m["name"] == "hello":
                    assert m["code"] == code
                    ws.send_text(json.dumps({"type": "module_status", "name": "hello", "version": m["version"], "ok": True}))
                    break
            t.join(5)
            mods = c.get("/api/modules", headers=auth()).json()["modules"]
            assert any(m["name"] == "hello" and m["source"] == "pushed" for m in mods)


def test_clip_upload_roundtrip(tmp_path):
    with make_client(tmp_path) as c:
        with c.websocket_connect("/ws") as ws:
            welcome = hello(ws)[0]
            samples = (np.sin(np.arange(4800) / 10) * 0.5 * 32767).astype("<i2")
            r = c.post(
                f"/upload/clip?device=dev1&key={welcome['upload_key']}&start=12345&sr=48000&label=test",
                content=samples.tobytes(),
            )
            assert r.status_code == 200
            clip_id = r.json()["clip_id"]
            bad = c.post("/upload/clip?device=dev1&key=nope&start=0&sr=48000", content=b"\x00\x00")
            assert bad.status_code == 403
            clips = c.get("/api/clips", headers=auth()).json()["clips"]
            assert clips[0]["start_frame"] == 12345 and clips[0]["n"] == 4800
            wav = c.get(f"/api/clips/{clip_id}.wav", headers=auth())
            assert wav.headers["x-start-frame"] == "12345"
            assert wav.content[:4] == b"RIFF"


def test_coordinator_needs_token(tmp_path):
    with make_client(tmp_path) as c:
        with c.websocket_connect("/ws") as ws:
            ws.send_text(json.dumps({"type": "hello", "id": "co", "role": "coordinator", "token": "wrong"}))
            m = json.loads(ws.receive_text())
            assert m["type"] == "error"


def test_query_is_read_only(tmp_path):
    with make_client(tmp_path) as c:
        r = c.post("/api/query", headers=auth(), json={"sql": "select count(*) from events"})
        assert r.status_code == 200
        r = c.post("/api/query", headers=auth(), json={"sql": "delete from events"})
        assert r.status_code >= 400
