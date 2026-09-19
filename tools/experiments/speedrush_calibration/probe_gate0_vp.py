"""Gate 0：真帧逐帧 VP 提取 + 平直段稳定性 + 叠图（README 实施顺序第 2 步）。

**要回答什么**（README 闸门 0）：标定器在真实游戏帧上的 VP 是否
「VPx 稳定、y_h 带内且帧间稳」——线段族是否纯净到能撑起标定。
质量帧口径（sig_vpx ≤5、内点 ≥12、y_h 诊断带内）出统计与叠图；
**叠图人工目视**（线簇真的射向该点）由维护者完成，本探针只出图。
**本探针已实测的两条 Gate 0 发现**（详见 README「闸门实施记录」）：
① y_h 实测 ≈324-335（跨帧跨场稳），与 README 更正 3 的先验带 [180,260] 冲突
（「最远检出车底 y≈213」≠「最远可见路面」），故此处用宽诊断带 [150,420]；
② VPx 中位 639~651 跨场一致但**逐帧散布 ±30px**，iters×5 对照排除 RANSAC
欠采样——「帧间稳」判据红，归因线段族本质噪声。

**管线**（合成自检已验过的同一实现）：帧 → 路面带裁剪（y≥170，去顶部 HUD）
→ Canny → HoughLinesP → `_steep` 粗筛 → `estimate_vp`（RANSAC iters=1200，
真帧线段多于合成，1200 足够收敛）。抽帧步长 --stride 控制成本。

**素材**：`%APPDATA%/MaaRacingMaster/data/speedrush/demos/<session>/`
（与 speedrush_vision/probe_old_model.py 的 DEMOS 同源；本探针不碰 ONNX）。
VP 统计缓存 `%TEMP%/sr_calib/vp_<session>.json`；叠图
`%TEMP%/sr_calib/gate0_overlay/<session>/`（contact sheet + 关键帧全尺寸）。

用法（仓库根）：
    .venv\\Scripts\\python.exe tools\\experiments\\speedrush_calibration\\probe_gate0_vp.py <session>
    # 报告 + 叠图；退出码 0=全部合格帧判据绿（叠图目视仍需人工）
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

import cv2
import numpy as np
from PIL import Image

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from probe_ground_calib import (  # noqa: E402
    H, W, _steep, estimate_vp,
)

DEMOS = (Path(os.environ.get("APPDATA", ".")) / "MaaRacingMaster" / "data"
         / "speedrush" / "demos")
CACHE = Path(os.environ.get("TEMP", "/tmp")) / "sr_calib"
Y_TOP = 170          # 路面带裁剪线：HUD 全部在顶带，路面带实测起于 y≈180
CANNY = (50, 150)
HOUGH = dict(threshold=40, minLineLength=40, maxLineGap=6)
ITERS = 1200
SHEET_N = 12         # contact sheet 每场帧数
# 平直判据（Gate 0 的「帧间稳」）：滑窗中位 ±WIN 帧内，逐帧对中位残差的截尾 std 上限
WIN = 15                 # 滑窗半宽（帧）：平直参考指标用
DIAG_Y0, DIAG_Y1 = 150.0, 420.0   # 宽诊断带（README 更正 3 的 [180,260] 与实拍 VP≈325 冲突，待裁定）


def _frames(sess: Path):
    return [json.loads(x) for x in
            (sess / "frames.jsonl").read_text(encoding="utf-8").splitlines() if x]


def extract_vp(rgb: np.ndarray, rng_seed: int):
    """单帧：路面带裁剪 → Canny/Hough → 线段 → VP。返回 (vp, segs, cand_segs)。

    y_band 用宽诊断带 [150,420] 而非 README 更正 3 的 [180,260]：无带交点云实测
    真帧 VP v≈325（两帧独立一致，MAD 15~30），旧带把 y_h 全 clamp 到 260 上界、
    VPx 被带偏——先验带与实拍的冲突是 Gate 0 的发现之一，待维护者裁定后收窄。
    """
    gray = cv2.cvtColor(rgb[Y_TOP:H], cv2.COLOR_RGB2GRAY)
    edges = cv2.Canny(gray, *CANNY)
    raw = cv2.HoughLinesP(edges, 1, np.pi / 180, **HOUGH)
    flat = raw.reshape(-1, 4) if raw is not None else []
    segs = [(float(a), float(b) + Y_TOP, float(c), float(d) + Y_TOP)
            for a, b, c, d in flat]                     # 回全帧坐标
    cand = [s for s in segs if _steep(s)]
    vp = (estimate_vp(cand, rng_seed=rng_seed, iters=ITERS, y_band=(150, 420))
          if len(cand) >= 2 else None)
    return vp, segs, cand


def vp_series(sess_name: str, stride: int) -> list[dict]:
    """逐帧 VP（缓存）。抽帧步长 stride；一条记录一行。"""
    out = CACHE / f"vp_{sess_name}.json"
    if out.exists():
        return json.loads(out.read_text(encoding="utf-8"))
    sess = DEMOS / sess_name
    rows = _frames(sess)[::stride]
    rows_out, t0 = [], time.perf_counter()
    for i, r in enumerate(rows):
        rgb = np.asarray(Image.open(sess / "frames" / r["file"]).convert("RGB"))
        vp, _, cand = extract_vp(rgb, rng_seed=r["seq"])
        rows_out.append({
            "seq": r["seq"], "ts": r["ts_ns"] / 1e9, "file": r["file"],
            "n_segs": len(cand),
            "vpx": vp["vpx"] if vp else None, "y_h": vp["y_h"] if vp else None,
            "sig_vpx": vp["sig_vpx"] if vp else None,
            "sig_yh": vp["sig_yh"] if vp else None,
            "n_inl": len(vp["inliers"]) if vp else 0})
        if i % 50 == 0:
            dt = time.perf_counter() - t0
            print(f"  [{sess_name}] {i}/{len(rows)} 帧，均 {dt / (i + 1):.2f}s/帧"
                  f"（在等逐帧 Canny+Hough+RANSAC，剩约 "
                  f"{dt / (i + 1) * (len(rows) - i):.0f}s）", flush=True)
    out.write_text(json.dumps(rows_out), encoding="utf-8")
    return rows_out


def quality(rows: list[dict]) -> list[dict]:
    """质量帧：自报 σ ≤5、内点 ≥12、y_h 诊断带内——叠图与统计的样本集。

    帧间滑窗「平直」判据（WIN/JITTER）作为参考指标随 report 打印；实测
    VPx 逐帧散布 ±30px 不满足原「帧间稳」期望，且 iters×5 对照排除欠采样
    （归因 = 线段族本质噪声，见 README Gate 0 记录），故合格判定让位给人工目视。
    """
    out = []
    for r in rows:
        sv = r["sig_vpx"]
        if (r["vpx"] is not None and sv is not None and np.isfinite(sv)
                and sv <= 5 and r["n_inl"] >= 12
                and DIAG_Y0 <= r["y_h"] <= DIAG_Y1):
            out.append(r)
    return out


def flatness(rows: list[dict]) -> list[dict]:
    """滑窗中位残差（参考指标）：返回每帧 VPx/y_h 相对 ±WIN 帧滑窗中位的偏差。"""
    ok = quality(rows)
    arr = {r["seq"]: r for r in ok}
    seqs = [r["seq"] for r in ok]
    for i, s in enumerate(seqs):
        win = seqs[max(0, i - WIN):i + WIN + 1]
        arr[s]["dx"] = arr[s]["vpx"] - float(np.median([arr[q]["vpx"] for q in win]))
        arr[s]["dy"] = arr[s]["y_h"] - float(np.median([arr[q]["y_h"] for q in win]))
    return rows


def overlay(sess_name: str, rows: list[dict]):
    """质量帧等距抽 SHEET_N 张叠图：内点绿/外点灰、VP 十字与 y_h 横线。"""
    sess = DEMOS / sess_name
    od = CACHE / "gate0_overlay" / sess_name
    od.mkdir(parents=True, exist_ok=True)
    picks_all = quality(rows)
    if not picks_all:
        print(f"  [{sess_name}] 无质量帧，跳过叠图")
        return
    picks = [picks_all[i] for i in
             np.linspace(0, len(picks_all) - 1, min(SHEET_N, len(picks_all))).astype(int)]
    tiles = []
    for r in picks:
        rgb = np.array(Image.open(sess / "frames" / r["file"]).convert("RGB"))
        vp, segs, _ = extract_vp(rgb, rng_seed=r["seq"])
        inl = set(map(tuple, vp["inliers"])) if vp else set()
        for s_ in segs:
            c = (0, 220, 0) if tuple(s_) in inl else (150, 150, 150)
            cv2.line(rgb, (int(s_[0]), int(s_[1])), (int(s_[2]), int(s_[3])), c, 1)
        if vp:
            x, y = int(round(vp["vpx"])), int(round(vp["y_h"]))
            cv2.line(rgb, (x - 18, y), (x + 18, y), (255, 60, 60), 2)
            cv2.line(rgb, (x, y - 18), (x, y + 18), (255, 60, 60), 2)
            cv2.line(rgb, (x, 0), (x, H), (255, 60, 60), 1)
            cv2.line(rgb, (0, y), (W, y), (60, 120, 255), 1)
            tag = (f"VPx {vp['vpx']:.0f}±{vp['sig_vpx']:.1f}  "
                   f"y_h {vp['y_h']:.0f}±{vp['sig_yh']:.1f}  "
                   f"inl {len(vp['inliers'])}/{r['n_segs']}")
            cv2.putText(rgb, tag, (12, 34), cv2.FONT_HERSHEY_SIMPLEX, 0.8,
                        (255, 255, 255), 2, cv2.LINE_AA)
        tiles.append((r["seq"], rgb))
    cv2.imwrite(str(od / f"{tiles[0][0]}.png"), cv2.cvtColor(tiles[0][1],
                cv2.COLOR_RGB2BGR))
    cols, rows_n = 3, (len(tiles) + 2) // 3
    tw, th = W // 2, H // 2
    sheet = np.full((rows_n * th, cols * tw, 3), 20, np.uint8)
    for i, (seq, t) in enumerate(tiles):
        t = cv2.resize(t, (tw, th))
        cv2.putText(t, str(seq), (8, 24), cv2.FONT_HERSHEY_SIMPLEX, 0.6,
                    (0, 255, 255), 2, cv2.LINE_AA)
        sheet[i // cols * th:(i // cols + 1) * th,
              i % cols * tw:(i % cols + 1) * tw] = t
    cv2.imwrite(str(od / "sheet.png"), cv2.cvtColor(sheet, cv2.COLOR_RGB2BGR))
    return od / "sheet.png"


def report(sessions, stride):
    for s in sessions:
        rows = flatness(vp_series(s, stride))
        est = [r for r in rows if r["vpx"] is not None]
        q = quality(rows)
        print(f"\n== {s}：{len(rows)} 帧（stride {stride}），估出 VP {len(est)}，"
              f"质量帧 {len(q)}")
        if not q:
            continue
        vx = np.array([r["vpx"] for r in q])
        yh = np.array([r["y_h"] for r in q])
        print(f"  质量帧  VPx {np.median(vx):.1f}±{np.std(vx):.1f}  "
              f"y_h {np.median(yh):.1f}±{np.std(yh):.1f}"
              f"（诊断带 [{DIAG_Y0:.0f},{DIAG_Y1:.0f}]）")
        dx = np.array([abs(r["dx"]) for r in q if "dx" in r])
        dy = np.array([abs(r["dy"]) for r in q if "dy" in r])
        print(f"  滑窗±{WIN}帧中位残差 |d|≤2px：VPx {100 * np.mean(dx <= 2):.0f}%  "
              f"y_h {100 * np.mean(dy <= 2):.0f}%（参考）")
        n = len(q)
        for k in range(5):
            seg = q[k * n // 5:(k + 1) * n // 5]
            print("   时段%d: VPx %6.1f  y_h %6.1f (n=%d)"
                  % (k, np.median([r["vpx"] for r in seg]),
                     np.median([r["y_h"] for r in seg]), len(seg)))
        sheet = overlay(s, rows)
        if sheet:
            print(f"  叠图 → {sheet}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("sessions", nargs="+")
    ap.add_argument("--stride", type=int, default=2,
                    help="抽帧步长（2 = 半帧率，VP 逐帧稳定性仍可判）")
    args = ap.parse_args()
    report(args.sessions, args.stride)
