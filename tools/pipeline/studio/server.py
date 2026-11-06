#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Studio v5 —— 单端口两页：回放预览 + ROI 调整。

无策略表、无模板截取页：回放只读 v5 会话清单与帧索引；ROI 只写节点 JSON 的
`custom_recognition_param.rect`，保存前后跑同一校验器。

用法：
    .venv\\Scripts\\python.exe -m tools.pipeline.studio.server [--port 26530] [--idle-exit 15]
"""
from __future__ import annotations

import argparse
import json
import mimetypes
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from tools.pipeline.studio.nodes import NodeIndex
from tools.pipeline.studio.replay import ReplayIndex

STATIC_DIR = Path(__file__).resolve().parent / "static"


class StudioApp:
    def __init__(self, *, replay: ReplayIndex | None = None,
                 nodes: NodeIndex | None = None):
        self.replay = replay or ReplayIndex()
        self.nodes = nodes or NodeIndex()
        self.last_ping = time.monotonic()

    def ping(self) -> None:
        self.last_ping = time.monotonic()


def _json_bytes(payload) -> bytes:
    return json.dumps(payload, ensure_ascii=False).encode("utf-8")


def make_handler(app: StudioApp):
    class Handler(BaseHTTPRequestHandler):
        server_version = "MaarStudio/5"

        def log_message(self, fmt, *args):  # noqa: A003 —— 静默访问日志
            return

        def _send(self, status: int, body: bytes, ctype: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, status: int, payload) -> None:
            self._send(status, _json_bytes(payload), "application/json; charset=utf-8")

        def _query(self) -> dict[str, str]:
            q = parse_qs(urlparse(self.path).query)
            return {k: v[-1] for k, v in q.items()}

        def _static(self, rel: str) -> None:
            target = (STATIC_DIR / rel).resolve()
            if not target.is_relative_to(STATIC_DIR.resolve()) or not target.is_file():
                self._json(404, {"error": "not found"})
                return
            ctype = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
            self._send(200, target.read_bytes(), ctype)

        def do_GET(self):  # noqa: N802
            app.ping()
            path = urlparse(self.path).path
            q = self._query()
            try:
                if path in ("/", "/index.html"):
                    self._static("index.html")
                elif path.startswith("/static/"):
                    self._static(path.removeprefix("/static/"))
                elif path == "/api/ping":
                    self._json(200, {"ok": True})
                elif path == "/api/replay/sessions":
                    self._json(200, {"sessions": app.replay.sessions()})
                elif path == "/api/replay/behaviors":
                    self._json(200, {"behaviors": app.replay.behaviors(
                        q.get("session", ""))})
                elif path == "/api/replay/frames":
                    session, module, behavior = (q.get("session", ""),
                                                 q.get("module", ""),
                                                 q.get("behavior", ""))
                    self._json(200, {
                        "manifest": app.replay.manifest(session, module, behavior),
                        "frames": app.replay.frames(session, module, behavior),
                    })
                elif path == "/api/replay/timeline":
                    self._json(200, {"rows": app.replay.timeline(
                        q.get("session", ""))})
                elif path in ("/api/replay/frame", "/api/roi/frame"):
                    self._frame(q)
                elif path == "/api/roi/nodes":
                    self._json(200, {"nodes": app.nodes.nodes()})
                elif path == "/api/health":
                    self._json(200, {"ok": True, "sessions": len(app.replay.sessions())})
                else:
                    self._json(404, {"error": "not found"})
            except Exception as exc:  # noqa: BLE001 —— 单请求失败不拖垮服务
                self._json(500, {"error": repr(exc)})

        def _frame(self, q: dict) -> None:
            try:
                seq = int(q.get("seq", ""))
            except ValueError:
                self._json(400, {"error": "seq 必须是整数"})
                return
            target = app.replay.resolve_frame(
                q.get("session", ""), q.get("module", ""),
                q.get("behavior", ""), seq)
            if target is None:
                self._json(404, {"error": "帧不存在或越权"})
                return
            ctype = mimetypes.guess_type(target.name)[0] or "image/jpeg"
            self._send(200, target.read_bytes(), ctype)

        def do_POST(self):  # noqa: N802
            app.ping()
            path = urlparse(self.path).path
            if path != "/api/roi/save":
                self._json(404, {"error": "not found"})
                return
            try:
                length = int(self.headers.get("Content-Length") or 0)
                if length <= 0 or length > 1_000_000:
                    raise ValueError("请求体为空或过大")
                body = json.loads(self.rfile.read(length).decode("utf-8"))
                if not isinstance(body, dict):
                    raise ValueError("请求体必须是对象")
                result = app.nodes.save(
                    str(body.get("file") or ""), str(body.get("node") or ""),
                    body.get("rect"))
            except ValueError as exc:
                self._json(400, {"ok": False, "errors": [str(exc)]})
                return
            except Exception as exc:  # noqa: BLE001
                self._json(500, {"ok": False, "errors": [repr(exc)]})
                return
            self._json(200 if result.get("ok") else 400, result)

    return Handler


def make_server(host: str, port: int, app: StudioApp) -> ThreadingHTTPServer:
    return ThreadingHTTPServer((host, port), make_handler(app))


def _idle_watch(server: ThreadingHTTPServer, app: StudioApp,
                idle_s: float) -> None:
    while idle_s > 0:
        time.sleep(1.0)
        if time.monotonic() - app.last_ping > idle_s:
            server.shutdown()
            return


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Studio v5：回放 + ROI")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=26530)
    parser.add_argument("--idle-exit", type=float, default=15.0,
                        help="无页面心跳多少秒后退出；0 = 常驻（默认 15）")
    parser.add_argument("--debug-root", type=Path, default=None,
                        help="覆盖会话根（默认用户数据 debug/）")
    args = parser.parse_args(argv)

    replay = ReplayIndex(args.debug_root)
    app = StudioApp(replay=replay)
    server = make_server(args.host, args.port, app)
    if args.idle_exit > 0:
        threading.Thread(target=_idle_watch, args=(server, app, args.idle_exit),
                         daemon=True, name="studio-idle").start()
    print(f"[studio] http://{args.host}:{args.port}/")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
