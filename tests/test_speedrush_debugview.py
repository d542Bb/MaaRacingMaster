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
from maaracing_master.plugins.speedrush.latsample import SampledTraj
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


def _plan() -> SampledTraj:
    return SampledTraj(d1=1.2, T=2.0, cost=3.5,
                       coeffs=(0.0, 0.6, 0.0, 0.0, 0.0, 0.0))


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
                         "road_offset": 0.3, "dgeo_age": 12, "dgeo_new": True},
            "hud": {"fields": {"score_a": 12345, "rate_a": 310,
                               "score_b": 9900, "rate_b": 275}},
            "plan": _plan(), "diag": {"ok": True, "why": "", "n_grid": 2},
            "n_groups": 3}
    snap.update(over)
    return snap


# ---------- 渲染 ----------

def test_render_hud_none_safe(frame):
    """全空快照不炸：无 reading/grid/plan/result 都走诚实降级路径。"""
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
    """程序接受域上限：z=16m 采集窗顶画亮黄虚线，线以上只画不收（维护者
    要求：目检延伸区与供数域一眼可分）。虚线上=延伸区底、虚线下=栅格带。"""
    out = render_hud(frame, _snap(frame_shape=(H, W, 3)), None)
    plot_y1 = int(H * dv._PLOT_Y1_FRAC)
    sy = (plot_y1 - (dv._TITLE_H + 6)) / ((dv._RENDER_Z_HI - 3.0) / 0.25)
    y_limit = int(plot_y1 - (13.0 / 0.25) * sy)      # z=16m 行
    row = out[y_limit, W:]
    lim = np.all(np.abs(row.astype(int) - dv._LIMIT_BGR) <= 2, axis=-1)
    assert lim.any(), "采集窗上限虚线应在场"
    assert int(out[y_limit - 6, W + 320, 0]) == 38
    assert int(out[y_limit + 6, W + 320, 0]) != 38


def test_render_hud_bev_raw_center_c0_drawn(frame):
    """c0=当帧原始路心 (L+R)/2·lane_w_m：与滤波 C 并排可辨（排障面）。
    L=-2/R=+2、ro=0.3 → c0 在自车轴（X=0）、C 在 X=-0.3 道，两线分离。"""
    cal = Calib(vpx=640.0, y_h=310.0, a_x=1.0, v_ego=660.0, ego_cx=640.0,
                min_denom=10.0, lane_w_m=2.75)
    out = render_hud(frame, _snap(frame_shape=(H, W, 3)), cal)
    row = out[200, W:]                       # BEV 绘图区中部横扫
    c0 = np.all(np.abs(row.astype(int) - (80, 170, 255)) <= 2, axis=-1)
    assert c0.any()                          # c0 橙线在场


def test_render_hud_grid_none_shows_placeholder(frame):
    out = render_hud(frame, _snap(grid=None, frame_shape=(H, W, 3)), None)
    # 无栅格：绘图区只画边框不涂色，底色保持面板深灰
    assert int(out[200, W + 320, 0]) == 28


def test_render_hud_plan_none_shows_reason(frame):
    """plan 缺席 + diag.why → 子带显式写原因（诚实弃权可见）。"""
    out = render_hud(frame, _snap(plan=None, frame_shape=(H, W, 3)), None)
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
    # Z≈72m 超渲染域 → None；分母退化（v=a·u+b）→ None
    hit = _project_detection(640, 400, (0.0, 0.0, 0.72), 1000.0, 1000.0, W, H)
    assert hit is not None and hit[1] == pytest.approx(18.0)
    assert _project_detection(640, 370, (0.0, 0.0, 0.72),
                              1000.0, 1000.0, W, H) is None
    assert _project_detection(640, 360, (0.0, 0.0, 0.72),
                              1000.0, 1000.0, W, H) is None


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
