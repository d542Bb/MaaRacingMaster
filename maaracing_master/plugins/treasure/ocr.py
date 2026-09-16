#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
巅峰鉴宝 OCR 识别模块（RapidOCR / onnxruntime）。

设计原则：
  • **局部 ROI 识别**：只对 treasure_rois.json 的 ocr 分类区域抠图识别，
    不做整帧 OCR（快 + 准，避免无关文字干扰）。
  • **懒加载引擎**：RapidOCR 首次加载模型较慢，采用懒加载；导入/初始化失败
    自动降级为 None，不阻塞观察循环。
  • **数字解析**：金额数字可能带千分位逗号 / 货币符号，区域内 OCR 文本拼接后
    提取最大整数（金额通常是主体）。

用法：
    ocr = TreasureOcr(proj)
    amounts = ocr.recognize_amounts(frame_rgb)   # {"bid_result_amount_box": 12345, "bid_player1": 800, ...}
"""
from __future__ import annotations

import re
from pathlib import Path

import cv2
import numpy as np

from maaracing_master.core.logger import logger

from maaracing_master.plugins.treasure import nav_source


# 数字提取：千分位格式优先（"1,234,567"），其次纯数字。
# 游戏金额一定会显示千分位逗号，因此逗号格式结果具有更高可信度。
# 均支持可选负号前缀：结算页利润/本场收入可能为负（"-12,345"）；OCR 对负号
# 可能输出 ASCII "-" / 全角 "－" / 数学减号 "−"，一并纳入。
_COMMA_NUM_RE = re.compile(r"[-－−]?\d{1,3}(?:,\d{3})+")
_PLAIN_NUM_RE = re.compile(r"[-－−]?\d+")

# 金额下限：游戏中 H 价几万、玩家出价几十万，1 万以下视为噪声
MIN_AMOUNT = 10_000

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
            logger.log(f"[鉴宝OCR] 已绑定进程 CPU 亲和性到 P-core {PIN_P_CORE_AFFINITY}", "DEBUG")
    except Exception as e:
        # 必须 WARNING：这里曾记 DEBUG，导致「绑核从未生效」静默存在了近一个月
        # （psutil 当时不在依赖里 → 每个跑 OCR 的会话都失败一次而无人看见）。
        # 后果：E-core 漂移防护缺位，OCR 尾部耗时在高负载下更易被拉爆；
        # 兜底消除方式：按 core/cpu_time.py 的口径改走无依赖实现。
        logger.log(
            f"[鉴宝OCR] CPU 亲和性未生效({e})——E-core 漂移防护缺位，"
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


def _extract_amount(text: str, min_amount: int = MIN_AMOUNT) -> int | None:
    """从 OCR 文本中提取金额。策略：
    0) 中文"万"单位：大额显示 "86万" / "86.5万" / "1,286万" 时取"万"前数值 ×10000。
       OCR 对"万"字识别稳定，可信度高，上限单独放宽（防亿级余额被 MAX 误滤）；
    1) 先找千分位逗号格式的数字（游戏金额固定带逗号，可信度高），取其中最大的；
    2) 合并重复逗号/删除空格后，再尝试一次逗号格式（修复 RapidOCR 放大后逗号被拆成两块 → 双逗号）；
    3) 无逗号格式时，回退为纯数字拼接：取整串数字，并对 7 位且无逗号合法格式的情况截短首位（通常是
       rect 左边界把相邻槽位号框了进来 → 噪点前缀）；
    4) 正数须在 [min_amount, 10_000_000] 区间内；负数（利润/收入亏钱）允许到 -10_000_000。
       min_amount 可传 0 以允许收入/利润等字段的 0 值（结算页"本场收入 0"此前被
       MIN_AMOUNT 误滤成 None → 显示 -）。"""
    if not text:
        return None
    # 负号归一化：OCR 可能输出 ASCII "-" / 全角 "－" / 数学减号 "−"，
    # 统一成 ASCII "-" 后才能被 int() 与 -MAX 比较正常解析
    text = text.replace("－", "-").replace("−", "-")
    MAX = 10_000_000
    MAX_WAN = 1_000_000_000  # "万"格式上限（10 亿），仅用于带"万"的可信大额文本

    def _ok(v, max_v=MAX):
        # 正数/负数对称：均受 min_amount（下限）与 max_v（上限）约束；
        # 负数 = 利润/收入为负（亏钱），绝对值同样不能低于 min_amount 防噪声
        return (v >= 0 and min_amount <= v <= max_v) or (v < 0 and min_amount <= -v <= max_v)

    def _valid(nums):
        return [n for n in nums if _ok(n)]

    # 0) 中文"万"单位（先于逗号/纯数字：带"万"时逗号值本身低于 MIN_AMOUNT 会被 1) 漏掉）
    m_wan = re.search(r"([-－−]?[\d,，\s\u3000]*\.?\d+)\s*万", text)
    if m_wan:
        num_str = re.sub(r"[,\s\u3000]", "", m_wan.group(1))
        try:
            val = float(num_str) * 10_000
            if val.is_integer():
                v_wan = int(val)
                if _ok(v_wan, MAX_WAN):
                    return v_wan
        except ValueError:
            pass

    # 1) 原始文本 → 逗号格式
    a = _valid([int(m.replace(",", "")) for m in _COMMA_NUM_RE.findall(text)])
    if a:
        return max(a)

    # 2) 清理重复逗号 + 空白 → 再试逗号格式（修复 "286,,660" → "286,660"）
    cleaned = re.sub(r",+", ",", re.sub(r"[\s\u3000]+", "", text))
    b = _valid([int(m.replace(",", "")) for m in _COMMA_NUM_RE.findall(cleaned)])
    if b:
        return max(b)

    # 3) 纯数字回退
    digits_only = re.sub(r"[,\s\u3000]", "", text)
    runs = _PLAIN_NUM_RE.findall(digits_only)
    if not runs:
        return None
    cands: list[int] = []

    def _try_run(s: str):
        # 单段 7 位纯数字：未命中逗号格式 → 不是合法 7 位数（UI 会强制加逗号）
        # 视为 6 位金额 + 噪点前缀，在后续 whole_len==7 分支统一截短处理
        if len(s) == 7:
            return
        try:
            v = int(s)  # s 可能带负号（"-12345"）
        except ValueError:
            return
        if _ok(v):
            cands.append(v)

    for s in runs:
        _try_run(s)

    whole = "".join(runs)
    # 仅纯数字整串做拼接（runs 含负号段时整串无意义，逐段已处理过）
    if whole.isdigit() and 1 <= len(whole) <= 6:
        vw = int(whole)
        if _ok(vw):
            cands.append(vw)
    # 7 位总长且未命中任何逗号格式 → 「1 位噪点前缀 + 6 位真实金额」，截短首位
    if whole.isdigit() and len(whole) == 7:
        v2 = int(whole[1:])
        if _ok(v2):
            cands.append(v2)
    return max(cands) if cands else None


class TreasureOcr:
    """巅峰鉴宝 OCR 识别器（RapidOCR，懒加载 + 失败降级）"""

    def __init__(self, proj: Path):
        self._regions: dict[str, tuple[float, float, float, float]] = self._load_regions()
        self._engine = None          # RapidOCR 引擎（懒加载）
        self._engine_failed = False  # 加载失败后不再重试

    # ---------------- 配置 ----------------
    def _load_regions(self) -> dict[str, tuple[float, float, float, float]]:
        """读取 ocr 识别区（归一化坐标）。

        P4b 唯一真源：policy.json `perception.spec` 中筛 `kind == "ocr"` 锚点取 rect；
        真源缺失/损坏 → 返回空 dict（OCR 区降级，v2 回退已死）。
        """
        regions: dict[str, tuple[float, float, float, float]] = {}
        nav = nav_source()
        if nav is None:
            logger.log("[鉴宝OCR] policy.json 真源不可用，识别区为空", "WARNING")
            return regions
        for name, anchor in nav.spec.items():
            if anchor.kind == "ocr":
                r4 = anchor.rect.as_list()
                regions[name] = (float(r4[0]), float(r4[1]), float(r4[2]), float(r4[3]))
        logger.log(f"[鉴宝OCR] 已加载 {len(regions)} 个识别区(v4): {', '.join(regions)}", "DEBUG")
        return regions

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
                    f"[鉴宝OCR] RapidOCR 引擎已加载(use_det={USE_DET}, use_cls={USE_CLS}, "
                    f"intra_op={OCR_INTRA_OP_THREADS})", "DEBUG")
            except Exception as e:
                self._engine_failed = True
                logger.log(f"[鉴宝OCR] RapidOCR 加载失败({e})，OCR 已禁用", "WARNING")
        return self._engine

    # ---------------- 识别 ----------------
    def recognize_single(
        self, frame_rgb: np.ndarray, rect_norm, *, min_amount: int = MIN_AMOUNT
    ) -> dict | None:
        """对单个归一化坐标矩形 (x1n,y1n,x2n,y2n) 抠图识别。

        主要用途：
          • 调试台：用户手动拖出来的临时 ROI（可能还没保存到 JSON）也能立刻识别。
          • 主程序：临时需要对非配置区域做一次识别时直接用，不绕 recognize_amounts。

        返回字段：
          amount   : 文本中提取的「最大」合法金额（主程序取主值用，兼容老逻辑）
          amounts  : 空格分隔的每段数字单独解析出的金额列表（多段并排框用）
          text     : 拼接后的完整 OCR 文本
          raw_lines: 原始识别行（调试台逐行展示用）
        min_amount：传给 _extract_amount 的金额下限（结算收入/利润等 0 值字段传 0）。
        如果引擎不可用 / 矩形为空 → 返回 None（调用方负责处理）。
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
        patch_bgr = cv2.cvtColor(patch, cv2.COLOR_RGB2BGR)
        patch_bgr = _preprocess_patch(patch_bgr)
        try:
            output = engine(patch_bgr)
        except Exception as e:
            logger.log(f"[鉴宝OCR] recognize_single 识别异常: {e}", "DEBUG")
            return None
        txts = getattr(output, "txts", None) or []
        text = "".join(str(t) for t in txts)
        raw_lines = [str(t) for t in txts]
        # 多段金额解析：「4 个历史出价空格分隔」这类多段并排框，
        # 整段取最大值会丢掉前三轮出价。这里按空格切分，每段单独跑一次金额提取，
        # 返回 amounts 列表供调试台展示、主程序按位置取历史回合出价。
        parts = [p for p in re.split(r"[\s\u3000]+", text) if p]
        amounts = [a for a in (_extract_amount(x) for x in parts) if a is not None]
        return {
            "amount": _extract_amount(text, min_amount=min_amount),
            "amounts": amounts,
            "text": text,
            "raw_lines": raw_lines,
        }

    def recognize_amounts(
        self,
        frame_rgb: np.ndarray,
        keys: list[str] | tuple[str, ...] | frozenset[str] | None = None,
        *,
        min_amounts: dict[str, int] | None = None,
    ) -> dict[str, dict]:
        """对每个 ocr 区域抠图识别，返回 {区域名: {"amount": 金额|None, "text": 原始文本}}。
        仅返回 OCR 有输出的区域；amount 为从文本中提取的最大整数（无数字则为 None）。
        keys=None 识别全部；否则只识别指定 key 子集（关键 ROI 优先通道用，见
        treasure_module.OCR_CRITICAL_KEYS）。
        min_amounts：按区域名覆盖金额下限（如 {"settle_my_income": 0} 允许 0 值字段）。"""
        engine = self._get_engine()
        if engine is None:
            return {}
        targets = self._regions if keys is None else {
            k: rect for k, rect in self._regions.items() if k in keys
        }
        out: dict[str, dict] = {}
        for key, rect_norm in targets.items():
            info = self.recognize_single(
                frame_rgb,
                rect_norm,
                min_amount=(min_amounts or {}).get(key, MIN_AMOUNT),
            )
            if info is not None:
                out[key] = info
        return out