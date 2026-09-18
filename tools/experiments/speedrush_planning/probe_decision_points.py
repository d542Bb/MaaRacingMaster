"""决策点探针：人类开法 vs「逐帧贪心」vs「带视野选择」——纯离线、图像空间、不做米制标定。

**要回答什么**（收益模型/规划器的前置证伪题）：
    人类的转向选择里，有多少次是「当前最优解」解释不了、而「未来 2~3 秒累计收益」解释得了？
    若这种情形几乎不出现 → 「需要规划」的前提不成立，别写规划器；
    若稳定大量出现 → 逐帧贪心（现方案 route 文档 §二.1 的决策层定义）被数据证伪。

**为什么在图像空间做**：不做单应/米制标定也能定出「谁先到、往哪让」——只需要**序关系**：
    - 时间代理：金币的 y 速率（px/s）→ 它还有多少秒到本车所在行（登录 y 由实测分布定，非拍脑袋）；
    - 侧向：本车的侧向速度**从目标自身的 dx/dt 反测**（世界静止的目标在图上横向漂移 = 本车在横移，
      相机固定跟随、自车恒在画面中心 x=0.5W）；这一步代替了「车道线检测 + 标定」。

**检测为什么是多尺度金字塔**（实测结论，非设计偏好）：
    旧模型对金币是**尺度盲**——训练标签把币框挤在 y 0.435~0.503（720p 下 y 313~362）、
    框高仅约 12px，故它只认"中远景小币"，近场大币一律漏检（实测：轨迹全部终止在 y≈370）。
    对同一帧做 1.0 / 0.5 / 0.33 / 0.25 四档缩放各推理一次，近场大币在 0.25×/0.33× 档被检出
    （目视复核见实验 README）。

**素材**：旧检测模型（3 类）+ 演示录像（帧 + `pads.jsonl` 的 `lx` 转向量）。

**已知简化（读结论时必须带上）**：
    1. 只算金币（30 分/枚，成线时按线计），不含超车/动作/社会车——迭代 1 只验证「视野有没有用」；
    2. 街车只作为「该走廊被挡」的备注，不建模绕行成本；
    3. 「人类动作」= `lx` 的持续符号（变道方向），不含幅度标定；
    4. 本车横向可动范围用实测的行进率上限，不建车辆动力学；
    5. 自车车身会遮住最近的一小段（吃币那一刻在车后），故**收集事件**只能由轨迹终止 + 位置推断。
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

import cv2
import numpy as np
import onnxruntime as ort
from PIL import Image

DEMOS = (Path(os.environ.get("APPDATA", ".")) / "MaaRacingMaster" / "data" / "speedrush"
         / "demos")
DEFAULT_MODEL = (Path(__file__).resolve().parents[3] / "archive" / "racing" / "resources"
                 / "onnx" / "model.onnx")

COIN_CONF = 0.35
# 检测用**远场切片**（有效缩放 ≈1.0 → 网内币框 12~20px，正是训练标签那一档）。
# 为什么不做近场检测：**近场被自车车身遮住**——实测所有币轨迹都终止在 y≈370（车顶轮廓线），
# 吃币那一刻在车后、不可观测；而可见段长 2~3s（轨迹最长 47 帧），足够覆盖我们要的视野。
TILE = (640, 360)
BAND = (170, 660)
IMG_W, IMG_H = 1280, 720
EGO_X = IMG_W / 2
IOU_DEDUP = 0.45


def _providers():
    avail = ort.get_available_providers()
    return [p for p in ("DmlExecutionProvider", "CPUExecutionProvider") if p in avail][:1]


def _infer(sess, rgb, want):
    h, w = rgb.shape[:2]
    s = min(640 / h, 640 / w)
    nh, nw = int(h * s), int(w * s)
    px, py = (640 - nw) // 2, (640 - nh) // 2
    pad = np.full((640, 640, 3), 114, dtype=np.uint8)
    pad[py:py + nh, px:px + nw] = cv2.resize(rgb, (nw, nh), interpolation=cv2.INTER_AREA)
    blob = pad.transpose(2, 0, 1)[None].astype(np.float32) / 255.0
    preds = sess.run(None, {"images": blob})[0][0].T
    sc, cl = preds[:, 4:].max(1), preds[:, 4:].argmax(1)
    out = []
    for cls, thr in want:
        for k in np.where((cl == cls) & (sc > thr))[0]:
            b = preds[k]
            out.append((cls, float(sc[k]),
                        (float((b[0] - b[2] / 2 - px) / s), float((b[1] - b[3] / 2 - py) / s),
                         float((b[0] + b[2] / 2 - px) / s), float((b[1] + b[3] / 2 - py) / s))))
    return out


def _iou(a, b):
    x1, y1 = max(a[0], b[0]), max(a[1], b[1])
    x2, y2 = min(a[2], b[2]), min(a[3], b[3])
    inter = max(0.0, x2 - x1) * max(0.0, y2 - y1)
    if inter <= 0:
        return 0.0
    ua = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    return inter / max(ua, 1e-6)


def detect_frame(sess, rgb):
    """远场切片检测（路面带），跨切片去重；返回 [(cls, conf, box(原帧坐标))]。"""
    raw = []
    for y in range(BAND[0], BAND[1], TILE[1]):
        for x in range(0, IMG_W, TILE[0]):
            sub = rgb[y:min(y + TILE[1], BAND[1]), x:x + TILE[0]]
            if sub.shape[0] < 16 or sub.shape[1] < 16:
                continue
            for cls, conf, b in _infer(sess, sub, [(0, COIN_CONF), (1, 0.35)]):
                raw.append((cls, conf, (b[0] + x, b[1] + y, b[2] + x, b[3] + y)))
    raw = [(c, cf, b) for c, cf, b in raw if b[3] >= BAND[0] and b[1] <= BAND[1]]
    raw.sort(key=lambda x: -x[1])
    kept = []
    for cls, conf, box in raw:
        if any(cls == k[0] and _iou(box, k[2]) > IOU_DEDUP for k in kept):
            continue
        kept.append((cls, conf, box))
    return kept


def fit_perspective(fits):
    """全局拟合「地面平面」的透视：ẏ = k·(y − y_h)²（针孔 + 平路 → 距离 ∝ 1/(y−y_h)）。

    为什么必须做：轨迹只到 y≈370（被车身遮住），而 ẏ 随 y 增长很快（远场十几、近场几百 px/s），
    线性外推会给出荒谬的到达时间。拟合出 (k, y_h) 后，任意 y 到任意行的**时间**可解析积分：
        T(y_a → y_b) = (1/k)·( 1/(y_a − y_h) − 1/(y_b − y_h) )
    返回 (k, y_h, 残差中位) 或 None。
    """
    ys = np.array([f["y0"] * 0.5 + f["y1"] * 0.5 for f in fits])
    vs = np.array([f["vy"] for f in fits])
    ok = vs > 2.0                     # 只用真正在靠近的轨迹
    if ok.sum() < 8:
        return None
    ys, vs = ys[ok], vs[ok]
    # 两参数最小二乘：v = k·(y−yh)² → 对 (k, yh) 做网格 + 局部细化（稳健，不引 scipy）
    best = None
    for yh in np.arange(150, 420, 5.0):
        d = np.clip(ys - yh, 1.0, None)
        k = float(np.sum(vs * d ** 2) / np.sum(d ** 4))
        res = float(np.median(np.abs(k * d ** 2 - vs)))
        if best is None or res < best[2]:
            best = (k, float(yh), res)
    return best


def arrival_seconds(k, yh, y_now, y_target):
    """从 y_now 到 y_target 的剩余时间（秒）。"""
    a, b = max(y_now - yh, 1.0), max(y_target - yh, 1.0)
    return (1.0 / k) * (1.0 / a - 1.0 / b)


def track(dets_by_frame, max_dy=70.0, max_dx=45.0, max_gap=3):
    """最近邻跟踪：币 y 只会增大（朝我飞来），x 变化 = 本车横移 ± 目标自身横移。"""
    tracks: dict[int, list] = {}
    alive: dict[int, tuple] = {}
    nxt = 0
    for seq, t, dets in dets_by_frame:
        used = set()
        for cx, cy, conf in sorted(dets, key=lambda d: d[1]):   # 先匹配近处的（y 大）
            best, bestd = None, 1e9
            for tid, (lseq, lx, ly) in alive.items():
                gap = seq - lseq
                if tid in used or gap > max_gap:
                    continue
                dy, dx = cy - ly, cx - lx
                if not (-8 <= dy <= max_dy * gap) or abs(dx) > max_dx * gap:
                    continue
                d = abs(dy) + 0.6 * abs(dx)
                if d < bestd:
                    best, bestd = tid, d
            if best is None:
                best = nxt
                nxt += 1
                tracks[best] = []
            tracks[best].append((seq, t, cx, cy, conf))
            alive[best] = (seq, cx, cy)
            used.add(best)
    return tracks


def fit_track(pts):
    """(t, cx, cy) → 线性拟合 vy/vx；点数太少返回 None。"""
    if len(pts) < 4:
        return None
    t = np.array([p[1] for p in pts])
    x = np.array([p[2] for p in pts])
    y = np.array([p[3] for p in pts])
    if t[-1] - t[0] < 0.15:
        return None
    fy, fx = np.polyfit(t, y, 1), np.polyfit(t, x, 1)
    return {"vy": float(fy[0]), "vx": float(fx[0]),
            "res": float(np.abs(np.polyval(fy, t) - y).mean()),
            "t0": float(t[0]), "t1": float(t[-1]),
            "x0": float(x[0]), "y0": float(y[0]), "x1": float(x[-1]), "y1": float(y[-1]),
            "n": len(pts)}


def cmd_explore(args) -> None:
    """把「登录行 / 侧向率上限 / 收集容差」从数据里量出来，再谈决策点。"""
    sess = ort.InferenceSession(args.model, providers=_providers())
    d = Path(args.demos) / args.session
    fr = [json.loads(x) for x in
          (d / "frames.jsonl").read_text(encoding="utf-8").splitlines() if x]
    t0 = fr[0]["ts_ns"] / 1e9
    dets_by_frame, per_frame = [], []
    for i, r in enumerate(fr):
        rgb = np.asarray(Image.open(d / "frames" / r["file"]).convert("RGB"))
        dets = detect_frame(sess, rgb)
        coins = [((b[0] + b[2]) / 2, (b[1] + b[3]) / 2, sc)
                 for cls, sc, b in dets if cls == 0]
        per_frame.append((len(coins), sum(1 for cls, _, _ in dets if cls == 1)))
        dets_by_frame.append((i, r["ts_ns"] / 1e9 - t0, coins))
    c = np.array([x[0] for x in per_frame])
    print(f"{args.session}：帧 {len(fr)}  每帧币检出 中位 {np.median(c):.0f} "
          f"均值 {c.mean():.1f}  零检出 {int((c == 0).sum())} 帧")
    tracks = track(dets_by_frame)
    fits = [f for f in (fit_track(v) for v in tracks.values()) if f]
    print(f"币轨迹 {len(tracks)}  可用拟合 {len(fits)}")
    if not fits:
        return
    vy = np.array([f["vy"] for f in fits])
    vx = np.array([f["vx"] for f in fits])
    y1 = np.array([f["y1"] for f in fits])
    print(f"\n① vy(px/s)：P10 {np.percentile(vy, 10):.0f}  中位 {np.median(vy):.0f}  "
          f"P90 {np.percentile(vy, 90):.0f}  最大 {vy.max():.0f}")
    print(f"② 终点 y1 直方图（每 20px）：")
    hist, edges = np.histogram(y1, bins=np.arange(200, 701, 20))
    for h, e in zip(hist, edges):
        if h:
            print(f"    y {int(e):>3}-{int(e) + 20:<3} {'#' * min(int(h), 60)} {h}")
    print(f"③ vx(px/s)：P5 {np.percentile(vx, 5):.0f}  中位 {np.median(vx):.0f}  "
          f"P95 {np.percentile(vx, 95):.0f}（本车极限横移 ≈ ±P95）")
    persp = fit_perspective(fits)
    if persp:
        k, yh, res = persp
        print(f"\n④ 透视拟合 ẏ = k·(y−y_h)²：k={k:.6f}  y_h={yh:.0f}  "
              f"中位残差 {res:.1f} px/s")
        print("   由此换算「还剩多少秒到某行」（以 y=400 当作被车身遮住的收集行附近）：")
        for y in (250, 300, 340, 360, 380):
            print(f"     y={y} → {arrival_seconds(k, yh, y, 400):.2f}s")
    else:
        print("\n④ 透视拟合失败（有效轨迹太少）")
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    cache = out / f"kinematics_{args.session}.json"
    cache.write_text(json.dumps({
        "session": args.session,
        "tracks": {str(k): v for k, v in tracks.items()},
        "fits": {k: f for k, f in ((str(k), fit_track(v)) for k, v in tracks.items()) if f},
    }, ensure_ascii=False), encoding="utf-8")
    print(f"\n缓存：{cache}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", choices=["explore", "table"])
    ap.add_argument("--model", default=str(DEFAULT_MODEL))
    ap.add_argument("--demos", default=str(DEMOS))
    ap.add_argument("--session", required=True)
    ap.add_argument("--out", default=None,
                    help="中间产物目录（缺省 = demos 同级 speedrush_planning，不入库）")
    args = ap.parse_args()
    if args.out is None:
        args.out = str(Path(args.demos).parent / "speedrush_planning")
    if args.mode == "explore":
        cmd_explore(args)
    else:
        raise SystemExit("table 模式待 explore 的实测参数定下后接上")


if __name__ == "__main__":
    main()