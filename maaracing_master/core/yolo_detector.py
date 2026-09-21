#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
YOLO 目标检测模块：基于 ONNX Runtime 的 YOLOv8 推理封装

类别语义不内置于本模块：默认从模型 ONNX 元数据（Ultralytics 导出的 names 字段）
读取类别表，或由构造参数 classes 显式给定；不内置任何游戏的类别名与阈值。
"""

from __future__ import annotations

import ast

import numpy as np
import cv2
import onnxruntime as ort

from maaracing_master.core.logger import logger


def _names_from_metadata(session: "ort.InferenceSession") -> dict[int, str] | None:
    """读 Ultralytics ONNX 导出内嵌的 names 元数据；无或格式异常返回 None"""
    raw = session.get_modelmeta().custom_metadata_map.get("names")
    if not raw:
        return None
    try:
        parsed = ast.literal_eval(raw)
    except (ValueError, SyntaxError):
        return None
    if not isinstance(parsed, dict):
        return None
    return {int(k): str(v) for k, v in parsed.items()}


def _normalize_classes(classes: dict | list) -> dict[int, str]:
    """类别表归一为 {id: 名称}；显式给定时的校验前置到 session 创建之前"""
    if isinstance(classes, dict):
        norm = {int(k): str(v) for k, v in classes.items()}
    else:
        norm = {i: str(n) for i, n in enumerate(classes)}
    if not norm:
        raise ValueError("classes 不能为空")
    return norm


def _resolve_conf_by_id(class_map: dict[int, str], class_conf: dict[str, float] | None) -> dict[int, float]:
    """class_conf 按类别名校验并转为 {id: 阈值}；未知类名报错（防止静默无效）"""
    cc = class_conf or {}
    unknown = set(cc) - set(class_map.values())
    if unknown:
        raise ValueError(f"class_conf 含未知类别名: {sorted(unknown)}")
    return {cid: cc[name] for cid, name in class_map.items() if name in cc}


class YOLODetector:
    def __init__(
        self,
        model_path: str,
        conf: float = 0.5,
        iou: float = 0.45,
        classes: dict[int, str] | list[str] | None = None,
        class_conf: dict[str, float] | None = None,
    ):
        """
        Args:
            classes: 类别表，{类别id: 名称} 或按 id 升序的名称列表；
                     None 时从模型元数据读取，读不到则报错
            class_conf: 按类别置信度阈值，键为类别名；未覆盖的类别回退 conf
        """
        # ── 显式类别表：在模型加载前完成解析与校验（配置错误 fail-fast） ──
        explicit = _normalize_classes(classes) if classes is not None else None
        if explicit is not None:
            self.classes: dict[int, str] = explicit
            self._conf_by_id = _resolve_conf_by_id(explicit, class_conf)

        # ── Session 选项（图优化 + 缓存） ──
        from pathlib import Path

        cache_dir = Path(__file__).resolve().parent / "__pycache__" / "ort_cache"
        cache_dir.mkdir(parents=True, exist_ok=True)

        sess_opts = ort.SessionOptions()
        sess_opts.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        sess_opts.optimized_model_filepath = str(cache_dir / "model_optimized.onnx")
        sess_opts.add_session_config_entry("session.dml_kernel_cache_path", str(cache_dir))
        sess_opts.add_session_config_entry("session.dml_kernel_cache_enabled", "1")
        sess_opts.intra_op_num_threads = 4
        sess_opts.inter_op_num_threads = 4
        sess_opts.enable_mem_pattern = True
        sess_opts.enable_cpu_mem_arena = True

        try:
            self.session = ort.InferenceSession(
                model_path,
                sess_options=sess_opts,
                providers=["DmlExecutionProvider", "CPUExecutionProvider"]
            )
            logger.log("YOLO 使用 GPU (DirectML)")
        except Exception:
            try:
                self.session = ort.InferenceSession(
                    model_path,
                    sess_options=sess_opts,
                    providers=["CUDAExecutionProvider", "CPUExecutionProvider"]
                )
                logger.log("YOLO 使用 GPU (CUDA)")
            except Exception:
                self.session = ort.InferenceSession(
                    model_path,
                    sess_options=sess_opts,
                    providers=["CPUExecutionProvider"]
                )
                logger.log("YOLO 使用 CPU (GPU 不可用)")

        logger.log(f"ONNX 缓存: {cache_dir}")
        self.input_name = self.session.get_inputs()[0].name
        self.conf = conf
        self.iou = iou
        self.input_size = 640
        self._call_count = 0

        # ── 类别表缺省：从模型元数据读取（Ultralytics 导出内嵌 names） ──
        if explicit is None:
            meta = _names_from_metadata(self.session)
            if meta is None:
                raise ValueError(
                    "模型未携带 names 元数据，请通过 classes 参数显式给定类别表 "
                    "（{id: 名称} 或按 id 升序的名称列表）"
                )
            self.classes = meta
            self._conf_by_id = _resolve_conf_by_id(meta, class_conf)
        # 模型输出类别维与类别表不一致时只告警一次（避免静默错映射）
        self._class_dim_checked = False

    def _empty_by_class(self) -> dict[str, list]:
        """按类别表建空结果容器（每个已声明类别都有键，值为空列表）"""
        return {name: [] for name in self.classes.values()}

    def _to_dets(self, xyxy, scores, classes, pad_x, pad_y, scale, ox, oy, orig_w, orig_h, indices, min_score=0.0):
        """将索引列表转为 (by_class, dets)

        by_class: {类别名: [(cx, cy, bw, bh), ...]}；dets 为检测结果 dict 列表。
        类别 id 不在类别表内（模型输出维多于声明类别）时跳过。
        """
        by_class = self._empty_by_class()
        dets = []
        for i in indices:
            i = int(i)
            if scores[i] < min_score:
                continue
            cls = int(classes[i])
            cls_name = self.classes.get(cls)
            if cls_name is None:
                continue
            x1, y1, x2, y2 = xyxy[i]
            x1, x2 = (x1 - pad_x) / scale + ox, (x2 - pad_x) / scale + ox
            y1, y2 = (y1 - pad_y) / scale + oy, (y2 - pad_y) / scale + oy
            x1, y1 = max(0, x1), max(0, y1)
            x2, y2 = min(orig_w + ox, x2), min(orig_h + oy, y2)
            cx, cy = (x1 + x2) / 2, (y1 + y2) / 2
            bw, bh = x2 - x1, y2 - y1
            dets.append({
                "box": (int(x1), int(y1), int(x2), int(y2)),
                "confidence": float(scores[i]),
                "class_name": cls_name,
            })
            by_class[cls_name].append((int(cx), int(cy), int(bw), int(bh)))
        return by_class, dets

    def _nms_per_class(self, xyxy, scores, classes, mask,
                       pad_x, pad_y, scale, ox, oy, orig_w, orig_h):
        """按类别分别做 NMS，避免跨类抑制（如 car 压掉 bonus_car）

        返回的索引是原始数组下标（供 _to_dets 使用）
        """
        classes_arr = classes[mask]
        unique_classes = np.unique(classes_arr)
        # mask_indices[i] = 原始数组中第 i 个被 mask 选中的下标
        mask_indices = np.where(mask)[0]
        all_indices = []
        for cls in unique_classes:
            cls_mask = classes_arr == cls
            # cls_local[j] = 第 j 个属于该类别的元素在 masked 数组中的位置
            cls_local = np.where(cls_mask)[0]
            cls_boxes = xyxy[mask][cls_mask].tolist()
            cls_scores = scores[mask][cls_mask].tolist()
            if not cls_boxes:
                continue
            nms_idx = cv2.dnn.NMSBoxes(cls_boxes, cls_scores, 0.0, self.iou)
            if len(nms_idx) > 0:
                # 映射链：NMS下标 → 类别内下标 → masked下标 → 原始下标
                orig = mask_indices[cls_local[np.array(nms_idx)]]
                all_indices.extend(orig.tolist())
        return all_indices

    def __call__(self, img_rgb: np.ndarray, roi: tuple | None = None):
        """YOLO 推理

        Args:
            img_rgb: 全屏 RGB 图像
            roi: (x1, y1, x2, y2) 裁剪区域（原始图坐标），None 表示全图

        Returns:
            (by_class, debug_dets, all_raw_dets)
            by_class: {类别名: [(cx, cy, bw, bh), ...]}，每个已声明类别都有键
            debug_dets: 过滤后检测的 dict 列表（box/confidence/class_name）
            all_raw_dets: 低阈值全量检测（debug 可视化用）
        """
        if roi is not None:
            x1, y1, x2, y2 = roi
            orig = img_rgb[y1:y2, x1:x2].copy()
            ox, oy = x1, y1
        else:
            orig = img_rgb
            ox, oy = 0, 0

        orig_h, orig_w = orig.shape[:2]
        scale = min(self.input_size / orig_h, self.input_size / orig_w)
        nh, nw = int(orig_h * scale), int(orig_w * scale)
        pad_y = (self.input_size - nh) // 2
        pad_x = (self.input_size - nw) // 2

        padded = np.full((self.input_size, self.input_size, 3), 114, dtype=np.uint8)
        padded[pad_y: pad_y + nh, pad_x: pad_x + nw] = cv2.resize(orig, (nw, nh), interpolation=cv2.INTER_LINEAR)
        blob = padded.transpose(2, 0, 1)[None].astype(np.float32) / 255.0

        raw_outputs = self.session.run(None, {self.input_name: blob})
        outputs = raw_outputs[0]
        assert isinstance(outputs, np.ndarray), f"ONNX 返回非数组: {type(outputs)}"
        preds = outputs[0].transpose(1, 0)

        xywh = preds[:, :4]
        cls_conf = preds[:, 4:]
        if not self._class_dim_checked:
            self._class_dim_checked = True
            if cls_conf.shape[1] != len(self.classes):
                logger.log(f"模型输出类别维 {cls_conf.shape[1]} ≠ 声明类别数 {len(self.classes)}，"
                           f"请核对 classes 参数", "WARNING")
        max_scores = np.max(cls_conf, axis=1)
        max_classes = np.argmax(cls_conf, axis=1)

        # ── xyxy 坐标（统一计算一次） ──
        xyxy = np.zeros_like(xywh)
        xyxy[:, 0] = xywh[:, 0] - xywh[:, 2] / 2
        xyxy[:, 1] = xywh[:, 1] - xywh[:, 3] / 2
        xyxy[:, 2] = xywh[:, 0] + xywh[:, 2] / 2
        xyxy[:, 3] = xywh[:, 1] + xywh[:, 3] / 2

        # ── 诊断：统计各类别原始置信度分布（每 10 帧一次） ──
        self._call_count += 1
        if self._call_count % 10 == 0:
            for cls_id, cls_name in sorted(self.classes.items()):
                confs = max_scores[max_classes == cls_id]
                if len(confs) > 0:
                    logger.log(f"[RAW] {cls_name}: {len(confs)}个 pred, "
                               f"max={confs.max():.3f}, mean={confs.mean():.3f}", "DEBUG")
                else:
                    logger.log(f"[RAW] {cls_name}: 无 pred（均低于0.01）", "DEBUG")

        # ── 全量低阈值检测（debug 可视化用，每类最多 20 个） ──
        RAW_CONF = 0.05
        raw_mask = max_scores > RAW_CONF
        all_raw_dets = []
        if np.any(raw_mask):
            raw_indices = np.where(raw_mask)[0]
            # 按置信度降序，每类取 top 20
            raw_sorted = raw_indices[np.argsort(-max_scores[raw_indices])]
            per_class_count = dict.fromkeys(self.classes, 0)
            top_raw = []
            for idx in raw_sorted:
                cls = int(max_classes[idx])
                if per_class_count.get(cls, 0) < 20:
                    top_raw.append(idx)
                    per_class_count[cls] = per_class_count.get(cls, 0) + 1
            _, all_raw_dets = self._to_dets(
                xyxy, max_scores, max_classes, pad_x, pad_y, scale, ox, oy,
                orig_w, orig_h, top_raw, min_score=RAW_CONF)

        # ── 正式过滤：按类别置信度阈值 + 按类分别做 NMS ──
        per_class_thresholds = np.array([self._conf_by_id.get(int(c), self.conf) for c in max_classes])
        mask = max_scores > per_class_thresholds
        if not np.any(mask):
            return self._empty_by_class(), [], all_raw_dets

        real_indices = self._nms_per_class(
            xyxy, max_scores, max_classes, mask,
            pad_x, pad_y, scale, ox, oy, orig_w, orig_h)
        if len(real_indices) == 0:
            return self._empty_by_class(), [], all_raw_dets
        by_class, debug_dets = self._to_dets(
            xyxy, max_scores, max_classes, pad_x, pad_y, scale, ox, oy,
            orig_w, orig_h, real_indices)

        return by_class, debug_dets, all_raw_dets
