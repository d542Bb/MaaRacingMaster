# -*- coding: utf-8 -*-
"""实机闭环接线锁（planner 设计稿 §九 step 3）：_drive_loop 的控制链装配与下发。

锁的是**接线**（感知结果→跟踪→聚合→深度供数→决策→规划→手柄这条管道通不通、
V0/V1 开关在不在位、异常降级停不亦），不重测各层内部（那些有各自的单测）。
桩手柄记录调用；深度几何经 take() 桩同步供数（不碰 GPU/线程）。
"""
import json
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

from maaracing_master.plugins.speedrush.config import load_decision  # noqa: E402
from maaracing_master.plugins.speedrush.depth_geo import DepthRoadReading  # noqa: E402
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


def _perc(fid, coins=(), cars=()):
    return PerceptionResult(frame_id=fid, ts_ns=fid * 50_000_000,
                            cars=list(cars), coins=list(coins), bonuses=[], infer_ms=1.0)


def _module():
    return smod.SpeedRushModule(None)


@pytest.fixture(autouse=True)
def _stub_depth(monkeypatch):
    """深度几何桩：session 加载置 None（不碰 GPU/权重），take() 同步回放
    cur['r']（供数测试显式赋值；默认 None=无供数，退纯模型积分）。"""
    cur = {"r": None}
    monkeypatch.setattr(smod, "load_session", lambda w: None)

    def _take(self):
        return cur["r"], cur["r"] is not None

    monkeypatch.setattr(smod.AsyncDepthRoadObserver, "take", _take)
    yield cur


@pytest.fixture(autouse=True)
def _pin_gate_off(monkeypatch):
    """决策闸自钉关：本文件锁的是 legacy 接线管道语义，不随部署
    decision.json 的运行态漂（验收期部署文件开闸）。"""
    d = load_decision()
    monkeypatch.setattr(
        smod, "load_decision",
        lambda: replace(d, mode=replace(d.mode, trajectory_sampling=False)))


# ---------- V0：allow_all_moves=false → 杆值恒 0、油门恒 255 ----------

def test_v0_straight_line():
    v0 = _decision_v0()
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


def _decision_v0():
    d = load_decision()
    return replace(d, mode=replace(d.mode, allow_all_moves=False))


def _decision_v1():
    d = load_decision()
    return replace(d, mode=replace(d.mode, allow_all_moves=True))


# ---------- V1：横向通道在位（自车偏置 → 反向收力回中）----------

def test_v1_lateral_channel_live():
    v1 = _decision_v1()
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

def _dgeo_lanes(el, er):
    return DepthRoadReading(left_edge_lane=el, right_edge_lane=er,
                            left_x=None, right_x=None,
                            sides=int(el is not None) + int(er is not None),
                            latency_ms=1.0, rejects=())


def test_road_offset_all_sources_feed_loop(_stub_depth):
    """单侧合成也进闭环（13:29 局证伪 pair-only：挡单侧=制造供数黑视，
    死亡螺旋段 87/118 拍被挡、陈旧重基与跳变门同时失效——黑视比噪声致命）。
    噪声由 0.8 新息门 + 1.0s 陈旧重基消化，不由接线层挡。"""
    cur = _stub_depth
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

    cur["r"] = _dgeo_lanes(-1.5, 1.5)                        # 双侧路缘,对称
    t1 = tick()
    assert t1["ro_source"] == "pair"
    assert t1["road_offset"] == pytest.approx(0.0)           # 车在路心
    cur["r"] = _dgeo_lanes(-2.0, None)                       # 单侧:有效对兜底
    t2 = tick()
    assert t2["ro_source"] == "pair_slot"
    assert t2["road_offset"] == pytest.approx(0.0)           # 槽照喂(不黑视)


def test_cross_layer_polarity_and_mirror(_stub_depth):
    """跨层极性契约（2026-09-30 复核裁决 P0：v3 三轮事故的总缺口——两侧各自
    测符号、端到端只断言过 0.0，0 是符号不变量）。

    场景：车在路心**右边** 1 道（3D 找边左缘 -2.9、右缘 -0.9，间隙中心 -1.9）。
    planner 契约：road_offset = 自车相对路心的位置（右正）→ 必须 **+1.9**，
    且闭环必须出**负杆**（向左修正）。镜像场景（车在路心左边）必须出正杆。
    任何一侧单独改符号、或观测/消费语义换牌，此测试必须红。"""
    cur = _stub_depth
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

    cur["r"] = _dgeo_lanes(-2.9, -0.9)   # 车在路心右 1.9 道
    for _ in range(25):                  # 锚定形成门(|1.9|>1.0)→ stale 重基后闭环启动
        t = tick()
    assert t["road_offset"] == pytest.approx(+1.9), "极性:车在路心右→off 必须正"
    assert t["steer_norm"] < 0, "车在路心右→必须向左修(负杆)"
    cur["r"] = _dgeo_lanes(+0.9, +2.9)   # 镜像:路心左 1.9 道
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


# ---------- 直行基线档：配置读写 + 直行拍下发（V0 语义，不碰感知/深度） ----------

def test_straight_mode_config_roundtrip():
    m = _module()
    assert m._straight_mode is False             # 默认走正常深度几何驾驶
    assert smod.SpeedRushModule.DEFAULT_MODULE_CONFIG["straight_mode"] is False
    out = m.set_module_config({"straight_mode": True})
    assert out["straight_mode"] is True and m._straight_mode is True
    assert m.set_module_config({"straight_mode": False})["straight_mode"] is False


def test_straight_tick_zero_steer_full_throttle():
    """直行拍：方向恒零、油门恒踩（throttle_raw 读 decision.json 单一真源）；
    trace 只记时间轴（fid/ts_ns）+ 常量列，供事后对齐 HUD 分数事件。"""
    m = _module()
    pad = StubPad()
    rows: list[dict] = []
    for fid in range(1, 6):
        m._straight_tick(pad, fid, fid * 50_000_000, rows,
                         throttle_raw=load_decision().planner.throttle_raw)
        assert pad.joy == (0, 0)                 # 横向恒零
        assert pad.trig == 255                   # 油门恒满（decision.json throttle_raw）
    assert pad.updates == 5
    assert [r["fid"] for r in rows] == [1, 2, 3, 4, 5]
    for r in rows:
        assert r["state"] == "STRAIGHT"
        assert r["x_target"] == 0.0 and r["steer_x"] == 0
        assert "ts_ns" in r


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
              "executed_lane", "v_lat_est", "road_offset",
              "dgeo_sides", "dgeo_left", "dgeo_right"):
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
                                    "executed_lane", "x_target", "road_offset",
                                    "dgeo_age", "dgeo_new"}
    assert m._control_last["frame_id"] == 3
    assert isinstance(m._control_last["executed_lane"], float)


# ---------- _EgoRoadObserver：路心合成 + 保鲜槽（供数源=深度几何 v4 读数） ----------


def _bnd_clusters(*clusters, sides=2):
    """update() 的鸭子消费面：sides + 两侧 edge_lane（DepthRoadReading 形状）。"""
    l, r = (clusters[0], clusters[-1]) if len(clusters) >= 2 else (None, None)
    return SimpleNamespace(sides=sides, left_edge_lane=l, right_edge_lane=r)


def test_ego_road_pair_off_polarity():
    """观测层极性（v3.2 契约沿用，供数源换 3D 找边）：off=自车相对路心
    （右正,planner 契约）——双侧路缘,off=−(左缘+右缘)/2。车在路心右 1.9 道
    必须出 +1.9。"""
    o = smod._EgoRoadObserver()
    assert o.update(_bnd_clusters(-2.9, -0.9), now=1000.0) == pytest.approx(+1.9)
    assert o.last["source"] == "pair"
    o2 = smod._EgoRoadObserver()
    assert o2.update(_bnd_clusters(0.9, 2.9), now=1000.0) == pytest.approx(-1.9)
    assert o2.update(_bnd_clusters(1.0, 3.0), now=1000.05) == pytest.approx(-2.0)


def test_ego_road_gate_sides2_and_slot_ttl():
    """读数契约门：消费方须 sides==2 且两侧在场——单侧/无效不吃当帧值,
    0.4s 内最近有效对保鲜兜底（pair_slot）,过期退 none（13:29 黑视教训:
    兜底优先,单侧安全包线是 P1）。"""
    o = smod._EgoRoadObserver()
    assert o.update(_bnd_clusters(-2.9, -0.9), now=1000.0) == pytest.approx(+1.9)
    assert o.update(_bnd_clusters((5.0,), sides=1), now=1000.05) == pytest.approx(+1.9)
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


def test_ego_road_median_filter_kills_outlier_pair():
    """第七轮抽风转向供数面修正：ro 单对野值（一侧找边偶发锁错结构，实机
    相邻拍 |Δro|>0.8 有 8~14 次）直注规划层 α/β 修正=抽风根因。近 3 对中值
    杀单对野值；窗口未满（前两拍）直喂最新对、与旧版逐值同形；垃圾对不入窗。"""
    o = smod._EgoRoadObserver()
    # p1: 干净对（窗口 1，直喂）
    assert o.update(_bnd_clusters(-2.9, -0.9), now=1000.0) == pytest.approx(+1.9)
    # p2: 右缘野值（窗口 2 不足，直喂当拍——单发噪声由规划层新息门兜底）
    assert o.update(_bnd_clusters(-2.9, +0.7), now=1000.05) == pytest.approx(+1.1)
    # p3: 干净对（窗口满 3，中值右缘 = median(-0.9, +0.7, -0.9) = -0.9）→ 野值被杀
    assert o.update(_bnd_clusters(-2.9, -0.9), now=1000.10) == pytest.approx(+1.9)
    # 垃圾对不毒化窗口：随后一对仍按干净窗口中值出数
    assert o.update(_bnd_clusters(-6.0, -2.0), now=1000.15) is None
    assert o.update(_bnd_clusters(-2.9, -0.9), now=1000.20) == pytest.approx(+1.9)


def test_ego_road_reuse_tick_consumes_no_new_evidence():
    """驻留复用拍（take() 的 is_new=False）不重复入中值窗、不喂 W、不刷新槽龄
    ——窗只认新证据（使用次数≠学习次数）。实机 10-04 两局取证：读数有效到达
    ~4.5Hz，同一读数 age 闸内被逐拍重复计入窗，窗内独立读数退化为 1~2 个
    （79 次 >0.3 道 ro 台阶 0 次在新读数拍、67 次旧值停滞≥2 拍后跳）——真变化
    滞后 ~0.3s 台阶跳变，单对野值凭重复入窗赢得中值。复用拍输出=槽值，
    source 记 pair_slot。"""
    o = smod._EgoRoadObserver()
    o.update(_bnd_clusters(-2.9, -0.9), now=1000.00)
    o.update(_bnd_clusters(-2.9, -0.9), now=1000.05)
    base = o.update(_bnd_clusters(-2.9, -0.9), now=1000.10)   # 窗满
    n_win, n_w = len(o._win), len(o._w_samples)
    for k in range(1, 5):                                     # 驻留重投 4 拍
        assert o.update(_bnd_clusters(-2.9, -0.9), now=1000.10 + 0.05 * k,
                        new=False) == pytest.approx(base)
        assert o.last["source"] == "pair_slot"
    assert len(o._win) == n_win and len(o._w_samples) == n_w
    # 右缘野值（新对）：满窗中值杀掉；随后野值重投不能再凭重复入窗翻面
    killed = o.update(_bnd_clusters(-2.9, +0.7), now=1000.35)
    assert killed == pytest.approx(1.9)   # median(-0.9,-0.9,+0.7)=-0.9
    for k in range(1, 4):
        assert o.update(_bnd_clusters(-2.9, +0.7), now=1000.40 + 0.05 * k,
                        new=False) == pytest.approx(killed)
    assert len(o._win) == 3


def test_control_chain_ego_road_gap_semantics():
    """chain 的 ego_road 即 pair 语义（无分档、无目标锁定机器）。"""
    m = _module()
    o = m._build_control_chain()["ego_road"]
    assert o.OFF_MAX == 3.0 and not hasattr(o, "retarget")


# ---------- 路宽 W 统计（阶段二 §一.3 兜底界）：找边对宽的中位数 + ±40% 门 ----------

def test_ego_road_width_forms_from_clean_pairs():
    """W=中值滤波后双侧对宽 (R−L) 的中位数；样本满 W_MIN_SAMPLES 才成形。
    语义=碰撞边界到碰撞边界（RULES：黄线外台阶），车道单位。"""
    o = smod._EgoRoadObserver()
    for k in range(smod._EgoRoadObserver.W_MIN_SAMPLES):
        o.update(_bnd_clusters(-2.23, +2.23), now=1000.0 + 0.05 * k)
    assert o.width == pytest.approx(4.46)
    assert o.last["width"] == pytest.approx(4.46)   # debug 数据面同值


def test_ego_road_width_none_before_enough_samples():
    o = smod._EgoRoadObserver()
    o.update(_bnd_clusters(-2.23, +2.23), now=1000.0)
    o.update(_bnd_clusters(-2.23, +2.23), now=1000.05)
    assert o.width is None and o.last["width"] is None


def test_ego_road_width_guard_rejects_pollution():
    """成形后 |对宽−W|>±40% 的污染对不进统计（找边锁错结构时 W 不被拽走）。"""
    o = smod._EgoRoadObserver()
    for k in range(smod._EgoRoadObserver.W_MIN_SAMPLES):
        o.update(_bnd_clusters(-2.23, +2.23), now=1000.0 + 0.05 * k)
    for k in range(10):                              # 宽 8.9 道的污染对
        o.update(_bnd_clusters(-4.45, +4.45), now=1100.0 + 0.05 * k)
    assert o.width == pytest.approx(4.46)


def test_ego_road_width_ignores_garbage_and_single_side():
    """垃圾对（|off|>OFF_MAX）与单侧拍不进宽度统计（与供数门同一入口纪律）。"""
    o = smod._EgoRoadObserver()
    for k in range(smod._EgoRoadObserver.W_MIN_SAMPLES + 5):
        o.update(_bnd_clusters(-6.0, -2.0), now=1000.0 + 0.05 * k)   # off=+4 垃圾
    o.update(_bnd_clusters((5.0,), sides=1), now=1100.0)             # 单侧
    assert o.width is None


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
