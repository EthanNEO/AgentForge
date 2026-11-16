"""零依赖 HTTP 服务：标准库 ThreadingHTTPServer。

启动：python -m app.server  （在 backend 目录下）
"""
from __future__ import annotations

import json
import re
import sys
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import config
from .browser_check import PREVIEW_CSP
from .router import ROUTES, artifact_path, bootstrap, input_path

FRONTEND = Path(__file__).resolve().parents[2] / "frontend"
MIME = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".pdf": "application/pdf",
    ".svg": "image/svg+xml",
    ".woff2": "font/woff2",
    ".json": "application/json; charset=utf-8",
    ".txt": "text/plain; charset=utf-8",
    ".py": "text/plain; charset=utf-8",
}

_COMPILED = [(m, re.compile(p), fn) for m, p, fn in ROUTES]


class Handler(BaseHTTPRequestHandler):
    server_version = "AgentForge/1.0"

    def log_message(self, fmt, *args):  # 静默访问日志，避免刷屏
        return

    # ---------- 基础响应 ----------
    def _send(self, status: int, body: bytes, ctype: str = "application/json; charset=utf-8", headers=None):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", "*")
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        try:
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            self.close_connection = True

    def _json(self, payload, status: int = 200):
        self._send(status, json.dumps(payload, ensure_ascii=False).encode("utf-8"))

    def _error(self, status: int, message: str):
        self._json({"error": message}, status)

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length > config.MAX_HTTP_JSON_BYTES:
            raise ValueError("请求体超过 35 MiB 限制")
        if not length:
            return {}
        raw = self.rfile.read(length)
        try:
            return json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return {}

    # ---------- 分发 ----------
    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET,POST,PUT,DELETE,OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        query = urllib.parse.parse_qs(parsed.query)
        if path in ("/", "/index.html"):
            return self._file(FRONTEND / "index.html")
        if path in ("/workbench", "/workbench.html"):
            return self._file(FRONTEND / "workbench.html")
        if path == "/favicon.ico":
            self.send_response(204)
            self.end_headers()
            return
        if path.startswith("/static/"):
            return self._file(FRONTEND / path[len("/static/"):])

        match = self._match("GET", path)
        if match:
            fn, params = match
            try:
                return self._json(fn(params, query))
            except KeyError:
                return self._error(404, "not found")
            except Exception as exc:  # noqa: BLE001
                return self._error(500, str(exc))

        art = re.match(r"^/api/runs/([\w\-]+)/(?:file|artifacts)/(.+)$", path)
        if art:
            try:
                return self._file(artifact_path(art.group(1), urllib.parse.unquote(art.group(2))), isolated=True)
            except KeyError:
                return self._error(404, "artifact not found")

        attachment = re.match(r"^/api/runs/([\w\-]+)/inputs/(.+)$", path)
        if attachment:
            try:
                return self._file(input_path(
                    attachment.group(1), urllib.parse.unquote(attachment.group(2))
                ))
            except KeyError:
                return self._error(404, "input not found")

        self._error(404, "not found")

    def do_POST(self):
        self._mutate("POST")

    def do_PUT(self):
        self._mutate("PUT")

    def do_DELETE(self):
        self._mutate("DELETE")

    def _mutate(self, method: str):
        path = urllib.parse.urlparse(self.path).path
        match = self._match(method, path)
        if not match:
            return self._error(404, "not found")
        fn, params = match
        try:
            return self._json(fn(params, self._body()))
        except ValueError as exc:
            return self._error(400, str(exc))
        except KeyError:
            return self._error(404, "not found")
        except Exception as exc:  # noqa: BLE001
            return self._error(500, str(exc))

    def _match(self, method: str, path: str):
        for m, pattern, fn in _COMPILED:
            if m != method:
                continue
            hit = pattern.match(path)
            if hit:
                return fn, hit.groupdict()
        return None

    def _file(self, path: Path, isolated=False):
        if not path.exists() or not path.is_file():
            return self._error(404, "file not found")
        ctype = MIME.get(path.suffix.lower(), "application/octet-stream")
        headers = {}
        if isolated:
            headers["X-Content-Type-Options"] = "nosniff"
            if path.suffix.lower() == ".html":
                headers["Content-Security-Policy"] = PREVIEW_CSP
            elif path.suffix.lower() in {".js", ".css", ".svg"}:
                ctype = "text/plain; charset=utf-8"
        return self._send(200, path.read_bytes(), ctype, headers)


def main():
    bootstrap()
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8000
    httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"AgentForge 已启动：http://127.0.0.1:{port}")
    print(f"运行模式：{'离线回放（无需 API Key）' if config.mode_label() == 'mock' else '在线推理'}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()
