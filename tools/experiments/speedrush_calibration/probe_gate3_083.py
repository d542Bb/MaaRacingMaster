"""动作序①：0.83 单位口径离线对账（维护者裁定 2026-09-21，判决性运行）。

**要回答什么**：HUD 上三个速度相关读数是否互相自洽——
  A = 左卡片「里程」冻结值 ÷ 周期（≈米/秒）；
  B = 蓝卡「得分速度」底值（>50 弃、窗口中位——维护者口径）；
  C = 蓝卡累计比分在同一周期窗口的差分（分/秒）。
三量全部取同帧带 OCR（离线重建，新旧批统一口径），旧批 jsonl 的 mileage 字段仅作 A 的交叉校验。
预注册判决（README 补采预分析轮 + direction_review §5-3）：
  A/B 跨速度带**恒定≈0.83** → 单位/口径错（「1 分=1 米」与「底值≈车速」至少一条不成立）；
  **随速度漂移** → 窗口/截断错。C 用来分离是哪一条：
  若 C≈底值 B（桶模型）且 C≠A → 里程显示窗口的账法与比分不同尺度；
  若 C≈A → 底值/里程之一读数有系统偏。

**判决结果（2026-09-21 本轮）**：A/B 跨速带 20 周期恒定 0.840（冻结值/底值
≈8.3±0.25s）——预注册二分**不完备**（时间窗截断之比同样与速度无关）。分解为
两个简并假设：R1 计数窗 ≈8.2s≠卡周期 10s，或 R2「1 分=1 米」实为 ≈1.22 米/分；
屏上量不可判定（入场上涨段系淡入动画，斜率非计数率）。两读法下游同果：
**速度锚用底值；左卡片周期值不得除以卡周期当车速**。定案记录 = README
「0.83 对账轮」条 + RULES §4.8。

**里程行为（口径与 RULES §4.8 一致）**：卡片每周期末入场（淡入）~3-4s；入场段数字
**递涨到周期末定值后冻结**——上涨段系淡入计数动画，不可当瞬时计数率；采信冻结值=
该周期总里程（旧批读到的定值即冻结段）。采信规则：burst 内出现 ≥2 次的最大值
（冻结后不涨；空窗噪点是个位数，被值下限排除）。

**自包含**：不 import maaracing_master。OCR 引擎为 speedrush_scoring 探针同款最小
副本（RapidOCR 关 det/cls、超采样+gamma 同口径）；区域共用插件内同一份真源。

用法（仓库根，计时防卡死）：
    .venv\\Scripts\\python.exe tools\\experiments\\speedrush_calibration\\probe_gate3_083.py <session>...
    --stride 3（帧抽样步长）  --jsonl-mileage（A 改从 hud.jsonl mileage 字段取，旧批可用）
输出：逐周期行 + 汇总判决。
"""
from __future__ import annotations

import argparse
import json
import os
import re
from pathlib import Path

DEMO_ROOT = Path(os.environ.get("APPDATA", ".")) / "MaaRacingMaster" / "data" / "speedrush" / "demos"
HERE = Path(__file__).resolve().parent
RECTS = HERE.parents[2] / "maaracing_master" / "plugins" / "speedrush" / "resources" / "policy" / "hud_regions.json"

TARGET_ROI_HEIGHT = 96
CONTRAST_GAMMA = 1.15
BURST_GAP_S = 2.0        # 里程 burst 断口
MILEAGE_MIN = 50         # 冻结里程下限（排除个位数噪点）
RATE_PULSE_DISCARD = 50  # 底值口径：>50 为超车/动作脉冲，弃


# ---------- OCR（最小引擎，口径与 scoring 探针一致） ----------

class RoiOcr:
    def __init__(self) -> None:
        self._engine = None
        self.rects = json.loads(RECTS.read_text(encoding="utf-8"))

    def _get(self):
        if self._engine is None:
            from rapidocr import RapidOCR
            self._engine = RapidOCR(params={
                "Global.use_det": False, "Global.use_cls": False,
                "EngineConfig.onnxruntime.intra_op_num_threads": 4,
                "EngineConfig.onnxruntime.inter_op_num_threads": 1,
            })
        return self._engine

    def text(self, frame_path: Path, rect_name: str) -> str:
        import numpy as np
        import cv2
        from PIL import Image
        rgb = np.asarray(Image.open(frame_path).convert("RGB"))
        rect = self.rects[rect_name]
        h, w = rgb.shape[:2]
        x1, y1 = int(rect[0] * w), int(rect[1] * h)
        x2, y2 = int(rect[2] * w), int(rect[3] * h)
        roi = rgb[y1:y2, x1:x2][:, :, ::-1].copy()
        scale = max(1.0, min(4.0, TARGET_ROI_HEIGHT / roi.shape[0]))
        if scale != 1.0:
            roi = cv2.resize(roi, None, fx=scale, fy=scale,
                             interpolation=cv2.INTER_CUBIC if scale < 3 else cv2.INTER_LANCZOS4)
        table = (np.arange(256, dtype=np.float32) / 255.0) ** (1.0 / CONTRAST_GAMMA) * 255.0
        roi = cv2.LUT(roi, table.clip(0, 255).astype(np.uint8))
        try:
            out = self._get()(roi)
        except Exception:
            return ""
        return "".join(str(t) for t in (getattr(out, "txts", None) or []))


def parse_int(t: str) -> int | None:
    return int(t) if re.fullmatch(r"\d{1,4}", t or "") else None


def parse_num(t: str) -> float | None:
    t = (t or "").replace(",", "").replace("，", "")
    m = re.fullmatch(r"(\d{1,4})(?:[.,](\d))?", t)
    return float(f"{m.group(1)}.{m.group(2)}") if m and m.group(2) else \
        (float(m.group(1)) if m else None)


# ---------- 帧序列装载 ----------

def load_frames(session: Path) -> list[dict]:
    return [json.loads(ln) for ln in (session / "frames.jsonl").read_text(encoding="utf-8").splitlines()]


def mileage_from_jsonl(session: Path) -> list[tuple[float, int]] | None:
    """旧批（v4 构建）hud.jsonl 自带 mileage 字段——仅作 A 的交叉校验。"""
    p = session / "hud.jsonl"
    if not p.exists():
        return None
    rows = [json.loads(ln) for ln in p.read_text(encoding="utf-8").splitlines()]
    if not any("mileage" in r.get("fields", {}) for r in rows):
        return None
    out = []
    for r in rows:
        f = r["fields"].get("mileage", {})
        v = parse_int(str(f.get("text", "")))
        if v is not None:
            out.append((r["ts_ns"] / 1e9, v))
    return out


def frame_series(session: Path, stride: int) -> dict[str, list[tuple[float, float]]]:
    """一次遍历帧。**同帧选槽**：得分速度行只在本机（蓝）卡上（RULES §4.8 实测），
    故某槽 rate 读出数字 ⇒ 该槽即我卡，比分只取同槽的 score——杜绝换位期另一槽
    的矩形扫到红卡数字造成的污染（点检实测：两槽同帧皆有值时必有一假）。"""
    ocr = RoiOcr()
    mile, rate, score = [], [], []
    for r in load_frames(session)[::stride]:
        ts = r["ts_ns"] / 1e9
        fp = session / "frames" / r["file"]
        m = parse_int(ocr.text(fp, "mileage"))
        if m is not None:
            mile.append((ts, float(m)))
        r_val, s_val = None, None
        for slot in ("a", "b"):
            rv = parse_num(ocr.text(fp, f"rate_blue_{slot}"))
            if rv is not None and r_val is None:
                r_val = rv
                s_val = parse_num(ocr.text(fp, f"score_blue_{slot}"))
        if r_val is not None:
            rate.append((ts, r_val))
            if s_val is not None:
                score.append((ts, s_val))
    return {"mileage": mile, "rate": rate, "score": score}


# ---------- 周期切分与三方对账 ----------

def bursts(readings: list[tuple[float, float]]) -> list[list[tuple[float, float]]]:
    """按时间断口或数值回跌切 burst：活计数周期内只增不降，回跌即新周期入场
    （防零散噪点读数把相邻两张卡桥接成一个 burst）。"""
    groups, cur = [], []
    for ts, v in readings:
        if cur and (ts - cur[-1][0] > BURST_GAP_S or v < cur[-1][1]):
            groups.append(cur)
            cur = []
        cur.append((ts, v))
    if cur:
        groups.append(cur)
    return groups


def plateau(burst: list[tuple[float, float]]) -> tuple[float, float] | None:
    """冻结里程 = burst 内出现 ≥2 次的最大值；返回 (首次出现 ts, 值)。"""
    if not burst:
        return None
    vals = [v for _, v in burst]
    top = max(vals)
    if top < MILEAGE_MIN or vals.count(top) < 2:
        return None
    return next(ts for ts, v in burst if v == top), top


def window_median(series: list[tuple[float, float]], t0: float, t1: float,
                  discard_above: float | None = None) -> float | None:
    vals = sorted(v for ts, v in series if t0 < ts <= t1 and
                  (discard_above is None or v <= discard_above))
    if len(vals) < 3:
        return None
    m = len(vals) // 2
    return vals[m] if len(vals) % 2 else 0.5 * (vals[m - 1] + vals[m])


def last_at(series: list[tuple[float, float]], t: float) -> float | None:
    v = None
    for ts, val in series:
        if ts <= t:
            v = val
        else:
            break
    return v


def reconcile(session: str, stride: int, jsonl_mileage: bool) -> list[dict]:
    sess = DEMO_ROOT / session
    fs = frame_series(sess, stride)
    mile = fs["mileage"]
    mile_src = "ocr"
    if jsonl_mileage:
        jm = mileage_from_jsonl(sess)
        if jm:
            mile, mile_src = [(ts, float(v)) for ts, v in jm], "jsonl"
    rate = fs["rate"]
    score = fs["score"]
    plats = [p for b in bursts(mile) if (p := plateau(b))]
    rows = []
    for (t1, m), (t0, _m0) in zip(plats[1:], plats[:-1]):
        period = t1 - t0
        if not (7.0 <= period <= 13.0):
            rows.append({"session": session, "note": "周期异常，跳过",
                         "period_s": round(period, 2)})
            continue
        v_a = m / period
        b = window_median(rate, t0, t1, RATE_PULSE_DISCARD)
        note = ""
        if b and (v_a / b > 3.0 or v_a / b < 0.2):
            # 底值窗混入停车/收尾帧（A 却来自更早的活计数窗）→ 弃该行，防假漂移
            note, b = "底值窗疑混入停车段，弃", None
        s0, s1 = last_at(score, t0), last_at(score, t1)
        c = (s1 - s0) / period if s0 is not None and s1 is not None and s1 > s0 else None
        rows.append({"session": session, "src": mile_src, "freeze_m": m,
                     "period_s": round(period, 2), "v_a_ms": round(v_a, 2),
                     "rate_base": None if b is None else round(b, 1),
                     "ratio_a_b": None if not b else round(v_a / b, 3),
                     "score_per_s": None if c is None else round(c, 1),
                     "ratio_b_c": None if (not b or not c) else round(b / c, 3),
                     "ratio_a_c": None if not c else round(v_a / c, 3),
                     "note": note})
    return rows


def summarize(all_rows: list[dict]) -> None:
    ok = [r for r in all_rows if r.get("ratio_a_b")]
    print("\n== 判决汇总 ==")
    if not ok:
        print("无可判周期（检查底值/里程采样密度）。")
        return
    def avg(xs):
        return sum(xs) / len(xs) if xs else None
    ab = [r["ratio_a_b"] for r in ok]
    print(f"A/B 可判周期 {len(ok)}，均值 {avg(ab):.3f}")
    lo = [r for r in ok if r["v_a_ms"] < 30]
    hi = [r for r in ok if r["v_a_ms"] >= 30]
    if lo and hi:
        print(f"低速带 A/B={avg([r['ratio_a_b'] for r in lo]):.3f} (n={len(lo)}) vs "
              f"高速带 A/B={avg([r['ratio_a_b'] for r in hi]):.3f} (n={len(hi)})")
    print(f"A/B 极差/均值 = {(max(ab) - min(ab)) / avg(ab):.1%}"
          "（参考：<10% 恒定证据→单位/口径错；>20% 漂移证据→窗口/截断错）")
    bc = [r["ratio_b_c"] for r in ok if r.get("ratio_b_c")]
    ac = [r["ratio_a_c"] for r in ok if r.get("ratio_a_c")]
    if bc:
        print(f"B/C（底值 vs 比分秒增量）均值 {avg(bc):.3f} n={len(bc)}"
              "——≈1 即桶模型在新素材上仍成立")
    if ac:
        print(f"A/C（里程换算 vs 比分秒增量）均值 {avg(ac):.3f} n={len(ac)}"
              "——≈1 即「1 分=1 米」成立；≈0.83 则分/米比不是 1")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("sessions", nargs="+")
    ap.add_argument("--stride", type=int, default=3)
    ap.add_argument("--jsonl-mileage", action="store_true",
                    help="旧批：A 用 hud.jsonl mileage 字段（仅交叉校验用，默认帧 OCR）")
    args = ap.parse_args()
    all_rows = []
    for s in args.sessions:
        rs = reconcile(s, args.stride, args.jsonl_mileage)
        all_rows += rs
        for r in rs:
            print(f"{r.get('session','')} [{r.get('src','—')}] 冻结 {r.get('freeze_m','—')}m "
                  f"周期 {r.get('period_s','?')}s → A={r.get('v_a_ms','?')}m/s  "
                  f"B底值={r.get('rate_base','—')}  A/B={r.get('ratio_a_b','—')}  "
                  f"C分/秒={r.get('score_per_s','—')}  B/C={r.get('ratio_b_c','—')} "
                  f"A/C={r.get('ratio_a_c','—')} {r.get('note','')}")
    summarize(all_rows)
