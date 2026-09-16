# -*- coding: utf-8 -*-
"""DebugIOWorker + FrameSource/DebugSink + module.config loader（统一计划 P2b）单测。

覆盖：
- DebugIOWorker：经依赖注入（renderer/sink/to_bgr/make_state）工作，**不访问任何
  debug 私有成员**、不拉 cv2/DebugState 重依赖；有界队列满则丢弃；frame/peep 两条
  指令的落盘与 PEEP 更新路径。
- module.config loader：dict 与文件路径加载、组装 ROIConfig+RenderPlan、结构校验、
  错误聚合为 ModuleConfigError。
"""
from __future__ import annotations

import json

import pytest

from maaracing_master.core.debug_io import DebugIOWorker
from maaracing_master.core.module_config import (
    ModuleConfig,
    ModuleConfigError,
    load_module_config,
)

# 一份包含 rois/stages/render 的完整配置（供 loader 测试）
_CFG = {
    "_schema_ver": 1,
    "reference_size": [1280, 720],
    "rois": {
        "hall_peak_appraise_card": {"rect": [0.76, 0.80, 0.90, 0.89]},
        "round_big_banner": {"rect": [0.39, 0.42, 0.60, 0.58],
                             "templates": ["round1_banner.png", "round2_banner.png"]},
    },
    "stages": {
        "order": ["大厅", "第1回合"],
        "global_anchors": ["hall_peak_appraise_card"],
        "definitions": {
            "大厅": {"active_rois": []},
            "第1回合": {"active_rois": ["round_big_banner"]},
        },
    },
    "render": {
        "debug": ["zones", "button", "templates"],
        "peep": ["zones", "button"],
        "hud_fields": ["stage", "round"],
    },
}


# ======================================================================
# DebugIOWorker
# ======================================================================

class _FakeRenderer:
    """fake renderer：渲染时记录调用，返回一个带标记的对象。"""

    def __init__(self):
        self.full_calls = []
        self.peep_calls = []

    def render_full(self, frame_bgr, state):
        self.full_calls.append(state)
        return ("full", frame_bgr)

    def render_peep(self, frame_bgr, state):
        self.peep_calls.append(state)
        return ("peep", frame_bgr)


class _FakeSink:
    """fake DebugSink：记录 save_raw/save_full/update_peep 调用，peep_enabled 可配。"""

    def __init__(self, peep_enabled=True):
        self.peep_enabled = peep_enabled
        self.raw = []
        self.full = []
        self.peep = []

    def update_peep(self, peep_img):
        self.peep.append(peep_img)

    def save_full(self, idx, full_img_bgr):
        self.full.append(idx)

    def save_raw(self, idx, frame_bgr):
        self.raw.append(idx)


def _make_worker(sink=None, queue_max=0):
    renderer = _FakeRenderer()
    sink = sink or _FakeSink()
    w = DebugIOWorker(
        renderer, sink, queue_max=queue_max,
        to_bgr=lambda f: f,                 # 纯逻辑：注入不转换（避免拉 cv2）
        make_state=lambda label, kw: (label, kw),  # 注入轻量 state（避免拉 DebugState）
    )
    return w, renderer, sink


class TestDebugIOWorkerInjection:
    def test_does_not_import_heavy_deps_at_construction(self):
        # 构造时绝不 import cv2 / core.debug（core 零重依赖）
        w, _, _ = _make_worker()
        assert w.pending == 0

    def test_frame_task_saves_raw_and_full_and_peep(self):
        w, r, s = _make_worker()
        assert w.enqueue("frame", "RGB", idx=1, didx=10, label="L", kwargs={"a": 1})
        w.process_one()
        assert s.raw == [1]
        assert s.full == [10]
        assert len(s.peep) == 1          # peep_enabled 默认 True
        assert len(r.full_calls) == 1 and len(r.peep_calls) == 1

    def test_peep_task_only_updates_peep(self):
        w, r, s = _make_worker()
        assert w.enqueue("peep", "RGB", idx=0, didx=0, label="L")
        w.process_one()
        assert s.raw == [] and s.full == []   # 不写盘
        assert len(s.peep) == 1
        assert len(r.full_calls) == 0 and len(r.peep_calls) == 1

    def test_peep_disabled_no_peep_render(self):
        w, r, s = _make_worker(sink=_FakeSink(peep_enabled=False))
        w.enqueue("frame", "RGB", idx=1, didx=10)
        w.process_one()
        assert s.peep == []
        assert len(r.full_calls) == 1 and len(r.peep_calls) == 0

    def test_bounded_queue_drops_when_full(self):
        # maxsize=1：第一帧入队成功，第二帧满则丢
        w, _, s = _make_worker(queue_max=1)
        assert w.enqueue("frame", "RGB", idx=1, didx=10)
        assert w.enqueue("frame", "RGB", idx=2, didx=20) is False  # 已满，丢弃
        assert w.dropped == 1
        w.drain()
        assert s.raw == [1]                # 只落盘入队成功的那帧
        assert w.processed == 1

    def test_state_kwargs_passed_through(self):
        w, r, _ = _make_worker()
        w.enqueue("frame", "RGB", idx=1, didx=1, label="L", kwargs={"treasure_h": 5})
        w.process_one()
        state = r.full_calls[0]
        assert state == ("L", {"treasure_h": 5})

    def test_unknown_cmd_ignored_not_raise(self):
        w, _, _ = _make_worker()
        assert w.enqueue("zzz", "RGB", idx=0, didx=0)
        w.process_one()                   # 不抛
        assert w.processed == 1

    def test_process_one_on_empty_returns_false(self):
        w, _, _ = _make_worker()
        assert w.process_one() is False

    def test_close_idempotent(self):
        w, _, _ = _make_worker()
        w.close()
        w.close()


# ======================================================================
# module.config loader 契约
# ======================================================================

class TestModuleConfigLoader:
    def test_from_dict_assembles_roicfg_and_plan(self):
        cfg = load_module_config(_CFG)
        assert isinstance(cfg, ModuleConfig)
        assert cfg.schema_ver == 1
        assert cfg.reference_size == (1280, 720)
        assert cfg.roi_config.get_detection_rois("第1回合") == (
            "hall_peak_appraise_card", "round_big_banner",
        )
        assert cfg.render_plan.debug_layers == ("zones", "button", "templates")
        assert cfg.render_plan.peep_layers == ("zones", "button")
        assert cfg.render_plan.hud_fields == ("stage", "round")

    def test_from_dict_classmethod(self):
        cfg = ModuleConfig.from_dict(_CFG)
        assert cfg.schema_ver == 1

    def test_from_file_path(self, tmp_path):
        p = tmp_path / "module.config.json"
        p.write_text(json.dumps(_CFG), encoding="utf-8")
        cfg = load_module_config(p)
        assert cfg.roi_config.stage_order == ("大厅", "第1回合")
        assert cfg.render_plan.debug_layers == ("zones", "button", "templates")

    def test_missing_file_raises_module_config_error(self, tmp_path):
        with pytest.raises(ModuleConfigError):
            load_module_config(tmp_path / "no_such.json")

    def test_bad_file_content_raises_module_config_error(self, tmp_path):
        p = tmp_path / "bad.json"
        p.write_text("not json{{{", encoding="utf-8")
        with pytest.raises(ModuleConfigError):
            load_module_config(p)

    def test_error_aggregated_from_roicfg(self):
        bad = dict(_CFG)
        bad["_schema_ver"] = "1"              # 让 ROIConfig 校验失败
        with pytest.raises(ModuleConfigError):
            load_module_config(bad)

    def test_render_bad_type_rejected(self):
        bad = dict(_CFG)
        bad["render"] = dict(_CFG["render"])
        bad["render"]["debug"] = ["zones", 123]   # 非字符串元素
        with pytest.raises(ModuleConfigError):
            load_module_config(bad)

    def test_missing_render_is_allowed(self):
        cfg = dict(_CFG)
        cfg.pop("render")                       # 某些模块不需要 Debug 渲染
        m = load_module_config(cfg)
        assert m.render_plan.all_layers() == ()