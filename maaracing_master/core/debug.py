#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MaaRacingMaster — 调试可视化模块
每帧截图标注：探测轮廓(黄) / 入围候选(绿) / 选中光标(红) / 按钮目标

两套渲染：
  • 存盘模式（enabled）→ 全量绘制，保存到磁盘
  • PEEP 模式（peep_enabled）→ 精简绘制，仅关键逻辑

共用辅助方法 _draw_* 统一绘制逻辑，lite=True 时跳过文字标注和边缘散点。
"""

from __future__ import annotations

import threading
import cv2
import numpy as np
from pathlib import Path
from datetime import datetime


def _put_text(frame, text, pos, scale=0.5, color=(255, 255, 255), stroke=1):
    """带黑色阴影和描边的文字绘制，确保任何背景都清晰"""
    x, y = pos
    # 1px 阴影（始终有，极低成本提升可读性）
    cv2.putText(frame, text, (x + 1, y + 1), cv2.FONT_HERSHEY_SIMPLEX, scale, (0, 0, 0), 1)
    if stroke > 1:
        cv2.putText(frame, text, (x, y), cv2.FONT_HERSHEY_SIMPLEX, scale, (0, 0, 0), stroke * 2 + 1)
    cv2.putText(frame, text, (x, y), cv2.FONT_HERSHEY_SIMPLEX, scale, color, max(1, stroke))


def _draw_dashed_rect(frame, pt1, pt2, color, thickness=1, dash_len=8):
    """画虚线矩形"""
    x1, y1 = pt1
    x2, y2 = pt2
    for i in range(x1, x2, dash_len * 2):
        x_end = min(i + dash_len, x2)
        cv2.line(frame, (i, y1), (x_end, y1), color, thickness)
        cv2.line(frame, (i, y2), (x_end, y2), color, thickness)
    for i in range(y1, y2, dash_len * 2):
        y_end = min(i + dash_len, y2)
        cv2.line(frame, (x1, i), (x1, y_end), color, thickness)
        cv2.line(frame, (x2, i), (x2, y_end), color, thickness)


def _dedup_overlapping(dets, iou_thresh=0.5):
    """同类别重叠框去重：每个重叠区域只保留最高置信度的一个，避免虚线框堆叠"""

    if not dets:
        return []
    by_class: dict[str, list] = {}
    for d in dets:
        by_class.setdefault(d["class_name"], []).append(d)

    result = []
    for cls, cls_dets in by_class.items():
        sorted_dets = sorted(cls_dets, key=lambda d: -d["confidence"])
        kept = []
        for d in sorted_dets:
            x1, y1, x2, y2 = d["box"]
            overlap = False
            for k in kept:
                kx1, ky1, kx2, ky2 = k["box"]
                ix1, iy1 = max(x1, kx1), max(y1, ky1)
                ix2, iy2 = min(x2, kx2), min(y2, ky2)
                if ix1 < ix2 and iy1 < iy2:
                    inter = (ix2 - ix1) * (iy2 - iy1)
                    union = (x2 - x1) * (y2 - y1) + (kx2 - kx1) * (ky2 - ky1) - inter
                    if union > 0 and inter / union > iou_thresh:
                        overlap = True
                        break
            if not overlap:
                kept.append(d)
        result.extend(kept)
    return result


class DebugState:
    """调试帧状态容器：save_frame 收到的全部 kwargs 快照，供渲染器读取。

    简单透传（__dict__ 存字段），渲染器可通过 to_kwargs() 展开为关键字参数，
    复用 DebugManager 的内置默认视图（_render_full/_render_peep）。
    """

    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)

    def to_kwargs(self) -> dict:
        """返回状态字段字典（供委托内置视图时展开）"""
        return dict(self.__dict__)


class NavigationDebugger:
    """调试管理器（DebugManager 角色）：调试基础设施 + 渲染器分发。

    • 基础设施：PEEP 线程 / frame buffer / 文件存盘，由本类统一管理。
    • 渲染器分发：模块通过 install_renderer 安装自己的调试视图（owner/token
      机制，旧模块 token 不匹配时无法误清新模块的 renderer）。
    • 内置默认视图：_render_full/_render_peep 作为未安装 renderer 时的兜底
      （导航侧元素 + 检测框 + 顶部信息栏；类别配色等模块语义归模块渲染器）。

    类名保持 NavigationDebugger 不变，避免大改调用方。
    """

    def __init__(self, debug_root: Path):
        self.debug_root = Path(debug_root)  # 调试存图根：user_data_dir()/debug（开发/发行一致）
        self.enabled = False  # GUI 控制开关 → 存盘
        self.session_dir: Path | None = None
        self.frame_count = 0

        # PEEP 实时预览（headless：帧写入缓冲，由前端内嵌显示，不再独立弹窗）
        self.peep_enabled = False
        self._latest_frame: np.ndarray | None = None
        self._frame_lock = threading.Lock()

        # 渲染器（token 机制：install 返回递增 token，remove 需 token 匹配）
        self._renderer = None
        self._renderer_token = 0
        self._renderer_lock = threading.Lock()

    # ---------- PEEP 实时预览 ----------

    def enable_peep(self):
        """开启 PEEP 实时预览（headless）：仅置标志，帧由 save_frame 写入缓冲，供前端内嵌拉取。"""
        self.peep_enabled = True

    def disable_peep(self):
        """关闭 PEEP 实时预览"""
        self.peep_enabled = False

    def get_peep_jpeg(self, quality: int = 70) -> bytes | None:
        """取最新 PEEP 帧并编码为 JPEG（无帧返回 None）。供 sidecar 拉取到前端内嵌预览。"""
        with self._frame_lock:
            if self._latest_frame is None:
                return None
            frame = self._latest_frame.copy()
        ok, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, quality])
        if not ok:
            return None
        return buf.tobytes()

    def update_peep(self, peep_img) -> None:
        """更新 PEEP 预览帧（带写锁）。供 IO worker / 模块注入 peep 帧。

        封装 `_frame_lock`/`_latest_frame` 的写入，模块不再直接访问私有成员（P5）。
        幂等；任意 ndarray 皆可写入，拷贝以避免调用方后续原地修改污染预览。
        写入与 get_peep_jpeg 的读取共用同一把锁，保证 sidecar 拉取不撕裂。
        """
        with self._frame_lock:
            self._latest_frame = peep_img.copy()

    def start_session(self, label: str):
        """开始一次导航调试会话（仅 enabled 时创建目录）"""
        self.frame_count = 0
        self.session_dir = None
        if not self.enabled:
            return
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.session_dir = self.debug_root / "navigate" / f"{label}_{ts}"
        self.session_dir.mkdir(parents=True, exist_ok=True)

    # ---------- 检测框颜色（core 不认识类别语义，类别配色由模块渲染器自定） ----------

    _DET_SOLID_COLOR = (0, 200, 0)      # 正式检测框（实线）
    _DET_RAW_COLOR = (160, 160, 160)    # 低阈值原始框（虚线）

    # ==================================================================
    #  共用绘制辅助方法
    # ==================================================================

    def _draw_yolo_dets(self, frame, detections, lite=False):
        """YOLO 正式检测框（置信度过滤后）"""
        if not detections:
            return
        for det in detections:
            x1, y1, x2, y2 = det["box"]
            cls_name = det.get("class_name", "?")
            color = self._DET_SOLID_COLOR
            cv2.rectangle(frame, (x1, y1), (x2, y2), color, 2)
            if not lite:
                _put_text(frame, f"{cls_name} {det['confidence']:.2f}", (x1, y1 - 5), 0.4, color)

    def _draw_raw_dets(self, frame, all_raw, detections):
        """全部原始检测框（低阈值，虚线，仅 debug 全量模式）

        去重策略：
          1. 同类别重叠框只保留最高分（_dedup_overlapping）
          2. 与实线框重叠的虚线框→隐藏（NMS 跨类压制导致高分变虚线很迷惑）
        """
        if not all_raw:
            return
        det_set = {(d["box"], d["class_name"]) for d in (detections or [])}
        deduped = _dedup_overlapping(all_raw, iou_thresh=0.5)

        # 构建实线框的边界盒列表供重叠检查
        solid_boxes = [d["box"] for d in (detections or [])]

        to_draw = []
        for det in deduped:
            if (det["box"], det["class_name"]) in det_set:
                continue
            dx1, dy1, dx2, dy2 = det["box"]
            # 检查是否与任何实线框显著重叠（IoU > 0.3，跨类也压）
            overlaps_solid = False
            for sx1, sy1, sx2, sy2 in solid_boxes:
                ix1, iy1 = max(dx1, sx1), max(dy1, sy1)
                ix2, iy2 = min(dx2, sx2), min(dy2, sy2)
                if ix1 < ix2 and iy1 < iy2:
                    inter = (ix2 - ix1) * (iy2 - iy1)
                    d_area = (dx2 - dx1) * (dy2 - dy1)
                    s_area = (sx2 - sx1) * (sy2 - sy1)
                    union = d_area + s_area - inter
                    if union > 0 and inter / union > 0.3:
                        overlaps_solid = True
                        break
            if not overlaps_solid:
                to_draw.append(det)

        for det in to_draw:
            x1, y1, x2, y2 = det["box"]
            cls_name = det.get("class_name", "?")
            conf = det.get("confidence", 0)
            color = self._DET_RAW_COLOR
            _draw_dashed_rect(frame, (x1, y1), (x2, y2), color, 1)
            _put_text(frame, f"{cls_name} {conf:.2f}", (x1, y1 - 3), 0.3, color)

    def _draw_cursor(self, frame, cursor_pos, cursor_area=0.0, cursor_score=0.0, dist=None, lite=False):
        """选中的光标位置（红色圈 + 十字）"""
        h, w = frame.shape[:2]
        if cursor_pos:
            cx, cy = cursor_pos
            r = 8 if lite else 12
            cv2.circle(frame, (cx, cy), r, (0, 0, 220), 2)
            cv2.line(frame, (cx - 6, cy), (cx + 6, cy), (0, 0, 220), 1)
            cv2.line(frame, (cx, cy - 6), (cx, cy + 6), (0, 0, 220), 1)
            if not lite:
                info = f"CURSOR({cx},{cy}) A={cursor_area:.0f} S={cursor_score:.3f}"
                if dist is not None:
                    info += f" D={dist:.0f}"
                _put_text(frame, info, (cx + 14, cy - 6), 0.4, (0, 0, 220))
        elif not lite:
            _put_text(frame, "NO CURSOR", (w // 2 - 50, 22), 0.55, (0, 0, 220), stroke=2)

    def _draw_button(self, frame, button_pos, lite=False):
        """按钮目标（蓝色圈 + 十字）"""
        if not button_pos:
            return
        bx, by = button_pos
        r = 10 if lite else 14
        cv2.circle(frame, (bx, by), r, (235, 206, 135), 2)
        cv2.line(frame, (bx - 8, by), (bx + 8, by), (235, 206, 135), 1)
        cv2.line(frame, (bx, by - 8), (bx, by + 8), (235, 206, 135), 1)
        if not lite:
            _put_text(frame, f"btn({bx},{by})", (bx + 16, by + 4), 0.4, (235, 206, 135))

    def _draw_templates(self, frame, template_rects):
        """模板匹配矩形（青色）— 仅 debug 全量模式"""
        if not template_rects:
            return
        for tr in template_rects:
            cx, cy = tr["pos"]
            tw, th = tr["size"]
            x1 = cx - tw // 2
            y1 = cy - th // 2
            cv2.rectangle(frame, (x1, y1), (x1 + tw, y1 + th), (255, 255, 0), 2)
            _put_text(frame, f"TPL {tr.get('name','')} {tr['confidence']:.2f}", (x1, y1 - 5), 0.4, (255, 255, 0))

    def _draw_nav_candidates(self, frame, candidates, all_candidates):
        """导航光标候选（黑=过滤, 绿=入围, 紫=拉黑）— 仅 debug 全量模式"""
        # 被过滤拉黑的轮廓（黑色）
        if all_candidates:
            cand_set = {c["pos"] for c in (candidates or [])}
            for c in all_candidates:
                if c["pos"] in cand_set:
                    continue
                px, py = c["pos"]
                cv2.circle(frame, (px, py), 5, (0, 0, 0), 1)
                _put_text(frame, f"A{c['area']:.0f} R{c['circularity']:.2f}", (px + 6, py - 4), 0.30, (0, 0, 0))
        # 入围候选（绿色/紫色）
        if candidates:
            for c in candidates:
                px, py = c["pos"]
                color = (255, 0, 255) if c.get("blacklisted") else (0, 200, 0)
                cv2.circle(frame, (px, py), 8, color, 1)
                _put_text(frame, f"A{c['area']:.0f} R{c['circularity']:.2f}", (px + 9, py - 4), 0.32, color)

    # ==================================================================
    #  组合渲染
    # ==================================================================

    def _render_full(self, img: np.ndarray, **kw) -> np.ndarray:
        """全量标注绘制（存盘用），返回 BGR 帧

        未安装模块渲染器时的内置兜底视图：导航侧元素 + 检测框 + 顶部信息栏。
        """
        frame = img.copy()
        label = kw.get("label", "")

        # 导航场景元素
        self._draw_nav_candidates(frame, kw.get("candidates"), kw.get("all_candidates"))
        self._draw_button(frame, kw.get("button_pos"))
        self._draw_cursor(frame, kw.get("cursor_pos"), kw.get("cursor_area", 0.0),
                          kw.get("cursor_score", 0.0), kw.get("dist"))
        self._draw_templates(frame, kw.get("template_rects"))

        # 检测框（调用方传了 detections 时）
        self._draw_raw_dets(frame, kw.get("all_raw_dets"), kw.get("detections"))
        self._draw_yolo_dets(frame, kw.get("detections"))

        # 顶部信息栏
        info_line = f"#{self.frame_count}"
        if label:
            info_line += f" | {label}"
        _put_text(frame, info_line, (10, 22), 0.5, (255, 255, 255))
        if kw.get("cursor_score", 0) > 0:
            _put_text(frame, f"score={kw['cursor_score']:.3f}", (10, 42), 0.4, (200, 200, 200))

        return frame

    def _render_peep(self, img: np.ndarray, **kw) -> np.ndarray:
        """精简绘制（PEEP 实时预览用），返回 BGR 帧

        未安装模块渲染器时的内置兜底视图：导航侧元素 + 检测框 + 顶部信息栏。
        """
        frame = img.copy()
        label = kw.get("label", "")

        # 检测框（精简：无置信度文字）
        self._draw_yolo_dets(frame, kw.get("detections"), lite=True)

        # 导航场景元素
        self._draw_cursor(frame, kw.get("cursor_pos"), lite=True)
        self._draw_button(frame, kw.get("button_pos"), lite=True)

        info_parts = [f"#{self.frame_count}"]
        if label:
            info_parts.append(label)
        _put_text(frame, " | ".join(info_parts), (10, 30), 0.75, (255, 255, 255), stroke=3)

        return frame

    # ==================================================================
    #  渲染器管理（owner/token 机制）
    # ==================================================================

    def install_renderer(self, renderer) -> int:
        """安装渲染器，返回 token；再次安装会替换旧渲染器

        模块退出时用返回的 token 调用 remove_renderer 归还；
        若期间被新模块替换，旧 token 将不匹配，remove 不会误清新渲染器。
        """
        with self._renderer_lock:
            self._renderer_token += 1
            self._renderer = renderer
            return self._renderer_token

    def remove_renderer(self, token: int) -> None:
        """按 token 移除渲染器；token 不匹配（已被新模块替换）时不动"""
        with self._renderer_lock:
            if token == self._renderer_token:
                self._renderer = None

    # ==================================================================
    #  统一入口
    # ==================================================================

    def save_frame(
        self,
        frame_rgb: np.ndarray,
        cursor_pos: tuple[int, int] | None = None,
        cursor_area: float = 0.0,
        cursor_score: float = 0.0,
        button_pos: tuple[int, int] | None = None,
        candidates: list[dict] | None = None,
        all_candidates: list[dict] | None = None,
        dist: float | None = None,
        label: str = "",
        template_rects: list[dict] | None = None,
        detections: list[dict] | None = None,
        save_to_disk: bool = True,
        all_raw_dets: list[dict] | None = None,
        **extra_kwargs,
    ):
        """保存一帧调试截图

        save_to_disk: False 时跳过 cv2.imwrite。
        PEEP 预览帧独立于存盘帧——PEEP 使用精简绘制，存盘使用全量绘制。

        颜色约定（全量绘制）：
          🔴 红 — 选中的光标
          🟢 绿 — 入围候选（通过硬过滤，参与评分）
          🟣 紫 — 静止拉黑（连续3帧不动，被跳过评分）
          ⚫ 黑 — 被硬过滤拉黑的探测项
          🔵 亮蓝 — 按钮目标
        """
        if not self.enabled and not self.peep_enabled:
            return
        if self.session_dir is None and not self.peep_enabled:
            return

        self.frame_count += 1
        img_bgr = cv2.cvtColor(frame_rgb, cv2.COLOR_RGB2BGR)

        kwargs = dict(
            cursor_pos=cursor_pos, cursor_area=cursor_area, cursor_score=cursor_score,
            button_pos=button_pos, candidates=candidates, all_candidates=all_candidates,
            dist=dist, label=label, template_rects=template_rects,
            detections=detections,
            all_raw_dets=all_raw_dets,
            **extra_kwargs,   # 模块自定义状态字段（如鉴宝的 treasure_*）透传给渲染器
        )

        # 渲染器分发：已安装 renderer 时走模块视图（render_full/render_peep 各自独立调用），
        # 否则走 DebugManager 内置默认视图（兼容兜底，与重构前行为一致）
        renderer = self._renderer
        if renderer is not None:
            state = DebugState(**kwargs)
            full_img = renderer.render_full(img_bgr, state)
            peep_img = renderer.render_peep(img_bgr, state)
        else:
            full_img = self._render_full(img_bgr, **kwargs)   # 内置默认视图（兼容兜底）
            peep_img = self._render_peep(img_bgr, **kwargs)

        # 存盘：全量绘制
        if self.enabled and self.session_dir is not None and save_to_disk:
            fname = f"{self.frame_count:03d}.png"
            cv2.imwrite(str(self.session_dir / fname), full_img)

        # PEEP：精简绘制
        if self.peep_enabled:
            with self._frame_lock:
                self._latest_frame = peep_img

        # 返回全量渲染结果，供调用方复用（如 treasure 模块全量 debug 存盘），
        # 避免同一帧被 render_full 二次渲染（CPU 竞争是 OCR 推理被拖慢的主因之一）。
        return full_img
