# cloakbrowser-mcp-server

Headed wrapper around [`swimmwatch/cloakbrowser-mcp`](https://github.com/swimmwatch/cloakbrowser-mcp) that exposes the Chromium browser over **VNC** so a human can watch the same session an MCP client is driving over **Streamable HTTP**.

The whole project is one Python launcher + one Dockerfile. It starts `Xvfb` + `openbox` so Chromium runs headed (required by anti-bot probes that inspect `window`/widget state), mirrors the display with `x11vnc`, then execs the upstream `cloakbrowser-mcp` CLI in Streamable HTTP mode.

---

## Quick start

```bash
docker build -t cloakbrowser-mcp-server .

docker run --rm \
  -p 3000:3000 \
  -p 5900:5900 \
  -e VNC_PASSWORD=changeme \
  -e DISPLAY_WIDTH=1024 \
  -e DISPLAY_HEIGHT=768 \
  cloakbrowser-mcp-server
```

Then:

1. **MCP client** — point it at `http://localhost:3000/mcp` (any Streamable HTTP MCP client: Claude Desktop via `claude mcp add --transport http cloakbrowser http://localhost:3000/mcp`, Codex CLI, Cursor, etc.)
2. **VNC viewer** — connect to `localhost:5900`, password `changeme`. macOS built-in: `open vnc://localhost:5900`. Any VNC client works.

---

## Architecture

```
                    ┌────────────────────────────────────────────┐
                    │  container                                 │
   host ── 3000 ──► │  cloakbrowser-mcp (Streamable HTTP)        │
                    │   └─ Playwright MCP ──► Chromium headed    │
   host ── 5900 ──► │   Xvfb :99 + openbox  ◄── x11vnc mirror    │
                    │                                            │
                    │  /data (bind-mounted, persistent profile)  │
                    └────────────────────────────────────────────┘
```

`scripts/launcher.py` orchestrates the whole thing in one process:

0. SIGKILL any leftover Chromium from an earlier unclean container death — otherwise it keeps holding the SingletonLock on `/data` and the next run starts with the upstream `User data directory is already active in this process` error. Then remove any stale `SingletonLock`/`SingletonSocket`/`SingletonCookie` files just in case.
1. `Xvfb :99` at `$DISPLAY_WIDTH x $DISPLAY_HEIGHT`
2. `openbox` (window manager so Chromium honours `--start-maximized`)
3. `x11vnc` on `:5900`, password from `$VNC_PASSWORD` or open
4. `exec` `node /opt/cloakbrowser-mcp/dist/cli.js --transport streamable-http --http-host 0.0.0.0 --http-port 3000`
5. force `PLAYWRIGHT_MCP_HEADLESS=false` in the child env (the wrapper's whole reason to exist)
6. wait for SIGTERM/SIGINT, tear down X + VNC

---

## Configuration

### Environment variables (wrapper-specific)

| Var              | Default | Effect                                                                                           |
| ---------------- | ------- | ------------------------------------------------------------------------------------------------ |
| `VNC_PASSWORD`   | unset   | If set, VNC requires this plaintext password. If unset, VNC is unauthenticated (`x11vnc -nopw`). |
| `DISPLAY_WIDTH` | `1024` | Xvfb screen width. Also passed to Playwright as the page viewport (unless the operator sets `CLOAK_PLAYWRIGHT_MCP_CONTEXT_OPTIONS`). |
| `DISPLAY_HEIGHT` | `768` | Xvfb screen height. Also passed to Playwright as the page viewport. |
| `NO_PERSISTENT_PROFILE` | `unset` | If `1`/`true`/`yes`/`on`, the wrapper strips `PLAYWRIGHT_MCP_USER_DATA_DIR` from upstream's env. Use this to skip persistent profile and avoid the upstream Chromium singleton lockfile (`/data/.cloakbrowser-mcp-profile.lock`) that wedges container restarts when the Node process recycles mid-session. Trades persistence for resilience. |

Any `PLAYWRIGHT_MCP_*` and `CLOAK_PLAYWRIGHT_MCP_*` variable is forwarded to the upstream `cloakbrowser-mcp` CLI untouched. See [Configuration](https://swimmwatch.github.io/cloakbrowser-mcp/configuration/) in the upstream docs for the full list. The wrapper only overrides:

- `PLAYWRIGHT_MCP_HEADLESS=false` (always)
- `--transport streamable-http --http-host 0.0.0.0 --http-port 3000` (always)

MCP auth, persistent profiles, Chrome extensions, regional proxies, humanized input — all configured via upstream env vars.

### Hardcoded (cannot be changed via env)

| Thing         | Value             | Reason                                                            |
| ------------- | ----------------- | ----------------------------------------------------------------- |
| MCP transport | `streamable-http` | The wrapper exists to expose the MCP server over HTTP, not stdio. |
| MCP HTTP host | `0.0.0.0`         | Containerised; operator maps ports on `docker run`.               |
| MCP HTTP port | `3000`            | Same.                                                             |
| MCP endpoint  | `/mcp`            | Upstream default.                                                 |
| Display       | `:99`             | Xvfb + WM + Chromium all agree.                                   |
| VNC port      | `5900`            | Operator maps with `-p`.                                          |

---

## Volume

Bind-mount `/data` to persist Chromium's profile across container runs:

```bash
docker run --rm \
  -p 3000:3000 -p 5900:5900 \
  -v ~/cloak-mcp-data:/data \
  -e VNC_PASSWORD=changeme \
  cloakbrowser-mcp-server
```

Without the mount, `/data` lives inside the container and is lost on `docker rm`.

---

## Port mapping

The internal MCP port is always `3000` and the internal VNC port is always `5900`, regardless of what you map them to on the host:

```bash
# default: same ports on host and container
-p 3000:3000 -p 5900:5900

# remap both
-p 8080:3000 -p 5999:5900
```

---

## Project layout

```
cloakbrowser-mcp-server/
├── Dockerfile             # FROM swimmwatch/cloakbrowser-mcp:latest + X stack
├── README.md
├── pyproject.toml         # ruff config (lint + format)
├── scripts/
│   └── launcher.py        # single-file orchestrator
└── tests/
    └── test_launcher.py   # stdlib unittest, no test framework dependency
```

No build system, no CI — keep it boring.

## Lint & test

```bash
ruff check .                 # lint
ruff format --check .        # format check
ruff format .                # format fix
python3 -m unittest discover -s tests -v   # 12 unit tests, stdlib only
docker build -t cloakbrowser-mcp-server . # full image build (~30s with cached base)
```

---

## License

Inherits MIT from cloakbrowser-mcp.
