"""Minimal smoke tests for the launcher. The bulk of launcher's job
(Xvfb, x11vnc, cloakbrowser-mcp subprocess) only runs inside a
container with X11, so these tests cover the pure parts only:
env parsing, vnc argv construction, terminate escalation.

Stdlib-only (unittest) — no test framework dependency."""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import launcher  # noqa: E402


class IntEnvTests(unittest.TestCase):
    def test_returns_default_when_unset(self) -> None:
        old = os.environ.pop("DISPLAY_WIDTH", None)
        try:
            self.assertEqual(launcher._int_env("DISPLAY_WIDTH", 1280), 1280)
        finally:
            if old is not None:
                os.environ["DISPLAY_WIDTH"] = old

    def test_parses_valid(self) -> None:
        old = os.environ.get("DISPLAY_WIDTH")
        os.environ["DISPLAY_WIDTH"] = "1920"
        try:
            self.assertEqual(launcher._int_env("DISPLAY_WIDTH", 1280), 1920)
        finally:
            if old is None:
                os.environ.pop("DISPLAY_WIDTH", None)
            else:
                os.environ["DISPLAY_WIDTH"] = old

    def test_falls_back_on_garbage(self) -> None:
        old = os.environ.get("DISPLAY_HEIGHT")
        os.environ["DISPLAY_HEIGHT"] = "not-a-number"
        try:
            self.assertEqual(launcher._int_env("DISPLAY_HEIGHT", 800), 800)
        finally:
            if old is None:
                os.environ.pop("DISPLAY_HEIGHT", None)
            else:
                os.environ["DISPLAY_HEIGHT"] = old


class VncArgsTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = Path("/tmp/vncpasswd")
        self._tmp.unlink(missing_ok=True)

    def tearDown(self) -> None:
        self._tmp.unlink(missing_ok=True)

    def test_no_password_uses_nopw(self) -> None:
        args = launcher._build_vnc_args(1280, 800, "")
        self.assertEqual(args[0], "x11vnc")
        self.assertIn("-display", args)
        self.assertIn(launcher.DISPLAY, args)
        self.assertIn("-rfbport", args)
        self.assertIn(str(launcher.VNC_PORT), args)
        self.assertIn("-nopw", args)
        self.assertNotIn("-passwdfile", args)

    def test_password_writes_file_with_0600(self) -> None:
        args = launcher._build_vnc_args(1024, 768, "secret123")
        self.assertIn("-passwdfile", args)
        self.assertNotIn("-nopw", args)
        passwd_path = Path(args[args.index("-passwdfile") + 1])
        self.assertTrue(passwd_path.is_file())
        self.assertEqual(passwd_path.read_text(), "secret123\n")
        self.assertEqual(oct(passwd_path.stat().st_mode & 0o777), "0o600")

    def test_geometry_reflects_size(self) -> None:
        args = launcher._build_vnc_args(640, 480, "")
        idx = args.index("-geometry") + 1
        self.assertEqual(args[idx], "640x480")


class TerminateTests(unittest.TestCase):
    def test_escalates_to_kill(self) -> None:
        proc = subprocess.Popen(
            [
                sys.executable,
                "-c",
                "import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(30)",
            ]
        )
        start = time.monotonic()
        launcher._terminate(proc, timeout=1.0)
        elapsed = time.monotonic() - start
        self.assertIsNotNone(proc.poll(), "process still alive after terminate")
        self.assertLess(elapsed, 5.0, "escalation to SIGKILL took too long")

    def test_no_op_on_dead_process(self) -> None:
        proc = subprocess.Popen([sys.executable, "-c", "pass"])
        proc.wait()
        launcher._terminate(proc, timeout=0.5)


class HaveTests(unittest.TestCase):
    def test_finds_real_binary(self) -> None:
        self.assertTrue(launcher._have(sys.executable.split("/")[-1]))

    def test_rejects_unknown(self) -> None:
        self.assertFalse(launcher._have("definitely-not-a-real-binary-xyz"))


class WaitForTcpTests(unittest.TestCase):
    def test_returns_true_for_listening_port(self) -> None:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as srv:
            srv.bind(("127.0.0.1", 0))
            srv.listen(1)
            port = srv.getsockname()[1]
            self.assertTrue(launcher._wait_for_tcp("127.0.0.1", port, 2.0, "test"))

    def test_returns_false_on_timeout(self) -> None:
        self.assertFalse(launcher._wait_for_tcp("127.0.0.1", 1, 0.5, "test"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
