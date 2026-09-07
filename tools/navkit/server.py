#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""NavKit 通用后端（模块开发模式统一计划 · P3）。

用 core（session/categories/reader/renderer）重建原 NavKit 控制台/server.py，
并抽象为「通用 server + 模块 adapter」：前端 API 契约与旧版**完全兼容**，因此旧前端
三件套可整套迁移零改动；OCR/彩蛋等领域专属能力由「模块 adapter」注册，供未来新模块
复用同一 server。

启动：
    cd tools/navkit
    python server.py --module treasure        # 默认
浏览器打开 http://localhost:8765

架构约定（对齐方案 §十）：
  - generic studio 不理解 treasure OCR/出价内容。领域端点（OCR/彩蛋识别）由 adapter
    注册 handler；server 只做 session/categories/模板匹配/ROI 读写 的通用路由。
  - 所有图片/模板 API 只接受白名单相对名（SessionBrowser 严格防目录穿越）。
  - ROI 保存走 CategoryDefs.save_atomic（临时文件 + os.replace + 校验通过才落盘）。
"""
from __future__ import annotations

import importlib
import json
import re
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import cv2

# 允许 `python tools/navkit/server.py` 独立运行：把项目根加入 sys.path，
# 使 `tools` / `maaracing_assistant` 两个包都可导入（cwd 非项目根时同样生效）。
_PROJ_ROOT = Path(__file__).resolve().parent.parent.parent
_PROJ_ROOT_STR = str(_PROJ_ROOT)
if _PROJ_ROOT_STR not in sys.path:
    sys.path.insert(0, _PROJ_ROOT_STR)

from tools.navkit.core import session as sessmod
from tools.navkit.core.categories import CategoryDefs
from tools.navkit.core.reader import TemplateStore, match_local
from tools.navkit.core.renderer import bgr_to_dataurl, gray_to_dataurl
from maaracing_assistant.core.navkit import Assets, compile_routes_json, validate_assets
from tools.navkit.graph_api import graph_document
from maaracing_assistant.core.paths import debug_dir

# ---------------------------------------------------------------------------
# adapter 选择
# ---------------------------------------------------------------------------


def _load_adapter(module: str):
    """按模块名从 `tools/navkit/adapters/` 自动发现并加载 adapter 模块。

    新模块接入只需在 adapters/ 放一个 `<module>.py`（契约见 treasure.py），
    server 核心零改动；module 必须是合法标识符，防把路径/表达式当模块名注入。
    """
    if not re.fullmatch(r"[A-Za-z]\w*", module):
        raise ValueError(f"非法模块名: {module!r}")
    adapters_dir = Path(__file__).resolve().parent / "adapters"
    available = sorted(p.stem for p in adapters_dir.glob("*.py") if not p.stem.startswith("_"))
    if module not in available:
        raise ValueError(
            f"暂不支持模块 adapter: {module!r}（当前可用: {', '.join(available) or '无'}）"
        )
    return importlib.import_module(f"tools.navkit.adapters.{module}")


# ---------------------------------------------------------------------------
# 通用 server 状态（由 adapter 装配）
# ---------------------------------------------------------------------------
class StudioState:
    """server 运行期共享状态：adapter 注入分类/模板/路径/领域端点。"""

    def __init__(self, adapter):
        self.adapter = adapter
        self.module_name: str = adapter.__name__.split(".")[-1]
        self.defs: CategoryDefs = adapter.make_category_defs()
        self.session_browser = adapter.make_session_browser()
        self.tpl_dir: Path = adapter.template_dir()
        self.rois_file: Path = adapter.rois_path()
        self.static_dir = Path(__file__).resolve().parent / "static"
        self.template_store = TemplateStore(self.tpl_dir)
        # 领域端点注册表：{path: handler(self, handler, body, qs)}；由 adapter 填充。
        self.extra_handlers: dict[str, dict] = {}


def assets_path_for(module: str) -> Path:
    """资产真源路径：plugins/<module>/resources/config/<module>_assets.json。

    目录与文件名都按模块名派生，避免接入新模块时写出错位文件名。
    """
    return _PROJ_ROOT / "maaracing_assistant" / "plugins" / module / "resources" / "config" / f"{module}_assets.json"


def build_state(module: str) -> StudioState:
    adapter = _load_adapter(module)
    state = StudioState(adapter)
    register = getattr(adapter, "register_endpoints", None)
    if callable(register):
        register(state)
    else:
        state.defs.fill_defaults(ensure_rois(state))
    return state


def ensure_rois(state: StudioState) -> dict:
    """确保 ROI 文件存在且含缺省分类；不存在则建缺省并保存。

    已存在且无需补填时不重写落盘：落点是运行时真源（git 跟踪），
    启动不应产生写副作用。
    """
    defs = state.defs
    if state.rois_file.is_file():
        data = defs.load(state.rois_file)
        before = json.dumps(data, ensure_ascii=False)
        defs.fill_defaults(data)
        if json.dumps(data, ensure_ascii=False) != before:
            defs.save_atomic(data, state.rois_file)
        return data
    data = {**{"_schema_ver": 2, "reference_size": [1280, 720]},
            **{c: {} for c in defs.categories}}
    defs.fill_defaults(data)
    defs.save_atomic(data, state.rois_file)
    return data


def load_rois(state: StudioState) -> dict:
    if not state.rois_file.is_file():
        return ensure_rois(state)
    return state.defs.load(state.rois_file)


# ---------------------------------------------------------------------------
# HTTP handler（通用路由 + 领域端点转发）
# ---------------------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    state: StudioState = None  # 由 main() 注入

    def log_message(self, format, *args):  # noqa: A002
        pass

    # ---- 静态 ----
    def _serve_static(self, rel: str) -> None:
        if rel in ("", "/"):
            rel = "index.html"
        base = self.state.static_dir.resolve()
        file = (base / rel).resolve()
        if not file.is_relative_to(base) or not file.is_file():
            self.send_error(404)
            return
        ctype = {
            ".html": "text/html; charset=utf-8",
            ".js": "application/javascript; charset=utf-8",
            ".css": "text/css; charset=utf-8",
        }.get(file.suffix, "application/octet-stream")
        body = file.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
        self.send_header("Pragma", "no-cache")
        self.send_header("Expires", "0")
        self.end_headers()
        self.wfile.write(body)

    # ---- 响应工具 ----
    def _send_json(self, obj, status=200) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self) -> dict:
        length = int(self.headers.get("Content-Length", 0))
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        try:
            return json.loads(raw.decode("utf-8"))
        except Exception:
            return {}

    # ---- 模板状态 ----
    def _template_status(self) -> dict:
        listed = sessmod.list_templates(self.state.tpl_dir)
        data = load_rois(self.state)
        referenced: dict[str, dict[str, list[str]]] = {}
        flat: set[str] = set()
        tpl_re = sessmod.TPL_RE
        for cat in self.state.defs.categories:
            refs: dict[str, list[str]] = {}
            for key, val in (data.get(cat) or {}).items():
                if isinstance(val, dict):
                    tpls = [t for t in val.get("templates", []) if isinstance(t, str) and tpl_re.match(t)]
                    refs[key] = tpls
                    flat.update(tpls)
            referenced[cat] = refs
        listed_set = set(listed)
        return {
            "listed": listed,
            "referenced": referenced,
            "unassigned": sorted(listed_set - flat),
            "dangling": sorted(flat - listed_set),
        }

    # ---- GET ----
    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        qs = parse_qs(parsed.query)
        if path.startswith("/api/"):
            if path == "/api/list_sessions":
                self._send_json(self.state.session_browser.list_sessions())
            elif path == "/api/list_images":
                session = (qs.get("session") or [None])[0]
                if session is None:
                    self._send_json({"error": "missing session"}, 400)
                    return
                self._send_json(self.state.session_browser.list_raw(session))
            elif path == "/api/list_templates":
                self._send_json(sessmod.list_templates(self.state.tpl_dir))
            elif path == "/api/template_status":
                self._send_json(self._template_status())
            elif path == "/api/image":
                session = (qs.get("session") or [None])[0]
                name = (qs.get("name") or [None])[0]
                if session is None or name is None:
                    self._send_json({"error": "missing session/name"}, 400)
                    return
                p = self.state.session_browser.resolve_raw(session, name)
                if p is None:
                    self.send_error(404)
                    return
                body = p.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "image/png")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            elif path == "/api/template":
                name = (qs.get("name") or [None])[0]
                if name is None or not sessmod.TPL_RE.match(name):
                    self.send_error(404)
                    return
                p = self.state.tpl_dir / name
                if not p.is_file():
                    self.send_error(404)
                    return
                body = p.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "image/png")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            elif path == "/api/rois":
                self._send_json(load_rois(self.state))
            elif path == "/api/assets":
                self._handle_assets_get(qs)
            elif path == "/api/graph":
                self._handle_graph_get(qs)
            elif path == "/api/trace":
                self._handle_trace_get(qs)
            else:
                # 领域端点（adapter 注册）
                extra = self.state.extra_handlers.get("GET", {})
                if path in extra:
                    extra[path](self, qs)
                    return
                self.send_error(404)
            return
        self._serve_static(path.lstrip("/"))

    # ---- POST ----
    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/")
        body = self._read_body()

        if path == "/api/assets":
            self._handle_assets_post(body)
            return
        if path == "/api/compile":
            self._handle_compile_post(body)
            return

        # ROI 原子保存
        if path == "/api/rois":
            try:
                self.state.defs.save_atomic(body, self.state.rois_file)
                self._send_json({"ok": True, "path": str(self.state.rois_file)})
            except ValueError as e:
                self._send_json({"ok": False, "error": str(e)}, 400)
            except Exception as e:  # noqa: BLE001
                self._send_json({"ok": False, "error": f"保存失败: {e}"}, 500)
            return

        # 模板上传（任意图片格式 → PNG 存盘）
        if path == "/api/template_upload":
            self._handle_template_upload(body)
            return

        # 裁剪当前区域作模板
        if path == "/api/crop_to_template":
            self._handle_crop_to_template(body)
            return

        # 匹配分数
        if path == "/api/match_score":
            self._handle_match_score(body)
            return

        # 跨帧测试
        if path == "/api/cross_frame_test":
            self._handle_cross_frame_test(body)
            return

        # 领域端点（adapter 注册：如 treasure 的 ocr_recognize / eggs_recognize）
        extra_post = self.state.extra_handlers.get("POST", {})
        if path in extra_post:
            extra_post[path](self, body)
            return

        self._send_json({"error": f"unknown POST endpoint: {path}"}, 404)

    # ---- NavKit v3 API ----
    def _assets_path(self) -> Path:
        return assets_path_for(self.state.module_name)

    def _load_assets_doc(self) -> dict:
        path = self._assets_path()
        return json.loads(path.read_text(encoding="utf-8"))

    def _handle_assets_get(self, qs) -> None:
        try:
            doc = self._load_assets_doc()
            assets = Assets.from_document(doc, module=self.state.module_name)
            report = validate_assets(assets)
            self._send_json({"document": doc, "report": {"ok": report.ok, "errors": [str(i) for i in report.errors], "warnings": [str(i) for i in report.warnings]}})
        except Exception as exc:
            self._send_json({"error": str(exc)}, 500)

    def _handle_graph_get(self, qs) -> None:
        try:
            self._send_json(graph_document(self._load_assets_doc()))
        except Exception as exc:
            self._send_json({"error": str(exc)}, 500)

    def _handle_trace_get(self, qs) -> None:
        """读决策流水：`?session=<名>` 返回该会话 trace.jsonl 行；缺省聚合全部会话。

        会话根按当前模块派生（debug_dir()/module，与 adapter.session_dir 同源），
        session 名先过白名单正则，杜绝路径注入。
        """
        root = debug_dir() / self.state.module_name
        session = (qs.get("session") or [None])[0]
        if session is None:
            files = sorted(root.glob("*/trace.jsonl")) if root.is_dir() else []
        else:
            if not sessmod.SESSION_RE.match(session):
                self._send_json({"error": "非法 session 名"}, 400)
                return
            files = [root / session / "trace.jsonl"]
        rows = []
        for path in files:
            try:
                rows.extend(json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip())
            except (OSError, ValueError):
                continue
        self._send_json(rows)

    def _handle_assets_post(self, body: dict) -> None:
        document = body.get("document")
        if not isinstance(document, dict):
            self._send_json({"ok": False, "error": "document 必须为 object"}, 400)
            return
        try:
            assets = Assets.from_document(document, module=self.state.module_name)
            report = validate_assets(assets)
            if not report.ok:
                self._send_json({"ok": False, "report": report.text()}, 400)
                return
            path = self._assets_path()
            tmp = path.with_suffix(path.suffix + ".tmp")
            tmp.write_text(json.dumps(document, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(path)
            self._send_json({"ok": True, "report": report.text()})
        except Exception as exc:
            self._send_json({"ok": False, "error": str(exc)}, 400)

    def _handle_compile_post(self, body: dict) -> None:
        try:
            doc = self._load_assets_doc()
            assets = Assets.from_document(doc, module=self.state.module_name)
            output = compile_routes_json(assets)
            self._send_json({"ok": True, "bytes": len(output.encode("utf-8")), "output": output})
        except Exception as exc:
            self._send_json({"ok": False, "error": str(exc)}, 400)

    # ---- 通用后端实现（不依赖 adapter 领域） ----
    def _handle_template_upload(self, body: dict) -> None:
        import base64
        name = body.get("name", "")
        data_url = body.get("dataUrl", "")
        if not sessmod.TPL_RE.match(name) or not name.lower().endswith(".png"):
            self._send_json({"ok": False, "error": "模板名非法，仅允许 xxx.png"}, 400)
            return
        if not data_url.startswith("data:image/"):
            self._send_json({"ok": False, "error": "仅支持图片格式（PNG/JPG/WebP/BMP 等）"}, 400)
            return
        b64 = data_url.split(",", 1)[1]
        raw_bytes = base64.b64decode(b64)
        if len(raw_bytes) > 5 * 1024 * 1024:
            self._send_json({"ok": False, "error": "图片超过 5MB"}, 400)
            return
        arr = np_frombuffer(raw_bytes)
        import numpy as np
        img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
        if img is None:
            self._send_json({"ok": False, "error": "无法解码为图片"}, 400)
            return
        ok, png_buf = cv2.imencode(".png", img)
        if not ok:
            self._send_json({"ok": False, "error": "图片编码失败"}, 500)
            return
        try:
            self.state.tpl_dir.mkdir(parents=True, exist_ok=True)
            (self.state.tpl_dir / name).write_bytes(png_buf.tobytes())
        except Exception as e:  # noqa: BLE001
            self._send_json({"ok": False, "error": f"写入失败: {e}"}, 500)
            return
        self.state.template_store.invalidate(name)
        self._send_json({"ok": True, "name": name, "size": [img.shape[1], img.shape[0]]})

    def _read_bgr(self, body: dict):
        """从请求读截图（session+image），返回 BGR ndarray 或 None。"""
        session = body.get("session")
        image_name = body.get("image") or body.get("name")
        if not (session and image_name):
            return None
        p = self.state.session_browser.resolve_raw(session, image_name)
        if p is None:
            return None
        img = cv2.imread(str(p))
        return img if img is not None else None

    def _handle_crop_to_template(self, body: dict) -> None:
        target_tpl = body.get("target")
        rect = body.get("rect")
        if not target_tpl or not rect:
            self._send_json({"error": "缺 target/rect"}, 400)
            return
        if not sessmod.TPL_RE.match(target_tpl) or not target_tpl.lower().endswith(".png"):
            self._send_json({"ok": False, "error": "目标模板名非法"}, 400)
            return
        img = self._read_bgr(body)
        if img is None:
            self._send_json({"error": "截图不存在"}, 404)
            return
        H, W = img.shape[:2]
        x1n, y1n, x2n, y2n = (float(v) for v in rect)
        x1, y1 = max(0, int(x1n * W)), max(0, int(y1n * H))
        x2, y2 = min(W, int(x2n * W)), min(H, int(y2n * H))
        if x2 <= x1 or y2 <= y1:
            self._send_json({"ok": False, "error": "ROI 为空"}, 400)
            return
        crop = img[y1:y2, x1:x2]
        ok, buf = cv2.imencode(".png", crop)
        if not ok:
            self._send_json({"ok": False, "error": "PNG 编码失败"}, 500)
            return
        try:
            self.state.tpl_dir.mkdir(parents=True, exist_ok=True)
            (self.state.tpl_dir / target_tpl).write_bytes(buf.tobytes())
        except Exception as e:  # noqa: BLE001
            self._send_json({"ok": False, "error": f"写入失败: {e}"}, 500)
            return
        self.state.template_store.invalidate(target_tpl)
        self._send_json({
            "ok": True, "name": target_tpl,
            "size": [crop.shape[1], crop.shape[0]],
            "rect_px": [x1, y1, x2, y2],
        })

    def _handle_match_score(self, body: dict) -> None:
        rect = body.get("rect")
        tpl = body.get("template")
        if not (tpl and rect):
            self._send_json({"error": "缺 rect/template"}, 400)
            return
        img = self._read_bgr(body)
        g = self.state.template_store.load_gray(tpl)
        if img is None or g is None:
            self._send_json({"error": "图或模板不存在"}, 404)
            return
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        res = match_local(gray, g, rect)
        res["crop_preview"] = ""
        res["tpl_preview"] = ""
        if res.get("size_ok"):
            x1, y1, x2, y2 = res["pixel_box"]
            res["crop_preview"] = gray_to_dataurl(gray[y1:y2, x1:x2])
        res["tpl_preview"] = gray_to_dataurl(g)
        self._send_json(res)

    def _handle_cross_frame_test(self, body: dict) -> None:
        rect = body.get("rect")
        tpl = body.get("template")
        threshold_raw = body.get("threshold")
        threshold = 0.75
        if isinstance(threshold_raw, (int, float)) and 0.0 <= threshold_raw <= 1.0:
            threshold = float(threshold_raw)
        if not (rect and tpl):
            self._send_json({"error": "缺 rect/template"}, 400)
            return
        g = self.state.template_store.load_gray(tpl)
        if g is None:
            self._send_json({"error": "模板不存在"}, 404)
            return
        session = body.get("session")
        scores = []
        for name in self.state.session_browser.list_raw(session):
            p = self.state.session_browser.resolve_raw(session, name)
            if p is None:
                continue
            img = cv2.imread(str(p))
            if img is None:
                continue
            gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
            s = match_local(gray, g, rect).get("score", -1.0)
            scores.append((name, s))
        if not scores:
            self._send_json({"error": "无可用帧"}, 404)
            return
        vals = [s for _, s in scores if s >= 0]
        resp = {"total_frames": len(scores), "histogram": [], "threshold": threshold}
        if vals:
            v = sorted(vals)
            n = len(v)
            resp["max"] = round(v[-1], 3)
            resp["p95"] = round(v[min(n - 1, int(n * 0.95))], 3)
            resp["median"] = round(v[n // 2], 3)
            resp["frames_ge_060"] = sum(1 for x in vals if x >= 0.60)
            resp["frames_ge_070"] = sum(1 for x in vals if x >= 0.70)
            resp["frames_ge_080"] = sum(1 for x in vals if x >= 0.80)
            resp["frames_ge_threshold"] = sum(1 for x in vals if x >= threshold)
            bins = [[round(i * 0.1, 1), round((i + 1) * 0.1, 1), 0] for i in range(10)]
            for x in vals:
                bins[min(9, int(x * 10))][2] += 1
            resp["histogram"] = bins
            resp["best_frames"] = [
                {"name": n_, "score": round(s, 3)}
                for n_, s in sorted([x for x in scores if x[1] >= 0],
                                   key=lambda x: x[1], reverse=True)[:10]
            ]
        self._send_json(resp)


def np_frombuffer(raw: bytes):
    import numpy as np
    return np.frombuffer(raw, dtype=np.uint8)


def main() -> None:
    import argparse
    parser = argparse.ArgumentParser(description="NavKit")
    parser.add_argument("--module", default="treasure",
                        help="模块 adapter 名（目前仅 treasure 已适配，其余模块待接入）")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    state = build_state(args.module)
    Handler.state = state
    ensure_rois(state)
    print(f"NavKit[{args.module}] 已启动: http://localhost:{args.port}")
    print(f"ROI 配置文件    : {state.rois_file}")
    print(f"截图根目录      : {state.session_browser.debug_root}")
    print(f"模板目录        : {state.tpl_dir}")
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")
        server.server_close()


if __name__ == "__main__":
    main()