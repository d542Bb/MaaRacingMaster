#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
巅峰鉴宝阶段自动检测器（基于 cv2.matchTemplate 模板匹配，无 OCR，毫秒级）。

设计原则：
  1. **局部 ROI 匹配**：每个模板只在「它理论上会出现的那一小块区域」内搜索，
     避免大图里其他相似文字造成假阳性。
  2. **模板只选「独有的稳定元素」**：巨型文字横幅 / 独有的卡片文字标题 /
     右上角「第 N 回合」完整小字（整段 89×25，因为每个 N 的整段字结构不同）。
  3. **强特征优先**：结算页的「竞拍失败」「最终竞拍价格」绝对唯一，优先级最高。
  4. **不要求全覆盖**：模糊阶段（匹配中、主题抽取动画）返回 None，
     上层状态机按「最近一次稳定阶段 + 时间推移」自行推进即可。

用法：
    detector = TreasureStageDetector(proj)
    stage, round_no = detector.detect(frame_rgb)
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np

from maaracing_assistant.core.logger import logger
from maaracing_assistant.plugins.treasure import IMAGE_DIR


MATCH_THRESHOLD = 0.75  # TM_CCOEFF_NORMED

# 多尺度匹配缩放档（0.70×~1.30×，步长 0.05）。
# 必须与调试台 tools/navkit/core/reader.py 的 MATCH_SCALES、
# treasure_module 的 _APPRAISER_MATCH_SCALES 保持完全一致——调试台校准的分数/阈值
# 要能原样复现于运行时，画面/ROI/模板/算法四者必须同口径（牵一发而动全身原则）。
MATCH_SCALES: tuple[float, ...] = (
    0.70, 0.75, 0.80, 0.85, 0.90, 0.95, 1.00, 1.05, 1.10, 1.15, 1.20, 1.25, 1.30,
)

# ============================================================
# 搜索 ROI / 模板 / 阈值：唯一真源 policy.json perception 数据面 → DetectionPlan（__init__ 载入）。
# rect 为归一化坐标 (x1n, y1n, x2n, y2n)，匹配时直接乘当前输入帧 W/H。
# M4/E1：不再提供 treasure_rois.json 读取器——v3 缺失/损坏时如实报告并跳过检测，
#        避免用残缺默认值掩盖真实配置导致阶段漏检。_ROI_STAGE 仅保留为常量元数据（无 rect）。
# ============================================================


@dataclass(frozen=True)
class DetectResult:
    """阶段检测结果及可还原判断树所需的逐锚点明细（D7）。

    `__iter__` 保留旧版 `stage, round_no = detector.detect(...)` 的解包兼容；
    新调用方直接读取 scores / hit_anchor / active_used / hit_template / hit_box。
    """

    stage: str | None
    round_no: int | None
    scores: dict[str, float]
    hit_anchor: str | None
    active_used: tuple[str, ...]
    hit_template: str | None = None
    hit_box: tuple[int, int, int, int] | None = None
    threshold: float | None = None

    def __iter__(self):
        yield self.stage
        yield self.round_no


@dataclass(frozen=True)
class _Rule:
    template: str
    roi_key: str
    stage: str
    round_no: int | None = None


# ROI → 阶段语义映射。模板列表完全由 JSON 的 templates 数组决定（_ROI_TPL），
# 这里只定义"这个 ROI 代表哪个阶段"、优先级、以及多模板互斥时的领先要求。
#   - __round_phase__：命中后走回合识别（智能出价按钮 / 回合横幅）
#   - round_from_template：True 时回合号从命中的模板文件名解析（如 round3_banner.png → 3）
#   - margin：多模板互斥时，最高分需领先次高分 ≥ margin 才算命中
_ROI_STAGE: dict[str, dict] = {
    # 结算后弹窗（领取分红后可能出现）。三个弹窗（今日最高/等级提升/彩蛋）合并为单一
    # 阶段「结算弹窗」，靠 _last_hit_roi_key 区分具体弹窗：
    #   - daily_high_banner 命中 → 今日最高积分上涨（需先 OCR 读积分再点）
    #   - egg_reward_title 命中 → 奖励结算彩蛋（需先 OCR 读蛋数量再点）
    #   - 都没命中（等级提升遮满全屏，无 ROI）→ 盲点跳过
    # 优先级（110/105）高于大厅（50）：弹窗在时一定先命中弹窗，弹窗全关后大厅才可见。
    # ②鉴宝等级提升 不识别（无 ROI），弹窗遮满全屏 → 三种弹窗模板都匹配不到 → 上层盲点。
    "daily_high_banner":  {"stage": "结算弹窗", "priority": 110},
    "egg_reward_title":   {"stage": "结算弹窗", "priority": 105},
    # 结算页
    "settle_title":       {"stage": "领取分红",           "priority": 100},
    "result_banner":      {"stage": "中标结算",           "priority": 90,
                           # 竞拍成功横幅带彩条特效，匹配分偏低，单独放宽阈值防误判
                           "thresholds": {"result_auction_win_banner": 0.60}},
    # 出价面板的智能出价按钮（常亮，但没有回合号 → 走小字像素差识别兜底）
    "smart_bid_btn":      {"stage": "__round_phase__",    "priority": 80},
    # 回合巨型横幅（5 张互斥模板，取最高分且领先次高 ≥ margin）
    "round_big_banner":   {"stage": "__round_phase__",    "priority": 70,
                           "round_from_template": True, "margin": 0.03},
    # 进入对局前
    "appraiser_title":    {"stage": "选择鉴宝师",         "priority": 60},
    "is_matching_btn":    {"stage": "匹配中",             "priority": 60},
    "participation_card": {"stage": "游戏大厅",           "priority": 50},
    "hall_peak_appraise_card": {"stage": "游戏大厅",       "priority": 50},
    "goto_appraise_btn":  {"stage": "活动页面",           "priority": 50},
    "hall_session_cards": {"stage": "鉴宝大厅(选择场次)", "priority": 50},
}

_ROUND_RE = re.compile(r"round(\d+)", re.IGNORECASE)


class TreasureStageDetector:
    """巅峰鉴宝自动阶段检测器（无状态，局部 ROI 匹配）"""

    def __init__(self, proj: Path, ocr=None):
        self.tpl_dir = IMAGE_DIR
        self._tpl_cache: dict[str, tuple[int, int, np.ndarray | None]] = {}
        # P4b：policy.json perception 段的 DetectionPlan 是**唯一真源**（v3
        # treasure_assets.json 已退役）。加载失败或真源缺失 → plan=None，阶段检测
        # 降级为空（不保留 v2 文件回退）。经插件包级 nav_source() 共用缓存加载；
        # 局部导入保持 detector 的 cv2/numpy 运行时依赖不泄漏到纯标准库 navkit 包。
        self.plan = None
        try:
            from maaracing_assistant.plugins.treasure import nav_source
            nav = nav_source()
            if nav is not None:
                self.plan = nav.plan
        except Exception as exc:
            logger.log(f"[鉴宝检测器] v4 DetectionPlan 加载失败: {exc}", "WARNING")
        if self.plan is None:
            logger.log(
                "[鉴宝检测器] 无可用 v4 DetectionPlan（policy.json 缺失/损坏），阶段检测跳过",
                "WARNING",
            )
        self.match_scales = tuple(self.plan.scales) if self.plan is not None else MATCH_SCALES
        self.match_threshold = (
            float(self.plan.default_threshold) if self.plan is not None else MATCH_THRESHOLD
        )
        if self.plan is not None:
            self.ROI = {
                name: tuple(spec.rect) for name, spec in self.plan.spec.items()
                if spec.kind == "template"
            }
            self.ROI_TPL = {
                name: list(spec.templates) for name, spec in self.plan.spec.items()
                if spec.kind == "template"
            }
            self.roi_thresholds = {
                name: spec.threshold for name, spec in self.plan.spec.items()
                if spec.threshold is not None
            }
        else:
            self.ROI = {}
            self.ROI_TPL = {}
            self.roi_thresholds = {}
        self.schema = {}
        # ROI 级自定义阈值 self.roi_thresholds：来自 plan.spec.threshold。供外部
        # （如 treasure_module._match_bid_smart_btn）与 detect()/banner_result 同源取阈值。
        self._weak_alert_ts: dict[str, float] = {}
        # 回合小字 OCR：识别不到回合号（横幅未命中）时激活一次，用 OCR 读「第N回合」
        # 文字提取回合数。引擎懒加载、失败自动降级，detector 自身不持有也不初始化引擎。
        self._ocr = ocr
        # 横幅权威回合号：仅由 round_big_banner 模板命中时更新（1~5 识别率 100%）。
        # 标准回合（<5）内 smart_bid_btn 命中用此值，不碰小字——小字比横幅早 1 帧变号，
        # 会提前切回合导致上一回合尾帧报价（如第 4 槽）被误标成新回合（丢失+污染）。
        self._last_round: int | None = None
        # 附加回合小字兜底开关：默认关闭，小字完全不参与回合号判定（标准回合 1~5 由横幅
        # 100% 覆盖）。仅当「第 5 回合 4 人报价读全 + 第一名=第二名（平局）+ 未进入结算」时，
        # 由上层 treasure_module 置 True，此时横幅模板（只有 round1~5）识别不到第 6+ 回合，
        # 需要小字 OCR 兜底识别真实回合号（>5，stage 名 clamp 到第 5 回合，raw_r 保留原始号）。
        self._allow_label_fallback: bool = False
        # 最近一次 detect() 命中的 ROI key（"daily_high_banner" / "egg_reward_title" / 其它 /
        # None=无命中）。合并后的「结算弹窗」阶段靠它区分具体弹窗（今日最高/彩蛋 vs 等级提升盲点）。
        self._last_hit_roi_key: str | None = None
        self._last_detect_scores: dict[str, float] = {}

    # ---------------- 对外主接口 ----------------
    def detect(
        self,
        frame_rgb: np.ndarray,
        active_rois: set[str] | None = None,
    ) -> DetectResult:
        """阶段检测（动态感知裁剪），统一返回 `DetectResult`（D7）。

        旧的二元组实现保留在 `_detect_legacy`，并通过 `DetectResult.__iter__` 兼容
        `stage, round_no = detector.detect(...)`。v3 资产加载成功时，ROI/模板/阈值
        来自 `DetectionPlan`；缺失或显式 `NAVKIT_SOURCE=v2` 时走旧实现。
        """
        legacy_stage, legacy_round = self._detect_legacy(frame_rgb, active_rois)
        if self.plan is not None:
            active_used = (
                tuple(self.plan.spec) if active_rois is None else tuple(sorted(active_rois))
            )
        else:
            active_used = tuple(sorted(active_rois)) if active_rois is not None else tuple(_ROI_STAGE)
        # `_detect_legacy` 在实际扫描同一帧时同步收集最高分，避免为了 trace 再做一遍
        # 19 ROI × 13 scales 的模板匹配（原实现会让 3582 帧回归耗时成倍增加）。
        return DetectResult(
            stage=legacy_stage,
            round_no=legacy_round,
            scores=dict(self._last_detect_scores),
            hit_anchor=self._last_hit_roi_key,
            active_used=active_used,
        )

    def _detect_legacy(
        self,
        frame_rgb: np.ndarray,
        active_rois: set[str] | None = None,
    ) -> tuple[str | None, int | None]:
        """v2 阶段检测实现（S1 等价回归对照；不再作为公共回传类型）。

        active_rois：本帧只匹配这些 stage ROI 键；None = 全量匹配（调试台/断点/测试用）。
        未命中的 ROI 不参与扫描 → 非当前阶段的背景元素不会干扰判定（配合阶段感知清单），
        也让阶段内阈值可以放宽而不担心跨阶段误识别。
        """
        H, W = frame_rgb.shape[:2]
        gray = cv2.cvtColor(frame_rgb, cv2.COLOR_RGB2GRAY)
        self._last_detect_scores = {}

        # 按计划优先级从高到低扫描；NAVKIT_SOURCE=v2 才使用旧常量顺序。
        # active_rois 提供时只扫描交集，优先级排序与命中短路逻辑不受影响。
        if self.plan is not None:
            scan_keys = sorted(
                self.plan.detect_anchors,
                key=lambda name: -self.plan.spec[name].stage_priority,
            )
        else:
            scan_keys = sorted(_ROI_STAGE, key=lambda k: -_ROI_STAGE[k]["priority"])
        for roi_key in scan_keys:
            if active_rois is not None and roi_key not in active_rois:
                continue
            rect = self.ROI.get(roi_key)
            if not rect:
                continue
            plan_spec = self.plan.spec.get(roi_key) if self.plan is not None else None
            if plan_spec is not None:
                st = {
                    "stage": plan_spec.stage or "",
                    "priority": plan_spec.stage_priority,
                    "margin": plan_spec.arbitration.get("margin", 0.0),
                    "round_from_template": plan_spec.arbitration.get("round_from_template", False),
                    "thresholds": plan_spec.arbitration.get("template_thresholds", {}),
                }
                templates = list(plan_spec.templates)
            else:
                st = _ROI_STAGE[roi_key]
                templates = self.ROI_TPL.get(roi_key) or []
            if not templates:
                continue

            # 聚合匹配：同 ROI 多模板都跑一遍，取最高分（及次高分）
            best = None  # (score, template_name)
            second_score = 0.0
            for t in templates:
                gt = self._load_gray(Path(t).stem)
                if gt is None:
                    continue
                s = self._match_local(gray, gt, rect[0], rect[1], rect[2], rect[3], W, H)
                if best is None or s > best[0]:
                    second_score = best[0] if best else 0.0
                    best = (s, t)
                elif s > second_score:
                    second_score = s
            if best is None:
                continue
            score, tpl_name = best
            self._last_detect_scores[roi_key] = float(score)

            # 先解析该 ROI+命中模板的实际阈值（优先级同上），供弱匹配告警 + 命中判定共享
            plan_spec = self.plan.spec.get(roi_key) if self.plan is not None else None
            plan_tpl_th = (
                (plan_spec.arbitration.get("template_thresholds", {}) or {}).get(Path(tpl_name).name)
                if plan_spec is not None else None
            )
            per_tpl_th = plan_tpl_th
            if per_tpl_th is None:
                per_tpl_th = st.get("thresholds", {}).get(Path(tpl_name).stem)
            if per_tpl_th is not None:
                threshold = float(per_tpl_th)
            else:
                roi_th = plan_spec.threshold if plan_spec is not None else None
                threshold = roi_th if isinstance(roi_th, float) else self.match_threshold

            # 弱匹配：[threshold - 0.25, threshold) 区间，便于发现「差一点命中」但低于 threshold 的情况
            weak_low = max(0.50, threshold - 0.25)
            if weak_low <= score < threshold and not self._weak_alerted(roi_key):
                logger.log(
                    f"[鉴宝检测器] 弱匹配 {Path(tpl_name).stem} @ {roi_key} "
                    f"score={score:.3f}（阈 {threshold:.3f}），可能是 ROI 偏移或模板过期",
                    "DEBUG",
                )
            arbitration = plan_spec.arbitration if plan_spec is not None else {}
            margin = float(arbitration.get("margin", st.get("margin", 0.0)))
            if margin > 0.0:
                if score < threshold or (score - second_score) < margin:
                    continue
            elif score < threshold:
                continue

            # 命中
            self._last_hit_roi_key = roi_key
            if st["stage"] == "__round_phase__":
                plan_spec = self.plan.spec.get(roi_key) if self.plan is not None else None
                round_from_template = bool(
                    (plan_spec.arbitration.get("round_from_template", False)
                     if plan_spec is not None else st.get("round_from_template", False))
                )
                if round_from_template:
                    # round_big_banner（回合横幅，1~5 识别率 100%）= 回合号权威来源，
                    # 模板命中的回合号即时生效并更新 _last_round。
                    r = self._round_from_template(tpl_name)
                    if r is not None:
                        self._last_round = r
                        return (f"第{r}回合出价", r)
                    return (None, None)
                # smart_bid_btn（出价面板开，priority 80 先于横幅检查）：标准回合用
                # _last_round（横幅上次结果），不碰小字——小字比横幅早 1 帧变号，
                # R2→R3 切换时小字先读 3 会把 R2 尾帧第 4 槽报价误标成 R3（丢失+污染）。
                if self._last_round is not None and self._last_round < 5:
                    return (f"第{self._last_round}回合出价", self._last_round)
                # 附加回合（第 5 回合平局追加，_allow_label_fallback 由上层激活）：
                # 横幅模板只有 1~5，识别不到 6+，用小字 OCR 读真实回合号。
                # stage 名 clamp 到第 5 回合（在 STAGE_ORDER 内），raw_r 保留原始号
                # 让上层感知附加回合切换（转场期重置/新 epoch）。
                if self._allow_label_fallback:
                    r = self._detect_round_full(frame_rgb, W, H)
                    if r is not None:
                        return (f"第{min(r, 5)}回合出价", r)
                return (None, None)
            return (st["stage"], None)

        # 兜底：横幅/smart 都没命中。标准回合（_last_round < 5）属转场/画面抖动，
        # 返回 None 保持现状（等横幅出现，避免小字提前切号）。
        # 附加回合（_allow_label_fallback=True）时横幅识别不到第 6+ 回合，用小字兜底。
        if self._allow_label_fallback:
            r = self._detect_round_full(frame_rgb, W, H)
            if r is not None:
                return (f"第{min(r, 5)}回合出价", r)
        self._last_hit_roi_key = None  # 无命中：弹窗链阶段区分"等级提升盲点"
        return (None, None)

    def banner_result(self, frame_rgb: np.ndarray) -> str | None:
        """中标结算阶段：判断竞拍结果横幅命中的是「中标」还是「未中标」模板。

        返回 "win" | "fail" | None（ROI/模板未配置、都不命中）。
        仅在 result_banner ROI 内对 win/fail 两个模板匹配取最高分，阈值与 detect()
        保持一致（win 模板有单独放宽阈值 0.60）。供 treasure_module 记录落盘字段。
        """
        rect = self.ROI.get("result_banner")
        tpls = self.ROI_TPL.get("result_banner") or []
        if not rect or not tpls:
            return None
        H, W = frame_rgb.shape[:2]
        gray = cv2.cvtColor(frame_rgb, cv2.COLOR_RGB2GRAY)
        best_name: str | None = None
        best_score = -1.0
        for t in tpls:
            gt = self._load_gray(Path(t).stem)
            if gt is None:
                continue
            s = self._match_local(gray, gt, rect[0], rect[1], rect[2], rect[3], W, H)
            if s > best_score:
                best_score = s
                best_name = t
        if best_name is None:
            return None
        # 阈值解析与 detect() 一致：优先 per-模板（win 0.60），再 ROI 通用，最后全局。
        # v3 锚点 arbitration.template_thresholds 以「无扩展名」为键，故 name 查不中再按 stem 查，
        # 命中即来自 plan；仅 plan 缺失（v2 模式）才回退 _ROI_STAGE 常量（M3：v3 下 _ROI_STAGE 不再可达）。
        plan_spec = self.plan.spec.get("result_banner") if self.plan is not None else None
        per_tpl_th = None
        if plan_spec is not None:
            ths = plan_spec.arbitration.get("template_thresholds", {}) or {}
            per_tpl_th = ths.get(Path(best_name).name)
            if per_tpl_th is None:
                per_tpl_th = ths.get(Path(best_name).stem)
        if per_tpl_th is None and self.plan is None:
            per_tpl_th = _ROI_STAGE["result_banner"].get("thresholds", {}).get(Path(best_name).stem)
        if per_tpl_th is not None:
            threshold = float(per_tpl_th)
        else:
            roi_th = plan_spec.threshold if plan_spec is not None else None
            threshold = roi_th if isinstance(roi_th, float) else self.match_threshold
        if best_score < threshold:
            return None
        if "win" in best_name:
            return "win"
        if "fail" in best_name:
            return "fail"
        return None

    @staticmethod
    def _round_from_template(tpl_name: str) -> int | None:
        """从模板文件名解析回合号，如 round3_banner.png → 3。"""
        m = _ROUND_RE.search(tpl_name)
        return int(m.group(1)) if m else None

    # ---------------- 内部工具 ----------------
    @staticmethod
    def _round_no_from_text(text: str) -> int | None:
        """从 OCR 文本提取回合号：支持「第1回合」「Round 2」「1/3」等常见写法。
        回合小字区域只有 1 个字+数字，命中任意数字即返回；允许 1~9（附加回合平局追加可到 6+，
        stage 名 clamp 由调用方做），0/两位数/无数字视为噪声返回 None。"""
        if not text:
            return None
        m = re.search(r"(\d+)", text)
        if not m:
            return None
        r = int(m.group(1))
        return r if 1 <= r <= 9 else None

    def _round_label_rect(self) -> tuple[float, float, float, float] | None:
        """回合小字识别区域。

        唯一路径：读 `DetectionPlan.spec["round_label_area"].rect`（数据面 loader 把
        **全部**锚点含 ocr 类都纳入 spec，故此 ocr 锚点在 plan 里可取）。plan 缺失
        （真源不可用）时返回 None——旧 v2 schema 回退分支自 v2 读取器移除后即为
        死路（self.schema 恒空），常量本体随 P4d v2 回退段一并清理。
        """
        if self.plan is not None:
            spec = self.plan.spec.get("round_label_area")
            if spec is None:
                return None
            r4 = list(spec.rect)
            if len(r4) == 4:
                return (float(r4[0]), float(r4[1]), float(r4[2]), float(r4[3]))
            return None
        if not isinstance(self.schema, dict):
            return None
        for seg_key in ("ocr", "round_labels"):
            seg = self.schema.get(seg_key)
            if not isinstance(seg, dict):
                continue
            rla = seg.get("round_label_area")
            if isinstance(rla, dict) and isinstance(rla.get("rect"), list) and len(rla["rect"]) == 4:
                r4 = rla["rect"]
                return (float(r4[0]), float(r4[1]), float(r4[2]), float(r4[3]))
        return None

    def _weak_alerted(self, roi_key: str) -> bool:
        """弱匹配告警节流：同一 ROI 最多每 30 秒告警一次，避免刷屏。"""
        now = time.time()
        last = self._weak_alert_ts.get(roi_key, 0.0)
        if now - last < 30.0:
            return True
        self._weak_alert_ts[roi_key] = now
        return False

    def _load_gray(self, name_no_ext: str) -> np.ndarray | None:
        """加载灰度模板，按文件 mtime_ns + size 失效缓存（R4）。

        控制台换图后同一路径可能仍被旧缓存命中；只按文件名永久缓存会让用户看到
        "改了没用"。不存在/读取失败也缓存当前指纹，文件后来出现时指纹变化会重读。
        """
        path = self.tpl_dir / f"{name_no_ext}.png"
        try:
            stat = path.stat()
            fingerprint = (int(stat.st_mtime_ns), int(stat.st_size))
        except OSError:
            fingerprint = (-1, -1)
        cached = self._tpl_cache.get(name_no_ext)
        if cached is not None and cached[:2] == fingerprint:
            return cached[2]
        if fingerprint == (-1, -1):
            self._tpl_cache[name_no_ext] = (*fingerprint, None)
            return None
        img = cv2.imread(str(path))
        if img is None:
            self._tpl_cache[name_no_ext] = (*fingerprint, None)
            return None
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        self._tpl_cache[name_no_ext] = (*fingerprint, gray)
        return gray

    @staticmethod
    def _crop(gray_big, x1n, y1n, x2n, y2n, W, H):
        x1, y1 = max(0, int(x1n * W)), max(0, int(y1n * H))
        x2, y2 = min(W, int(x2n * W)), min(H, int(y2n * H))
        if x2 <= x1 or y2 <= y1:
            return None
        return gray_big[y1:y2, x1:x2]

    def _match_local(self, gray_big, gray_tpl, x1n, y1n, x2n, y2n, W, H) -> float:
        """ROI 内多尺度模板匹配，返回所有尺度下的最高命中分（0~1）。

        与调试台 server.match_local 同一算法：模板在 MATCH_SCALES（0.70×~1.30×）
        逐档缩放取最优分。非标准窗口/DPI 下画面内容会被重采样缩放，模板渲染尺寸
        可能偏离 1.0×，单尺度匹配会漏检（分数被拉低、横幅互斥区分度消失）；
        多尺度把实际渲染尺寸对应的最优档找出来，保证运行时分数与调试台校准口径一致。
        """
        crop = self._crop(gray_big, x1n, y1n, x2n, y2n, W, H)
        if crop is None:
            return 0.0
        th0, tw0 = gray_tpl.shape[:2]
        ch, cw = crop.shape[:2]
        best = 0.0
        attempted = False
        for s in self.match_scales:
            nw = max(4, int(round(tw0 * s)))
            nh = max(4, int(round(th0 * s)))
            if nh > ch or nw > cw:
                continue  # 该尺度放不下，跳过（与调试台一致）
            attempted = True
            if nw == tw0 and nh == th0:
                tpl_s = gray_tpl
            else:
                try:
                    # 缩小时用 AREA（避免锯齿），放大时用 CUBIC
                    interp = cv2.INTER_AREA if s < 1.0 else cv2.INTER_CUBIC
                    tpl_s = cv2.resize(gray_tpl, (nw, nh), interpolation=interp)
                except Exception:
                    continue
            try:
                res = cv2.matchTemplate(crop, tpl_s, cv2.TM_CCOEFF_NORMED)
                _, max_val, _, _ = cv2.minMaxLoc(res)
            except cv2.error:
                continue
            if float(max_val) > best:
                best = float(max_val)
        if not attempted:
            # 模板即使缩到最小档 0.70× 仍超出 ROI → 该 ROI 永远无法命中
            now = time.time()
            if now - getattr(self, "_last_size_warn", 0.0) > 10.0:
                self._last_size_warn = now
                logger.log(
                    f"[鉴宝检测器] ROI 尺寸不足 (crop {cw}×{ch} < tpl {tw0}×{th0}×0.70)，"
                    f"该 ROI 永远无法命中，请用调试台调大",
                    "WARNING",
                )
        return best

    def _detect_round_full(self, frame_rgb, W, H) -> int | None:
        """识别不到回合（横幅未命中）时激活一次：OCR 读回合小字区域 → 提取回合号。

        仅当注入的 OCR 引擎可用时执行；引擎未注入/加载失败/文本无数字均返回 None，
        由调用方（detect）走兜底，不抛异常、不阻塞主流程。
        """
        if self._ocr is None:
            return None
        rect = self._round_label_rect()
        if rect is None:
            return None
        info = self._ocr.recognize_single(frame_rgb, rect)
        if info is None:
            return None
        text = info.get("text") or ""
        return self._round_no_from_text(text)
