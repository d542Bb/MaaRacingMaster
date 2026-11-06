# -*- coding: utf-8 -*-
"""Studio v5 两页契约：回放读取器与 ROI 节点写盘。"""
from __future__ import annotations

import json
import re
import threading
import urllib.error
import urllib.request

import pytest

from tools.pipeline.studio.nodes import NodeIndex, normalize_rect
from tools.pipeline.studio.replay import ReplayIndex
from tools.pipeline.studio.server import StudioApp, make_server


def _session(root):
    sess = root / "20260101_000000"
    bdir = sess / "speedrush" / "drive"
    bdir.mkdir(parents=True)
    (bdir / "manifest.json").write_text(json.dumps({
        "schema": 5, "session": sess.name, "module": "speedrush",
        "behavior": "drive", "started_at": "2026-01-01T00:00:00",
        "ended_at": "2026-01-01T00:01:00",
        "frames_index": "frames.jsonl", "frames_dir": ".",
    }), encoding="utf-8")
    (bdir / "frames.jsonl").write_text(
        json.dumps({"seq": 1, "ts_mono_ns": 100, "file": "raw/0001.jpg"})
        + "\n" + json.dumps({"seq": 2, "ts_mono_ns": 200, "file": "raw/0002.jpg"})
        + "\n", encoding="utf-8")
    (bdir / "raw").mkdir()
    (bdir / "raw" / "0001.jpg").write_bytes(b"one")
    (bdir / "raw" / "0002.jpg").write_bytes(b"two")
    (sess / "index.jsonl").write_text(
        json.dumps({"type": "segment_start", "behavior": "speedrush/drive"}) + "\n",
        encoding="utf-8")
    return sess


def _pipeline(tmp_path, *, newline="\n", indent=2):
    lib = tmp_path / "pipeline"
    lib.mkdir()
    payload = {
        "speedrush.race": {
            "attach": {"_page": "hall", "_label": "比赛按钮"},
            "recognition": {"type": "Custom", "param": {
                "custom_recognition": "MaaRM_Template",
                "custom_recognition_param": {
                    "mode": "template", "colorspace": "gray",
                    "rect": [0.1, 0.2, 0.3, 0.4],
                    "templates": ["hall_race_btn.png"], "threshold": 0.8,
                }}},
            "action": {"type": "Custom", "param": {"custom_action": "MaaRM_Click"}},
        }
    }
    text = json.dumps(payload, ensure_ascii=False, indent=indent)
    # 仓库真源里的 rect 是紧凑数组，不随 indent 展开；测试同样模拟这个形态。
    text = re.sub(r'"rect": \[[^\]]*\]',
                  '"rect": [0.1, 0.2, 0.3, 0.4]', text, flags=re.S)
    if newline != "\n":
        text = text.replace("\n", newline)
    path = lib / "demo.json"
    path.write_bytes((text + newline).encode("utf-8"))
    return lib, path


def test_replay_reads_v5_manifest_and_frames(tmp_path):
    sess = _session(tmp_path / "debug")
    idx = ReplayIndex(tmp_path / "debug")
    assert idx.sessions() == [sess.name]
    assert idx.behaviors(sess.name) == [{
        "session": sess.name, "module": "speedrush", "behavior": "drive",
        "frames": 2, "started_at": "2026-01-01T00:00:00",
        "ended_at": "2026-01-01T00:01:00",
    }]
    assert [r["seq"] for r in idx.frames(sess.name, "speedrush", "drive")] == [1, 2]
    frame = idx.resolve_frame(sess.name, "speedrush", "drive", 2)
    assert frame is not None and frame.read_bytes() == b"two"
    assert idx.resolve_frame(sess.name, "speedrush", "drive", 99) is None
    assert idx.timeline(sess.name)[0]["type"] == "segment_start"


def test_replay_rejects_traversal_and_bad_session(tmp_path):
    sess = _session(tmp_path / "debug")
    idx = ReplayIndex(tmp_path / "debug")
    assert idx.frames("../x", "speedrush", "drive") == []
    assert idx.frames(sess.name, "..", "drive") == []
    assert idx.resolve_frame(sess.name, "speedrush", "../drive", 1) is None


def test_normalize_rect_rejects_bad_geometry():
    assert normalize_rect([0.1, 0.2, 0.3, 0.4]) == [0.1, 0.2, 0.3, 0.4]
    for bad in ([0.3, 0.2, 0.1, 0.4], [0, 0, 0, 1], [0, 0, 1.1, 1],
                [0, 0, 1], "nope"):
        with pytest.raises(ValueError):
            normalize_rect(bad)


def test_node_index_lists_custom_rect(tmp_path):
    lib, path = _pipeline(tmp_path)
    index = NodeIndex(extra_dirs=(lib,))
    node = next(n for n in index.nodes() if n["node"] == "speedrush.race")
    assert node["rect"] == [0.1, 0.2, 0.3, 0.4]
    assert node["page"] == "hall"
    assert node["module"] == "core"       # 临时目录不在 plugins/ 下
    assert node["colorspace"] == "gray"


def test_save_text_replacement_is_surgical(tmp_path, monkeypatch):
    lib, path = _pipeline(tmp_path, newline="\r\n", indent=4)
    from tools.pipeline import truth
    monkeypatch.setattr(truth, "REPO", tmp_path)
    before = path.read_bytes()
    index = NodeIndex(extra_dirs=(lib,), validator=lambda: [])
    result = index.save(path.relative_to(tmp_path).as_posix(),
                        "speedrush.race", [0.11, 0.22, 0.33, 0.44])
    assert result["ok"] and result["changed"]
    after = path.read_bytes()
    assert b"\r\n" in after
    assert b'"rect": [0.1, 0.2, 0.3, 0.4]' not in after
    assert b'"rect": [0.11, 0.22, 0.33, 0.44]' in after
    # 除 rect 外的字节不变
    assert before.replace(b"[0.1, 0.2, 0.3, 0.4]", b"[0.11, 0.22, 0.33, 0.44]") == after


def test_save_rolls_back_when_validator_reports_new_error(tmp_path, monkeypatch):
    lib, path = _pipeline(tmp_path)
    from tools.pipeline import truth
    monkeypatch.setattr(truth, "REPO", tmp_path)
    before = path.read_bytes()

    def validator():
        return ["boom"] if b"0.11" in path.read_bytes() else []

    index = NodeIndex(extra_dirs=(lib,), validator=validator)
    result = index.save(path.relative_to(tmp_path).as_posix(),
                        "speedrush.race", [0.11, 0.22, 0.33, 0.44])
    assert result["ok"] is False and "boom" in result["errors"]
    assert path.read_bytes() == before


def test_http_endpoints_and_save(tmp_path, monkeypatch):
    sess = _session(tmp_path / "debug")
    lib, path = _pipeline(tmp_path)
    from tools.pipeline import truth
    monkeypatch.setattr(truth, "REPO", tmp_path)
    replay = ReplayIndex(tmp_path / "debug")
    nodes = NodeIndex(extra_dirs=(lib,), validator=lambda: [])
    server = make_server("127.0.0.1", 0, StudioApp(replay=replay, nodes=nodes))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        def get(path):
            with urllib.request.urlopen(base + path, timeout=5) as res:
                return res.status, res.read()

        status, body = get("/api/replay/sessions")
        assert status == 200 and json.loads(body)["sessions"] == [sess.name]
        status, body = get(f"/api/replay/frames?session={sess.name}"
                           "&module=speedrush&behavior=drive")
        assert json.loads(body)["frames"][1]["seq"] == 2
        status, body = get(f"/api/replay/frame?session={sess.name}"
                           "&module=speedrush&behavior=drive&seq=1")
        assert status == 200 and body == b"one"
        status, body = get("/api/roi/nodes")
        assert any(n["node"] == "speedrush.race" for n in json.loads(body)["nodes"])

        req = urllib.request.Request(
            base + "/api/roi/save", method="POST",
            data=json.dumps({"file": path.relative_to(tmp_path).as_posix(),
                             "node": "speedrush.race",
                             "rect": [0.12, 0.22, 0.32, 0.42]}).encode(),
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=5) as res:
            saved = json.loads(res.read())
        assert saved["ok"] and b"0.12" in path.read_bytes()

        with pytest.raises(urllib.error.HTTPError) as bad:
            get(f"/api/replay/frame?session={sess.name}"
                "&module=speedrush&behavior=drive&seq=99")
        assert bad.value.code == 404
    finally:
        server.shutdown()
        server.server_close()
