# -*- coding: utf-8 -*-
"""深度模型包（可选 DLC）的解析、校验与缺模型门禁回归锁。"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from maaracing_master.plugins.speedrush import model_assets

REL = "resources/models/test/model.bin"


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _patch_dirs(monkeypatch, tmp_path):
    pack = tmp_path / "pack"
    packaged = tmp_path / "packaged"
    monkeypatch.setattr(model_assets, "model_pack_dir", lambda: pack)
    monkeypatch.setattr(model_assets, "MODELS_DIR", packaged)
    monkeypatch.setattr(model_assets, "MODEL_MANIFEST", {REL: (4, _sha(b"good"))})
    monkeypatch.setattr(model_assets, "DEPTH_MANIFEST", {REL: (4, _sha(b"good"))})
    model_assets.clear_cache()
    return pack, packaged


def _write(path: Path, data: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def test_pack_layout_strips_resources_prefix(monkeypatch, tmp_path):
    pack = tmp_path / "models"
    monkeypatch.setattr(model_assets, "model_pack_dir", lambda: pack)
    assert model_assets.packed_rel("resources/models/perception/model.onnx") == \
        "perception/model.onnx"
    assert model_assets.pack_path("resources/models/perception/model.onnx") == \
        pack / "perception" / "model.onnx"


def test_resolve_prefers_user_pack(monkeypatch, tmp_path):
    pack, packaged = _patch_dirs(monkeypatch, tmp_path)
    user_file = _write(model_assets.pack_path(REL), b"good")
    _write(packaged / "test" / "model.bin", b"good")
    assert model_assets.resolve(REL) == user_file


def test_resolve_falls_back_to_packaged(monkeypatch, tmp_path):
    pack, packaged = _patch_dirs(monkeypatch, tmp_path)
    packed_file = _write(packaged / "test" / "model.bin", b"good")
    assert model_assets.resolve(REL) == packed_file


def test_missing_and_check_pack_report_install_path(monkeypatch, tmp_path):
    pack, _packaged = _patch_dirs(monkeypatch, tmp_path)
    assert model_assets.missing() == (REL,)
    err = model_assets.check_pack()
    assert err and str(pack / "test" / "model.bin") in err


def test_verify_accepts_good_file_and_rejects_size_and_hash(monkeypatch, tmp_path):
    pack, _packaged = _patch_dirs(monkeypatch, tmp_path)
    good = _write(model_assets.pack_path(REL), b"good")
    assert model_assets.verify(good, REL) is None

    wrong_size = _write(model_assets.pack_path(REL), b"too-long")
    assert "大小不符" in (model_assets.verify(wrong_size, REL) or "")

    wrong_hash = _write(model_assets.pack_path(REL), b"evil")
    model_assets.clear_cache()      # 同尺寸重写可能命中同一 mtime 刻度
    assert "校验失败" in (model_assets.verify(wrong_hash, REL) or "")


def test_select_skips_corrupt_user_pack_and_uses_packaged(monkeypatch, tmp_path):
    pack, packaged = _patch_dirs(monkeypatch, tmp_path)
    _write(model_assets.pack_path(REL), b"evil")
    good = _write(packaged / "test" / "model.bin", b"good")
    path, err = model_assets.select(REL)
    assert err is None and path == good


def test_lane_model_is_optional_pack_member():
    """线观测随可选模型包分发，但不参与深度档的启动门禁。"""
    assert model_assets.LANE_MODEL_REL in model_assets.LANE_MANIFEST
    assert model_assets.LANE_MODEL_REL not in model_assets.DEPTH_MANIFEST
    assert set(model_assets.MODEL_MANIFEST) == (
        set(model_assets.DEPTH_MANIFEST) | set(model_assets.LANE_MANIFEST))


def test_default_mode_is_straight_and_assets_are_optional():
    pytest.importorskip("maa")
    import maaracing_master.plugins.speedrush.module as smod
    assert smod.SpeedRushModule.DEFAULT_MODULE_CONFIG["drive_mode"] == "straight"
    assert smod.SpeedRushModule.REQUIRED_ASSETS == ()


def test_config_ui_marks_depth_with_badge_and_notice_when_missing():
    pytest.importorskip("maa")
    from maaracing_master.core import config_ui_contract as C
    import maaracing_master.plugins.speedrush.module as smod

    ui = smod._build_config_ui(False)
    field = ui["groups"][0]["fields"][0]
    depth = field["options"][-1]
    assert depth["label"] == "深度几何"
    assert depth["badge"] == {"text": "↓", "level": "warn"}
    assert field["notice"]["level"] == "warn"
    assert field["notice"]["actions"][1]["subpath"] == "data/speedrush/models"

    class _Decl:
        CONFIG_UI_CONTRACT = 1
        CONFIG_UI = ui

    checked = C.normalize_config_ui(
        _Decl, set(smod.SpeedRushModule.DEFAULT_MODULE_CONFIG))
    assert checked.state == C.UI_OK and checked.warnings == ()

    ready = smod._build_config_ui(True)["groups"][0]["fields"][0]
    assert "notice" not in ready
    assert "badge" not in ready["options"][-1]


def test_summary_marks_missing_depth_model(monkeypatch):
    pytest.importorskip("maa")
    import maaracing_master.plugins.speedrush.module as smod
    monkeypatch.setattr(smod.model_assets, "missing", lambda: (smod.DEPTH_MODEL_REL,))
    summary = smod.SpeedRushModule.config_summary({"drive_mode": "depth"})
    assert any("模型未安装" in item for item in summary)

    # 缺模型提示挂在字段内联提示上；状态行不再重复一遍
    mod = object.__new__(smod.SpeedRushModule)
    mod._macro_rec = None
    mod._macro_player = None
    mod._recorder = None
    mod._running = False
    mod._record_mode = False
    assert mod.config_status() is None


def test_depth_start_blocks_before_connecting_when_pack_invalid(monkeypatch):
    pytest.importorskip("maa")
    import maaracing_master.plugins.speedrush.module as smod
    connected = {"n": 0}

    class Ctx:
        def connect(self):
            connected["n"] += 1
            return False

    mod = object.__new__(smod.SpeedRushModule)
    mod.ctx = Ctx()
    mod._drive_mode = "depth"
    mod._log_grp = None
    monkeypatch.setattr(smod.model_assets, "check_pack", lambda: "缺少模型")
    smod.SpeedRushModule.start(mod)
    assert connected["n"] == 0
