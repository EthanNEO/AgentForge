from __future__ import annotations

import os
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.request
from pathlib import Path

from app.launcher import build_child_environment


BACKEND = Path(__file__).resolve().parents[1]


class LauncherTests(unittest.TestCase):
    def test_user_model_settings_override_stale_parent_environment(self):
        environment = build_child_environment(
            {"PATH": "C:\\Python", "AF_DEEPSEEK_API_KEY": "old"},
            lambda name: {
                "AF_DEEPSEEK_API_KEY": "new-secret",
                "AF_MIMO_API_KEY": "mimo-secret",
            }.get(name),
        )

        self.assertEqual(environment["PATH"], "C:\\Python")
        self.assertEqual(environment["AF_DEEPSEEK_API_KEY"], "new-secret")
        self.assertEqual(environment["AF_MIMO_API_KEY"], "mimo-secret")

    @unittest.skipUnless(os.name == "nt", "detached launcher behavior is Windows-specific")
    def test_launcher_survives_parent_exit_and_is_idempotent(self):
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]

        with tempfile.TemporaryDirectory() as temp_dir:
            runtime = Path(temp_dir)
            pid_file = runtime / "agentforge.pid"
            command = [
                sys.executable,
                "-m",
                "app.launcher",
                "--port",
                str(port),
                "--no-browser",
                "--pid-file",
                str(pid_file),
                "--log-dir",
                str(runtime),
            ]

            first = subprocess.run(
                command,
                cwd=BACKEND,
                capture_output=True,
                text=True,
                timeout=20,
                check=False,
            )
            self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
            pid = int(pid_file.read_text(encoding="utf-8").strip())
            try:
                self.assertTrue(self._wait_for_health(port), first.stdout + first.stderr)

                second = subprocess.run(
                    command,
                    cwd=BACKEND,
                    capture_output=True,
                    text=True,
                    timeout=20,
                    check=False,
                )
                self.assertEqual(second.returncode, 0, second.stdout + second.stderr)
                self.assertEqual(int(pid_file.read_text(encoding="utf-8").strip()), pid)
            finally:
                self._terminate_process(pid)

    @staticmethod
    def _wait_for_health(port: int) -> bool:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            try:
                with urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/api/health", timeout=0.5
                ) as response:
                    return response.status == 200
            except OSError:
                time.sleep(0.1)
        return False

    @staticmethod
    def _terminate_process(pid: int) -> None:
        subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            capture_output=True,
            check=False,
        )
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            try:
                os.kill(pid, 0)
            except OSError:
                return
            time.sleep(0.05)


if __name__ == "__main__":
    unittest.main()
