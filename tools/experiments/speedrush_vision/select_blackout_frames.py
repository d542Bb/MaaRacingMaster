# -*- coding: utf-8 -*-
"""黑视帧金标集选帧：扫 depth_debug_* 证据包，离线复算找边读数，出黑视代表帧短名单。

背景（113349 局证伪后的契约拆分）：road_offset 只认 pair 双侧缘；黑视拍诚实弃权。
今后任何「补位候选」（2D 车道线复出/单侧+W/远场深度改进）上产线的先决条件 =
在**黑视帧**上有独立人工金标（真路心/碰撞边界）验证——教训是补位类修复必须对
被补位的帧验值，双源在场帧的一致性验不出补位错（选择偏差）。

本脚本做四件事：
  ① 离线复算：每张证据包帧按产线口径（evid→assemble→reading_from_points，
     ego/object 掩码同喂）重放找边，得 sides/左右缘——不依赖 trace 帧↔fid 对齐
     （现有包无自动对齐源，3 个会话的人工誊录表 SEQ2FID 仅作 fid 报告）。
  ② 黑视段检测：连续 sides<2 的帧按 debug 节流间隔估时长，跨过保鲜槽 TTL
     （0.4s）才算黑视段——单拍缺陷会被 slot 兜住，不构成供数缺口。
  ③ 成因分桶（启发式，最终以人工看拼版图确认为准）：
     - 夜间：frame.jpg 亮度（会话中位）低于阈值；
     - 超车挖洞：object 掩码盖掉近场路域有效点的比例高；
     - 双缘齐缺：sides==0（宽路近场双缘出视野的典型形态）；
     - 雨天：--rain-dirs 手工指定会话（沿用 probe 惯例，图像判雨不可靠）。
  ④ 输出（APPDATA depth_review/blackout_gold/）：shortlist.csv（gold_annotate
     兼容 path,stratum 列）+ 审阅拼版 PNG（调试图三行堆叠=自带标注底图）+
     stats.json（逐会话 sides 分布与分段统计）。

用法：
  .venv/Scripts/python.exe tools/experiments/speedrush_vision/select_blackout_frames.py
      [--n-per-bucket 4] [--rain-dirs depth_debug_... ...] [--dirs depth_debug_... ...]

黑视帧只有半幅 _frame.jpg（证据包固有），标注在它之上做；d*.jpg 三行堆叠
（看了什么/算了什么/判了什么）作审阅底图，决策带含 fid 供人工核对 trace。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections import Counter
from pathlib import Path

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from maaracing_master.plugins.speedrush import depth_geo as dg  # noqa: E402
from maaracing_master.plugins.speedrush.world_model import load_calib  # noqa: E402

# ── 自包含口径（内联自 probe_drivable_grid，实验自包含约定：不依赖未入库脚本）──
FULL_W, FULL_H = 1280, 720
NAT_H, NAT_W = 336, 598

# seq→fid 人工誊录表（调试图决策带 D[fid] 读得，仅 3 个会话有；作 fid 报告用。
# 0c7490d 之后的新包 npz 自带 fid，此表只服务老包。）
SEQ2FID = {
    "depth_debug_20261004_175855": {
        1: None, 2: 2555, 3: 2572, 4: 2593, 5: 2616, 6: 2642, 7: 2666, 8: 2689,
        9: 2708, 10: 2732, 11: 2756, 12: 2778, 13: 2803, 14: 2828, 15: 2852,
        16: 2881, 17: 2907, 18: 2931, 19: 2956, 20: 2981, 21: 3005, 22: 3025,
        23: 3052, 24: 3079, 25: 3101, 26: 3125, 27: 3150, 28: 3179, 29: 3207,
        30: 3231, 31: 3255, 32: 3280, 33: 3306, 34: 3334, 35: 3360, 36: 3383,
        37: 3409, 38: 3436, 39: 3461, 40: 3482, 41: 3506, 42: 3533, 43: 3557,
        44: 3585, 45: 3606, 46: 3628, 47: 3654, 48: 3678, 49: 3699, 50: 3724,
        51: 3747, 52: 3773, 53: 3796, 54: 3818, 55: 3840, 56: 3868, 57: 3892,
        58: 3915, 59: 3939, 60: 3966, 61: 3992, 62: 4013, 63: 4040, 64: 4065},
    "depth_debug_20261004_140930": {
        1: None, 2: 5556, 3: 5583, 4: 5611, 5: 5635, 6: 5663, 7: 5688, 8: 5715,
        9: 5736, 10: 5754, 11: 5776, 12: 5804, 13: 5833, 14: 5861, 15: 5880,
        16: 5908, 17: 5933, 18: 5957, 19: 5982, 20: 6009, 21: 6035, 22: 6062,
        23: 6090, 24: 6119, 25: 6138, 26: 6167, 27: 6191, 28: 6217, 29: 6245,
        30: 6271, 31: 6299, 32: 6323, 33: 6342, 34: 6370, 35: 6394, 36: 6420,
        37: 6445, 38: 6471, 39: 6494, 40: 6521, 41: 6548, 42: 6575, 43: 6606,
        44: 6631, 45: 6659, 46: 6684, 47: 6712, 48: 6739, 49: 6764, 50: 6791,
        51: 6811, 52: 6838, 53: 6859, 54: 6883, 55: 6900, 56: 6917, 57: 6941,
        58: 6959, 59: 6986, 60: 7004},
    "depth_debug_20261002_211445": {
        1: None, 2: 2790, 3: 2874, 4: 2958, 5: 3039, 6: 3121, 7: 3199, 8: 3278,
        9: 3356, 10: 3442, 11: 3522, 12: 3604, 13: 3682, 14: 3767, 15: 3844,
        16: 3926, 17: 4008, 18: 4092, 19: 4173, 20: 4257, 21: 4334, 22: 4408},
}


def _load_ego() -> np.ndarray:
    p = Path(dg.__file__).parent / "resources" / "calibration" / "ego_mask.json"
    d = json.loads(p.read_text(encoding="utf-8"))
    m = np.zeros((FULL_H, FULL_W), bool)
    m[d["y0"]:d["y1"], d["x0"]:d["x1"]] = True
    return m


def assemble(stem: Path):
    """证据包 → (全幅点图, fx, fy, ego, obj)。口径同 probe_drivable_grid/probe_depth_equiv_441。"""
    z = np.load(Path(str(stem) + "_evid.npz"))
    valid = np.unpackbits(z["valid"])[: NAT_H * NAT_W].reshape(NAT_H, NAT_W).astype(bool)
    pn = z["pts"].astype(np.float32)
    pts = np.stack([cv2.resize(pn[..., k], (FULL_W, FULL_H),
                               interpolation=cv2.INTER_LINEAR) for k in range(3)], -1)
    vf = cv2.resize(valid.astype(np.float32), (FULL_W, FULL_H),
                    interpolation=cv2.INTER_NEAREST).astype(bool)
    pts = pts.copy()
    pts[~vf] = np.nan
    fx = float(z["fx"]) * FULL_W
    fy = float(z["fy"]) * FULL_H
    ego = _load_ego()
    obj = None
    mp = Path(str(stem) + "_mask.npy")
    if mp.exists():
        merged = np.unpackbits(np.load(mp))[: FULL_W * FULL_H] \
            .reshape(FULL_H, FULL_W).astype(bool)
        obj = merged & ~ego
    return pts, fx, fy, ego, obj

DATA = Path(os.environ.get("APPDATA", ".")) / "MaaRacingMaster" / "data" / "speedrush"
TRACES = DATA / "control_traces"
OUT = DATA / "depth_review" / "blackout_gold"

SLOT_TTL_S = 0.4          # _EgoRoadObserver.SLOT_TTL_S：跨过它才算真黑视段
DEBUG_INTERVAL_S = 0.5    # depth_geo 异步 worker 的调试图节流（帧间隔估计值）
NIGHT_GRAY = 70           # 会话 frame.jpg 灰度中位低于此 → 夜间
DIG_FRAC = 0.15           # object 掩码盖掉近场有效点比例高于此 → 挖洞嫌疑
DIG_BAND = (3.0, 9.0)     # 近场带（米，z 向）——视锥盲区的主战场
SHEET_COLS = 4            # 拼版列数
MENU_RED = 0.03           # 顶半幅强红占比 ≥ 此值 → 结算/暂停等非驾驶画面
                          # （实测驾驶帧 ≤0.013、结算/暂停 ≥0.06，分离干净；
                          #  select_dirty_frames 的教训：这些画面找边全瞎，混进
                          #  黑视段会污染金标集）


def _menu_red(stem: Path) -> float:
    """顶半幅强红像素占比——结算横幅/暂停菜单的大面积红色叠加特征。"""
    img = cv2.imread(str(_frame_path(stem)))
    if img is None:
        return float("nan")
    hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
    h2 = img.shape[0] // 2
    m = (((hsv[:h2, :, 0] < 10) | (hsv[:h2, :, 0] > 170))
         & (hsv[:h2, :, 1] > 120) & (hsv[:h2, :, 2] > 120))
    return float(m.mean())


def _session_stems(session: str) -> list[Path]:
    """会话目录 → 有序证据包 stem（剥掉 _evid.npz 后缀，与 probe 的 assemble 对齐）。"""
    d = TRACES / session
    return sorted(d / p.name[:-len("_evid.npz")] for p in d.glob("*_evid.npz"))


def _sessions() -> list[str]:
    return sorted(d.name for d in TRACES.glob("depth_debug_*") if d.is_dir())


def _frame_gray(stem: Path) -> float:
    """帧亮度。新包读 _frame.jpg；老包（2026-10-02 前格式）回退调试图顶行
    （堆叠第一行=原图叠加层，场景仍占主体，文本/线标占比小，中位亮度近似可用）。"""
    f = Path(str(stem) + "_frame.jpg")
    if not f.exists():
        f = Path(str(stem) + ".jpg")
        if not f.exists():
            return float("nan")
    img = cv2.imread(str(f), cv2.IMREAD_GRAYSCALE)
    if img is None:
        return float("nan")
    if f.name.endswith("_frame.jpg"):
        return float(img.mean())
    return float(img[:img.shape[0] // 3].mean())


def _frame_path(stem: Path) -> Path | None:
    """标注底图路径：仅认 _frame.jpg（纯原图）。老包的 d*.jpg 是三行堆叠
    调试图（检测框/点云/决策带全叠在上面），不是干净标注底图——不进金标
    清单（维护者判决 2026-10-05：混入标注流的堆叠图整批剔除）。"""
    f = Path(str(stem) + "_frame.jpg")
    return f if f.exists() else None


def _obj_dig_frac(stem: Path, obj: np.ndarray | None, pts: np.ndarray) -> float:
    """object 掩码盖掉的近场带（z∈DIG_BAND）有效点比例——挖洞强度。"""
    if obj is None:
        return 0.0
    z = pts[..., 2]
    near = np.isfinite(z) & (z >= DIG_BAND[0]) & (z < DIG_BAND[1])
    n = int(near.sum())
    if n == 0:
        return 0.0
    # obj 是全幅像素掩码，与 pts 同栅格（assemble 已对齐全幅）
    return float((obj & near).sum() / n)


def _fid_of(session: str, seq: int) -> int | None:
    return SEQ2FID.get(session, {}).get(seq)


def _banner_white(stem: Path) -> bool:
    """白色横幅检测（红幕过滤的补集）：会话尾部的阶段转换横幅（如「进入策略
    商店」）是白心横带，红幕掩码抓不到——y∈[120,260] 内近白(min BGR>200)行
    占比 >0.5 的行 ≥3 判非驾驶。实测判决 d00057：white_max=0.75/55 行，其余
    全部 ≤0.22/0 行。只查 _frame.jpg（老包本就不进标注流）。"""
    f = Path(str(stem) + "_frame.jpg")
    if not f.exists():
        return False
    img = cv2.imread(str(f))
    if img is None:
        return False
    band = img[120:260]
    frac = (band.min(axis=2) > 200).mean(axis=1)
    return int((frac > 0.5).sum()) >= 3


def classify_session(session: str, grays: list[float], rain_dirs: set[str]) -> dict:
    g = np.nanmedian(grays) if grays else float("nan")
    return {"gray_med": round(g, 1) if g == g else None,
            "night": bool(g == g and g < NIGHT_GRAY),
            "rain": session in rain_dirs}


def select_episodes(frames: list[dict]) -> list[dict]:
    """帧序列 → 黑视段（连续 sides<2、估时 > TTL）。段内帧共享成因标签并集。

    非驾驶画面（结算/暂停，menu=True）截断黑视段且自身不参选——那些画面里
    路本来就不在视野，找边瞎是正确行为，不是供数缺口。"""
    eps, cur = [], []
    for fr in frames:
        if fr["sides"] < 2 and not fr["menu"]:
            cur.append(fr)
            continue
        if len(cur) >= 2:
            eps.append(cur)
        cur = []
    if len(cur) >= 2:
        eps.append(cur)
    out = []
    for ep in eps:
        dur = (len(ep) - 1) * DEBUG_INTERVAL_S
        if dur < SLOT_TTL_S:
            continue
        tags = set()
        for fr in ep:
            tags |= fr["tags"]
        mid = ep[len(ep) // 2]
        out.append({"session": ep[0]["session"], "seqs": [f["seq"] for f in ep],
                    "mid": mid, "dur_s": dur, "tags": sorted(tags),
                    "n": len(ep)})
    return out


def contact_sheet(stems: list[Path], captions: list[str], out_path: Path) -> None:
    """调试图三行堆叠缩放拼版（每张已自带：原图+点云+判定带）。"""
    tiles = []
    for st, cap in zip(stems, captions):
        img = cv2.imread(str(st) + ".jpg")
        if img is None:
            continue
        h, w = img.shape[:2]
        scale = 640 / w
        img = cv2.resize(img, (640, int(h * scale)))
        cv2.rectangle(img, (0, 0), (639, 22), (0, 0, 0), -1)
        cv2.putText(img, cap, (4, 16), cv2.FONT_HERSHEY_SIMPLEX, 0.45,
                    (255, 255, 255), 1)
        tiles.append(img)
    if not tiles:
        return
    rows = (len(tiles) + SHEET_COLS - 1) // SHEET_COLS
    th = max(t.shape[0] for t in tiles)
    canvas = np.full((rows * th, SHEET_COLS * 640, 3), 30, np.uint8)
    for i, t in enumerate(tiles):
        r, c = divmod(i, SHEET_COLS)
        canvas[r * th:r * th + t.shape[0], c * 640:c * 640 + 640] = t
    ok, buf = cv2.imencode(".png", canvas)
    if ok:
        out_path.write_bytes(buf.tobytes())  # imencode+字节写：cv2.imwrite 对
                                             # 非 ASCII 路径在 Windows 会乱码


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n-per-bucket", type=int, default=4,
                    help="每桶选出的代表帧数（默认 4）")
    ap.add_argument("--rain-dirs", nargs="*", default=[
        "depth_debug_20261004_175812", "depth_debug_20261004_175855"],
        help="雨天会话（沿用 probe_drivable_grid 的人工确认惯例）")
    ap.add_argument("--dirs", nargs="*", default=None,
                    help="只跑这些会话（默认全部 depth_debug_*）")
    a = ap.parse_args()

    t0 = time.monotonic()
    OUT.mkdir(parents=True, exist_ok=True)
    cal = load_calib()
    rain = {d if d.startswith("depth_debug_") else f"depth_debug_{d}"
            for d in a.rain_dirs}
    sessions = [s for s in _sessions()
                if a.dirs is None or s in a.dirs or s[len("depth_debug_"):] in a.dirs]
    print(f"会话 {len(sessions)} 个：{sessions}")

    rows: list[dict] = []          # 逐帧明细（进 stats）
    all_eps: list[dict] = []
    for si, sess in enumerate(sessions):
        ts = time.monotonic()
        stems = _session_stems(sess)
        frames = []
        for st in stems:
            seq = int(st.name[1:])
            try:
                pts, fx, fy, ego, obj = assemble(st)
            except Exception as e:  # noqa: BLE001 —— 单帧坏不拦全场
                print(f"  跳过 {sess}/{st.name}: {type(e).__name__}: {e}")
                continue
            rd = dg.reading_from_points(pts, fx, cal, ego_mask=ego,
                                        object_mask=obj, fy=fy)
            miss = ("L" if rd.left_edge_lane is None else "") \
                 + ("R" if rd.right_edge_lane is None else "")
            fr = {"session": sess, "seq": seq, "sides": rd.sides, "miss": miss,
                  "gray": _frame_gray(st), "stem": st,
                  "dig": _obj_dig_frac(st, obj, pts),
                  "menu": _menu_red(st) >= MENU_RED or _banner_white(st)}
            frames.append(fr)
        env = classify_session(sess, [f["gray"] for f in frames], rain)
        for fr in frames:
            tags = set()
            if env["night"]:
                tags.add("夜间")
            if env["rain"]:
                tags.add("雨天")
            if fr["dig"] >= DIG_FRAC:
                tags.add("挖洞")
            if fr["sides"] == 0:
                tags.add("双缘齐缺")
            elif fr["sides"] == 1:
                tags.add("单缘缺")
            fr["tags"] = tags
        eps = select_episodes(frames)
        for ep in eps:
            ep["env"] = env
            ep["fid"] = _fid_of(sess, ep["mid"]["seq"])
        all_eps += eps
        dist = Counter(f["sides"] for f in frames)
        nmenu = sum(1 for f in frames if f["menu"])
        rows += [{k: (str(v) if isinstance(v, Path) else sorted(v)
                      if isinstance(v, set) else v)
                  for k, v in f.items() if k != "stem"} | {"stem": str(f["stem"])}
                 for f in frames]
        print(f"[{si+1}/{len(sessions)}] {sess}: {len(frames)}帧 "
              f"sides分布={dict(sorted(dist.items()))} 黑视段={len(eps)} "
              f"非驾驶={nmenu} "
              f"亮度中位={env['gray_med']} 夜间={env['night']} "
              f"({time.monotonic()-ts:.0f}s)")

    # ── 分桶选代表：每桶按时长降序、跨会话轮转，会话内不过量 ──────────
    buckets: dict[str, list[dict]] = {}
    for ep in all_eps:
        for tag in (ep["tags"] or {"成因未判"}):
            buckets.setdefault(tag, []).append(ep)
    shortlist, sheet_jobs = [], {}
    n_skip_base = 0
    for tag, eps in sorted(buckets.items()):
        eps.sort(key=lambda e: (-e["dur_s"], -e["n"]))
        picked, per_sess = [], Counter()
        for ep in eps:
            if len(picked) >= a.n_per_bucket:
                break
            if per_sess[ep["session"]] >= max(1, a.n_per_bucket // 2):
                continue
            picked.append(ep)
            per_sess[ep["session"]] += 1
        for ep in picked:
            base = _frame_path(ep["mid"]["stem"])
            if base is None:      # 老包无干净原图：堆叠调试图不进标注流
                n_skip_base += 1
                continue
            shortlist.append({
                "path": str(base),
                "stratum": f"黑视_{tag}", "session": ep["session"],
                "seq": ep["mid"]["seq"], "fid": ep["fid"] or "",
                "sides": ep["mid"]["sides"], "missing": ep["mid"]["miss"],
                "episode_seqs": " ".join(map(str, ep["seqs"])),
                "dur_s": round(ep["dur_s"], 2), "dig_frac": round(ep["mid"]["dig"], 3),
                "tags": "|".join(ep["tags"])})
            sheet_jobs.setdefault(tag, []).append(ep)
        print(f"桶[{tag}]: 候选段 {len(eps)} → 选 {len(picked)}")
    if n_skip_base:
        print(f"剔除无干净原图（老包堆叠图）条目 {n_skip_base} 个")

    # ── 输出 ─────────────────────────────────────────────────────────
    import csv
    sl = OUT / "shortlist.csv"
    if shortlist:
        with sl.open("w", newline="", encoding="utf-8") as f:
            # 无 BOM：gold_annotate 按 utf-8 读列名，BOM 会把首列变成 \ufeffpath
            w = csv.DictWriter(f, fieldnames=list(shortlist[0]))
            w.writeheader()
            w.writerows(shortlist)
    for tag, eps in sheet_jobs.items():
        stems = [ep["mid"]["stem"] for ep in eps]
        caps = [f"{ep['session'][12:]} d{ep['mid']['seq']:05d} fid={ep['fid']}"
                f" sides={ep['mid']['sides']}{ep['mid']['miss']}"
                f" {ep['dur_s']:.1f}s" for ep in eps]
        contact_sheet(stems, caps, OUT / f"sheet_{tag}.png")
    stats = {"sessions": sessions, "rain": sorted(rain),
             "frames_total": len(rows), "episodes_total": len(all_eps),
             "bucket_counts": {k: len(v) for k, v in sorted(buckets.items())},
             "shortlist_n": len(shortlist)}
    (OUT / "stats.json").write_text(json.dumps(stats, ensure_ascii=False,
                                               indent=2), encoding="utf-8")
    (OUT / "frames_detail.json").write_text(json.dumps(rows, ensure_ascii=False),
                                            encoding="utf-8")
    print(f"完成：短名单 {len(shortlist)} 条 → {sl}")
    print(f"输出目录：{OUT}（耗时 {time.monotonic()-t0:.0f}s）")


if __name__ == "__main__":
    main()
