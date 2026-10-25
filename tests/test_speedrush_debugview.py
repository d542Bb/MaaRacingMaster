# -*- coding: utf-8 -*-
"""debugview 单测：双联渲染 None 安全 / BEV 三态着色 / 反投影口径 / 节流落盘。

口径锚：BEV 反投影 Z=c/(v−a·u−b) 与 depth_geo 平面拟合同式；栅格坐标与
_grid_penalty 同源（x 原点=相机光轴）。渲染是纯函数、写盘走 tmp_path，
测试不落用户 debug 目录。
"""

import numpy as np
import pytest
from pathlib import Path

from maaracing_master.plugins.speedrush import debugview as dv
from maaracing_master.plugins.speedrush.debugview import (
    DebugPairWriter, _project_detection, render_hud)
from maaracing_master.plugins.speedrush.depth_geo import (
    GRID_BLOCKED, GRID_DRIVABLE, GRID_NX, GRID_NZ, GRID_UNKNOWN,
    DepthRoadReading, DrivableGrid)
from maaracing_master.plugins.speedrush.hud import HudObserver
from maaracing_master.plugins.speedrush.perception import (
    Detection, PerceptionResult)
from maaracing_master.plugins.speedrush.world_model import Calib

W, H = 1280, 720


@pytest.fixture()
def frame():
    return np.zeros((H, W, 3), np.uint8)


def _grid(state_val: int = GRID_BLOCKED) -> DrivableGrid:
    return DrivableGrid(
        state=np.full((GRID_NZ, GRID_NX), state_val, np.int8),
        coef=(0.0, 0.0, 0.72),
        counts=np.full((GRID_NZ, GRID_NX), 9, np.int32),
        dig_cells=0, latency_ms=1.0)


def _reading() -> DepthRoadReading:
    return DepthRoadReading(
        left_edge_lane=-2.0, right_edge_lane=2.0, left_x=100, right_x=1100,
        sides=2, latency_ms=1.0, rejects=(), edge_pts=(),
        coef=(0.0, 0.0, 0.72), fx=1000.0, fy=1000.0)


def _snap(**over):
    snap = {"fid": 42, "phase": 1,
            "result": PerceptionResult(
                frame_id=42, ts_ns=0,
                cars=[Detection(cx=640, cy=300, w=80, h=60, conf=0.9)],
                coins=[Detection(cx=400, cy=500, w=20, h=20, conf=0.8)],
                bonuses=[]),
            "reading": _reading(), "grid": _grid(),
            "road_offset": 0.3,
            "decision": {"state": "CHANGE", "reason": "coin", "steer": 1234,
                         "executed_lane": 0.4, "x_target": 1.1,
                         "sel_layer": 3, "sel_why": "coin", "sel_nwp": 2,
                         "road_offset": 0.3, "dgeo_age": 12, "dgeo_new": True},
            "hud": {"fields": {"score_a": 12345, "rate_a": 310,
                               "score_b": 9900, "rate_b": 275}},
            "n_groups": 3}
    snap.update(over)
    return snap


# ---------- 渲染 ----------

def test_render_hud_none_safe(frame):
    """全空快照不炸：无 reading/grid/decision/result 都走诚实降级路径。"""
    out = render_hud(frame, {"fid": 1, "phase": 0}, None)
    assert out.shape[:2] == (H, W + dv._PANEL_W)


def test_render_hud_full_snapshot_paints_bev(frame):
    """全量快照：栅格全域 blocked → BEV 绘图区内像素呈 blocked 红。"""
    out = render_hud(frame, _snap(frame_shape=(H, W, 3)), None)
    assert out.shape[:2] == (H, W + dv._PANEL_W)
    # 绘图区内部取点（避开刻度线：z≈8m、x≈1m 处），应为 blocked 红
    y, x = int(H * dv._PLOT_Y1_FRAC) - 60, W + 320
    b, g, r = out[y, x]
    assert (int(b), int(g), int(r)) == (50, 50, 220)


def test_render_hud_bev_orientation_near_bottom_far_top(frame):
    """BEV 朝向锁：近端行（iz0）渲染在画布**底**、远端在顶——与 depth_geo
    取证面板 z3(bottom)..z16(top) 同约定，自车标（画在底）落在近端。
    顶带 z≈39.3m 已在栅格采集窗（16m）之外=目检延伸区深灰（38,38,38）。"""
    st = np.full((GRID_NZ, GRID_NX), GRID_UNKNOWN, np.int8)
    st[:4] = GRID_DRIVABLE          # z 3~4.1m 近端带可走，其余未知
    g = DrivableGrid(state=st, coef=(0.0, 0.0, 0.72),
                     counts=np.full((GRID_NZ, GRID_NX), 9, np.int32),
                     dig_cells=0, latency_ms=1.0)
    out = render_hud(frame, _snap(grid=g, frame_shape=(H, W, 3)), None)
    x = W + 320                     # x≈+1m：避开 0/5 竖刻度线
    b, gr, r = out[int(H * dv._PLOT_Y1_FRAC) - 8, x]
    assert (int(b), int(gr), int(r)) == (70, 140, 70)     # 底带=近端可走绿
    b, gr, r = out[dv._TITLE_H + 14, x]
    assert (int(b), int(gr), int(r)) == (38, 38, 38)      # 顶带=目检延伸区


def test_render_hud_bev_grid_limit_dashed_line(frame):
    """程序接受域上限：z=GRID_Z_HI 采集窗顶画亮黄虚线，线以上只画不收（维护者
    要求：目检延伸区与供数域一眼可分）。虚线上=延伸区底、虚线下=栅格带。"""
    out = render_hud(frame, _snap(frame_shape=(H, W, 3)), None)
    plot_y1 = int(H * dv._PLOT_Y1_FRAC)
    sy = (plot_y1 - (dv._TITLE_H + 6)) / ((dv._RENDER_Z_HI - 3.0) / 0.25)
    y_limit = int(plot_y1 - (27.0 / 0.25) * sy)      # z=GRID_Z_HI 行
    row = out[y_limit, W:]
    lim = np.all(np.abs(row.astype(int) - dv._LIMIT_BGR) <= 2, axis=-1)
    assert lim.any(), "采集窗上限虚线应在场"
    assert int(out[y_limit - 6, W + 320, 0]) == 38
    assert int(out[y_limit + 6, W + 320, 0]) != 38


def test_render_hud_bev_raw_center_c0_drawn(frame):
    """c0=当帧原始路心 (L+R)/2·lane_w_m：与滤波 C 并排可辨（排障面）。
    L=-2/R=+2、ro=0.3 → c0 在自车轴（X=0）、C 在 X=-0.3 道，两线分离。"""
    cal = Calib(vpx=640.0, y_h=310.0, a_x=1.0,
                min_denom=10.0, lane_w_m=2.75)
    out = render_hud(frame, _snap(frame_shape=(H, W, 3)), cal)
    row = out[200, W:]                       # BEV 绘图区中部横扫
    c0 = np.all(np.abs(row.astype(int) - (80, 170, 255)) <= 2, axis=-1)
    assert c0.any()                          # c0 橙线在场


def test_render_hud_grid_none_shows_placeholder(frame):
    out = render_hud(frame, _snap(grid=None, frame_shape=(H, W, 3)), None)
    # 无栅格：绘图区只画边框不涂色，底色保持面板深灰
    assert int(out[200, W + 320, 0]) == 28


def test_render_hud_selection_none_shows_placeholder(frame):
    """选择层缺席 → 子带显式写 none（诚实弃权可见）。"""
    out = render_hud(
        frame,
        _snap(decision={"state": "CRUISE", "sel_layer": None},
              frame_shape=(H, W, 3)), None)
    assert out.shape[:2] == (H, W + dv._PANEL_W)


def test_render_hud_selection_overlay(frame):
    """选择层/机会/路点/中心带叠层可渲染（新调试图数据面）。"""
    from maaracing_master.plugins.speedrush.selection import (
        SelectionReport, Waypoint)
    snap = _snap(
        frame_shape=(H, W, 3),
        selection=SelectionReport(
            3, (Waypoint(2.0, 1.0, "coin"),), 1.0, "coin", True, True),
        sel_objects={"reward": [], "car": [], "coin": [(1.0, 2.0)]},
        sel_debug={"ego": 0.2, "target": 1.0, "c0": 0.0, "c8": 0.0,
                   "left": -2.0, "right": 2.0, "band": 0.5, "look": 8.0,
                   "wp": [[2.0, 1.0, "coin"]], "rw": [], "car": [],
                   "coin": [[1.0, 2.0]]})
    out = render_hud(frame, snap, None)
    assert out.shape[:2] == (H, W + dv._PANEL_W)


def test_render_hud_steer_history_strip(frame):
    """转向时间带：左右交替的历史 → 子带内出现右打绿段与左打橙段。"""
    hist = [(i, 24000 if i % 2 else -24000) for i in range(120)]
    out = render_hud(frame, _snap(frame_shape=(H, W, 3),
                                  steer_hist=hist), None)
    band = out[H - 100:H - 24, W:]
    assert (band[:, :, 1] > 150).any()
    assert (band[:, :, 2] > 150).any()


# ---------- 10fps / 人工对照输入带 / 车道线叠加 ----------

def test_debug_interval_is_10fps():
    """调试图节流定为 10fps：控制拍 20Hz，落盘密度减半。"""
    assert dv.DEBUG_FPS == 10
    assert dv.DEBUG_INTERVAL_S == pytest.approx(0.1)


def test_lane_line_x_reconstructs_fit():
    """回投公式自洽：由已知斜率/截距反解 L/theta，再逐点还原像素横坐标。"""
    import math
    cam_h, s, a = 1.469, 0.8, 100.0
    line = {"L": s * cam_h / dv.LANE_W,
            "theta": math.atan((dv.VPX - a - s * dv.Y_H) / dv.FOCAL)}
    for y in (500.0, 600.0, 700.0):
        assert dv._lane_line_x(line, cam_h, y) == pytest.approx(a + s * y)


def test_render_hud_lane_overlay_draws_line(frame):
    """线观测叠加：拟合线按参数回投到画面，近端像素着黄边界色。"""
    cam_h = 1.469
    line = {"L": 0.5, "theta": 0.0, "color": "y", "side": -1, "k": -2,
            "y0": 550, "y1": 645, "n": 10, "rms": 0.5}
    snap = {"fid": 1, "phase": 1, "lane": {"cam_h": cam_h, "state": "TRACK",
                                           "det_age_ms": 20.0, "lines": [line]}}
    out = render_hud(frame, snap, None)
    y = 640
    x = int(round(dv._lane_line_x(line, cam_h, y)))
    patch = out[y - 2:y + 3, max(0, x - 2):x + 3].astype(int)
    # 黄边界色 (B,G,R)=(0,220,255)：允许线宽/取整误差，按通道判
    hit = ((patch[..., 0] < 40) & (patch[..., 1] > 180)
           & (patch[..., 2] > 220)).any()
    assert hit, f"lane overlay should paint yellow near x={x}, y={y}"


def test_render_hud_input_band_pad_and_counterfactual(frame):
    """人工对照局：底部输入带同时画物理手柄杆量（主）与程序反事实（标记）。"""
    frame[:] = 255
    snap = _snap(frame_shape=(H, W, 3),
                 pad={"lx": 20000, "ly": 0, "rt": 255},
                 decision={"state": "CRUISE", "steer": -20000})
    out = render_hud(frame, snap, None)
    ybar = H - 64 + 20
    xa, xb = 66, W - 340
    cx, half = (xa + xb) // 2, (xb - xa) // 2
    xv = int(cx + (20000 / 32767.0) * half)
    b, g, r = (int(v) for v in out[ybar, xv])
    assert g > 150 and b < 80            # 右打：主条绿色
    # 程序反事实标记在左打侧，白色三角与主条分离
    xc = int(cx - (20000 / 32767.0) * half)
    assert out[ybar - 6:ybar + 5, xc - 2:xc + 3].max() > 180
    assert out[H - 8, 4].max() < 200     # 底带压暗，未盖到画面外


def test_render_hud_input_band_cmd_only_and_lane_none(frame):
    """自动局（无物理手柄快照）显示程序下发值；线观测缺席显式写 none。"""
    out = render_hud(frame, _snap(frame_shape=(H, W, 3),
                                  decision={"state": "CRUISE",
                                            "steer": -30000}), None)
    ybar = H - 64 + 20
    xa, xb = 66, W - 340
    cx, half = (xa + xb) // 2, (xb - xa) // 2
    xv = int(cx - (30000 / 32767.0) * half)
    b, g, r = (int(v) for v in out[ybar, xv])
    assert r > 150 and g > 60 and b < 60  # 左打：主条橙色
    assert out.shape[:2] == (H, W + dv._PANEL_W)


# ---------- 反投影口径 ----------

def test_project_detection_roundtrip():
    """平面 Y=c（a=b=0）时 Z=c/v：底中心 (640,540)、c=0.72 → (X=0, Z=4.0)。"""
    hit = _project_detection(640, 540, (0.0, 0.0, 0.72), 1000.0, 1000.0, W, H)
    assert hit is not None
    x, z = hit
    assert x == pytest.approx(0.0, abs=1e-9)
    assert z == pytest.approx(4.0, rel=1e-9)


def test_project_detection_rejects_out_of_domain():
    # Z≈18m 在渲染延伸域内（>栅格窗 16m、≤40m，标记照画供目检）；
    # Z≈72m 超渲染域但仍在深度锚点域（≤90m）→ 照常投影，消费端钉顶缘
    # +距离标注（远距感知可见面）；Z>90（超锚点域）与分母退化 → None
    hit = _project_detection(640, 400, (0.0, 0.0, 0.72), 1000.0, 1000.0, W, H)
    assert hit is not None and hit[1] == pytest.approx(18.0)
    hit = _project_detection(640, 370, (0.0, 0.0, 0.72),
                             1000.0, 1000.0, W, H)
    assert hit is not None and hit[1] == pytest.approx(72.0)
    assert _project_detection(640, 365, (0.0, 0.0, 0.72),
                              1000.0, 1000.0, W, H) is None
    assert _project_detection(640, 360, (0.0, 0.0, 0.72),
                              1000.0, 1000.0, W, H) is None


# ---------- 雷达叠层（墙折线） ----------

def test_edge_side_pts_splits_and_sorts():
    """edge_pts 分侧按 z 排序：side 过滤、乱序输入 → z 升序 (z, x)。"""
    eps = ((1, 8.0, 2.0), (-1, 6.0, -3.0), (1, 4.0, 1.5), (-1, 10.0, -4.0),
           (1, 12.0, 2.5))
    left = dv._edge_side_pts(eps, -1)
    assert left == [(6.0, -3.0), (10.0, -4.0)]
    right = dv._edge_side_pts(eps, 1)
    assert right == [(4.0, 1.5), (8.0, 2.0), (12.0, 2.5)]


# ---------- 写入器 ----------

def _writer(tmp_path, interval_s=0.0):
    return DebugPairWriter(tmp_path / "sess", interval_s=interval_s,
                           cal=None)  # cal=None → load_calib() 真源


def test_writer_saves_raw_hud_pair(tmp_path, frame):
    w = _writer(tmp_path)
    try:
        w.tick(frame, _snap())
        w.stop()
    finally:
        pass
    assert w.saved == 1 and w.errors == 0
    assert (tmp_path / "sess" / "raw" / "0001_raw.jpg").exists()
    assert (tmp_path / "sess" / "0001_hud.jpg").exists()


def test_writer_throttles_to_interval(tmp_path, frame):
    w = _writer(tmp_path, interval_s=10.0)
    for _ in range(5):            # 同一节流窗内连 tick：只首拍出队
        w.tick(frame, _snap())
    w.stop()
    assert w.saved == 1


def test_writer_none_frame_noop(tmp_path):
    w = _writer(tmp_path)
    w.tick(None, _snap())
    w.stop()
    assert w.saved == 0


def test_writer_keeps_steer_history_every_tick(tmp_path, frame):
    """节流只影响出图；转向历史按控制拍逐拍记录。"""
    w = _writer(tmp_path, interval_s=10.0)
    try:
        for i in range(5):
            w.tick(None if i % 2 else frame,
                   {"fid": i, "decision": {"steer": i * 100}})
    finally:
        w.stop()
    assert [v for _, v in w._steer_hist] == [0, 100, 200, 300, 400]


def test_writer_render_error_counted_not_raised(tmp_path, frame, monkeypatch):
    """渲染炸 → worker 计数不外泄（观测件不碰主路的守卫在 _loop 层）。"""
    def _boom(*a, **k):
        raise RuntimeError("boom")
    monkeypatch.setattr(dv, "render_hud", _boom)
    w = _writer(tmp_path)
    w.tick(frame, _snap())
    w.stop()
    assert w.errors == 1 and w.saved == 0
    assert (tmp_path / "sess" / "raw" / "0001_raw.jpg").exists()  # raw 先行落盘


# ---------- hud 最新行槽 ----------

def test_hud_latest_returns_copy():
    h = HudObserver(Path("."), frame_source=None, regions={})
    h._latest = {"fields": {"score_a": 1}}
    got = h.latest()
    assert got == {"fields": {"score_a": 1}}
    got["fields"]["score_a"] = 2          # 改副本不影响内部槽
    assert h.latest()["fields"]["score_a"] == 1


def test_hud_latest_none_before_first_sample():
    h = HudObserver(Path("."), frame_source=None, regions={})
    assert h.latest() is None
