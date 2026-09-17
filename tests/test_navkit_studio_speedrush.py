# -*- coding: utf-8 -*-
"""speedrush HUD 只读复核通路（Studio 第四面：真录制帧 + core OCR 读数）。

覆盖：
- HUD 区域投影形状与区域数——**区域数从真源文件读出比对，不写死数值**；
- treasure 三组 + `nodes`/`tuning` 的计数由 policy 真源现算比对（新组不得改变既有面；
  `tests/test_navkit_studio_server.py` 里那组写死的计数断言是同一条约束的另一半）；
- speedrush 帧白名单与目录穿越防护（`..` / 绝对路径 / 会话越界一律拒绝，`frames/` 外
  的同名帧不可达）；
- `SessionBrowser` 参数化后 treasure 缺省布局逐项不变（既有调用方零改动）；
- 只读边界：新组不在 `SPEC_GROUPS`/`EDITABLE_GROUPS` 内、保存管线对它是无操作（dry_run
  不产生 diff）、前端把它声明为只读且三处门（hitTest / onMove / 保存 body）都在。

不依赖本机录制数据：帧库布局用 tmp_path 造；HTTP 正向测试向 `HUD_BROWSER` 注入临时根；
OCR 引擎可用性**不作前提**（引擎缺失时只校验响应形状与「无金额字段」）。
"""
from __future__ import annotations

import json
import shutil
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import numpy as np
import pytest

from maaracing_master.core.image_io import write_rgb
from tools.navkit import studio_server as s
from tools.navkit import studio_sessions as ss

HUD_SESSION = "20260916_213104_p1"
HUD_FRAME = "000001.jpg"


def _truth_regions() -> dict:
    return json.loads(s.HUD_REGIONS_FILE.read_text(encoding="utf-8"))


def _hud_browser(root: Path) -> ss.SessionBrowser:
    """按 speedrush 参数构造浏览器（与服务端 `_HudBrowser` 注入的三份白名单同源）。"""
    return ss.SessionBrowser(
        root,
        session_re=ss.SPEEDRUSH_SESSION_RE,
        frame_re=ss.SPEEDRUSH_FRAME_RE,
        frame_dir=ss.SPEEDRUSH_FRAME_DIR,
        trace_name=ss.SPEEDRUSH_TRACE_NAME,
    )


@pytest.fixture
def demos(tmp_path):
    """造一份 speedrush 演示帧库（合法会话 + 三个越界诱饵）。"""
    root = tmp_path / "demos"
    frames = root / HUD_SESSION / "frames"
    frames.mkdir(parents=True)
    assert write_rgb(frames / HUD_FRAME, np.zeros((16, 16, 3), np.uint8), jpeg_quality=85)
    (root / HUD_SESSION / "frames.jsonl").write_text("{}\n", encoding="utf-8")
    # 诱饵 ①：库根有同名帧（不在 frames/ 内 —— 绝不允许被解析到）
    (root / HUD_FRAME).write_bytes((frames / HUD_FRAME).read_bytes())
    # 诱饵 ②：会话名形态不对但目录与帧都在（不在 session_re 内）
    (root / "evil_p1" / "frames").mkdir(parents=True)
    (root / "evil_p1" / "frames" / HUD_FRAME).write_bytes(b"x")
    # 诱饵 ③：会话根有合法帧名（在 sessions/ 下但不在 frames/ 内）
    (root / HUD_SESSION / "000002.jpg").write_bytes(b"x")
    return root


# ----------------------------------------------------------------------
# 投影：区域数从真源读出比对
# ----------------------------------------------------------------------


class TestHudProjection:
    def test_region_count_and_rects_match_truth_source(self):
        """区域名集合与 rect 逐一等于真源（真源增删区域，这里自动跟随，不写死数值）。"""
        truth = _truth_regions()
        proj = s.project_hud_regions()
        assert set(proj) == set(truth)
        assert len(proj) == len(truth)
        for name, rect in truth.items():
            assert proj[name]["rect"] == [float(x) for x in rect], name
            assert proj[name]["templates"] == []

    def test_group_visible_in_rois_projection(self):
        proj = s.project_rois()
        assert s.HUD_GROUP in proj
        assert proj[s.HUD_GROUP] == s.project_hud_regions()

    def test_missing_truth_file_projects_empty(self, tmp_path, monkeypatch):
        """真源缺失（如换机器没录过）→ 空组，不抛错、不影响其余投影。"""
        monkeypatch.setattr(s, "HUD_REGIONS_FILE", tmp_path / "nope.json")
        assert s.project_hud_regions() == {}

    def test_underscore_and_malformed_entries_skipped(self, tmp_path, monkeypatch):
        f = tmp_path / "hud_regions.json"
        f.write_text(json.dumps({"_note": "说明", "ok": [0, 0, 1, 1], "short": [1, 2, 3]}),
                     encoding="utf-8")
        monkeypatch.setattr(s, "HUD_REGIONS_FILE", f)
        assert set(s.project_hud_regions()) == {"ok"}


# ----------------------------------------------------------------------
# treasure 面不变
# ----------------------------------------------------------------------


class TestTreasureFaceUnchanged:
    def test_treasure_group_counts_derive_from_policy(self):
        """既有三组计数由 policy 真源现算比对（不是抄写死数字）。"""
        spec = s.read_policy()["perception"]["spec"]
        counts = {"template": 0, "point": 0, "ocr": 0}
        for anchor in spec.values():
            if anchor.get("kind") in counts:
                counts[anchor["kind"]] += 1
        proj = s.project_rois()
        for group, n in counts.items():
            assert len(proj[group]) == n, group
        # 其余 kind（未知值）仍不投影——保守口径不变
        assert sum(counts.values()) <= len(spec)

    def test_nodes_and_tuning_groups_intact(self):
        proj = s.project_rois()
        assert len(proj["tuning"]) == 1 and "appraiser_search_roi" in proj["tuning"]
        assert proj["nodes"], "nodes 组不应为空"
        for key, item in proj["nodes"].items():
            assert key.rpartition("#")[1] == "#"
            assert len(item["rect"]) == 4
            assert item["file"] in ("treasure.json", "treasure.entry.json")

    def test_treasure_reads_unaffected_by_hud_group(self):
        """`_meta` 等既有读面字段不因新组而改变形状。"""
        meta = s.project_rois()["_meta"]
        assert set(meta["base_hash"]) == {"policy", "treasure", "entry"}
        assert s.HUD_GROUP not in s.SPEC_GROUPS
        assert s.HUD_GROUP not in s.EDITABLE_GROUPS


# ----------------------------------------------------------------------
# 帧库：白名单 + 穿越防护
# ----------------------------------------------------------------------


class TestHudFrameLibrary:
    def test_lists_own_layout(self, demos):
        b = _hud_browser(demos)
        assert b.list_sessions() == [HUD_SESSION]
        assert b.list_raw(HUD_SESSION) == [HUD_FRAME]
        assert b.has_frames(HUD_SESSION) is True

    def test_resolves_inside_frames_dir(self, demos):
        p = _hud_browser(demos).resolve_raw(HUD_SESSION, HUD_FRAME)
        assert p is not None and p.is_file()
        assert p.parent.name == ss.SPEEDRUSH_FRAME_DIR

    def test_decoy_outside_frames_dir_unreachable(self, demos):
        """会话根下的合法帧名不可达、库根的同名帧不入列（is_relative_to 边界）。"""
        b = _hud_browser(demos)
        assert b.resolve_raw(HUD_SESSION, "000002.jpg") is None
        assert b.list_raw(HUD_SESSION) == [HUD_FRAME]

    @pytest.mark.parametrize("name", [
        "..", "../000001.jpg", "..\\000001.jpg", "../../frames/000001.jpg",
        "000001.jpg/../../000001.jpg", "./000001.jpg",
        "C:\\Windows\\win.ini", "C:/Windows/win.ini", "/etc/passwd",
        "\\\\server\\share\\f.jpg", "\\\\?\\C:\\Windows\\win.ini",
        "00001.jpg", "0000001.jpg", "000001.jpeg", "000001.png",
        "000001.jpg.txt", "000001_raw.jpg", "000001.JPG", "",
    ])
    def test_rejects_illegal_frame_names(self, demos, name):
        assert _hud_browser(demos).resolve_raw(HUD_SESSION, name) is None

    @pytest.mark.parametrize("session", [
        "..", "../x", "..\\..\\demos", "", "20260916_213104", "20260916_213104_p",
        "evil_p1", "20260916_213104_p1/x", "20260916_213104_p1\\..\\..",
        "20260916_213104_p1/../../../..",
    ])
    def test_rejects_illegal_sessions(self, demos, session):
        assert _hud_browser(demos).resolve_raw(session, HUD_FRAME) is None
        assert _hud_browser(demos).list_raw(session) == []


class TestTreasureLayoutDefaults:
    def test_defaults_still_treasure_shape(self, tmp_path):
        """参数化后缺省布局 = debug 形态（treasure 调用方零改动的回归锁）。"""
        root = tmp_path / "debug"
        raw = root / "20260812_183611" / "raw"
        raw.mkdir(parents=True)
        (raw / "0001_raw.jpg").write_bytes(b"x")
        (raw / "not_raw.txt").write_text("x", encoding="utf-8")
        trace_only = root / "20260202_000000"
        trace_only.mkdir()
        (trace_only / "trace.jsonl").write_text("{}\n", encoding="utf-8")
        legacy = root / "session_20260303_000000"
        legacy.mkdir()
        (legacy / "trace.jsonl").write_text("{}\n", encoding="utf-8")

        b = ss.SessionBrowser(root)
        assert b.session_re is ss.SESSION_RE
        assert b.frame_re is ss.RAW_RE
        assert b.frame_dir == "raw"
        assert b.trace_name == "trace.jsonl"
        assert b.list_sessions() == [
            "20260812_183611", "session_20260303_000000", "20260202_000000",
        ]
        assert b.list_raw("20260812_183611") == ["0001_raw.jpg"]
        assert b.has_frames("20260812_183611") is True
        assert b.has_frames("20260202_000000") is False
        assert b.resolve_raw("20260812_183611", "0001_raw.jpg") is not None

    def test_speedrush_session_needs_frame_dir_or_index(self, demos, tmp_path):
        """会话在场判据参数化：仅 frames/ 或仅 frames.jsonl 都算合法会话。"""
        b = _hud_browser(demos)
        assert b.list_sessions() == [HUD_SESSION]
        bare = tmp_path / "bare"
        (bare / "20260916_213104_p1" / "frames").mkdir(parents=True)
        assert _hud_browser(bare).list_sessions() == ["20260916_213104_p1"]
        empty = tmp_path / "empty"
        (empty / "20260916_213104_p1").mkdir(parents=True)
        assert _hud_browser(empty).list_sessions() == []


# ----------------------------------------------------------------------
# HTTP：帧通路与读数端点
# ----------------------------------------------------------------------


def _get(url: str):
    try:
        with urllib.request.urlopen(url, timeout=120) as resp:
            return resp.status, resp.headers.get("Content-Type", ""), resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.headers.get("Content-Type", ""), e.read()


def _post(url: str, body: dict):
    req = urllib.request.Request(
        url, data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=180) as resp:
            return resp.status, resp.headers.get("Content-Type", ""), resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.headers.get("Content-Type", ""), e.read()


@pytest.fixture
def live_hud(demos, monkeypatch):
    """临时帧库 + 本机临时端口（不依赖本机录制数据）。"""
    monkeypatch.setattr(s, "HUD_BROWSER", s._HudBrowser(demos))
    srv = ThreadingHTTPServer(("127.0.0.1", 0), s.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{srv.server_address[1]}"
    finally:
        srv.shutdown()
        srv.server_close()


class TestHudRoutes:
    def test_list_sessions(self, live_hud):
        st, ct, body = _get(live_hud + "/api/speedrush/list_sessions")
        assert st == 200 and ct.startswith("application/json")
        assert json.loads(body) == [HUD_SESSION]

    def test_list_images(self, live_hud):
        st, _, body = _get(live_hud + f"/api/speedrush/list_images?session={HUD_SESSION}")
        assert st == 200 and json.loads(body) == [HUD_FRAME]

    def test_list_images_bad_session_empty(self, live_hud):
        st, _, body = _get(live_hud + "/api/speedrush/list_images?session=../x")
        assert st == 200 and json.loads(body) == []

    def test_image_serves_real_bytes(self, live_hud):
        st, ct, body = _get(
            live_hud + f"/api/speedrush/image?session={HUD_SESSION}&name={HUD_FRAME}")
        assert st == 200 and ct == "image/jpeg"
        assert body[:2] == b"\xff\xd8" and body[-2:] == b"\xff\xd9"   # JPEG SOI / EOI

    @pytest.mark.parametrize("query", [
        f"session=..&name={HUD_FRAME}",
        f"session={HUD_SESSION}&name=..",
        f"session={HUD_SESSION}&name=../{HUD_FRAME}",
        f"session={HUD_SESSION}&name=..%5C{HUD_FRAME}",
        f"session={HUD_SESSION}&name=C%3A%5CWindows%5Cwin.ini",
        f"session={HUD_SESSION}&name=%2Fetc%2Fpasswd",
        f"session={HUD_SESSION}&name=000002.jpg",
        "session=evil_p1&name=000001.jpg",
        "session=&name=",
    ])
    def test_image_rejects_traversal(self, live_hud, query):
        st, _, _ = _get(live_hud + "/api/speedrush/image?" + query)
        assert st == 404, query

    def test_ocr_readout_shape(self, live_hud):
        """读数端点：有文本就给合并/逐块文本，引擎不可用就给 error——两种都不带金额字段。"""
        st, ct, body = _post(live_hud + "/api/speedrush/ocr",
                             {"session": HUD_SESSION, "image": HUD_FRAME,
                              "rect": [0.05, 0.02, 0.9, 0.11]})
        assert st == 200 and ct.startswith("application/json")
        res = json.loads(body)
        # treasure 的金额语义不得被带过来（本通路没有领域解释）
        assert "amount" not in res and "amounts" not in res
        assert isinstance(res["crop_size"], list) and len(res["crop_size"]) == 2
        assert res["crop_preview"].startswith("data:image/png;base64,")
        if "error" in res:
            assert "识别失败" in res["error"]
        else:
            assert isinstance(res["text"], str)
            assert isinstance(res["lines"], list)
            assert all(isinstance(x, str) for x in res["lines"])
            assert res["text"] == "".join(res["lines"])     # 合并文本 = 逐块拼接
            assert isinstance(res["duration_ms"], int)

    def test_ocr_requires_rect(self, live_hud):
        st, _, body = _post(live_hud + "/api/speedrush/ocr",
                            {"session": HUD_SESSION, "image": HUD_FRAME})
        assert st == 400 and "error" in json.loads(body)

    def test_ocr_rejects_traversal_frame(self, live_hud):
        st, _, _ = _post(live_hud + "/api/speedrush/ocr",
                         {"session": HUD_SESSION, "image": "../../x.jpg", "rect": [0, 0, 1, 1]})
        assert st == 404


# ----------------------------------------------------------------------
# 只读边界
# ----------------------------------------------------------------------


@pytest.fixture
def truth_copy(tmp_path, monkeypatch):
    """真源副本 + 路径重定向（保存管线测试一律在副本上跑，绝不触碰仓库真源）。"""
    policy = tmp_path / "treasure.policy.json"
    treasure = tmp_path / "treasure.json"
    entry = tmp_path / "treasure.entry.json"
    shutil.copy2(s.POLICY_FILE, policy)
    shutil.copy2(s.PIPELINE_FILES["treasure"], treasure)
    shutil.copy2(s.PIPELINE_FILES["entry"], entry)
    monkeypatch.setattr(s, "POLICY_FILE", policy)
    monkeypatch.setattr(s, "PIPELINE_FILES", {"treasure": treasure, "entry": entry})
    return {"policy": policy, "treasure": treasure, "entry": entry}


class TestReadonlyBoundary:
    def test_hud_group_outside_editable_sets(self):
        assert s.HUD_GROUP not in s.SPEC_GROUPS
        assert s.HUD_GROUP not in s.EDITABLE_GROUPS
        assert s.HUD_GROUP not in s.EDIT_FIELDS

    def test_save_pipeline_ignores_hud_group(self, truth_copy):
        """HUD 组即使被塞进保存 body 也不产生任何写面动作（服务端无它的合并分支）。"""
        before = {k: p.read_bytes() for k, p in truth_copy.items()}
        proj = s.project_rois()
        res = s.apply_save({**proj, "added": {}, "deleted": [], "preview": True,
                            "base_hash": proj["_meta"]["base_hash"]}, dry_run=True)
        assert res["diff"] == {"added": [], "removed": [], "changed": []}
        assert {k: p.read_bytes() for k, p in truth_copy.items()} == before

    def test_frontend_declares_hud_readonly(self, live_hud):
        """前端只读声明与三处门的存在性锁定（防回归：只读分类不得重新变得可写）。

        三处门 = hitTest 不命中 / onMove 第二道门 / 保存 body 剔除；缺任一处，
        "只读"就只是视觉约定（nodes/tuning 今天可拖正是同类漏门，见 README 小节）。
        """
        st, _, body = _get(live_hud + "/static/app.js")
        assert st == 200
        js = body.decode("utf-8")
        assert f'const READONLY_CATS = new Set(["{s.HUD_GROUP}"]);' in js
        assert 'const EDITABLE_CATS = new Set(["template", "point", "ocr"]);' in js
        assert f'const HUD_CAT = "{s.HUD_GROUP}";' in js
        assert "if (isReadonlyCat(state.currentCat)) return null;" in js      # hitTest
        assert "if (isReadonlyCat(state.currentCat)) return;" in js           # onMove
        assert "for (const cat of READONLY_CATS) delete rois[cat];" in js     # 保存 body