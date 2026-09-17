#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""RapidOCR 文字识别引擎（core 公共层）：单 ROI 抠图识别 + 硬测出来的引擎调参。

**为什么住在 core**：这是插件间共享的识别底座——plugins/treasure 的出价/结算读数、
`tools/navkit/studio_server.py` 的 ROI 校准台都要用它，后续 speedrush 的 HUD 读数也要。
插件自包含契约禁止插件互相 import（见 `plugins/treasure/__init__.py`），故公共层只能在 core。
**本模块是这套调参的唯一真源**：出现第二份副本，ORT 线程数与绑核那套实测定下的值就会分叉。

**本模块只做引擎**：抠图 → 预处理 → 推理 → 交出文本。
「文本怎么解释」（金额解析、数蛋计数、HUD 记分口径）留在各业务层——那是领域知识，不是引擎能力。

设计原则：
  • **局部 ROI 识别**：只识别调用方给的矩形，不做整帧 OCR（快 + 准，避免无关文字干扰）。
  • **懒加载引擎**：RapidOCR 首次加载模型较慢，采用懒加载；`rapidocr` 在函数内导入，
    保证本模块导入期不拉重依赖；加载失败自动降级为 None，不抛、不阻塞调用方的观察循环。
  • **通道序**：对外一律收 RGB（标准语义，见 `core/image_io.py`），交给 RapidOCR 前由
    `to_bgr` 翻一次——翻转只发生在 image_io 这一个边界上，本模块不自写 `[:, :, ::-1]`。

用法：
    engine = RapidOcrEngine()
    res = engine.recognize(frame_rgb, [x1n, y1n, x2n, y2n])   # 归一化 rect
    if res is not None:
        res.text    # 合并文本（"".join(lines)）
        res.lines   # 逐块文本
"""
from __future__ import annotations

from dataclasses import dataclass

import cv2
import numpy as np

from maaracing_master.core.image_io import to_bgr
from maaracing_master.core.logger import logger

__all__ = [
    "OcrText",
    "RapidOcrEngine",
    "USE_DET",
    "USE_CLS",
    "OCR_INTRA_OP_THREADS",
    "OCR_INTER_OP_THREADS",
    "PIN_P_CORE_AFFINITY",
    "TARGET_ROI_HEIGHT",
    "UPSCALE_MAX",
    "UPSCALE_HQ_THRESHOLD",
    "CONTRAST_GAMMA",
]

# 是否启用文本检测(det)。OCR ROI 均为固定 HUD 文字框，det 属冗余计算且占 95%+ 耗时
# （实测 det 单 ROI ~1062ms，关闭后单 ROI ~12ms、9-ROI 一轮 ~105ms）。
# 关闭 det 直接识别，保留开关便于 A/B 与回滚。
USE_DET = False

# 是否启用方向分类(cls)。固定 HUD 文字框方向恒定，cls 属冗余计算
# （每 ROI 多 1 次方向分类推理；关闭后 18 ROI 每帧省 30~50ms）。保留开关便于回滚。
USE_CLS = False

# onnxruntime 推理线程数。实测 CPU 多核并行对 rec 小模型无收益反而变慢
# （default 用满 12 核 → 9-ROI 一轮 ~515ms；intra_op=4 → ~105ms）。固定合理值护系统稳定。
OCR_INTRA_OP_THREADS = 4
OCR_INTER_OP_THREADS = 1

# --------- CPU 亲和性（本机混合架构优化） ---------
# 本机 Intel Alder Lake 8 P-core + 4 E-core（Windows 逻辑核 0-7 = P、8-11 = E）。
# 性能分析证实：ORT intra_op=4 的多线程推理
# 被 Windows 调度器偶发迁到 E-core 时，单次推理从 ~14ms 拉爆到 200ms+，
# 18 ROI 循环累加成 1~2s 尖峰；绑定 P-core 后尖峰 6→0 个、性能仅 +4%（med 289→302ms）。
# 注意这是「本机固定配置」，不是通用 Alder Lake 检测——Windows 的 processor number
# 不保证前 N 个就是 P-core（受 BIOS/processor group/SMT 影响）；其它机器请按拓扑调整
# 或置空列表禁用（传统同构多核无需绑定）。
# ⚠️ 生效前提是 psutil（2026-09-14 起为正式依赖，见 requirements.txt）：此前不在
#    依赖里，_pin_to_p_cores 每次失败报 WARNING，本段校准结论（尖峰 6→0）近一个月
#    从未落到运行时上。
PIN_P_CORE_AFFINITY: list[int] = list(range(8))
_p_affinity_pinned = False


def _pin_to_p_cores() -> None:
    """进程级绑定到 P-core（幂等）。失败静默降级，不阻塞 OCR。

    在 RapidOCR 引擎构造前调用：ORT 线程池线程在此之后创建，会继承进程亲和性，
    从根上避免 worker 线程被调度到 E-core。"""
    global _p_affinity_pinned
    if _p_affinity_pinned or not PIN_P_CORE_AFFINITY:
        return
    try:
        import psutil
        n = psutil.cpu_count(logical=True)
        if n and max(PIN_P_CORE_AFFINITY) < n:
            psutil.Process().cpu_affinity(PIN_P_CORE_AFFINITY)
            logger.log(f"[OCR] 已绑定进程 CPU 亲和性到 P-core {PIN_P_CORE_AFFINITY}", "DEBUG")
    except Exception as e:
        # 必须 WARNING：这里曾记 DEBUG，导致「绑核从未生效」静默存在了近一个月
        # （psutil 当时不在依赖里 → 每个跑 OCR 的会话都失败一次而无人看见）。
        # 后果：E-core 漂移防护缺位，OCR 尾部耗时在高负载下更易被拉爆；
        # 兜底消除方式：按 core/cpu_time.py 的口径改走无依赖实现。
        logger.log(
            f"[OCR] CPU 亲和性未生效({e})——E-core 漂移防护缺位，"
            "OCR 尾部耗时可能偏高", "WARNING")
    _p_affinity_pinned = True

# ---------------- 预处理参数（小尺寸文字识别关键） ----------------
# PP-OCR rec 模型输入高度固定为 48px，识别前会把整图高 resize 到 48。
# 因此预处理后 ROI 高度必须有足够余量给模型"降采样到 48px"，否则笔画细节直接糊：
#   • ROI 整体高 < 48px：模型反而要上采样，信息严重损失 → 基本识别不出
#   • ROI 整体高 ≈ 72px：降采样比例 1.5x → 可用
#   • ROI 整体高 ≈ 96px：降采样比例 2x   → 黄金区，小数字/逗号最稳
TARGET_ROI_HEIGHT = 96         # 放大后的目标 ROI 高度（整体高，非字符高）
UPSCALE_MAX = 6.0              # 防止极端 ROI 过度放大（带来伪影+浪费算力）
# 插值：放大倍率 ≥3x 时 LANCZOS4 比 CUBIC 边缘更锐利（数字/逗号清晰），倍率低时 CUBIC 足够快
UPSCALE_HQ_THRESHOLD = 3.0
# 轻度对比度增强（游戏 HUD 白字半透明底，1.15x gamma 拉一下能把边缘从背景里分离出来）
CONTRAST_GAMMA = 1.15


def _preprocess_patch(patch_bgr: np.ndarray) -> np.ndarray:
    """OCR 前预处理：自适应放大 + 对比度轻度增强，保证小字也有足够像素喂给 rec 模型。

    核心逻辑（解释给未来的自己/AI，别瞎调回固定 2x）：
      1. PP-OCR rec 输入高固定 48px。如果输入只有 24px 高，模型内部会 2x 上采样，
         这一步的信息损失远大于我们用 LANCZOS4 先放大到 96px 再让模型 2x 下采样。
      2. 因此先"超采样到冗余像素"再让模型"降采样到目标高"是识别率更优的策略。
      3. 插值选择与倍率挂钩：小倍率用 CUBIC（快）；≥3x 倍率用 LANCZOS4（锐利）。
      4. gamma 1.15 是纯经验值：白字半透明底上刚好能把边缘灰度从 ~235 推到 ~248，
         不破坏数字形状但提升了笔画的可分离度。
    """
    h = patch_bgr.shape[0]
    if h <= 0:
        return patch_bgr
    scale = max(1.0, min(UPSCALE_MAX, TARGET_ROI_HEIGHT / h))
    interp = cv2.INTER_LANCZOS4 if scale >= UPSCALE_HQ_THRESHOLD else cv2.INTER_CUBIC
    if scale != 1.0:
        patch_bgr = cv2.resize(patch_bgr, None, fx=scale, fy=scale, interpolation=interp)
    # 轻度 gamma 增强（仅对亮区即文字有效，暗区几乎不变）
    if CONTRAST_GAMMA != 1.0 and patch_bgr.size > 0:
        inv_gamma = 1.0 / CONTRAST_GAMMA
        table = (np.arange(256, dtype=np.float32) / 255.0) ** inv_gamma * 255.0
        table = table.clip(0, 255).astype(np.uint8)
        patch_bgr = cv2.LUT(patch_bgr, table)
    return patch_bgr


@dataclass(frozen=True)
class OcrText:
    """单 ROI 识别结果：逐块文本与合并文本两种取法。

    lines 为引擎返回的逐块文本（关 det 时每 ROI 恒为一块，接口不假定块数——开 det
    做 A/B 时会变成多块）；text 是直接拼接，供「整段当一条文本用」的调用方取用。
    """

    lines: tuple[str, ...]

    @property
    def text(self) -> str:
        return "".join(self.lines)


class RapidOcrEngine:
    """RapidOCR 引擎封装（懒加载 + 加载失败降级：返回 None，不抛）。"""

    def __init__(self) -> None:
        self._engine = None          # RapidOCR 实例（懒加载）
        self._engine_failed = False  # 加载失败后不再重试

    # ---------------- 引擎（懒加载） ----------------
    def _get_engine(self):
        if self._engine_failed:
            return None
        if self._engine is None:
            try:
                # 新包 rapidocr（持续维护）替代已废弃的 rapidocr_onnxruntime
                from rapidocr import RapidOCR
                # 绑定 P-core 亲和性必须在 ORT 引擎构造前：ORT 线程池线程继承进程亲和性，
                # 避免 worker 被 Windows 调度到 E-core（见模块常量 PIN_P_CORE_AFFINITY 说明）。
                _pin_to_p_cores()
                # USE_DET=False / USE_CLS=False：固定 HUD 文字框跳过检测与方向分类
                # （性能优化，见模块常量说明）；线程参数：CPU 多核并行对 rec 小模型无收益
                # 反而变慢，固定合理值护系统稳定（见模块常量）
                self._engine = RapidOCR(params={
                    "Global.use_det": USE_DET,
                    "Global.use_cls": USE_CLS,
                    "EngineConfig.onnxruntime.intra_op_num_threads": OCR_INTRA_OP_THREADS,
                    "EngineConfig.onnxruntime.inter_op_num_threads": OCR_INTER_OP_THREADS,
                })
                logger.log(
                    f"[OCR] RapidOCR 引擎已加载(use_det={USE_DET}, use_cls={USE_CLS}, "
                    f"intra_op={OCR_INTRA_OP_THREADS})", "DEBUG")
            except Exception as e:
                self._engine_failed = True
                logger.log(f"[OCR] RapidOCR 加载失败({e})，OCR 已禁用", "WARNING")
        return self._engine

    # ---------------- 识别 ----------------
    def recognize(self, frame_rgb: np.ndarray, rect_norm) -> OcrText | None:
        """对单个归一化矩形 (x1n,y1n,x2n,y2n) 抠图识别。

        **入口形态（二选一，本模块选矩形）**：收「整帧 + 归一化 rect」而不是「已裁剪的
        patch」——所有已知消费者（treasure 的 policy 锚点、studio 校准台的手拖选区、
        后续 speedrush 的 HUD 格子）手里都是归一化 rect，抠图与边界夹紧因此只有这一份实现。

        坐标语义同 `core/roi_config.py`：top-left 原点、归一化到 [0,1]、x2/y2 排他。
        像素边界**刻意沿用历史口径**（x1/y1/x2/y2 一律 `int()` 截断后夹紧到帧内，不是
        roi_config 的 floor/ceil）：边界差一个像素就换掉喂给模型的像素，识别文本会漂移，
        而这条路径的输出被下游当作稳定读数用（固化的出价金额等）。

        返回 `OcrText`（lines 逐块 / text 合并）；引擎不可用、矩形退化到无像素或推理
        异常 → None（调用方负责处理，不抛）。
        """
        engine = self._get_engine()
        if engine is None:
            return None
        H, W = frame_rgb.shape[:2]
        x1n, y1n, x2n, y2n = (float(n) for n in rect_norm)
        x1, y1 = max(0, int(x1n * W)), max(0, int(y1n * H))
        x2, y2 = min(W, int(x2n * W)), min(H, int(y2n * H))
        if x2 <= x1 or y2 <= y1:
            return None
        patch = frame_rgb[y1:y2, x1:x2]
        patch_bgr = _preprocess_patch(to_bgr(patch))
        try:
            output = engine(patch_bgr)
        except Exception as e:
            logger.log(f"[OCR] 识别异常: {e}", "DEBUG")
            return None
        txts = getattr(output, "txts", None) or []
        return OcrText(lines=tuple(str(t) for t in txts))