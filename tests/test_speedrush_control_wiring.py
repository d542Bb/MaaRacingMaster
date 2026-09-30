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

    cur["b"] = _bnd_lanes(-1.5, 1.5, clusters=(-1.5, 1.5))   # 黄线双侧路缘,对称
    t1 = tick()
    assert t1["ro_source"] == "pair"
    assert t1["road_offset"] == pytest.approx(0.0)           # 车在路心
    cur["b"] = _bnd_lanes(-2.0, None, sides=1)               # 单侧:有效对兜底
    t2 = tick()
    assert t2["ro_source"] == "pair_slot"
    assert t2["road_offset"] == pytest.approx(0.0)           # 槽照喂(不黑视)


def test_cross_layer_polarity_and_mirror(monkeypatch):
    """跨层极性契约（2026-09-30 复核裁决 P0：v3 三轮事故的总缺口——两侧各自
    测符号、端到端只断言过 0.0，0 是符号不变量）。

    场景：车在路心**右边** 1 道（黄线左缘 -2.9、右缘 -0.9，间隙中心 -1.9）。
    planner 契约：road_offset = 自车相对路心的位置（右正）→ 必须 **+1.9**，
    且闭环必须出**负杆**（向左修正）。镜像场景（车在路心左边）必须出正杆。
    任何一侧单独改符号、或观测/消费语义换牌，此测试必须红。"""
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

    cur["b"] = _bnd_lanes(-2.9, -0.9, clusters=(-2.9, -0.9))  # 车在路心右 1.9 道
    for _ in range(25):                    # 锚定形成门(|1.9|>1.0)→ stale 重基后闭环启动
        t = tick()
    assert t["road_offset"] == pytest.approx(+1.9), "极性:车在路心右→off 必须正"
    assert t["steer_norm"] < 0, "车在路心右→必须向左修(负杆)"
    cur["b"] = _bnd_lanes(+0.9, +2.9, clusters=(+0.9, +2.9))  # 镜像:路心左 1.9 道
    for _ in range(25):
        t2 = tick()
    assert t2["road_offset"] == pytest.approx(-1.9)
    assert t2["steer_norm"] > 0, "镜像:车在路心左→必须向右修(正杆)"


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


def _bnd_clusters(*clusters, sides=2):
    l, r = (clusters[0], clusters[-1]) if len(clusters) >= 2 else (None, None)
    return SimpleNamespace(validity=bool(clusters), sides=sides,
                           left_edge_lane=l, right_edge_lane=r,
                           clusters=tuple(clusters))


def test_ego_road_pair_off_polarity():
    """观测层极性（v3.2）：off=自车相对路心（右正,planner 契约）——
    黄线双侧路缘,off=−(左缘+右缘)/2。车在路心右 1.9 道必须出 +1.9。"""
    o = smod._EgoRoadObserver()
    assert o.update(_bnd_clusters(-2.9, -0.9), now=1000.0) == pytest.approx(+1.9)
    assert o.last["source"] == "pair"
    o2 = smod._EgoRoadObserver()
    assert o2.update(_bnd_clusters(0.9, 2.9), now=1000.0) == pytest.approx(-1.9)
    assert o2.update(_bnd_clusters(1.0, 3.0), now=1000.05) == pytest.approx(-2.0)


def test_ego_road_gate_sides2_and_slot_ttl():
    """boundary 契约门：居中类消费方须 sides==2——单侧/无效不吃当帧值,
    0.4s 内最近有效对保鲜兜底（pair_slot）,过期退 none（13:29 黑视教训:
    兜底优先,单侧安全包线是 P1）。"""
    o = smod._EgoRoadObserver()
    assert o.update(_bnd_clusters(-2.9, -0.9), now=1000.0) == pytest.approx(+1.9)
    assert o.update(_bnd_lanes(5.0, None, sides=1), now=1000.05) == pytest.approx(+1.9)
    assert o.last["source"] == "pair_slot"       # 单侧→兜底
    assert o.update(None, now=1000.30) == pytest.approx(+1.9)
    assert o.update(None, now=1000.60) is None   # TTL 过期
    assert o.last["source"] == "none"


def test_ego_road_off_max_garbage_not_fed_nor_cached():
    """|off|>OFF_MAX 的垃圾对不喂、不入槽（宁弃不喂假路心,也不留毒槽）。"""
    o = smod._EgoRoadObserver()
    assert o.update(_bnd_clusters(-6.0, -2.0), now=1000.0) is None  # off=+4.0
    assert o.last["source"] == "pair"
    assert o.update(_bnd_clusters(-2.9, -0.9), now=1000.05) == pytest.approx(+1.9)
    assert o.last["source"] == "pair"            # 垃圾对未入槽,干净对直接生效


def test_control_chain_ego_road_gap_semantics():
    """chain 的 ego_road 即 v3.2 pair 语义（无分档、无目标锁定机器）。"""
    m = _module()
    o = m._build_control_chain()["ego_road"]
    assert o.OFF_MAX == 3.0 and not hasattr(o, "retarget")


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
