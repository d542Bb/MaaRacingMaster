"""raw depth 排查：三帧"中场塌陷"疑云的定量判定（零标注、零阈值）。

背景：维护者复核伪彩图后指出"视距没有这么近"，怀疑中场（路面以上）塌陷。
伪彩图按逐帧 p1/p99 归一化，本身可能制造塌陷假象；且 DA-V2 相对输出是**视差**
（d = a/Z + b，大=近），远场在视差尺度上天然被 1/Z 压扁。本脚本先回答：

  Q1 raw 视差里，路面以上各区域到底有没有差异？（区域统计，原始单位）
  Q2 物体与背景在 raw 里可分吗？（卡车/加长车/桥/墙 vs 同列背景）
  Q3 伪彩塌陷是可视化/归一化假象，还是模型真塌了？（固定全局色阶 vs 逐帧色阶）
  Q4 缓存 `__da2s.npy` 的推理口径是多少（@518 还是 @280）？（清晰度指纹 + 现推对比）

用法（仓库根 .venv）：
  python tools/experiments/speedrush_vision/probe_rawdepth.py stats    # Q1 Q2 Q4
  python tools/experiments/speedrush_vision/probe_rawdepth.py figs     # Q3 出图
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
import probe_depth as pd  # noqa: E402

NPY = pd.OUT / "npy"
OUT = pd.OUT

# 三帧（维护者点名的"塌陷"帧）：key = npy 文件名前缀；框为目视原图后手定的 1280×720 区域
FRAMES = {
    "F1 白天高速": "badframes_20260922_201847_p1__fid_2390",
    "F2 日落直线": "badframes_20260922_204519_p1__fid_1295",
    "F3 桥下夕阳": "badframes_20260922_204603_p2__fid_3426",
}
# 每帧区域 (y0,y1,x0,x1)。HUD 在右上 x>940,y<260，所有框避开。
BOXES = {
    "F1 白天高速": {
        "天空": (0, 120, 100, 600), "树/远场": (200, 280, 200, 500),
        "墙(右)": (300, 390, 900, 1200), "路远(消失点带)": (340, 400, 560, 760),
        "路中": (450, 600, 100, 500), "路近": (600, 700, 100, 500),
        "卡车": (380, 480, 150, 450), "自车": (360, 540, 520, 760),
    },
    "F2 日落直线": {
        "天空": (0, 120, 100, 600), "墙(左)": (280, 360, 0, 300),
        "路远(消失点带)": (330, 400, 560, 760), "路中": (450, 600, 100, 500),
        "路近": (600, 700, 100, 500), "自车": (360, 540, 520, 760),
    },
    "F3 桥下夕阳": {
        "桥底(头顶)": (0, 120, 100, 600), "天空": (180, 280, 200, 500),
        "墙(右)": (290, 360, 800, 1200), "人行道(木道左侧)": (400, 520, 100, 450),
        "路远(消失点带)": (330, 400, 560, 760), "路中": (500, 650, 760, 900),
        "加长车": (400, 500, 950, 1250), "红车(前)": (320, 350, 690, 730),
        "自车": (360, 540, 520, 760),
    },
}
# 物体 vs 同列背景（Q2）：物体框 + 其正上方/同 x 的背景框
PAIRS = {
    "F1 白天高速": [("卡车", "树/远场"), ("墙(右)", "天空")],
    "F2 日落直线": [("墙(左)", "天空")],
    "F3 桥下夕阳": [("桥底(头顶)", "天空"), ("加长车", "墙(右)"),
                    ("人行道(木道左侧)", "墙(右)")],
}


def load(key: str) -> np.ndarray:
    return np.load(NPY / f"{key}__da2s.npy").astype(np.float32)


def rgb_of(key: str) -> np.ndarray:
    sess, fid = key.split("__")
    p = pd.APP / "control_traces" / sess / f"{fid}.jpg"
    return cv2.cvtColor(cv2.imread(str(p)), cv2.COLOR_BGR2RGB)


def stats_row(d: np.ndarray, box) -> str:
    y0, y1, x0, x1 = box
    v = d[y0:y1, x0:x1].ravel()
    lo, p10, med, p90, hi = np.percentile(v, [0, 10, 50, 90, 100])
    return (f"{lo:6.3f} {p10:6.3f} {med:6.3f} {p90:6.3f} {hi:6.3f} "
            f"{v.std():6.3f} {(p90 - p10) / max(med, 1e-6):6.3f}")


def cmd_stats(_args) -> None:
    print("== Q1 区域统计（raw 视差，大=近；IQR相对= (P90-P10)/中位）==")
    head = f"{'区域':<14s}{'min':>7s}{'P10':>7s}{'中位':>7s}{'P90':>7s}{'max':>7s}{'σ':>7s}{'IQR相对':>8s}"
    maps = {k: load(k) for k in FRAMES.values()}
    for name, key in FRAMES.items():
        print(f"\n-- {name} ({key}) --")
        print(head)
        for region, box in BOXES[name].items():
            print(f"{region:<14s}{stats_row(maps[key], box)}")
    print("\n== Q2 物体 vs 背景（中位差 / 各自 IQR；可分 = 差 ≫ 两框散布）==")
    for name, pairs in PAIRS.items():
        d = maps[FRAMES[name]]
        for obj, bg in pairs:
            vo = d[BOXES[name][obj][0]:BOXES[name][obj][1], BOXES[name][obj][2]:BOXES[name][obj][3]]
            vb = d[BOXES[name][bg][0]:BOXES[name][bg][1], BOXES[name][bg][2]:BOXES[name][bg][3]]
            io = np.percentile(vo, 90) - np.percentile(vo, 10)
            ib = np.percentile(vb, 90) - np.percentile(vb, 10)
            diff = np.median(vo) - np.median(vb)
            print(f"  {name} {obj} vs {bg}: Δ中位={diff:+.3f}  物体IQR={io:.3f} 背景IQR={ib:.3f}"
                  f"  ⇒ {'可分' if abs(diff) > max(io, ib) else '混叠'}")
    # Q4 缓存清晰度指纹：路面带平均梯度 vs 现推 @518/@280
    print("\n== Q4 缓存口径指纹（路面带 y[400,700] 平均|∇d|，越大细节越多）==")
    import onnxruntime as ort
    sess = ort.InferenceSession(str(pd.MODELS["small"]), providers=["DmlExecutionProvider"])

    def grad(d):
        gy, gx = np.gradient(d[400:700, :])
        return float(np.mean(np.abs(gy)) + np.mean(np.abs(gx)))

    for name, key in FRAMES.items():
        rgb = rgb_of(key)
        row = [f"缓存={grad(maps[key]):.4f}"]
        for short in (518, 280):
            t0 = time.perf_counter()
            d = pd.depth_map(sess, rgb, short)
            row.append(f"@{short}={grad(d):.4f} ({(time.perf_counter()-t0)*1e3:.0f}ms)")
        print(f"  {name}: " + "  ".join(row))


def jet(d: np.ndarray, lo: float, hi: float) -> np.ndarray:
    du = np.clip((d - lo) / max(hi - lo, 1e-6) * 255, 0, 255).astype(np.uint8)
    return cv2.applyColorMap(du, cv2.COLORMAP_JET)


def cmd_figs(_args) -> None:
    maps = {k: load(k) for k in FRAMES.values()}
    allv = np.concatenate([m.ravel() for m in maps.values()])
    glo_lo, glo_hi = np.percentile(allv, [0.5, 99.5])
    print(f"全局固定色阶: lo={glo_lo:.3f} hi={glo_hi:.3f}（三帧并集 p0.5/p99.5）")
    for name, key in FRAMES.items():
        d = maps[key]
        rgb = cv2.cvtColor(rgb_of(key), cv2.COLOR_RGB2BGR)
        lo, hi = np.percentile(d, [1, 99])
        panels = [rgb, jet(d, lo, hi), jet(d, glo_lo, glo_hi), jet(d, 0.0, 2.0)]
        labels = ["RGB", f"per-frame p1/p99=[{lo:.2f},{hi:.2f}]",
                  "fixed global scale [%.2f,%.2f]" % (glo_lo, glo_hi),
                  "mid-field zoom d=[0,2]"]
        # 顶部标题条
        strip = np.full((28, 1280 * len(panels), 3), 255, np.uint8)
        for i, lab in enumerate(labels):
            cv2.putText(strip, lab, (8 + i * 1280, 19), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 0, 0), 1)
        out = np.vstack([strip, np.hstack(panels)])
        p = OUT / f"rawdepth_{key.split('__')[-1]}.jpg"
        cv2.imwrite(str(p), out)
        print(f"[fig] {p}")


def main() -> None:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("stats")
    sub.add_parser("figs")
    args = ap.parse_args()
    {"stats": cmd_stats, "figs": cmd_figs}[args.cmd](args)


if __name__ == "__main__":
    main()
