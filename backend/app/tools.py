"""工具沙箱：所有工具调用都在运行目录内执行，带超时与输出截断。"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import time
from pathlib import Path

MAX_OUTPUT = 6000


def write_artifact(workdir: Path, name: str, content: str) -> Path:
    workdir.mkdir(parents=True, exist_ok=True)
    path = workdir / name
    path.write_text(content, encoding="utf-8")
    return path


def run_python_tests(workdir: Path, pattern: str = "test_*.py", timeout: int = 60) -> dict:
    """在沙箱目录内执行 unittest 发现，返回真实执行结果。"""
    files = sorted(p.name for p in Path(workdir).glob(pattern))
    if not files:
        return {"ok": False, "ran": 0, "files": [], "stdout": "",
                "stderr": "未发现测试文件", "returncode": -1, "duration": 0, "failures": []}

    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    started = None
    import time

    started = time.time()
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "unittest", "discover", "-s", ".", "-p", pattern, "-v"],
            cwd=str(workdir),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            env=env,
        )
        stdout, stderr, code = proc.stdout, proc.stderr, proc.returncode
    except subprocess.TimeoutExpired:
        stdout, stderr, code = "", f"测试执行超时（>{timeout}s）", 124

    duration = round(time.time() - started, 2)
    combined = f"{stdout}\n{stderr}"
    ran = _count_tests(stderr)
    ok = code == 0 and ran > 0
    failures = _extract_failures(combined)
    return {
        "ok": ok,
        "ran": ran,
        "returncode": code,
        "files": files,
        "failures": failures,
        "stdout": stdout[-MAX_OUTPUT:],
        "stderr": stderr[-MAX_OUTPUT:],
        "duration": duration,
    }


def _count_tests(text: str) -> int:
    m = re.search(r"Ran (\d+) tests?", text)
    return int(m.group(1)) if m else 0


def _extract_failures(text: str) -> list[str]:
    found = []
    for m in re.finditer(r"(FAIL|ERROR): (\S+) \(([^)]+)\)", text):
        found.append(f"{m.group(2)} @ {m.group(3)}")
    return found[:10]


def run_python_snippet(workdir: Path, code: str, timeout: int = 30) -> dict:
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    try:
        proc = subprocess.run(
            [sys.executable, "-c", code],
            cwd=str(workdir),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            env=env,
        )
        return {"ok": proc.returncode == 0, "stdout": proc.stdout[-MAX_OUTPUT:],
                "stderr": proc.stderr[-MAX_OUTPUT:], "returncode": proc.returncode}
    except subprocess.TimeoutExpired:
        return {"ok": False, "stdout": "", "stderr": "执行超时", "returncode": 124}


def run_script(workdir: Path, script: str, timeout: int = 60) -> dict:
    """在沙箱目录中执行指定脚本文件。"""
    path = Path(workdir) / script
    if not path.exists():
        return {"ok": False, "returncode": -1, "stdout": "", "stderr": f"脚本不存在：{script}"}
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    started = time.time()
    try:
        proc = subprocess.run(
            [sys.executable, script],
            cwd=str(workdir),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            env=env,
        )
        return {
            "ok": proc.returncode == 0,
            "returncode": proc.returncode,
            "stdout": proc.stdout[-MAX_OUTPUT:],
            "stderr": proc.stderr[-MAX_OUTPUT:],
            "duration": round(time.time() - started, 2),
        }
    except subprocess.TimeoutExpired:
        return {"ok": False, "returncode": 124, "stdout": "", "stderr": "执行超时",
                "duration": round(time.time() - started, 2)}
