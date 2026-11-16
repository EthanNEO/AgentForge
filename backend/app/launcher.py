"""Persistent, idempotent Windows launcher for the local AgentForge service."""
from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path
from typing import Callable, Mapping


PROJECT_ROOT = Path(__file__).resolve().parents[2]
BACKEND_ROOT = PROJECT_ROOT / "backend"
MODEL_ENV_NAMES = (
    "AF_DEFAULT_PROVIDER",
    "MDATAPLUS_API_KEY",
    "AF_DEEPSEEK_API_KEY",
    "AF_DEEPSEEK_BASE_URL",
    "AF_DEEPSEEK_MODEL",
    "AF_MIMO_API_KEY",
    "AF_MIMO_BASE_URL",
    "AF_MIMO_MODEL",
)


def _read_user_environment(name: str) -> str | None:
    if os.name != "nt":
        return None
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
            value, _ = winreg.QueryValueEx(key, name)
            return str(value)
    except (FileNotFoundError, OSError):
        return None


def build_child_environment(
    base: Mapping[str, str] | None = None,
    user_reader: Callable[[str], str | None] | None = None,
) -> dict[str, str]:
    """Merge fresh user-scoped model settings into the child environment."""
    environment = dict(os.environ if base is None else base)
    reader = user_reader or _read_user_environment
    for name in MODEL_ENV_NAMES:
        value = reader(name)
        if value and value.strip():
            environment[name] = value.strip()
    environment["PYTHONUNBUFFERED"] = "1"
    environment["PYTHONUTF8"] = "1"
    return environment


def _healthy(port: int, timeout: float = 0.8) -> bool:
    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{port}/api/health", timeout=timeout
        ) as response:
            payload = json.loads(response.read().decode("utf-8"))
            return response.status == 200 and payload.get("ok") is True
    except (OSError, ValueError, urllib.error.URLError):
        return False


def _port_in_use(port: int) -> bool:
    with socket.socket() as probe:
        probe.settimeout(0.3)
        return probe.connect_ex(("127.0.0.1", port)) == 0


def _start_detached(port: int, environment: Mapping[str, str], log_dir: Path):
    log_dir.mkdir(parents=True, exist_ok=True)
    stdout_path = log_dir / "server.stdout.log"
    stderr_path = log_dir / "server.stderr.log"
    stdout = stdout_path.open("ab")
    stderr = stderr_path.open("ab")
    kwargs = {
        "cwd": str(BACKEND_ROOT),
        "env": dict(environment),
        "stdin": subprocess.DEVNULL,
        "stdout": stdout,
        "stderr": stderr,
        "close_fds": True,
    }
    if os.name == "nt":
        kwargs["creationflags"] = (
            subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
        )
    else:
        kwargs["start_new_session"] = True
    try:
        return subprocess.Popen(
            [sys.executable, "-u", "-m", "app.server", str(port)],
            **kwargs,
        )
    finally:
        stdout.close()
        stderr.close()


def _wait_for_health(port: int, process, timeout: float = 15.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _healthy(port):
            return True
        if process.poll() is not None:
            return False
        time.sleep(0.2)
    return False


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="启动 AgentForge 本地服务")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--pid-file", type=Path)
    parser.add_argument("--log-dir", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    runtime_dir = args.log_dir or PROJECT_ROOT / "output" / "runtime"
    pid_file = args.pid_file or runtime_dir / "agentforge.pid"
    workbench = f"http://127.0.0.1:{args.port}/workbench"

    if _healthy(args.port):
        print(f"AgentForge 已在运行：{workbench}")
        if not args.no_browser:
            webbrowser.open(workbench)
        return 0
    if _port_in_use(args.port):
        print(f"端口 {args.port} 已被其他程序占用，AgentForge 未启动。", file=sys.stderr)
        return 2

    process = _start_detached(args.port, build_child_environment(), runtime_dir)
    pid_file.parent.mkdir(parents=True, exist_ok=True)
    pid_file.write_text(str(process.pid), encoding="utf-8")
    if not _wait_for_health(args.port, process):
        print(
            f"AgentForge 启动失败，请查看日志：{runtime_dir / 'server.stderr.log'}",
            file=sys.stderr,
        )
        return 1

    print(f"AgentForge 已在后台运行（PID {process.pid}）：{workbench}")
    if not args.no_browser:
        webbrowser.open(workbench)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
