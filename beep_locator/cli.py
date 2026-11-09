"""Command line: ``beep-locator serve`` and ``beep-locator ctl ...``."""

from __future__ import annotations

import asyncio
import atexit
import json
import os
import re
import secrets
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

import click

from .client import CONFIG, ApiError, Client

ENV_TUNNEL_URL = "BEEP_LOCATOR_TUNNEL_URL"
ENV_TUNNEL_PID = "BEEP_LOCATOR_TUNNEL_PID"


@click.group()
@click.version_option()
def cli():
    "Find a beeping device using a bunch of phones and laptops as microphones."


# ---------------------------------------------------------------------------
# serve
# ---------------------------------------------------------------------------


def _load_token(data_dir: Path, token: str | None) -> str:
    data_dir.mkdir(parents=True, exist_ok=True)
    f = data_dir / "token.txt"
    if token:
        return token
    if f.exists():
        return f.read_text().strip()
    t = secrets.token_urlsafe(16)
    f.write_text(t)
    return t


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _start_tunnel(port: int, data_dir: Path, scheme: str) -> str:
    """Start a Cloudflare quick tunnel and return its https URL.

    cloudflared writes to a log file rather than a pipe, so it survives the
    server restarting itself with execv (auto-update)."""
    existing = os.environ.get(ENV_TUNNEL_URL)
    pid = os.environ.get(ENV_TUNNEL_PID)
    if existing and pid and _pid_alive(int(pid)):
        atexit.register(_kill, int(pid))
        return existing
    exe = shutil.which("cloudflared")
    if not exe:
        raise click.ClickException(
            "cloudflared not found. Install it (macOS: brew install cloudflared; see "
            "https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/) "
            "or run your own tunnel and pass --public-url."
        )
    log_path = data_dir / "cloudflared.log"
    log = open(log_path, "w")
    args = [exe, "tunnel", "--no-autoupdate", "--url", f"{scheme}://127.0.0.1:{port}"]
    if scheme == "https":
        args += ["--no-tls-verify"]
    proc = subprocess.Popen(args, stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, start_new_session=True)
    atexit.register(_kill, proc.pid)
    click.echo("Starting Cloudflare tunnel...", err=True)
    deadline = time.time() + 40
    while time.time() < deadline:
        time.sleep(0.5)
        m = re.search(r"https://[a-z0-9-]+\.trycloudflare\.com", log_path.read_text())
        if m:
            os.environ[ENV_TUNNEL_URL] = m.group(0)
            os.environ[ENV_TUNNEL_PID] = str(proc.pid)
            return m.group(0)
        if proc.poll() is not None:
            break
    raise click.ClickException(f"cloudflared did not report a URL; see {log_path}")


def _kill(pid: int):
    # only on real exit, not on execv restarts
    if os.environ.get("BEEP_LOCATOR_RESTARTING"):
        return
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        pass


def _self_signed(data_dir: Path) -> tuple[str, str]:
    cert, key = data_dir / "cert.pem", data_dir / "key.pem"
    if not cert.exists():
        if not shutil.which("openssl"):
            raise click.ClickException("openssl is needed to make a self-signed certificate")
        subprocess.run(
            [
                "openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "365",
                "-keyout", str(key), "-out", str(cert), "-subj", "/CN=beep-locator",
            ],
            check=True,
            capture_output=True,
        )
    return str(cert), str(key)


class Updater:
    """git pull + restart when the branch on origin moves (opt-in)."""

    def __init__(self, repo: Path, interval: float, restart):
        self.repo = repo
        self.interval = interval
        self.restart = restart
        self.branch = self.git("rev-parse", "--abbrev-ref", "HEAD").strip()
        self.last_check = None
        self.last_result = None

    def git(self, *args) -> str:
        return subprocess.run(["git", "-C", str(self.repo), *args], check=True, capture_output=True, text=True).stdout

    def check(self, force_restart=False) -> dict:
        self.last_check = time.time()
        try:
            self.git("fetch", "--quiet", "origin", self.branch)
            head = self.git("rev-parse", "HEAD").strip()
            remote = self.git("rev-parse", f"origin/{self.branch}").strip()
            if head == remote:
                res = {"updated": False, "head": head}
                if force_restart:
                    self.restart()
                    res["restarting"] = True
                self.last_result = res
                return res
            try:
                self.git("merge-base", "--is-ancestor", "HEAD", f"origin/{self.branch}")
            except subprocess.CalledProcessError:
                res = {"updated": False, "error": "local branch has diverged; not updating", "head": head, "remote": remote}
                self.last_result = res
                return res
            changed = self.git("diff", "--name-only", head, remote).split()
            self.git("merge", "--ff-only", "--quiet", f"origin/{self.branch}")
            if any(f in ("pyproject.toml", "uv.lock") for f in changed) and shutil.which("uv"):
                subprocess.run(["uv", "sync", "--quiet"], cwd=self.repo)
            res = {"updated": True, "from": head, "to": remote, "files": changed, "restarting": True}
            self.last_result = res
            click.echo(f"Updated {head[:8]} -> {remote[:8]}; restarting", err=True)
            self.restart()
            return res
        except Exception as e:  # pragma: no cover
            res = {"updated": False, "error": str(e)}
            self.last_result = res
            return res

    async def loop(self):
        while True:
            await asyncio.sleep(self.interval)
            await asyncio.to_thread(self.check)


@cli.command()
@click.option("--host", default="0.0.0.0", show_default=True)
@click.option("--port", default=8765, show_default=True, type=int)
@click.option("--data-dir", default="beep-data", show_default=True, type=click.Path(file_okay=False))
@click.option("--token", envvar="BEEP_LOCATOR_TOKEN", help="Admin token (default: generated and saved in the data dir)")
@click.option("--public-url", help="URL that phones should use, if you run your own tunnel/proxy")
@click.option("--tunnel", is_flag=True, help="Start a Cloudflare quick tunnel (needs cloudflared) for an https URL")
@click.option("--https", "use_https", is_flag=True, help="Serve https with a self-signed certificate (LAN use)")
@click.option("--auto-update", is_flag=True, help="Poll git origin for new commits on this branch; pull and restart")
@click.option("--update-interval", default=60.0, show_default=True, type=float)
def serve(host, port, data_dir, token, public_url, tunnel, use_https, auto_update, update_interval):
    "Run the coordinating server."
    import uvicorn

    from .hub import Hub
    from .server import create_app
    from .store import Store

    data = Path(data_dir)
    token = _load_token(data, token)
    ssl = {}
    scheme = "http"
    if use_https:
        cert, key = _self_signed(data)
        ssl = {"ssl_certfile": cert, "ssl_keyfile": key}
        scheme = "https"
    if tunnel:
        public_url = _start_tunnel(port, data, scheme)
    base = public_url or f"{scheme}://localhost:{port}"
    store = Store(data)
    hub = Hub(store, token, public_url)
    app = create_app(data, token, public_url, hub=hub)
    config = uvicorn.Config(app, host=host, port=port, log_level="warning", ws_ping_interval=20, ws_ping_timeout=30, **ssl)
    server = uvicorn.Server(config)
    restart = {"flag": False}

    def do_restart():
        restart["flag"] = True
        server.should_exit = True

    hub.restart_hook = do_restart
    updater = None
    if auto_update:
        repo = Path(__file__).resolve().parent.parent
        if not (repo / ".git").exists():
            raise click.ClickException("--auto-update needs the server to run from a git checkout")
        updater = Updater(repo, update_interval, do_restart)

        async def update_hook(force_restart=False):
            return await asyncio.to_thread(updater.check, force_restart)

        hub.update_hook = update_hook

    join = base.rstrip("/") + "/join"
    click.echo("")
    click.echo(f"  Coordinator (keep private):  {base}/?token={token}")
    click.echo(f"  Listeners join at:           {join}")
    click.echo(f"  Simulated room:              {base}/sim")
    click.echo(f"  Admin API for Claude/scripts: BEEP_URL={base} BEEP_TOKEN={token}")
    if auto_update:
        click.echo(f"  Auto-update: watching origin/{updater.branch} every {update_interval:.0f}s")
    if not public_url and not use_https:
        click.echo("\n  Note: phones need https for microphone access. Use --tunnel (or --https on a LAN).")
    try:
        import segno

        click.echo("")
        segno.make(join, error="m").terminal(compact=True)
    except Exception:
        pass
    click.echo("")

    async def main():
        tasks = []
        if updater:
            tasks.append(asyncio.create_task(updater.loop()))
        try:
            await server.serve()
        finally:
            for t in tasks:
                t.cancel()

    asyncio.run(main())
    if restart["flag"]:
        os.environ["BEEP_LOCATOR_RESTARTING"] = "1"
        click.echo("Restarting...", err=True)
        sys.stdout.flush()
        sys.stderr.flush()
        env = dict(os.environ)
        env.pop("BEEP_LOCATOR_RESTARTING", None)
        os.execve(sys.executable, [sys.executable, "-m", "beep_locator", *sys.argv[1:]], env)


# ---------------------------------------------------------------------------
# ctl
# ---------------------------------------------------------------------------


@cli.group()
@click.option("--url", envvar="BEEP_URL", help="Server URL (or BEEP_URL)")
@click.option("--token", envvar="BEEP_TOKEN", help="Admin token (or BEEP_TOKEN)")
@click.pass_context
def ctl(ctx, url, token):
    "Control a running server through its admin API."
    ctx.obj = {"url": url, "token": token}


def _client(ctx) -> Client:
    return Client.from_env(ctx.obj.get("url"), ctx.obj.get("token"))


def _print(obj):
    click.echo(json.dumps(obj, indent=2, default=str))


def _wrap(fn):
    import functools

    @functools.wraps(fn)
    def inner(*a, **kw):
        try:
            return fn(*a, **kw)
        except ApiError as e:
            raise click.ClickException(f"{e.status}: {json.dumps(e.body) if not isinstance(e.body, str) else e.body}")

    return inner


@ctl.command()
@click.argument("url")
@click.argument("token")
def login(url, token):
    "Save URL and token to ~/.beep-locator.json"
    CONFIG.write_text(json.dumps({"url": url.rstrip("/"), "token": token}))
    os.chmod(CONFIG, 0o600)
    click.echo(f"Saved to {CONFIG}")


@ctl.command()
@click.option("--json", "as_json", is_flag=True)
@click.pass_context
@_wrap
def state(ctx, as_json):
    "Show connected devices, calibration and settings."
    s = _client(ctx).state()
    if as_json:
        return _print(s)
    click.echo(f"procedure: {s['procedure'] or '-'}   public_url: {s['public_url']}   monitoring: {s['settings'].get('monitoring')}")
    for d in s["devices"]:
        st = d["status"] or {}
        lvl = st.get("level") or {}
        click.echo(
            f"  {d['name']:<12} {d['role']:<11} {'sim ' if d['sim'] else ''}{'running' if st.get('running') else 'stopped':<8} "
            f"{st.get('sampleRate') or '-':>6}Hz lvl={lvl.get('rms', float('nan')):.0f}dB rtt={d['clock']['rtt_ms']}ms "
            f"vis={st.get('visibility')} wake={st.get('wakeLock')} gaps={st.get('gaps')} id={d['id']}"
        )
        if st.get("lastError"):
            click.echo(f"      last error: {st['lastError']}")
    L = s.get("layout")
    if L:
        click.echo(f"layout (fit {L['rms_residual'] * 100:.1f}cm):")
        for i, did in enumerate(L["device_ids"]):
            x, y = L["positions"][i]
            click.echo(f"  {L.get('names', {}).get(did, did):<12} ({x:6.2f}, {y:6.2f})")
    else:
        click.echo("not calibrated")
    click.echo("modules: " + ", ".join(f"{m['role']}/{m['name']}@{m['version']}({m['source']})" for m in s["modules"]))


@ctl.command("eval")
@click.argument("code", required=False)
@click.option("-f", "--file", type=click.File())
@click.option("--to", default="listeners", help="listeners, coordinators, all, or comma-separated ids/names")
@click.option("--timeout", default=15.0)
@click.pass_context
@_wrap
def eval_(ctx, code, file, to, timeout):
    """Run JavaScript on clients. The code is an async function body with
    `ctx` (module context) and `rt` (runtime) in scope; `return` a value."""
    if file:
        code = file.read()
    if not code:
        raise click.UsageError("give CODE or -f FILE")
    target = to.split(",") if "," in to else to
    _print(_client(ctx).eval(code, target, timeout))


@ctl.command()
@click.argument("method")
@click.argument("args", required=False, default="{}")
@click.option("--to", default="listeners")
@click.option("--timeout", default=15.0)
@click.pass_context
@_wrap
def rpc(ctx, method, args, to, timeout):
    "Call an RPC method on clients, ARGS is JSON."
    target = to.split(",") if "," in to else to
    _print(_client(ctx).rpc(method, json.loads(args), target, timeout))


@ctl.command()
@click.pass_context
@_wrap
def modules(ctx):
    "List modules."
    for m in _client(ctx).modules():
        click.echo(f"{m['role']}/{m['name']}  {m['version']}  {m['source']}  {time.ctime(m['ts'])}")


@ctl.command()
@click.argument("role", type=click.Choice(["listener", "coordinator"]))
@click.argument("name")
@click.argument("file", type=click.File())
@click.pass_context
@_wrap
def push(ctx, role, name, file):
    "Install/replace a module on every client of ROLE, from FILE."
    _print(_client(ctx).push_module(role, name, file.read()))


@ctl.command()
@click.argument("role", type=click.Choice(["listener", "coordinator"]))
@click.argument("name")
@click.pass_context
@_wrap
def unpush(ctx, role, name):
    "Remove a pushed module (reverting to the version on disk, if any)."
    _print(_client(ctx).remove_module(role, name))


@ctl.command()
@click.argument("procedure")
@click.argument("args", nargs=-1)
@click.pass_context
@_wrap
def run(ctx, procedure, args):
    """Run a procedure: calibrate, testbeep device=Red, locate, identify device=Blue,
    monitoring on=false. ARGS are key=value (values parsed as JSON when possible)."""
    kw = {}
    for a in args:
        k, _, v = a.partition("=")
        try:
            kw[k] = json.loads(v)
        except ValueError:
            kw[k] = v
    _print(_client(ctx).run(procedure, **kw))


@ctl.command()
@click.option("--topic")
@click.option("--since", "since_id", default=0, type=int)
@click.option("--limit", default=50, type=int)
@click.option("--follow", is_flag=True)
@click.pass_context
@_wrap
def events(ctx, topic, since_id, limit, follow):
    "Show recorded events (logs, onsets, progress...)."
    c = _client(ctx)
    while True:
        evs = c.events(since_id=since_id, topic=topic, limit=limit)
        for e in evs:
            click.echo(f"{e['id']:>6} {time.strftime('%H:%M:%S', time.localtime(e['ts']))} {e['topic']:<12} {e['device_id'] or '':<14} {json.dumps(e['data'])[:300]}")
            since_id = max(since_id, e["id"])
        if not follow:
            break
        time.sleep(2)


@ctl.command()
@click.option("--label")
@click.option("--limit", default=30, type=int)
@click.pass_context
@_wrap
def clips(ctx, label, limit):
    "List recorded clips."
    for r in _client(ctx).clips(label=label, limit=limit):
        click.echo(f"{r['id']:>6} {time.strftime('%H:%M:%S', time.localtime(r['ts']))} {r['label']:<20} {r['device_id']:<14} {r['sr']:.0f}Hz n={r['n']} start={r['start_frame']}")


@ctl.command()
@click.argument("clip_id", type=int)
@click.option("-o", "--output", type=click.Path(dir_okay=False))
@click.pass_context
@_wrap
def clip(ctx, clip_id, output):
    "Download a clip as WAV."
    body, headers = _client(ctx).clip_wav(clip_id)
    out = output or f"clip-{clip_id}.wav"
    Path(out).write_bytes(body)
    click.echo(f"{out}  start_frame={headers.get('X-Start-Frame')} sr={headers.get('X-Sample-Rate')} device={headers.get('X-Device-Id')}")


@ctl.command()
@click.option("--kind")
@click.option("--limit", default=20, type=int)
@click.pass_context
@_wrap
def results(ctx, kind, limit):
    "List calibration / locate results."
    for r in _client(ctx).results(kind=kind, limit=limit):
        click.echo(f"{r['id']:>5} {time.strftime('%H:%M:%S', time.localtime(r['ts']))} {r['kind']:<12} {json.dumps(r['summary'])}")


@ctl.command()
@click.argument("result_id", type=int)
@click.pass_context
@_wrap
def result(ctx, result_id):
    "Show one result in full."
    _print(_client(ctx).result(result_id))


@ctl.command()
@click.argument("patch", required=False)
@click.pass_context
@_wrap
def settings(ctx, patch):
    "Show settings, or update them with a JSON PATCH."
    _print(_client(ctx).settings(json.loads(patch) if patch else None))


@ctl.command()
@click.argument("sql")
@click.pass_context
@_wrap
def sql(ctx, sql):
    "Run a read-only SQL query against the server's database."
    r = _client(ctx).query(sql)
    click.echo("\t".join(r["columns"]))
    for row in r["rows"]:
        click.echo("\t".join(str(v) for v in row))


@ctl.command()
@click.option("--to", default="listeners")
@click.pass_context
@_wrap
def reload(ctx, to):
    "Ask client pages to reload (they will need a tap to restart audio)."
    _print(_client(ctx).reload(to))


@ctl.command()
@click.pass_context
@_wrap
def update(ctx):
    "Tell the server to git pull and restart (needs --auto-update)."
    _print(_client(ctx).update())


@ctl.command()
@click.pass_context
@_wrap
def restart(ctx):
    "Restart the server process."
    _print(_client(ctx).restart())


if __name__ == "__main__":
    cli()
