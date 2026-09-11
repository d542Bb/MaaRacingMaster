#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""彩蛋（蛋类藏品）识别器。

奖励结算(彩蛋)弹窗里蛋卡**居中排布**、种类/数量不定 → 固定 OCR ROI 不可行。
（2026-08-17 与用户确认：红/黄/蓝蛋灰度长得太像，3 色独立模板会串色）
  1) **单个通用蛋模板**在 search_rect 内多尺度匹配（灰度匹配只认轮廓/星星，忽略颜色）
  2) 收齐所有 ≥threshold 的候选 → NMS 去重 → 最多取 Top-3（对应三色各 1 张卡）
  3) 每命中 1 张卡 → 取中心 ~40% 区域的彩色像素 → 转 HSV → 按色相判断 红/黄/蓝
  4) 命中 → 在图标框下方开「×N」计数区 → OCR → 解析数量（兼容 ×2 / x2 / 2）
  5) 返回 {red, yellow, blue} 数量 + 命中框（供日志 / 调试台渲染）

配置源：treasure_rois.json 的 eggs 段：
    "eggs": {
      "_count_dx_norm": 0.00,   // 计数区相对图标中心线的水平偏移（可选，默认 0，正数=向右）
      "_count_dy_norm": 0.02,   // 计数区相对图标框下边缘的垂直偏移（可选，默认）
      "_count_w_norm":  0.14,   // 计数区宽度（可选，默认）
      "_count_h_norm":  0.05,   // 计数区高度（可选，默认）
      "egg": {                  // 通用蛋模板（单条目替代原 egg_red/egg_yellow/egg_blue）
        "rect": [x1,y1,x2,y2],  // 三色蛋共用的搜索区（居中排布，同一区域）
        "templates": ["egg.png"],// 或过渡期 egg_yellow.png 作通用模板
        "threshold": 0.72       // 图标命中阈值（灰度匹配）
      }
    }
  模板缺失 / rect 非法 / 整段缺失 → 识别器 configured=False → 上层降级为超时点关闭弹窗。
"""
from __future__ import annotations

import re
from pathlib import Path

import cv2
import numpy as np

from maaracing_master.plugins.treasure import IMAGE_DIR, nav_source


MATCH_THRESHOLD = 0.72  # TM_CCOEFF_NORMED（与鉴宝师匹配同一数量级）
# 多尺度：覆盖 0.70× ~ 1.30×（步长 0.05），容忍蛋图标渲染尺寸偏差
MATCH_SCALES: tuple[float, ...] = (
    0.70, 0.75, 0.80, 0.85, 0.90, 0.95, 1.00, 1.05, 1.10, 1.15, 1.20, 1.25, 1.30,
)
# 计数区默认几何（归一化）：从图标框下方推导
COUNT_DX_DEFAULT = 0.00   # 计数区相对图标中心水平偏移（默认 0=居中；>0 向右，<0 向左）
COUNT_DY_DEFAULT = 0.02   # 图标框下边缘 → 计数区上边缘的偏移
COUNT_W_DEFAULT = 0.14    # 计数区宽度（图标中心对齐）
COUNT_H_DEFAULT = 0.05    # 计数区高度
# 多目标 + NMS
NMS_IOU_THRESHOLD = 0.5   # IoU ≥ 0.5 的重叠框只留最高分
MAX_EGGS = 3              # 彩蛋弹窗最多 3 色（红/黄/蓝）各 1 张卡
# 颜色分类：中心采样比例 + HSV 色相阈值
COLOR_CENTER_RATIO = 0.4  # 取命中框中心 40%（避开边缘高光/阴影/卡片边框）
COLOR_ORDER = ("red", "yellow", "blue")


# ---------------------------------------------------------------------------
# 颜色分类（HSV 色相）：OpenCV H∈[0,179] S∈[0,255] V∈[0,255]
#   红：H∈[0,10] ∪ [170,179]（绕 0 度）  黄：H∈[18,38]  蓝：H∈[100,130]
#   S 下限兜底（避免灰白/近黑像素被误判）
# ---------------------------------------------------------------------------
def _classify_egg_color(rgb_center: np.ndarray) -> str | None:
    """取中心区域平均 RGB → 转 HSV → 按色相返回 'red'/'yellow'/'blue'；无法判断返回 None。"""
    if rgb_center is None or rgb_center.size == 0:
        return None
    # 单像素平均：shape (3,) → 包成 (1,1,3) 供 cvtColor
    if rgb_center.ndim == 1 and rgb_center.shape == (3,):
        avg_rgb_u8 = np.clip(rgb_center, 0, 255).astype(np.uint8).reshape(1, 1, 3)
    else:
        # 整个中心块：直接平均后再转，更稳
        m = rgb_center.reshape(-1, 3).mean(axis=0)
        avg_rgb_u8 = np.clip(m, 0, 255).astype(np.uint8).reshape(1, 1, 3)
    avg_hsv = cv2.cvtColor(avg_rgb_u8, cv2.COLOR_RGB2HSV).reshape(3)
    H, S, V = float(avg_hsv[0]), float(avg_hsv[1]), float(avg_hsv[2])
    # 饱和度过低（< 60 / 255 ≈ 23.5%）→ 灰度化，不判色
    if S < 60:
        return None
    # 亮度兜底：太暗/太白 不判
    if V < 50 or V > 250:
        return None
    if (0 <= H <= 10) or (170 <= H <= 179):
        return "red"
    if 18 <= H <= 38:
        return "yellow"
    if 100 <= H <= 130:
        return "blue"
    return None


# ---------------------------------------------------------------------------
# NMS（非极大值抑制）：IoU 去重
# ---------------------------------------------------------------------------
def _box_area(b) -> float:
    return max(0.0, b[2] - b[0]) * max(0.0, b[3] - b[1])


def _box_iou(a, b) -> float:
    ix1, iy1 = max(a[0], b[0]), max(a[1], b[1])
    ix2, iy2 = min(a[2], b[2]), min(a[3], b[3])
    inter = max(0.0, ix2 - ix1) * max(0.0, iy2 - iy1)
    union = _box_area(a) + _box_area(b) - inter
    if union <= 0:
        return 0.0
    return inter / union


def _nms(cands, iou_th: float, topk: int):
    """cands: list[(score, box_norm)]；返回去重后的列表（按分数降序，最多 topk 个）。"""
    if not cands:
        return []
    order = sorted(range(len(cands)), key=lambda i: cands[i][0], reverse=True)
    keep: list[int] = []
    for i in order:
        b1 = cands[i][1]
        ok = True
        for j in keep:
            b2 = cands[j][1]
            if _box_iou(b1, b2) >= iou_th:
                ok = False
                break
        if ok:
            keep.append(i)
            if len(keep) >= topk:
                break
    return [cands[i] for i in keep]


class EggRewardRecognizer:
    """彩蛋识别器：通用蛋模板匹配（多目标 NMS）+ 中心颜色取向 + 图标下方 OCR 计数。

    无状态（模板/rect/阈值在 __init__ 从 JSON 加载）；OCR 引擎由外部注入。
    """

    def __init__(self, proj: Path, ocr=None):
        self.tpl_dir = IMAGE_DIR
        self._ocr = ocr
        # (gray_tpl, rect_norm, threshold)
        self._entry: tuple[np.ndarray, tuple[float, float, float, float], float] | None = None
        self._count_dx = COUNT_DX_DEFAULT
        self._count_dy = COUNT_DY_DEFAULT
        self._count_w = COUNT_W_DEFAULT
        self._count_h = COUNT_H_DEFAULT
        self._load(proj)

    # ---------- 加载 ----------
    def _load(self, proj: Path) -> None:
        # P4b 唯一真源：读 policy.json spec 的 egg 锚点（rect/templates/threshold +
        # domain 的 _count_*_norm）。真源缺失/损坏 → 直接保持未配置（configured=False）
        # → 彩蛋识别降级、奖励结算走超时点关闭。
        nav = nav_source()
        if nav is None:
            return
        anchor = nav.spec.get("egg")
        if anchor is not None:
            self._load_spec_entry(anchor)

    def _load_spec_entry(self, anchor) -> None:
        """从 spec `egg` 锚点装配 `self._entry` + 计数区参数，语义与迁移前逐字段等价。

        - 计数区 `_count_*_norm`：存于 `anchor.domain`（v2 时代在 eggs 段顶层，同键名）。
        - rect / templates[0] / threshold：spec 锚点直取；threshold 非法/缺省回落 `MATCH_THRESHOLD`。
        - 模板图缺失/灰度非法 → 直接 return（`self._entry` 保持 None，`configured` 为 False）。
        """
        dom = anchor.domain or {}
        try:
            self._count_dx = float(dom.get("_count_dx_norm", COUNT_DX_DEFAULT))
            self._count_dy = float(dom.get("_count_dy_norm", COUNT_DY_DEFAULT))
            self._count_w = float(dom.get("_count_w_norm", COUNT_W_DEFAULT))
            self._count_h = float(dom.get("_count_h_norm", COUNT_H_DEFAULT))
        except (TypeError, ValueError):
            pass
        rect = anchor.rect.as_list()
        if not (isinstance(rect, (list, tuple)) and len(rect) == 4):
            return
        tpls = anchor.templates
        fname = tpls[0] if isinstance(tpls, (list, tuple)) and tpls and isinstance(tpls[0], str) else ""
        if not fname:
            return
        p = self.tpl_dir / fname
        if not p.exists():
            return
        img = cv2.imread(str(p))
        if img is None:
            return
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        if gray.size == 0 or gray.shape[0] < 4 or gray.shape[1] < 4:
            return
        th = anchor.threshold
        threshold = (
            float(th)
            if isinstance(th, (int, float)) and not isinstance(th, bool) and 0.0 <= float(th) <= 1.0
            else MATCH_THRESHOLD
        )
        self._entry = (gray, (float(rect[0]), float(rect[1]), float(rect[2]), float(rect[3])), threshold)

    @property
    def configured(self) -> bool:
        return self._entry is not None

    # ---------- 识别 ----------
    def recognize(self, frame_rgb: np.ndarray) -> dict | None:
        """通用蛋模板匹配 → 多目标 NMS → 每蛋中心颜色分类 → 图标下方 OCR「×N」数量。

        返回 {"counts": {red,yellow,blue}, "eggs": [...]} | None
        eggs 每项: {color, score, box[归一化4值], count_rect[归一化4值],
                   count, count_text, center_rgb, center_hsv}
        """
        if self._entry is None:
            return None
        if frame_rgb is None or not hasattr(frame_rgb, "shape") or frame_rgb.ndim < 2:
            return None
        H, W = frame_rgb.shape[:2]
        try:
            gray = cv2.cvtColor(frame_rgb, cv2.COLOR_RGB2GRAY)
        except Exception:
            return None
        tpl, rect, threshold = self._entry
        cands = self._match_candidates(gray, tpl, rect, W, H, threshold)
        cands = _nms(cands, NMS_IOU_THRESHOLD, MAX_EGGS)  # 最多 3 个蛋

        counts = {"red": 0, "yellow": 0, "blue": 0}
        eggs: list[dict] = []
        for score, box in cands:
            # 取中心区域像素做颜色判断
            rgb_center, center_rgb = self._sample_center_rgb(frame_rgb, box, W, H)
            color = _classify_egg_color(rgb_center)
            if color is None:
                # 颜色判不出来就跳过（蛋形状命中但色相不在区间，可能是卡背景/误识别）
                continue
            count_rect = self._count_rect_from_box(box)
            count, count_text = self._read_count(frame_rgb, count_rect)
            # 同色已存在（罕见，可能 NMS 漏了重叠卡）：取分数高的
            if counts[color] > 0:
                prev = next((e for e in eggs if e["color"] == color), None)
                if prev and score <= prev["score"]:
                    continue
                # 替换旧的
                eggs = [e for e in eggs if e["color"] != color]
            counts[color] = count
            eggs.append({
                "color": color,
                "score": round(float(score), 4),
                "box": [round(float(v), 4) for v in box],
                "count_rect": [round(float(v), 4) for v in count_rect],
                "count": int(count),
                "count_text": count_text,
                "center_rgb": [round(float(x), 1) for x in center_rgb.tolist()],
            })
        return {"counts": counts, "eggs": eggs}

    # ---------- 内部工具 ----------
    def _match_candidates(self, gray, tpl, rect, W, H, threshold) -> list[tuple[float, list[float]]]:
        """多尺度多候选：收集所有 score ≥ threshold 的框（后续 NMS 去重）。"""
        x1n, y1n, x2n, y2n = rect
        x1 = max(0, int(x1n * W)); y1 = max(0, int(y1n * H))
        x2 = min(W, int(x2n * W)); y2 = min(H, int(y2n * H))
        if x2 <= x1 or y2 <= y1:
            return []
        roi = gray[y1:y2, x1:x2]
        rh, rw = roi.shape[:2]
        th0, tw0 = tpl.shape[:2]
        all_cands: list[tuple[float, list[float]]] = []
        for s in MATCH_SCALES:
            nw = max(4, int(round(tw0 * s)))
            nh = max(4, int(round(th0 * s)))
            if nh > rh or nw > rw:
                continue
            if nw == tw0 and nh == th0:
                tpl_s = tpl
            else:
                interp = cv2.INTER_AREA if s < 1.0 else cv2.INTER_CUBIC
                try:
                    tpl_s = cv2.resize(tpl, (nw, nh), interpolation=interp)
                except Exception:
                    continue
            try:
                res = cv2.matchTemplate(roi, tpl_s, cv2.TM_CCOEFF_NORMED)
            except Exception:
                continue
            # 取所有 ≥ threshold 的点（含次高点），避免同一张卡两个尺度都过阈值后 NMS 合并
            ys, xs = np.where(res >= threshold)
            if len(xs) == 0:
                # 至少保留最高分项（即使未过阈值），供 NMS 上层统一过滤
                _, smax, _, lmax = cv2.minMaxLoc(res)
                if smax >= threshold:
                    ys = np.array([lmax[1]]); xs = np.array([lmax[0]])
                else:
                    continue
            for px, py in zip(xs.tolist(), ys.tolist()):
                s_val = float(res[py, px])
                if s_val < threshold:
                    continue
                box = [
                    max(0.0, min(1.0, (x1 + px) / W)),
                    max(0.0, min(1.0, (y1 + py) / H)),
                    max(0.0, min(1.0, (x1 + px + nw) / W)),
                    max(0.0, min(1.0, (y1 + py + nh) / H)),
                ]
                all_cands.append((s_val, box))
        # 若候选 > 500 个（极端高残差图像），先按分数截断避免 NMS O(n²) 爆炸
        if len(all_cands) > 500:
            all_cands.sort(key=lambda x: x[0], reverse=True)
            all_cands = all_cands[:500]
        return all_cands

    def _sample_center_rgb(self, frame_rgb, box, W, H):
        """取命中框中心 `COLOR_CENTER_RATIO` 比例的子块做颜色采样。

        返回 (center_bgr_hwc, avg_rgb_vec3)：前者为子块 uint8(RGB)，后者为均值 float[R,G,B]。
        """
        x1n, y1n, x2n, y2n = box
        cx, cy = (x1n + x2n) / 2.0, (y1n + y2n) / 2.0
        bw, bh = (x2n - x1n) * COLOR_CENTER_RATIO, (y2n - y1n) * COLOR_CENTER_RATIO
        nx1 = max(0.0, min(1.0, cx - bw / 2.0))
        nx2 = max(0.0, min(1.0, cx + bw / 2.0))
        ny1 = max(0.0, min(1.0, cy - bh / 2.0))
        ny2 = max(0.0, min(1.0, cy + bh / 2.0))
        px1 = max(0, int(nx1 * W)); py1 = max(0, int(ny1 * H))
        px2 = min(W, int(nx2 * W)); py2 = min(H, int(ny2 * H))
        if px2 <= px1 or py2 <= py1:
            return np.zeros((1, 1, 3), dtype=np.uint8), np.array([0.0, 0.0, 0.0])
        center = frame_rgb[py1:py2, px1:px2]
        avg = center.reshape(-1, 3).mean(axis=0).astype(np.float32)
        return center, avg

    def _count_rect_from_box(self, box) -> list[float]:
        """计数区：图标框下方一横向小条（图标中心 + dx 对齐）。"""
        cx = (box[0] + box[2]) / 2.0 + self._count_dx
        bottom = box[3]
        x1 = max(0.0, min(1.0, cx - self._count_w / 2.0))
        x2 = max(0.0, min(1.0, cx + self._count_w / 2.0))
        y1 = max(0.0, min(1.0, bottom + self._count_dy))
        y2 = max(0.0, min(1.0, y1 + self._count_h))
        return [x1, y1, x2, y2]

    def _read_count(self, frame_rgb: np.ndarray, rect) -> tuple[int, str]:
        """OCR 计数区 → (数量, 原文)。OCR 不可用/无数字 → 默认 (1, '')。"""
        if self._ocr is None:
            return 1, ""
        try:
            info = self._ocr.recognize_single(frame_rgb, rect) or {}
        except Exception:
            return 1, ""
        text = str(info.get("text") or "").strip()
        m = re.search(r"(\d+)", text)
        if not m:
            return 1, text
        return int(m.group(1)), text
