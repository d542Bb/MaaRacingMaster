#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
巅峰鉴宝 OCR 识别模块 —— **业务薄层**，引擎真源在 [`core/ocr.py`](../../core/ocr.py)。

引擎段（抠图 → 预处理 → RapidOCR 推理、以及那套硬测出来的调参：关 det/cls、ORT 线程数、
P-core 绑核、放大与对比度口径）整体住在 core：那里是插件间共享的公共层，treasure 插件
与 NAVKIT 校准台、后续 speedrush 共用同一份实现，**不得在本文件留第二份**——改参去 core。

本文件只保留鉴宝专属语义（领域知识，不上提 core）：
  • **识别区真源**：policy.json `perception.spec` 中 `kind == "ocr"` 的锚点 rect；
  • **金额解析**：千分位逗号 / "万" 单位 / 7 位截首 / 正负号与区间口径（`_extract_amount`）；
  • **字段组装**：面向调用方的 amount / amounts / text / raw_lines 形状。

用法：
    ocr = TreasureOcr(proj)
    amounts = ocr.recognize_amounts(frame_rgb)   # {"bid_result_amount_box": 12345, "bid_player1": 800, ...}
"""
from __future__ import annotations

import re
from pathlib import Path

import numpy as np

from maaracing_master.core.logger import logger
from maaracing_master.core.ocr import RapidOcrEngine

from maaracing_master.plugins.treasure import nav_source


# 数字提取：千分位格式优先（"1,234,567"），其次纯数字。
# 游戏金额一定会显示千分位逗号，因此逗号格式结果具有更高可信度。
# 均支持可选负号前缀：结算页利润/本场收入可能为负（"-12,345"）；OCR 对负号
# 可能输出 ASCII "-" / 全角 "－" / 数学减号 "−"，一并纳入。
_COMMA_NUM_RE = re.compile(r"[-－−]?\d{1,3}(?:,\d{3})+")
_PLAIN_NUM_RE = re.compile(r"[-－−]?\d+")


# 金额下限：游戏中 H 价几万、玩家出价几十万，1 万以下视为噪声
MIN_AMOUNT = 10_000


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
    """巅峰鉴宝 OCR 识别器（引擎由 core.ocr 提供，懒加载 + 失败降级）"""

    def __init__(self, proj: Path):
        self._regions: dict[str, tuple[float, float, float, float]] = self._load_regions()
        self._engine = RapidOcrEngine()

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
        res = self._engine.recognize(frame_rgb, rect_norm)
        if res is None:
            return None
        text = res.text
        raw_lines = list(res.lines)
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
        min_amounts：按区域名覆盖金额下限（如 {"settle_my_income": 0} 允许 0 值字段）。
        引擎不可用 → 每个区域都拿不到结果，返回空 dict（降级不抛）。"""
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