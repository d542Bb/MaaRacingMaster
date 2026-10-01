# -*- coding: utf-8 -*-
"""3D 找边实证：候选 MoGe q4f16 点云上找马路牙子（kerb，高度台阶）与
护栏（wall，竖直高出簇），与人工金标边线对质。

逻辑（纯几何，零外观匹配，输出即"程序用 3D 数据找边"的候选形态）：
  1. 检测带内拟合路面平面 Y = aX + bZ + c（口径同历史；点云 Y 向下为正，
     高度取 hgt = ±(Y−平面) 使高出路面为正，符号由平面高 c 的符号定）；
  2. 按 Z 分箱（3→16m），每箱左右两侧分别从路心向外扫横向（0.25m 格）高度
     中位剖面（校准结论：路缘在点云里是缓坡爬升不是竖直台阶）：
     - 边位置统一口径 = 离地穿越：金标的边就是路面消失点，kerb 与 wall 两类
       通用。穿越取阈值（0.03m@3m 起每米 +5mm，跟平面残差与远场噪声同尺度）
       两格持续 + 必须先见到地面 + 相邻格线性插值细分；墙低带（0.3~0.6m）
       只作护栏/路牙的结构分类语义，不作位置（全簇中位偏外 0.5~3.7m）；
  3. 各箱读数按 Z 排序后：比较用对质 z 插值（不做直线外推），渲染用投影连线；
  4. 与金标对质只认近点：远点射线与地面交点在 30~50m 处敏感度爆炸，不兼裁判；
     对质 z 须落在探测器覆盖域内（各侧不同），金标按近远两点 3D 直线插值。

口径（2026-10-01 维护者拍板）：
  - 「边」= 可行驶路面消失处，即离地穿越本身是目标；宽肩帧金标（墙脚）与检出
    的差是口径差非误差（输出表中以「金标=墙脚」标注）。
  - 弯道不做特殊优化：逐 Z 箱独立读数无直线假设，天然描出弯道边线；弯道帧
    金标的 BEV 米制转换失真（ray-ground 走直路面平面），对质应回图像空间。
  - 空中结构（桥/天空，hgt≥7.5m 实测）按 SKY_HGT 剔除（阈与找边常量反向引用
    产线 depth_geo 常量区）；3D 检测用紧自车掩码（产线 v4 构造，仅车身矩形）。

选帧：按 (lcls, rcls) 组合取首帧 + 强制含一条 curve_cont 与一条 obstacle。
用法（仓库根，.venv）：
    python tools/experiments/speedrush_vision/probe_boundary3d.py
输出：<depth_review>/boundary3d/<stem>_bnd3d.png（三联：原图叠加 / 俯视网格 /
高度剖面）+ stdout 对质表。
"""
from __future__ import annotations

import csv
import sys
from pathlib import Path

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from maaracing_master.plugins.speedrush import depth_geo as dg  # noqa: E402
from tools.experiments.speedrush_vision import compare_depth_sources as cds  # noqa: E402
from tools.experiments.speedrush_vision import export_pointcloud_html as eph  # noqa: E402
from tools.experiments.speedrush_vision import moge2_post as mp  # noqa: E402

GOLD = Path(r"C:/Users/yomen/AppData/Roaming/MaaRacingMaster/data/speedrush/"
            r"depth_review/gold_labels.csv")
OUT = eph.OUT.parent / "boundary3d"
Q4_W = (eph.OUT.parent / "weights"
        / "moge2_vits_static_336x598_t1032_q4f16.onnx")

# 找边常量反向引用产线（深度几何 v4 换装后阈值真源在 depth_geo 常量区；
# 探针只复用不另立——否则两处必然漂移）。本探针仪器 = cloud_of/scan_side/
# 金标投影 + 可视化面板；姿态探针（probe_ego_pose）复用同一批仪器。
ZBIN = dg.ZBIN
X_MAX_M = dg.X_MAX_M
XBIN = np.arange(-X_MAX_M, X_MAX_M + 1e-9, dg.XBIN_DX)
MIN_BIN_PTS = dg.MIN_BIN_PTS
MIN_SIDE_PTS = dg.MIN_SIDE_PTS
MIN_BOX_PTS = dg.MIN_BOX_PTS
EDGE_HT = dg.EDGE_HT
EDGE_HT_SLOPE = dg.EDGE_HT_SLOPE
SKY_HGT = dg.SKY_HGT
WALL_LO, WALL_HI = 0.3, 0.6   # 墙低带高度窗（探针自持的结构分类语义，未上产线）
_IMG_XZ = 0.5   # 画面横边界比 |X|/Z=(W/2)/fx 的占位；main 每帧用点云实际
# （自动）焦距覆盖——等效 FOV 逐帧在变，锥线跟着逐帧画，静态内参已退役。
MIN_WALL_PTS = 15
COL_GOLD = (60, 220, 60)      # 绿 (RGB 画布)
COL_EDGE = (255, 255, 0)      # 黄 (RGB 画布)
COL_WALL = (255, 120, 60)     # 橙


def pick_frames(rows):
    seen: dict = {}
    order = []
    for r in rows:
        key = (r["lcls"], r["rcls"])
        if key not in seen:
            seen[key] = r
            order.append(r)
    for want in ("curve_cont", "obstacle"):
        extra = [r for r in rows if r["stratum"] == want and r not in order]
        if extra:
            order.append(extra[0])
    return order[:6]


def cloud_of(img, sess):
    points, mask, mscale = mp.forward(sess, mp.preprocess(img, 1280, 720), 1032)
    # 自动焦距（逐帧）是仲裁结论：强制游戏静态内参会让 cam_h 跨帧漂移
    # 2.5→3.9m（物理不可能），逐帧自估焦距则稳定在 ~2m——等效 FOV 逐帧在变，
    # 自动焦距吸收了它。返回逐帧 (fx, fy)（归一化→全幅像素），供视锥线、
    # 画内守卫与金标投影共用——3D 量的像素往返禁用任何写死焦距。
    res = mp.reconstruct(points, mask, mscale)
    pts = res["pts"].astype(np.float32)
    pts = np.stack([cv2.resize(pts[..., k], (1280, 720),
                               interpolation=cv2.INTER_LINEAR) for k in range(3)], -1)
    valid = cv2.resize(res["valid"].astype(np.float32), (1280, 720),
                       interpolation=cv2.INTER_NEAREST).astype(bool)
    pts[~valid] = np.nan
    fx = float(res["fx"]) * 1280
    fy = float(res["fy"]) * 720
    return pts[..., 0], pts[..., 1], pts[..., 2], fx, fy


def scan_side(xb, hb, side: int, zc: float):
    """单 Z 箱单侧：返回 (离地穿越 X, 墙低带 X, 剖面px, 剖面ph)，找不到为 nan。
    px/ph 已按 路心→外 排序，供剖面图渲染。side=+1 右 / -1 左。"""
    sel = (xb * side > 0.2) & (np.abs(xb) <= X_MAX_M) & np.isfinite(xb) & np.isfinite(hb)
    if sel.sum() < MIN_SIDE_PTS:
        return np.nan, np.nan, None, None
    xs, hs = xb[sel], hb[sel]
    prof_x, prof_h = [], []
    for i in range(len(XBIN) - 1):
        x0 = (XBIN[i] + XBIN[i + 1]) / 2
        if x0 * side <= 0 or abs(x0) > _IMG_XZ * zc:  # 画外格（u 越界）不参与
            continue
        m = (xs >= XBIN[i]) & (xs < XBIN[i + 1])
        if m.sum() >= MIN_BIN_PTS:
            prof_x.append(x0)
            prof_h.append(float(np.median(hs[m])))
    if len(prof_x) < 3:
        return np.nan, np.nan, None, None
    px = np.array(prof_x)
    ph = np.array(prof_h)
    srt = np.argsort(px * side)  # 路心 → 外
    px, ph = px[srt], ph[srt]
    thr = EDGE_HT + EDGE_HT_SLOPE * max(0.0, zc - 3.0)  # 平面残差随 Z 增长，阈值同尺度
    edge_x = np.nan
    g = -1
    for i in range(len(px)):  # 先找到地面（内侧格可能被自车/阴影抬高，跳过）
        if ph[i] <= thr:
            g = i
            break
    if g >= 0:
        for i in range(g + 1, len(px) - 1):
            if ph[i] > thr and ph[i + 1] > thr:  # 两格持续
                f = (thr - ph[i - 1]) / (ph[i] - ph[i - 1])
                edge_x = float(px[i - 1] + f * (px[i] - px[i - 1]))
                break
    wm = (hs > WALL_LO) & (hs <= WALL_HI)
    wall_x = float(np.median(xs[wm])) if wm.sum() >= MIN_WALL_PTS else np.nan
    return edge_x, wall_x, px * side, ph  # 剖面横轴统一为「向外距离」


def eval_at(samples, z: float):
    """离散 (z, x) 读数在 z 处线性插值；z 超出覆盖域 ±1m 视为不可评。"""
    if len(samples) < 2:
        return np.nan
    zs = np.array([s[0] for s in samples])
    if z < zs.min() - 1.0 or z > zs.max() + 1.0:
        return np.nan
    order = np.argsort(zs)
    return float(np.interp(z, zs[order], np.array([s[1] for s in samples])[order]))


def gold_ray_ground(u, v, coef, fx, fy, cx=640.0, cy=360.0):
    """金标像素射线与路面平面求交 → (X, Z)；射线近水平/不在前方时 nan。
    焦距/主点=同帧点云实际值（静态内参已退役）。"""
    a, b, c = coef
    denom = (v - cy) / fy - a * (u - cx) / fx - b
    if abs(denom) < 1e-4 or c == 0:
        return np.nan, np.nan
    z = c / denom
    if not (1.0 < z < 200):
        return np.nan, np.nan
    return (u - cx) * z / fx, z


def project_point(x, z, coef, fx, fy, cx=640.0, cy=360.0):
    """3D 路面点 → 像素（同帧焦距/主点；y 沿拟合平面）。"""
    a, b, c = coef
    y = a * x + b * z + c
    return (int(round(cx + x * fx / z)),
            int(round(cy + y * fy / z)))


def dashed_line(img, p0, p1, col, gap=6, seg=5):
    x0, y0 = p0
    x1, y1 = p1
    n = max(abs(x1 - x0), abs(y1 - y0))
    if n == 0:
        return
    for s in range(0, n, gap + seg):
        t0, t1 = s / n, min((s + seg) / n, 1.0)
        cv2.line(img, (int(x0 + (x1 - x0) * t0), int(y0 + (y1 - y0) * t0)),
                 (int(x0 + (x1 - x0) * t1), int(y0 + (y1 - y0) * t1)), col, 1)


def _hgt_rgb(h):
    """高出路面(m) → RGB：蓝=低于路面，灰=路面同高，
    灰→橙(0.3m)→红(0.7m)=抬升值，暗红=更高。hgt 为数组。"""
    h = np.asarray(h, np.float32)
    out = np.zeros(h.shape + (3,), np.uint8)
    lo = h < -0.06
    road = (~lo) & (np.abs(h) <= 0.06)
    out[lo] = (60, 60, 220)
    out[road] = (150, 150, 150)
    ab = (~lo) & (~road)
    if ab.any():
        ha = h[ab][:, None]
        t1 = np.clip((ha - 0.06) / 0.24, 0, 1)
        t2 = np.clip((ha - 0.30) / 0.40, 0, 1)
        g2o = np.array([150, 150, 150]) + t1 * (np.array([255, 120, 60]) - np.array([150, 150, 150]))
        o2r = np.array([255, 120, 60]) + t2 * (np.array([230, 30, 30]) - np.array([255, 120, 60]))
        out[ab] = np.where(ha <= 0.3, g2o, o2r).astype(np.uint8)
    hi = h > 0.7
    out[hi] = (150, 20, 20)
    return out


def _tv_frame(size, extent=None):
    """BEV 画布骨架：黑底 + 5m 网格刻度 + 画面边界线 + 底部中央自车标记。
    等比例：横向 ±12m，1m = W/24 px，纵深由高度定。返回 (tv, MU, MW, ZMAX)。
    extent=画面横边界比（None 则取当前 _IMG_XZ，逐帧随自动焦距）。"""
    W, H = size
    if extent is None:
        extent = _IMG_XZ
    PX = W / 24.0
    XMIN, ZMIN = -12.0, 0.0
    ZMAX = H / PX
    tv = np.zeros((H, W, 3), np.uint8)

    def MU(x):
        return np.clip(((np.asarray(x, np.float32) - XMIN) * PX).astype(int), 0, W - 1)

    def MW(z):
        return np.clip((H - (np.asarray(z, np.float32) - ZMIN) * PX).astype(int), 0, H - 1)

    for x in range(-10, 11, 5):
        cv2.line(tv, (MU(x), 0), (MU(x), H - 1), (60, 60, 60), 1)
        cv2.putText(tv, f"{x}m", (MU(x) + 2, H - 8), 0, 0.4, (160, 160, 160), 1)
    for z in range(5, int(ZMAX), 5):
        cv2.line(tv, (0, MW(z)), (W - 1, MW(z)), (60, 60, 60), 1)
        cv2.putText(tv, f"{z}m", (4, MW(z) - 3), 0, 0.4, (160, 160, 160), 1)
    for s in (1, -1):  # 画面横边界（逐帧随自动焦距，≈相机当前视锥）
        cv2.line(tv, (int(MU(s * extent)), int(MW(1.0))),
                 (int(MU(s * extent * ZMAX)), 0), (200, 200, 200), 1)
    cv2.putText(tv, "img edge", (W // 2 - 30, 14), 0, 0.4, (200, 200, 200), 1)
    # 自车标记（底部中央，白描边三角）
    apex, bl, br = (int(MU(0)), int(MW(1.0))), (int(MU(-0.7)), int(MW(0.1))), (int(MU(0.7)), int(MW(0.1)))
    cv2.fillPoly(tv, [np.array([apex, bl, br])], (30, 30, 30))
    cv2.polylines(tv, [np.array([apex, bl, br])], True, (220, 220, 220), 1)
    return tv, MU, MW, ZMAX


def _tv_overlay(tv, MU, MW, samples, gold_pts, caption):
    """金标绿线 + 检出黄线（黑描边压住点云）+ 面板说明文字。"""
    for (xn, zn), (xf, zf) in gold_pts:
        if np.isfinite(xn) and np.isfinite(xf):
            a, b = (int(MU(xn)), int(MW(zn))), (int(MU(xf)), int(MW(zf)))
            cv2.line(tv, a, b, (0, 0, 0), 5)
            cv2.line(tv, a, b, COL_GOLD, 2)
    for side in (1, -1):
        smp = sorted(samples.get(("edge", side), []))
        pl = [(int(MU(x)), int(MW(z))) for z, x in smp]
        for i in range(len(pl) - 1):
            cv2.line(tv, pl[i], pl[i + 1], (0, 0, 0), 5)
            cv2.line(tv, pl[i], pl[i + 1], COL_EDGE, 2)
        for p in pl:
            cv2.circle(tv, p, 4, (0, 0, 0), -1)
            cv2.circle(tv, p, 3, COL_EDGE, -1)
    cv2.putText(tv, caption, (6, 14), 0, 0.45, (180, 180, 180), 1)
    cv2.putText(tv, "yellow=detect  green=gold", (6, 30), 0, 0.45, (180, 180, 180), 1)


def draw_topview_rgb(X, Z, img, ego, samples, gold_pts, size=(480, 400), cull=None,
                     extent=None):
    """BEV 真彩色纹理：每个点涂原图像素颜色（无人机俯拍效果）。
    与高度图并排对照用——蓝车=蓝色斑、人行道=灰色带，可逐物验证方位无镜像。"""
    tv, MU, MW, ZMAX = _tv_frame(size)
    sf = np.isfinite(X) & np.isfinite(Z) & (Z > 0) & (Z < ZMAX)
    if cull is not None:
        sf &= ~cull
    pts = sf & (~ego)
    tv[MW(Z[pts]), MU(X[pts])] = img[pts]
    egp = sf & ego
    if egp.any():
        tv[MW(Z[egp]), MU(X[egp])] = (img[egp] * 0.25).astype(np.uint8)  # 自车压暗
    _tv_overlay(tv, MU, MW, samples, gold_pts, "BEV: real color, car at bottom")
    return tv


def draw_topview(X, Z, hgt, ego, coef, samples, gold_pts, size=(480, 400), cull=None,
                 extent=None):
    """BEV 高度图：每点按「高出路面」连续上色（蓝=低于路面、灰=路面、
    灰→橙→红=抬高 0→0.7m），右侧色标即高度尺。与真彩色面板并排同框同向。"""
    tv, MU, MW, ZMAX = _tv_frame(size)
    sf = np.isfinite(X) & np.isfinite(Z) & (Z > 0) & (Z < ZMAX) & np.isfinite(hgt)
    if cull is not None:
        sf &= ~cull
    egp = sf & ego
    pts = sf & (~ego)
    tv[MW(Z[pts]), MU(X[pts])] = _hgt_rgb(hgt[pts])
    if egp.any():
        tv[MW(Z[egp]), MU(X[egp])] = (40, 40, 40)
    _tv_overlay(tv, MU, MW, samples, gold_pts, "BEV: color = height")
    # 高度色标（右侧）
    BX, BW, BT, BB = tv.shape[1] - 70, 12, 18, tv.shape[0] - 22
    HMIN, HMAX = -0.15, 0.7
    for py in range(BT, BB):
        hv = HMAX - (py - BT) / (BB - BT) * (HMAX - HMIN)
        tv[py, BX:BX + BW] = _hgt_rgb(np.array([hv]))[0]
    cv2.rectangle(tv, (BX, BT), (BX + BW, BB), (200, 200, 200), 1)
    for hv, lab in ((0.0, "0"), (0.15, "0.15"), (0.3, "0.30"), (0.5, "0.50"), (0.7, "0.70")):
        py = int(BT + (HMAX - hv) / (HMAX - HMIN) * (BB - BT))
        cv2.line(tv, (BX + BW, py), (BX + BW + 3, py), (200, 200, 200), 1)
        cv2.putText(tv, lab, (BX + BW + 5, py + 4), 0, 0.35, (200, 200, 200), 1)
    cv2.putText(tv, "h(m)", (BX - 4, BB + 14), 0, 0.35, (200, 200, 200), 1)
    return tv


def draw_profiles(prof_data, gold_d, size=(960, 400)):
    """高度剖面图：每个侧一块面板，两档 Z（6m 近 / 14m 远）。
    横轴=向外距离(m)，纵轴=高出路面(m)；黄圈=穿越点，绿线=金标位置。"""
    W, H = size
    profiles = np.zeros((H, W, 3), np.uint8)
    cv2.rectangle(profiles, (0, 0), (W - 1, H - 1), (30, 30, 30), -1)
    panel_w = W // 2
    for j, side in ((0, -1), (1, 1)):
        name = "LEFT " if side < 0 else "RIGHT"
        ox, oy, pw, ph_ = j * panel_w + 46, 30, panel_w - 60, H - 70
        xmin, xmax, ymin, ymax = 0.0, 12.0, -0.10, 0.45

        def PX(v):
            return ox + int((v - xmin) / (xmax - xmin) * pw)

        def PY(v):
            return oy + ph_ - int((v - ymin) / (ymax - ymin) * ph_)

        cv2.line(profiles, (PX(0), PY(0)), (PX(xmax), PY(0)), (120, 120, 120), 1)
        for v in np.arange(ymin, ymax + 0.01, 0.1):
            dashed_line(profiles, (PX(xmin), PY(v)), (PX(xmax), PY(v)), (55, 55, 55))
            cv2.putText(profiles, f"{v:+.1f}", (ox - 40, PY(v) + 4), 0, 0.4, (150, 150, 150), 1)
        for v in range(0, 13, 2):
            cv2.putText(profiles, f"{v}", (PX(v) - 4, oy + ph_ + 16), 0, 0.4, (150, 150, 150), 1)
        cv2.putText(profiles, f"{name}  outward distance (m) / height above road (m)",
                    (ox - 40, oy - 12), 0, 0.5, (200, 200, 200), 1)
        for k, (zc, px, ph_arr) in enumerate(prof_data.get(side, [])):
            col = (255, 255, 255) if k == 0 else (255, 200, 80)
            thr = EDGE_HT + EDGE_HT_SLOPE * max(0.0, zc - 3.0)
            dashed_line(profiles, (PX(0), PY(thr)), (PX(xmax), PY(thr)), col)
            pts = [(PX(v), PY(h)) for v, h in zip(px, ph_arr)]
            for i in range(len(pts) - 1):
                cv2.line(profiles, pts[i], pts[i + 1], col, 2)
            cv2.putText(profiles, f"Z~{zc:.0f}m thr={thr:.2f}", (PX(7.5), PY(thr) - 5),
                        0, 0.42, col, 1)
            # 该档的穿越点
            smp = [(z, x) for z, x in _last_samples.get(("edge", side), [])
                   if abs(z - zc) < 1.6]
            for z, x in smp:
                cv2.circle(profiles, (PX(abs(x)), PY(thr)), 6, COL_EDGE, 2)
        gd = gold_d.get(side)
        if gd is not None and np.isfinite(gd):
            cv2.line(profiles, (PX(abs(gd)), PY(ymax)), (PX(abs(gd)), PY(ymin)), COL_GOLD, 2)
            cv2.putText(profiles, "GOLD", (PX(abs(gd)) + 3, PY(ymax) + 12), 0, 0.45, COL_GOLD, 2)
    return profiles


_last_samples: dict = {}


def main() -> None:
    rows = list(csv.DictReader(GOLD.open(encoding="utf-8")))
    frames = pick_frames(rows)
    # 3D 紧掩码：产线 v4 构造（仅车身矩形；列带下延的 2D 语义已随旧链退役）
    ego3d = dg.DepthRoadObserver._load_ego_mask()
    ego = ego3d
    sess = cds._moge_session(1032, wpath=Q4_W)
    OUT.mkdir(parents=True, exist_ok=True)

    print(f"{'帧':30s} {'侧':2s} {'类':5s} {'对质Z':>6s} {'检出X':>9s} {'墙带X':>7s} {'误差m':>7s}")
    for r in frames:
        stem = Path(r["path"]).stem
        img = cv2.cvtColor(cv2.imread(r["path"]), cv2.COLOR_BGR2RGB)
        X, Y, Z, fx_f, fy_f = cloud_of(img, sess)
        global _IMG_XZ
        _IMG_XZ = 0.5 / fx_f
        band = (slice(dg.Y0, dg.DIAG_Y1), slice(None))
        coef = eph.fit_road_plane(X[band], Y[band], Z[band], ego[band])
        if coef is None:
            print(f"{stem}  平面拟合失败，跳过")
            continue
        a, b, c = coef
        road_y0 = b * 5.0 + c
        sgn = -1.0 if road_y0 > 0 else 1.0
        hgt = sgn * (Y - (a * X + b * Z + c))  # 高出路面为正
        # 空中结构剔除：桥/天空域点 hgt 实测 ≥7.5m，与护栏（≤1m）间隙巨大，
        # 不剔会把远端 Z 箱中位数整体抬高、穿越在桥带内缘误触发。
        sky = np.isfinite(hgt) & (hgt > SKY_HGT)

        samples: dict = {}   # ("edge"/"wallband", side) → [(zc, x), ...]
        profiles: dict = {}  # side → [(zc, px, ph)]
        for z0, z1 in ZBIN:
            m = ((Z >= z0) & (Z < z1) & np.isfinite(X) & np.isfinite(hgt)
                 & (~ego3d) & (~sky))
            if m.sum() < MIN_BOX_PTS:
                continue
            zc = (z0 + z1) / 2
            for side in (1, -1):
                ex, wx, px, ph = scan_side(X[m], hgt[m], side, zc)
                if np.isfinite(ex):
                    samples.setdefault(("edge", side), []).append((zc, ex))
                if np.isfinite(wx):
                    samples.setdefault(("wallband", side), []).append((zc, wx))
                if px is not None and zc in (6.0, 14.0):
                    profiles.setdefault(side, []).append((zc, px, ph))
        _last_samples.clear()
        _last_samples.update(samples)

        gold_pts, gold_d = {}, {}
        vis = img.copy()
        for key in ("l", "r"):
            cls = r[f"{key}cls"]
            side_i = -1 if key == "l" else 1
            u_n, v_n = float(r[f"{key}_nx"]), float(r[f"{key}_ny"])
            u_f, v_f = float(r[f"{key}_fx"]), float(r[f"{key}_fy"])
            cv2.line(vis, (int(u_n), int(v_n)), (int(u_f), int(v_f)), COL_GOLD, 2)
            cv2.circle(vis, (int(u_n), int(v_n)), 5, COL_GOLD, -1)
            cv2.circle(vis, (int(u_f), int(v_f)), 5, COL_GOLD, -1)
            xn, zn = gold_ray_ground(u_n, v_n, coef, fx_f, fy_f)
            xf, zf = gold_ray_ground(u_f, v_f, coef, fx_f, fy_f)
            if np.isfinite(xn):
                gold_pts[side_i] = ((xn, zn), (xf, zf))
                # 对质：金标边线是 3D 直线；对质 z 取探测器覆盖域内、以近点为锚
                smp = samples.get(("edge", side_i), [])
                fx = gx = zj = np.nan
                if smp:
                    zmin = min(s[0] for s in smp)
                    zmax = max(s[0] for s in smp)
                    zj = min(max(zn, zmin), zmax)
                    fx = eval_at(smp, zj)
                    if np.isfinite(zf) and zf > zn:
                        gx = float(np.interp(zj, [zn, zf], [xn, xf]))
                        gold_d[side_i] = gx
                es = f"{abs(fx - gx):7.2f}" if np.isfinite(fx) and np.isfinite(gx) else "      —"
                xs_ = f"{fx:9.2f}" if np.isfinite(fx) else "  未检出"
                # 口径注记：金标若贴墙低带读数 = 金标标的是墙脚，
                # 与检出（路面消失处）的差是口径差，非检测误差
                ws = samples.get(("wallband", side_i), [])
                wx = eval_at(sorted(ws), zj) if ws else np.nan
                ws_ = f"{wx:7.2f}" if np.isfinite(wx) else "      —"
                tag = " ←金标=墙脚" if (np.isfinite(wx) and np.isfinite(gx)
                                        and abs(abs(wx) - abs(gx)) < 0.6) else ""
                print(f"{stem:30s} {key.upper():2s} {cls:5s} {zj:6.1f} {xs_} {ws_} {es}{tag}")
            for kind, col in (("edge", COL_EDGE), ("wallband", COL_WALL)):
                smp_l = sorted(samples.get((kind, side_i), []))
                if len(smp_l) >= 2:
                    pl = [project_point(x, z, coef, fx_f, fy_f) for z, x in smp_l]
                    for i in range(len(pl) - 1):
                        cv2.line(vis, pl[i], pl[i + 1], col, 2)

        # 链路结果可视化：检出缘按 Z 插值成连续线画回原图（黑描边黄线），
        # 并输出车道读数——这就是整条链路对一帧图的最终回答
        for side in (1, -1):
            smp = sorted(samples.get(("edge", side), []))
            if len(smp) >= 2:
                zs = [s[0] for s in smp]
                zg = np.arange(zs[0], zs[-1] + 0.01, 0.5)
                xg = np.interp(zg, zs, [s[1] for s in smp])
                pl = [project_point(float(x), float(z), coef, fx_f, fy_f)
                      for z, x in zip(zg, xg)]
                for i in range(len(pl) - 1):
                    cv2.line(vis, pl[i], pl[i + 1], (0, 0, 0), 6)
                    cv2.line(vis, pl[i], pl[i + 1], COL_EDGE, 3)
        r8 = {}
        for side in (1, -1):
            smp = sorted(samples.get(("edge", side), []))
            if smp and smp[0][0] <= 8.0 <= smp[-1][0]:
                r8[side] = eval_at(smp, 8.0)
        if len(r8) == 2:
            xl, xr = r8[-1], r8[1]
            msg = (f"z=8m  L:{xl:+.1f}m  R:{xr:+.1f}m  "
                   f"width:{xr - xl:.1f}m  lane-center off:{(xl + xr) / 2:+.2f}m")
            cv2.putText(vis, msg, (14, 30), 0, 0.75, (0, 0, 0), 4)
            cv2.putText(vis, msg, (14, 30), 0, 0.75, (255, 255, 255), 2)
            print(f"{stem:30s} 车道读数@8m: L={xl:+.2f} R={xr:+.2f} "
                  f"宽={xr - xl:.2f} 路心偏={(xl + xr) / 2:+.2f}m")

        gp_list = [gp for gp in (gold_pts.get(1), gold_pts.get(-1)) if gp is not None]
        tv = draw_topview(X, Z, hgt, ego3d, coef, samples, gp_list, cull=sky, extent=extent)
        tvr = draw_topview_rgb(X, Z, img, ego3d, samples, gp_list, cull=sky, extent=extent)
        pf = draw_profiles(profiles, gold_d)
        vis_s = cv2.resize(vis, (960, 540))
        canvas = np.vstack([vis_s, np.hstack([tvr, tv]), pf])
        ok, buf = cv2.imencode(".png", cv2.cvtColor(canvas, cv2.COLOR_RGB2BGR))
        (OUT / f"{stem}_bnd3d.png").write_bytes(buf.tobytes())
    print(f"\n图输出：{OUT}")
    print("面板1: 绿=金标边线 黄=检出缘 橙=墙低带 | 面板2: 俯视网格(近在下,白线=视场边界) "
          "| 面板3: 高度剖面(白=Z6m 橙=Z14m, 黄圈=穿越, 绿线=金标)")


if __name__ == "__main__":
    main()
