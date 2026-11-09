# Notes for Claude Code working on beep-locator

Read `problem.md` (the why) and `README.md` (how to run). You are running on
the user's laptop, on the same network as the listener devices.

## Running

- Server: `uv run beep-locator serve --tunnel` (phones need https for the mic;
  the tunnel needs `cloudflared`). Run it in the background and keep it up -
  restarting is fine, listener pages reconnect and keep their audio.
- `uv run beep-locator ctl ...` talks to `http://localhost:8765` and reads the
  token from `beep-data/token.txt` when run from the repo root.
- `uv run pytest` - simulation-based tests of the maths plus server tests.
- `/sim?n=5` (open in headless Chromium via Playwright) is a fake room for
  testing without hardware; sim devices report true positions via
  `ctl eval 'return [rt.io.x, rt.io.y]'`.

## Iterating

- Listener/coordinator browser code lives in `beep_locator/modules/{listener,coordinator}/*.js`.
  Saving a file there pushes it to every connected client within ~1s, hot-swapped
  without a page reload. Check `ctl events --topic module_error` after edits.
- `beep_locator/static/runtime.js` is the stable bootloader: changing it needs
  every device to reload and tap Start again - avoid unless necessary.
- Python changes (`procedures.py`, `analysis.py`, `dsp.py`, ...) need a server
  restart: `ctl restart`.
- Experiment without editing files: `ctl eval` runs JS on devices,
  `ctl clips` / `ctl clip ID` fetch recorded audio, `Client.clip(id)` in
  `beep_locator/client.py` returns a numpy `Recording` for offline analysis
  with `analysis.py`. All results are stored in `beep-data/beep.db`
  (`ctl sql ...`).

## Suggested first steps on real hardware

1. `ctl state` - all devices running, no gaps, wake lock held?
2. `ctl run selftest` - each device must hear its own chirp with echo
   cancellation/AGC off. iOS Safari is the main risk.
3. `ctl run calibrate`, then compare a couple of distances against a tape
   measure (ask the user). Check `ctl results` fit error.
4. `ctl run testbeep device=<name>` for each device - error should be small.
5. Tune detection (`ctl settings '{"detector": {...}, "trigger": {...}}'`)
   so real beeps trigger and room noise doesn't.

Keep listeners' audio running: never send `ctl reload` unless the user can
go and tap every device.
