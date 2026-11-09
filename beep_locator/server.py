"""
The Starlette app: pages, the WebSocket hub endpoint, uploads, admin API.

Admin API (all need ``Authorization: Bearer <token>`` or ``?token=``):

    GET    /api/state                      devices, layout, settings, modules
    POST   /api/eval      {to, code, timeout}       run JS on clients, get results
    POST   /api/rpc       {to, method, args, timeout}
    GET    /api/modules
    PUT    /api/modules/{role}/{name}      body = JS source; installs everywhere
    DELETE /api/modules/{role}/{name}
    POST   /api/run/{procedure}  {args}    calibrate, testbeep, locate, identify, ...
    GET    /api/events?since_id=&topic=&limit=&device_id=
    GET    /api/clips?since_id=&label=&limit=
    GET    /api/clips/{id}.wav
    GET    /api/results?kind=&limit=&since_id=&full=1
    GET    /api/results/{id}
    GET    /api/settings      POST /api/settings {patch}
    POST   /api/query     {sql, params}   read-only SQL against beep.db
    POST   /api/reload    {to}            ask clients to reload their page
    POST   /api/update                    git pull + restart (if --auto-update)
    POST   /api/restart                   restart the server process
"""

from __future__ import annotations

import asyncio
import io
import json
import sqlite3
import time
from contextlib import asynccontextmanager
from pathlib import Path

import segno
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, Response
from starlette.routing import Mount, Route, WebSocketRoute
from starlette.staticfiles import StaticFiles
from starlette.websockets import WebSocket, WebSocketDisconnect

from . import procedures
from .hub import Conn, Hub, RpcError, clean
from .store import Store

STATIC = Path(__file__).parent / "static"


class JSON(JSONResponse):
    def render(self, content) -> bytes:
        return json.dumps(clean(content), allow_nan=False).encode()


def _token_from(request) -> str | None:
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return request.query_params.get("token")


def admin(handler):
    async def wrapped(request: Request):
        hub: Hub = request.app.state.hub
        if not hub.check_token(_token_from(request)):
            return JSON({"error": "bad or missing token"}, status_code=401)
        try:
            return await handler(request, hub)
        except procedures.Busy as e:
            return JSON({"error": str(e)}, status_code=409)
        except (ValueError, KeyError, RpcError, sqlite3.Error) as e:
            return JSON({"error": f"{type(e).__name__}: {e}"}, status_code=400)

    wrapped.__name__ = handler.__name__
    return wrapped


# -- pages --------------------------------------------------------------------


def page(name):
    async def handler(request):
        return FileResponse(STATIC / name, headers={"Cache-Control": "no-store"})

    return handler


async def qr_svg(request):
    data = request.query_params.get("data", "")[:500]
    qr = segno.make(data, error="m")
    buf = io.BytesIO()
    qr.save(buf, kind="svg", scale=6, border=2, dark="#000", light="#fff")
    return Response(buf.getvalue(), media_type="image/svg+xml")


async def health(request):
    hub: Hub = request.app.state.hub
    return JSON({"ok": True, "devices": len(hub.conns), "time": time.time()})


# -- uploads from listeners ------------------------------------------------------


async def upload_clip(request: Request):
    hub: Hub = request.app.state.hub
    q = request.query_params
    conn = hub.conns.get(q.get("device", ""))
    if conn is None or q.get("key") != conn.upload_key:
        return JSON({"error": "unknown device or bad key"}, status_code=403)
    body = await request.body()
    if len(body) % 2:
        return JSON({"error": "body must be int16 PCM"}, status_code=400)
    meta = {}
    if request.headers.get("x-meta"):
        try:
            meta = json.loads(request.headers["x-meta"])
        except ValueError:
            pass
    clip_id = hub.store.add_clip(
        conn.id,
        q.get("label", "clip"),
        float(q.get("sr", conn.sr or 48000)),
        int(float(q.get("start", 0))),
        body,
        meta,
    )
    return JSON({"clip_id": clip_id})


# -- websocket ----------------------------------------------------------------------


async def ws_endpoint(websocket: WebSocket):
    hub: Hub = websocket.app.state.hub
    await websocket.accept()
    try:
        hello = json.loads(await websocket.receive_text())
    except Exception:
        await websocket.close(code=4400)
        return
    if hello.get("type") != "hello" or not hello.get("id"):
        await websocket.close(code=4400)
        return
    role = hello.get("role", "listener")
    if role not in ("listener", "coordinator"):
        await websocket.close(code=4400)
        return
    if role == "coordinator" and not hub.check_token(hello.get("token")):
        await websocket.send_text(json.dumps({"type": "error", "error": "bad token"}))
        await websocket.close(code=4401)
        return
    conn = Conn(websocket, hello, hub)
    hub.assign_identity(conn, hello.get("name"))
    await hub.register(conn)
    await conn.send(
        {
            "type": "welcome",
            "id": conn.id,
            "name": conn.name,
            "color": conn.color,
            "upload_key": conn.upload_key,
            "server_time": time.time(),
            "settings": hub.settings,
        }
    )
    if role == "coordinator":
        await conn.send({"type": "state", "state": hub.state()})
        await conn.send({"type": "history", "results": hub.store.results(limit=60, full=False), "events": hub.store.events(limit=100)})
    await hub.send_modules(conn)
    ping_task = asyncio.create_task(hub.ping_loop(conn))
    hub.store.add_event("connect", {"role": role, "ua": conn.ua, "name": conn.name}, device_id=conn.id)
    try:
        while True:
            msg = json.loads(await websocket.receive_text())
            conn.last_seen = time.time()
            t = msg.get("type")
            if t == "pong":
                hub.handle_pong(conn, msg)
            elif t == "rpc_result":
                hub.handle_rpc_result(conn, msg)
            elif t == "status":
                conn.status = msg.get("status", {})
                hub.mark_dirty()
            elif t == "module_status":
                conn.module_status[msg.get("name")] = {k: msg.get(k) for k in ("version", "ok", "error")}
                if not msg.get("ok"):
                    await hub.handle_event(conn, "module_error", {"name": msg.get("name"), "error": msg.get("error")})
                hub.mark_dirty()
            elif t == "log":
                await hub.handle_event(conn, "log", {"level": msg.get("level", "info"), "message": msg.get("message")})
            elif t == "event":
                await hub.handle_event(conn, msg.get("topic", "event"), msg.get("data"))
            elif t == "command" and role == "coordinator":
                asyncio.create_task(_run_command(hub, conn, msg))
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        ping_task.cancel()
        hub.unregister(conn)
        hub.store.add_event("disconnect", {"name": conn.name}, device_id=conn.id)


async def _run_command(hub: Hub, conn: Conn, msg: dict):
    try:
        value = await procedures.run(hub, msg.get("name"), msg.get("args") or {})
        reply = {"type": "command_result", "id": msg.get("id"), "ok": True, "value": value}
    except Exception as e:
        reply = {"type": "command_result", "id": msg.get("id"), "ok": False, "error": f"{e}"}
        await hub.progress(f"{msg.get('name')} failed: {e}")
    try:
        await conn.send(reply)
    except Exception:
        pass


# -- admin API ------------------------------------------------------------------------


@admin
async def api_state(request, hub: Hub):
    return JSON(hub.state())


@admin
async def api_eval(request, hub: Hub):
    body = await request.json()
    targets = hub.resolve_targets(body.get("to", "listeners"))
    if not targets:
        return JSON({"error": "no matching clients", "results": {}}, status_code=404)
    results = await hub.rpc_many(targets, "eval", {"code": body["code"]}, timeout=float(body.get("timeout", 15)))
    names = {c.id: c.name for c in targets}
    return JSON({"results": {cid: {**r, "name": names.get(cid)} for cid, r in results.items()}})


@admin
async def api_rpc(request, hub: Hub):
    body = await request.json()
    targets = hub.resolve_targets(body.get("to", "listeners"))
    results = await hub.rpc_many(targets, body["method"], body.get("args") or {}, timeout=float(body.get("timeout", 15)))
    names = {c.id: c.name for c in targets}
    return JSON({"results": {cid: {**r, "name": names.get(cid)} for cid, r in results.items()}})


@admin
async def api_modules(request, hub: Hub):
    return JSON({"modules": hub.module_list()})


@admin
async def api_module(request, hub: Hub):
    role, name = request.path_params["role"], request.path_params["name"]
    if role not in ("listener", "coordinator"):
        return JSON({"error": "role must be listener or coordinator"}, status_code=400)
    if request.method == "GET":
        for m in hub.modules_for(role):
            if m["name"] == name:
                return Response(m["code"], media_type="text/javascript")
        return JSON({"error": "not found"}, status_code=404)
    if request.method == "DELETE":
        await hub.remove_module(role, name)
        return JSON({"ok": True})
    code = (await request.body()).decode()
    await hub.push_module(role, name, code)
    # give clients a moment to report whether the module loaded
    await asyncio.sleep(float(request.query_params.get("wait", 1.5)))
    statuses = {
        c.id: {"name": c.name, **c.module_status.get(name, {"ok": None})} for c in hub.conns.values() if c.role == role
    }
    return JSON({"ok": True, "clients": statuses})


@admin
async def api_run(request, hub: Hub):
    name = request.path_params["name"]
    try:
        args = await request.json()
    except ValueError:
        args = {}
    return JSON(await procedures.run(hub, name, args or {}))


@admin
async def api_events(request, hub: Hub):
    q = request.query_params
    return JSON(
        {
            "events": hub.store.events(
                since_id=int(q.get("since_id", 0)),
                topic=q.get("topic"),
                limit=int(q.get("limit", 200)),
                device_id=q.get("device_id"),
            )
        }
    )


@admin
async def api_clips(request, hub: Hub):
    q = request.query_params
    return JSON({"clips": hub.store.clips(int(q.get("since_id", 0)), q.get("label"), int(q.get("limit", 200)))})


@admin
async def api_clip_wav(request, hub: Hub):
    clip_id = int(request.path_params["id"])
    row = hub.store.clip_row(clip_id)
    if not row:
        return JSON({"error": "not found"}, status_code=404)
    return Response(
        hub.store.clip_wav_bytes(clip_id),
        media_type="audio/wav",
        headers={
            "X-Start-Frame": str(row["start_frame"]),
            "X-Sample-Rate": str(row["sr"]),
            "X-Device-Id": row["device_id"],
            "X-Label": row["label"] or "",
        },
    )


@admin
async def api_results(request, hub: Hub):
    q = request.query_params
    return JSON(
        {
            "results": hub.store.results(
                kind=q.get("kind"),
                limit=int(q.get("limit", 20)),
                since_id=int(q.get("since_id", 0)),
                full=q.get("full") in ("1", "true"),
            )
        }
    )


@admin
async def api_result(request, hub: Hub):
    r = hub.store.result(int(request.path_params["id"]))
    if not r:
        return JSON({"error": "not found"}, status_code=404)
    return JSON(r)


@admin
async def api_settings(request, hub: Hub):
    if request.method == "POST":
        patch = await request.json()
        hub.update_settings(patch)
        await hub.broadcast({"type": "settings", "settings": hub.settings})
    return JSON(hub.settings)


@admin
async def api_query(request, hub: Hub):
    body = await request.json()
    return JSON(hub.store.query(body["sql"], body.get("params"), int(body.get("limit", 1000))))


@admin
async def api_reload(request, hub: Hub):
    body = await request.json() if (await request.body()) else {}
    targets = hub.resolve_targets(body.get("to", "listeners"))
    for c in targets:
        await hub._safe_send(c, {"type": "reload"})
    return JSON({"reloaded": [c.name for c in targets]})


@admin
async def api_update(request, hub: Hub):
    if hub.update_hook is None:
        return JSON({"error": "server was not started with --auto-update"}, status_code=400)
    return JSON(await hub.update_hook(force_restart=True))


@admin
async def api_restart(request, hub: Hub):
    if hub.restart_hook is None:
        return JSON({"error": "restart not available"}, status_code=400)
    asyncio.get_running_loop().call_later(0.5, hub.restart_hook)
    return JSON({"restarting": True})


# -- app factory ------------------------------------------------------------------------


def create_app(data_dir="beep-data", token="dev", public_url=None, hub: Hub | None = None) -> Starlette:
    store = Store(data_dir)
    hub = hub or Hub(store, token, public_url)

    @asynccontextmanager
    async def lifespan(app):
        tasks = [
            asyncio.create_task(hub.state_loop()),
            asyncio.create_task(hub.watch_disk_modules()),
        ]
        procedures.Monitor(hub)
        app.state.background = tasks
        try:
            yield
        finally:
            for t in tasks:
                t.cancel()

    routes = [
        Route("/", page("coordinator.html")),
        Route("/join", page("listener.html")),
        Route("/sim", page("sim.html")),
        Route("/qr.svg", qr_svg),
        Route("/health", health),
        Route("/upload/clip", upload_clip, methods=["POST"]),
        WebSocketRoute("/ws", ws_endpoint),
        Route("/api/state", api_state),
        Route("/api/eval", api_eval, methods=["POST"]),
        Route("/api/rpc", api_rpc, methods=["POST"]),
        Route("/api/modules", api_modules),
        Route("/api/modules/{role}/{name}", api_module, methods=["GET", "PUT", "POST", "DELETE"]),
        Route("/api/run/{name}", api_run, methods=["POST"]),
        Route("/api/events", api_events),
        Route("/api/clips", api_clips),
        Route("/api/clips/{id:int}.wav", api_clip_wav),
        Route("/api/results", api_results),
        Route("/api/results/{id:int}", api_result),
        Route("/api/settings", api_settings, methods=["GET", "POST"]),
        Route("/api/query", api_query, methods=["POST"]),
        Route("/api/reload", api_reload, methods=["POST"]),
        Route("/api/update", api_update, methods=["POST"]),
        Route("/api/restart", api_restart, methods=["POST"]),
        Mount("/static", app=StaticFiles(directory=STATIC), name="static"),
    ]
    app = Starlette(routes=routes, lifespan=lifespan)
    app.state.hub = hub
    return app
