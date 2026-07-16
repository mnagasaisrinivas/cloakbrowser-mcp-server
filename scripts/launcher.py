"""CloakBrowser MCP wrapper — headed mode + VNC mirror.

Brings up Xvfb + a window manager + x11vnc, then execs the upstream
cloakbrowser-mcp CLI in Streamable HTTP mode so an MCP client can drive
the same visible browser a human watches over VNC.

Operator knobs (all optional, defaults shown):

    VNC_PASSWORD     if set, VNC requires this password; unset → no password
    DISPLAY_WIDTH    Xvfb screen width  (default: 1280)
    DISPLAY_HEIGHT   Xvfb screen height (default: 800)

Hardcoded:

    MCP transport    streamable-http on 0.0.0.0:3000
    X display        :99
    VNC port         5900

All other PLAYWRIGHT_MCP_* and CLOAK_PLAYWRIGHT_MCP_* variables pass
through to cloakbrowser-mcp untouched.
"""

from __future__ import annotations

import logging
import os
import signal
import socket
import subprocess
import sys
import time
from contextlib import suppress
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s: %(message)s",
    stream=sys.stderr,
)
log = logging.getLogger("launcher")

DISPLAY = ":99"
VNC_PORT = 5900
MCP_HTTP_PORT = 3000
MCP_HTTP_HOST = "0.0.0.0"

DEFAULT_WIDTH = 1024
DEFAULT_HEIGHT = 768

CLOAK_MCP_BIN = "/opt/cloakbrowser-mcp/dist/cli.js"


def _have(cmd: str) -> bool:
    from shutil import which

    return which(cmd) is not None


def _int_env(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        log.warning("%s=%r is not an integer; using default %d", name, raw, default)
        return default


def _wait_for_tcp(
    host: str,
    port: int,
    timeout: float,
    what: str,
    hint: subprocess.Popen | None = None,
) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if hint is not None and hint.poll() is not None:
            log.error(
                "%s exited (rc=%s) before %s:%d opened",
                Path(hint.args[0]).name,
                hint.returncode,
                host,
                port,
            )
            return False
        try:
            with socket.create_connection((host, port), timeout=0.5):
                return True
        except OSError:
            time.sleep(0.2)
    log.error("timeout waiting for %s on %s:%d", what, host, port)
    return False


def _terminate(proc: subprocess.Popen, timeout: float = 5.0) -> None:
    if proc.poll() is not None:
        return
    try:
        proc.terminate()
        proc.wait(timeout=timeout)
        return
    except (ProcessLookupError, subprocess.TimeoutExpired):
        pass
    try:
        proc.kill()
        proc.wait(timeout=timeout)
    except ProcessLookupError:
        pass


def _start_x_stack(width: int, height: int) -> tuple[subprocess.Popen, subprocess.Popen | None]:
    """Xvfb + optional WM. /tmp is not tmpfs in the upstream image, so
    a stale X99-lock from a prior SIGKILL'd run survives and Xvfb refuses
    to start without a manual clean."""
    Path("/tmp/.X99-lock").unlink(missing_ok=True)
    for f in Path("/tmp/.X11-unix").glob("X*"):
        f.unlink(missing_ok=True)

    os.environ["DISPLAY"] = DISPLAY
    xvfb = subprocess.Popen(
        [
            "Xvfb",
            DISPLAY,
            "-screen",
            "0",
            f"{width}x{height}x24",
            "-nolisten",
            "tcp",
        ]
    )

    for _ in range(50):
        try:
            subprocess.run(
                ["xdotool", "getdisplaygeometry"],
                check=True,
                capture_output=True,
                timeout=1,
            )
            break
        except (
            subprocess.CalledProcessError,
            subprocess.TimeoutExpired,
            FileNotFoundError,
        ):
            time.sleep(0.2)
    else:
        raise SystemExit("Xvfb failed to start within 10s")
    log.info("Xvfb ready on %s (%dx%d)", DISPLAY, width, height)

    wm: subprocess.Popen | None = None
    if _have("openbox"):
        wm = subprocess.Popen(["openbox"])
    else:
        log.warning("openbox not installed; browser windows will be unmanaged")
    return xvfb, wm


def _build_vnc_args(width: int, height: int, password: str) -> list[str]:
    """Build the x11vnc argv. Extracted so tests can assert the arg list
    without spawning a process. The passwdfile path is returned alongside
    via the side-effect of writing /tmp/vncpasswd when a password is set."""
    cmd = [
        "x11vnc",
        "-display",
        DISPLAY,
        "-forever",
        "-shared",
        "-rfbport",
        str(VNC_PORT),
        "-listen",
        "0.0.0.0",
        "-noxdamage",
        "-geometry",
        f"{width}x{height}",
    ]
    if password:
        pw_file = Path("/tmp/vncpasswd")
        pw_file.write_text(password + "\n")
        pw_file.chmod(0o600)
        # -passwdfile reads plaintext; -rfbauth wants vncpasswd's binary
        # DES format and rejects our plaintext file with auth failures.
        cmd += ["-passwdfile", str(pw_file)]
    else:
        cmd += ["-nopw"]
    return cmd


def _start_vnc(width: int, height: int) -> subprocess.Popen | None:
    if not _have("x11vnc"):
        log.warning("x11vnc not installed; skipping VNC")
        return None

    vnc_pw = os.environ.get("VNC_PASSWORD", "")
    cmd = _build_vnc_args(width, height, vnc_pw)
    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    mode = "password" if vnc_pw else "no password"
    if _wait_for_tcp("127.0.0.1", VNC_PORT, 5.0, "x11vnc", hint=proc):
        log.info("x11vnc on :%d (%s)", VNC_PORT, mode)
    else:
        log.warning("x11vnc didn't open :%d within 5s; continuing", VNC_PORT)
    return proc


def _build_mcp_env(
    width: int,
    height: int,
    base_env: dict[str, str] | None = None,
    data_dir_path: Path = Path("/data"),
) -> dict[str, str]:
    """Build the environment variables for the cloakbrowser-mcp process."""
    env = dict(base_env) if base_env is not None else os.environ.copy()
    env["DISPLAY"] = DISPLAY
    # Upstream defaults to headless; this wrapper's whole purpose is
    # the visible browser, so force it on regardless of caller env.
    env["PLAYWRIGHT_MCP_HEADLESS"] = "false"
    # If the operator hasn't set context options, default the viewport
    # to the Xvfb screen so the Chromium window fills the VNC display
    # without a black strip. Operator-supplied CLOAK_PLAYWRIGHT_MCP_CONTEXT_OPTIONS
    # is honoured untouched.
    if "CLOAK_PLAYWRIGHT_MCP_CONTEXT_OPTIONS" not in env:
        env["CLOAK_PLAYWRIGHT_MCP_CONTEXT_OPTIONS"] = (
            f'{{"viewport":{{"width":{width},"height":{height}}}}}'
        )
    # Enable persistent user data by defaulting to /data if it exists and is writable.
    if "PLAYWRIGHT_MCP_USER_DATA_DIR" not in env and data_dir_path.is_dir():
        if os.access(data_dir_path, os.W_OK):
            env["PLAYWRIGHT_MCP_USER_DATA_DIR"] = str(data_dir_path)
            log.info("defaulting PLAYWRIGHT_MCP_USER_DATA_DIR to %s", data_dir_path)
        else:
            log.warning(
                "directory %s exists but is not writable by the current user; "
                "not enabling persistent profile by default",
                data_dir_path,
            )
    return env


def main() -> None:
    width = _int_env("DISPLAY_WIDTH", DEFAULT_WIDTH)
    height = _int_env("DISPLAY_HEIGHT", DEFAULT_HEIGHT)
    vnc_pw_set = bool(os.environ.get("VNC_PASSWORD", "").strip())
    log.info(
        "starting cloakbrowser-mcp headful wrapper: display=%dx%d vnc=%s mcp=%s:%d",
        width,
        height,
        "password" if vnc_pw_set else "open",
        MCP_HTTP_HOST,
        MCP_HTTP_PORT,
    )

    if not Path(CLOAK_MCP_BIN).is_file():
        raise SystemExit(f"cloakbrowser-mcp CLI not found at {CLOAK_MCP_BIN}")

    xvfb = wm = vnc = mcp = None
    try:
        xvfb, wm = _start_x_stack(width, height)
        vnc = _start_vnc(width, height)

        env = _build_mcp_env(width, height)
        mcp = subprocess.Popen(
            [
                "node",
                CLOAK_MCP_BIN,
                "--transport",
                "streamable-http",
                "--http-host",
                MCP_HTTP_HOST,
                "--http-port",
                str(MCP_HTTP_PORT),
            ],
            env=env,
            start_new_session=True,
        )
        log.info("cloakbrowser-mcp started (pid=%d)", mcp.pid)

        def _shutdown(signum: int, _frame) -> None:
            log.info("received signal %d; terminating cloakbrowser-mcp tree", signum)
            with suppress(ProcessLookupError):
                os.killpg(mcp.pid, signum)

        for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
            signal.signal(sig, _shutdown)

        rc = mcp.wait()
    finally:
        for proc in (mcp, vnc, wm, xvfb):
            if proc is not None:
                _terminate(proc, timeout=3.0)

    log.info("cloakbrowser-mcp exited rc=%d", rc)
    sys.exit(rc)


if __name__ == "__main__":
    main()
