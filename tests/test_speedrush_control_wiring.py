# -*- coding: utf-8 -*-
"""实机闭环接线锁（planner 设计稿 §九 step 3）：_drive_loop 的控制链装配与下发。

锁的是**接线**（感知结果→跟踪→聚合→边界→决策→规划→手柄这条管道通不通、
V0/V1 开关在不在位、异常降级停不亦），不重测各层内部（那些有各自的单测）。
桩手柄记录调用，detect_boundary 打桩避开 CV 依赖。
"""
import json
import time as _time
from dataclasses import replace
from types import SimpleNamespace

import numpy as np
import pytest

try:
    import maaracing_master.plugins.speedrush.module as smod
    _OK, _ERR = True, ""
except Exception as exc:  # noqa: BLE001 —— CI 轻依赖环境缺 maa/… 时整文件跳过
    _OK, _ERR = False, str(exc)

pytestmark = pytest.mark.skipif(not _OK, reason=f"需要完整运行时依赖（maa/…）：{_ERR}")

from maaracing_master.plugins.speedrush.boundary import BoundarySummary  # noqa: E402
from maaracing_master.plugins.speedrush.config import load_decision  # noqa: E402
from maaracing_master.plugins.speedrush.perception import (  # noqa: E402
    Detection, PerceptionResult)


class StubPad:
    def __init__(self):
        self.joy = None
        self.trig = None
        self.updates = 0

    def left_joystick(self, x_value=0, y_value=0):
        self.joy = (x_value, y_value)

    def right_trigger(self, value=0):
        self.trig = value

    def update(self):
        self.updates += 1


def _boundary():
    return BoundarySummary(schema_version=2, left_x=80.0, right_x=1200.0,
                           road_width=1120.0, straight_residual=0.0, vp_row=None,
                           validity=True, uncertainty=0.1)


def _perc(fid, coins=(), cars=()):
    return PerceptionResult(frame_id=fid, ts_ns=fid * 50_000_000,
                            cars=list(cars), coins=list(coins), bonuses=[], infer_ms=1.0)


def _module():
    return smod.SpeedRushModule(None)


@pytest.fixture(autouse=True)
def _stub_boundary(monkeypatch):
    monkeypatch.setattr(smod, "detect_boundary", lambda frame, **kw: _boundary())


# ---------- V0：allow_all_moves=false → 杆值恒 0、油门恒 255 ----------

def test_v0_straight_line(monkeypatch):
    v0 = replace(load_decision(), mode=replace(load_decision().mode, allow_all_moves=False))
    monkeypatch.setattr(smod, "load_decision", lambda: v0)
    m = _module()
    chain = m._build_control_chain()
    pad = StubPad()
    for fid in range(1, 12):
        m._control_tick(chain, pad, None, _perc(fid), fid, fid * 50_000_000, 10.0, 1)
        assert pad.joy[0] == 0                 # 横向恒零
        assert pad.trig == 255                 # 油门恒满
    assert pad.updates == 11
    assert chain["disabled"] is False
    assert m._control_last["state"] == "CRUISE"


# ---------- V1：横向通道在位（自车偏置 → 反向收力回中）----------

def test_v1_lateral_channel_live(monkeypatch):
    # 显式 V1（横向开），不依赖 decision.json 的当前部署态
    v1 = replace(load_decision(), mode=replace(load_decision().mode, allow_all_moves=True))
    monkeypatch.setattr(smod, "load_decision", lambda: v1)
    m = _module()
    chain = m._build_control_chain()
    chain["planner"].state.executed_lane = 1.0   # 自车在 +1 车道，无目标 → 应向左回中
    pad = StubPad()
    saw_negative = False
    for fid in range(1, 16):
        m._control_tick(chain, pad, None, _perc(fid), fid, fid * 50_000_000, 10.0, 1)
        assert pad.trig == 255
        if pad.joy[0] < 0:
            saw_negative = True
    assert saw_negative                          # 横向控制确实活了
    # 回中：executed_lane 从 1.0 向 0 收敛
    assert chain["planner"].state.executed_lane < 1.0


# ---------- 闭环供数：pair 与单侧合成都喂（80e9a1c 供数门撤销锁） ----------

def _bnd_lanes(el, er, sides=2, clusters=()):
    return BoundarySummary(schema_version=3, left_x=80.0, right_x=1200.0,
                           road_width=1120.0, straight_residual=0.0, vp_row=None,
                           validity=True, uncertainty=0.1, sides=sides,
                           left_edge_lane=el, right_edge_lane=er,
                           clusters=clusters)


def test_road_offset_all_sources_feed_loop(monkeypatch):
    """单侧合成也进闭环（13:29 局证伪 pair-only：挡单侧=制造供数黑视，
    死亡螺旋段 87/118 拍被挡、陈旧重基与跳变门同时失效——黑视比噪声致命）。
    噪声由 0.8 新息门 + 1.0s 陈旧重基消化，不由接线层挡。"""
    cur = {}
    monkeypatch.setattr(smod, "detect_boundary", lambda frame, **kw: cur.get("b"))
    m = _module()
    chain = m._build_control_chain()
    pad = StubPad()
    fid = 0

    def tick():
        nonlocal fid
        fid += 1
        m._control_tick(chain, pad, None, _perc(fid), fid,
                        fid * 50_000_000, 10.0, 1)
        return chain["trace"][-1]

    cur["b"] = _bnd_lanes(-1.5, 1.5, clusters=(-1.5, 1.5))   # 簇间隙 [-1.5,1.5]
    t1 = tick()
    assert t1["ro_source"] == "gap"
    assert t1["road_offset"] == pytest.approx(0.0)           # 0 在间隙中心
    cur["b"] = _bnd_lanes(-2.0, None, sides=1)               # 单侧无簇:槽兜底
    t2 = tick()
    assert t2["ro_source"] == "gap_slot"
    assert t2["road_offset"] == pytest.approx(0.0)           # 槽簇照喂(不黑视)


# ---------- 控制链异常 → 停控转观测，不崩主循环、不 strand 油门 ----------

def test_chain_exception_disables_control():
    m = _module()
    chain = m._build_control_chain()
    pad = StubPad()
    m._control_tick(chain, pad, None, _perc(10), 10, 10 * 50_000_000, 10.0, 1)
    assert pad.updates == 1
    # frame_id 倒退：tracker.update fail-loud → _control_tick 捕获、停控
    m._control_tick(chain, pad, None, _perc(5), 5, 5 * 50_000_000, 10.0, 1)
    assert chain["disabled"] is True
    assert pad.updates == 1                      # 停控后不再下发
    # 停控是终态（本阶段）：再喂正常帧也不再动
    m._control_tick(chain, pad, None, _perc(20), 20, 20 * 50_000_000, 10.0, 1)
    assert pad.updates == 1


# ---------- 配置面：control_mode 读写 + 默认关 ----------

def test_control_mode_config_roundtrip():
    m = _module()
    assert m._control_mode is False              # 默认不接管（实机安全边界）
    assert smod.SpeedRushModule.DEFAULT_MODULE_CONFIG["control_mode"] is False
    out = m.set_module_config({"control_mode": True})
    assert out["control_mode"] is True and m._control_mode is True
    assert m.set_module_config({"control_mode": False})["control_mode"] is False


# ---------- 控制 trace：逐拍记录 + 阶段出口 flush ----------

def test_control_trace_records_and_flushes(tmp_path, monkeypatch):
    monkeypatch.setattr(smod, "_control_trace_root", lambda: tmp_path)
    m = _module()
    chain = m._build_control_chain()
    pad = StubPad()
    for fid in range(1, 6):
        m._control_tick(chain, pad, None, _perc(fid), fid, fid * 50_000_000, 10.0, 1)
    assert len(chain["trace"]) == 5
    row = chain["trace"][-1]
    # C1/§七.1 标定要的关键列都在
    for k in ("fid", "dt", "state", "x_target", "steer_norm", "steer_x",
              "executed_lane", "v_lat_est", "bnd_valid", "bnd_left_x", "bnd_right_x"):
        assert k in row, k
    m._flush_control_trace(chain, 1)
    files = list(tmp_path.glob("trace_*_p1.jsonl"))
    assert len(files) == 1
    lines = files[0].read_text(encoding="utf-8").splitlines()
    assert len(lines) == 5
    assert json.loads(lines[0])["fid"] == 1


def test_control_trace_empty_not_flushed(tmp_path, monkeypatch):
    monkeypatch.setattr(smod, "_control_trace_root", lambda: tmp_path)
    m = _module()
    m._flush_control_trace({"trace": [], "t_start": 0.0}, 1)
    assert list(tmp_path.glob("trace_*.jsonl")) == []   # 空 trace 不落盘


# ---------- 控制实况字段形状（GUI 消费契约）----------

def test_control_last_shape():
    m = _module()
    chain = m._build_control_chain()
    m._control_tick(chain, StubPad(), None, _perc(3), 3, 3 * 50_000_000, 10.0, 1)
    assert set(m._control_last) == {"state", "reason", "steer", "frame_id",
                                    "executed_lane", "road_offset",
                                    "dgeo_age", "dgeo_new"}
    assert m._control_last["frame_id"] == 3
    assert isinstance(m._control_last["executed_lane"], float)


# ---------- v3.1 _EgoRoadObserver：簇间隙 + 目标锁定（2026-09-28 重写） ----------


def _bnd_clusters(*clusters):
    return SimpleNamespace(clusters=tuple(clusters))


def test_ego_road_gap_center_feeds():
    """0 落在簇间隙 → off=该间隙中心(负=目标在左);目标随车到位后微调跟随。"""
    o = smod._EgoRoadObserver()
    off = o.update(_bnd_clusters(-2.1, 0.1, 2.1), now=1000.0, ego_lane=0.0)
    assert off == pytest.approx((-2.1 + 0.1) / 2)
    assert o.last["source"] == "gap"
    # 同位再拍:目标已锚定,off 持续同值(稳定点)
    assert o.update(_bnd_clusters(-2.1, 0.1, 2.1), now=1000.05,
                    ego_lane=0.0) == pytest.approx(off)


def test_ego_road_gap_target_lock_no_flip():
    """振荡回归锁(212734 后继局实证:归属 73s 跳 148 次,怼完右怼左):
    车跨过簇线后目标**不翻转**——继续拉回原目标间隙;车回到目标间隙附近
    后目标才微调跟随。"""
    o = smod._EgoRoadObserver()
    o.update(_bnd_clusters(-2.1, 0.1, 2.1), now=1000.0, ego_lane=0.0)  # 目标 -1.0
    # 车右移 0.3 道跨过簇线 0.1:0 落进相邻间隙(中心 +0.8),但目标不翻转
    off = o.update(_bnd_clusters(-2.4, -0.2, 1.8), now=1000.05, ego_lane=0.3)
    assert off == pytest.approx(-1.3)   # 目标路心(路系 -1.0)补偿后在左 1.3 道
    assert off != pytest.approx(0.8)    # 不翻转成当前所在间隙的中心
    # 车被拉回原目标间隙内(executed 0.05)→ 目标微调跟随到 -1.05
    off2 = o.update(_bnd_clusters(-2.15, 0.05, 2.15), now=1000.10, ego_lane=0.05)
    assert off2 == pytest.approx(-1.05)


def test_ego_road_gap_outside_pulls_back_and_overrides():
    """0 越出簇范围(撞墙前兆)→ 指向最近边界的拉回信号,**覆盖**锁定目标
    (保命优先);回到簇内后恢复目标语义。"""
    o = smod._EgoRoadObserver()
    o.update(_bnd_clusters(-2.1, 0.1, 2.1), now=1000.0, ego_lane=0.0)  # 目标 -1.0
    # 车右冲出路面(簇全负=车在最右簇右侧):当帧几何拉回信号覆盖目标
    off = o.update(_bnd_clusters(-3.9, -2.9), now=1000.05, ego_lane=1.5)
    assert off == pytest.approx(-2.9)         # 目标=最右簇(左 2.9 道)
    # 回到路面内第一拍:重新归属所在间隙中心(保命值不持久,否则被拉向路缘骑线)
    assert o.update(_bnd_clusters(-2.1, 0.1, 2.1), now=1000.10,
                    ego_lane=1.2) == pytest.approx(-1.0)


def test_ego_road_gap_slot_fallback_and_ttl():
    """当帧簇 <2 → 0.4s 内用最近簇集兜底(gap_slot),过期退 none;目标保持。"""
    o = smod._EgoRoadObserver()
    assert o.update(_bnd_clusters(-2.1, 0.1, 2.1), now=1000.0,
                    ego_lane=0.0) == pytest.approx(-1.0)
    assert o.update(None, now=1000.05, ego_lane=0.0) == pytest.approx(-1.0)
    assert o.update(_bnd_clusters(0.5), now=1000.10,
                    ego_lane=0.0) == pytest.approx(-1.0)      # 单簇,兜底
    assert o.update(None, now=1000.5, ego_lane=0.0) is None       # TTL 过期
    assert o.last["source"] == "none"


def test_control_chain_ego_road_gap_semantics():
    """chain 的 ego_road 即簇间隙+目标锁定语义(v3.1,无分档)。"""
    m = _module()
    o = m._build_control_chain()["ego_road"]
    assert o.OFF_MAX == 3.0 and hasattr(o, "retarget")


# ---------- YOLO 物体掩码：框入掩码（外扩+夹边）；无检测 None（深度路径零成本） ----------

def test_yolo_object_mask_boxes_with_margin():
    d = Detection(cx=100, cy=100, w=40, h=20, conf=0.9)
    mask = smod._yolo_object_mask(_perc(1, cars=(d,)))
    assert mask is not None and mask.shape == (720, 1280)
    ys, xs = np.nonzero(mask)
    m = smod.YOLO_MASK_MARGIN
    assert (int(xs.min()), int(xs.max())) == (100 - 20 - m, 100 + 20 + m)
    assert (int(ys.min()), int(ys.max())) == (100 - 10 - m, 100 + 10 + m)


def test_yolo_object_mask_edge_clamped():
    """框贴画面边缘：外扩后夹在画面内，不越界。"""
    d = Detection(cx=2, cy=2, w=40, h=40, conf=0.9)
    mask = smod._yolo_object_mask(_perc(1, cars=(d,)))
    assert mask is not None
    assert mask[0, 0] and not mask[:, 43 + smod.YOLO_MASK_MARGIN:].any()


def test_yolo_object_mask_none_when_no_detections():
    assert smod._yolo_object_mask(_perc(1)) is None
