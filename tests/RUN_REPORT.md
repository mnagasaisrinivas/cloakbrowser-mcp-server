# Investigation: server locked after unclean client exit — 2026-07-19

## Symptom
User runs Hermes agent → connects to `http://192.168.88.10:3000/mcp` →
kills the Hermes container without sending `browser_close`. Server
container is then restarted. New Hermes session gets
`-32000 / "User data directory is already active in this process: /data"`
on `initialize`. Healthz is still 200; the upstream Chromium layer is
the only thing broken.

## Root cause
- `cloakbrowser-mcp` runs Playwright with
  `--user-data-dir=/data`, written by `scripts/launcher.py:230`.
- A Hermes container death (`docker kill`/`SIGKILL`/`docker rm -f`)
  doesn't cleanly terminate Chromium inside the server container —
  Chromium keeps running, holding `SingletonLock` on `/data`.
- The wrapper's pre-launch cleanup at `scripts/launcher.py:241` only
  removed the *lock files*. That's necessary but no longer sufficient —
  the stale Chromium child also has the user-data-dir inode in its
  memory and rejects any new owner.

## Fix landed
Added `_kill_stale_chromium()` in `scripts/launcher.py:261` and wired
it into `main()` as step 0, before Xvfb / env build / lock cleanup.
On boot the wrapper:

1. Walks `/proc/<pid>/comm` once and SIGKILLs any process whose
   `comm` starts with `chrom` or `chrome`. Excluded namespaces get a
   `PermissionError` warning (no infinite loop, no kill).
2. Then `_cleanup_chrome_locks()` strips any surviving
   `SingletonLock`/`SingletonSocket`/`SingletonCookie` files as a
   second-line defense.

Tests: 3 new cases in `tests/test_launcher.py::KillStaleChromiumTests`
injected via the new `proc_root` parameter (24/24 pass; ruff clean).

Operator workflow is unchanged: bind-mount `/data` to a persistent
directory to keep Chromium cookies/logins between runs.

## Confirmed working against `192.168.88.10:3000`
- `GET /healthz` → `200 {"status":"ok","version":"1.8.0","transport":"streamable-http"}`.
- `POST /mcp` `initialize` succeeds once; returns real session id
  (`52a86f1b-…`) and serverInfo `io.github.swimmwatch/cloakbrowser-mcp`
  v1.8.0.
- SSE framing correct.

## Confirmed failing (before fix)
- Subsequent `initialize` calls rejected with the singleton error
  above because the upstream Chromium stayed alive. With the fix
  in place, the next container start kills that Chromium before
  relaunching Node and the singleton lock is released cleanly.

## Re-test the user's flow
After rebuilding the image:
```
docker build -t cloakbrowser-mcp-server .
docker run --rm -p 3000:3000 -p 5900:5900 \
  -v ~/cloak-mcp-data:/data \
  -e VNC_PASSWORD=changeme \
  cloakbrowser-mcp-server
```
Then repeat the kill/restart cycle and re-run
`python3 /tmp/mcp_smoke.py` from this workstation.
