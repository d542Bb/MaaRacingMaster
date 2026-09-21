#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
巅峰鉴宝阶段自动检测器（P4c 起：匹配统一走 core.template_match 引擎）。

设计原则：
  1. **局部 ROI 匹配**：每个模板只在「它理论上会出现的那一小块区域」内搜索，
     避免大图里其他相似文字造成假阳性。
  2. **模板只选「独有的稳定元素」**：巨型文字横幅 / 独有的卡片文字标题 /
     右上角「第 N 回合」完整小字（整段 89×25，因为每个 N 的整段字结构不同）。
  3. **强特征优先**：结算页的「竞拍失败」「最终竞拍价格」绝对唯一，优先级最高。
  4. **不要求全覆盖**：模糊阶段（匹配中、主题抽取动画）返回 None，
     上层状态机按「最近一次稳定阶段 + 时间推移」自行推进即可。
  5. **色彩空间按锚点声明**（policy.json `perception.spec.<锚点>.colorspace`）：
     默认 rgb；回合横幅/结算横幅等高成本锚点声明 gray（帧预算与历史灰度校准
     保真，对拍报告见 commit `0554f58`）。本文件不再有第二套
     匹配实现——缩放口径（整数尺寸 + AREA/CUBIC）已收敛进 find_template。

用法：
    detector = TreasureStageDetector(proj)
    stage, round_no = detector.detect(frame_rgb)
"""
# pyright: reportAttributeAccessIssue=false, reportOptionalMemberAccess=false, reportOptionalCall=false, reportOptionalOperand=false, reportGeneralTypeIssues=false, reportArgumentType=false, reportPossiblyUnboundVariable=false, reportOperatorIssue=false
# 冻结域豁免（treasure 活动下线在即，开发方向转 speedrush）：上述报错几乎全部源于
# ctx=None 离线只读形态下的属性访问，类型收窄需要域级重构，冻结期不做。
# speedrush 新域不得复制本豁免。
from __future__ import annotations

import re
import time
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from maaracing_master.core.logger import logger
from maaracing_master.core.navkit import ROUND_PHASE_STAGE
from maaracing_master.core.template_match import best_match_score, load_template
from maaracing_master.plugins.treasure import IMAGE_DIR

# 回合族阶段名（本类按回合号实例化）：与 _scan 哨兵分支的生成口径配对。
_ROUND_STAGE_RE = re.compile(r"^第\d+回合出价$")


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


# ============================================================
# 搜索 ROI / 模板 / 阈值：唯一真源 policy.json perception 数据面 → DetectionPlan
# （__init__ 载入）。rect 为归一化坐标 (x1n, y1n, x2n, y2n)，匹配时乘当前输入帧
# W/H 换算像素搜索区。plan 缺失（policy.json 不可用）→ 检测降级为空，
# 不做任何常量兜底（M4/E1 定案：避免残缺默认值掩盖真实配置导致阶段漏检）。
# ============================================================

_ROUND_RE = re.compile(r"round(\d+)", re.IGNORECASE)


class TreasureStageDetector:
    """巅峰鉴宝自动阶段检测器（无状态，局部 ROI 多尺度匹配）"""

    def __init__(self, proj: Path, ocr=None):
        # P4c：policy.json perception 段的 DetectionPlan 是**唯一真源**。加载失败
        # 或真源缺失 → plan=None，阶段检测降级为空。经插件包级 nav_source() 共用
        # 缓存加载；局部导入保持 detector 的运行时依赖不泄漏到纯标准库 navkit 包。
        self.plan = None
        try:
            from maaracing_master.plugins.treasure import nav_source
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
            self.ROI: dict[str, tuple] = {}
            self.ROI_TPL: dict[str, list[str]] = {}
            self.roi_thresholds: dict[str, float] = {}
            self.match_scales: tuple[float, ...] = ()
            self.match_threshold = 0.75
        else:
            self.match_scales = tuple(self.plan.scales)
            self.match_threshold = float(self.plan.default_threshold)
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
        # 模板的灰度投影缓存（热修失效靠源数组对象身份——engine 重读时返回新
        # ndarray，id 变化即重转）：{name: (id(rgb_arr), gray_arr)}
        self._gray_view: dict[str, tuple[np.ndarray, np.ndarray]] = {}
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

        `DetectResult.__iter__` 兼容 `stage, round_no = detector.detect(...)`。
        ROI/模板/阈值/色彩空间全部来自 `DetectionPlan`（policy.json 数据面）；
        plan 缺失 → 直接空结果（无常量兜底）。
        """
        if self.plan is None:
            return DetectResult(stage=None, round_no=None, scores={},
                                hit_anchor=None, active_used=())
        stage, round_no, hit_tpl, hit_box = self._scan(frame_rgb, active_rois)
        active_used = (
            tuple(self.plan.spec) if active_rois is None else tuple(sorted(active_rois))
        )
        return DetectResult(
            stage=stage,
            round_no=round_no,
            scores=dict(self._last_detect_scores),
            hit_anchor=self._last_hit_roi_key,
            active_used=active_used,
            hit_template=hit_tpl,
            hit_box=hit_box,
        )

    # ---------------- 帧内页面探针 ----------------
    @staticmethod
    def is_round_stage(stage: str | None) -> bool:
        """阶段名是否属于回合族（本类按回合号实例化的「第N回合出价」，见 _scan 哨兵分支）。

        回合族的唯一定义，两侧共用：_proof_anchors 判锚点归属，页面门控判令牌是否
        落在某信号的允许阶段集合内（见 module._ocr_filter_by_page）——同一份判据，
        不各自表述。
        """
        return bool(stage) and bool(_ROUND_STAGE_RE.match(stage))

    def _proof_anchors(self, stages: Iterable[str] | None) -> set[str]:
        """标志锚点：`active_for(stage)` 里「命中即进入该阶段」的模板锚点。

        active 混着邻页锚点（「领取分红」的 active 含 daily_high_banner，其归属阶段是
        「结算弹窗」），只有 spec.stage 等于本阶段的才是该页的现场证据；归属哨兵的锚点
        （smart_bid_btn / round_big_banner）对回合族阶段成立，与 _scan 实例化口径一致。
        """
        if self.plan is None:
            return set()
        out: set[str] = set()
        for stage in stages or ():
            for anchor in self.plan.active_for(stage) or ():
                spec = self.plan.spec.get(anchor)
                if spec is None or spec.kind != "template" or not spec.templates:
                    continue
                if spec.stage == stage or (
                        spec.stage == ROUND_PHASE_STAGE and self.is_round_stage(stage)):
                    out.add(anchor)
        return out

    def probe_page(self, frame_rgb: np.ndarray, stages: Iterable[str] | None) -> str | None:
        """帧内页面探针：本帧是否属于给定阶段之一，是则返回该页阶段名，否则 None。

        返回值口径：本帧命中归属具体阶段的锚点 → 该阶段名；命中回合族锚点
        （spec.stage 为 ROUND_PHASE_STAGE）→ 返回哨兵本身，即「本帧在出价面板页」。
        回合族内具体是第几回合不在这里推导——那是观察线程按周期维护的跨帧状态，而
        门控只问页族（见 module._ocr_filter_by_page）。

        供 OCR worker 对**被识别的那一帧**现场取页面令牌——令牌与像素同源，取代
        「投递时快照观察线程的周期判定」那套跨线程搬运（搬运必有窗口，实证见
        commit 69f52e9：结算页 ROI 在已转场的大厅帧上读到 300000）。

        锚点集、阈值、尺度全部来自 policy 数据面，与 detect() 共用同一个 _scan；
        差别只在 record=False——不写实例状态（`_last_hit_roi_key` / `_last_detect_scores`
        / `_last_round` 的生产者是观察线程，决策线程正在读它们）。
        认不出当前页（未命中任何标志锚点）→ None，由调用方按「认不出本页」处置。
        """
        anchors = self._proof_anchors(stages)
        if not anchors:
            return None
        stage, _round_no, _hit_tpl, _hit_box = self._scan(frame_rgb, anchors, record=False)
        return stage

    def confirm_round_page(self, frame_rgb: np.ndarray, round_no: int | None) -> bool:
        """同帧回合小字能否为「本帧属于回合族出价页」作证（probe_page 的第二类证据）。

        为什么需要第二类：标志锚点（smart_bid_btn / round_big_banner）只在回合内的**部分
        时段**可见——面板打开期与横幅闪现期；而 4 人报价数字是在「4 人都已出价、面板关闭」
        之后才逐个显示出来的。那段时间两个锚点同时缺席 → probe_page 返回 None → 页面门控
        fail-closed，把整段公开报价读数丢掉（实测帧：OCR 已正确读出 122,100 / 250,000 /
        163,100，令牌却是 None）。回合小字（round_label_area）在整个回合常驻，是同一帧上
        现成的证据。

        判据是**两个来源互相印证**，不是单方作数：本帧文字解析出的回合号必须等于调用方
        给的 round_no（生产口径 = 该帧投递时快照的 `_round_no`，即阶段标签当时的答案）。
        任一方缺失或不等 → False，回到 fail-closed。理由两侧都有前科：阶段标签单独说过话
        不算数（它曾在结算转场慢半拍，把大厅画面报成「领取分红」，见 commit d6181ca
        的 300000 条）；本帧文字单独说话也不算数（它只证明画面上写着「第 N 回合」，不证明
        这是哪一页——大厅/结算页顶部也可能出现数字）。

        回合号按 set_stage 的 clamp 口径比较（第 6+ 附加回合在阶段名里 clamp 成 5，
        `_round_no` 存的是 clamp 后的值）。只读一次性单 ROI OCR（复用 `_detect_round_full`
        的取字与解析，不另立第二套读法），不新增取图、不写任何实例状态。
        """
        if round_no is None:
            return False
        H, W = frame_rgb.shape[:2]
        parsed = self._detect_round_full(frame_rgb, W, H)
        if parsed is None:
            return False
        return min(parsed, 5) == round_no

    # ---------------- 扫描核心 ----------------
    def _px_roi(self, rect, W: int, H: int) -> tuple[int, int, int, int] | None:
        """归一化 rect (x1n,y1n,x2n,y2n) → 引擎像素搜索区 (x, y, w, h)。"""
        x1, y1, x2n, y2n = rect
        px1 = max(0, int(x1 * W))
        py1 = max(0, int(y1 * H))
        px2 = min(W, int(x2n * W))
        py2 = min(H, int(y2n * H))
        if px2 <= px1 or py2 <= py1:
            return None
        return px1, py1, px2 - px1, py2 - py1

    def _match_score(self, roi_key: str, tpl_name: str, frame_rgb: np.ndarray,
                     gray_frame: np.ndarray | None, px_roi, colorspace: str):
        """单模板在 ROI 内的多尺度最高分（+命中框）。

        gray 锚点走帧灰度投影（同帧一次转换、全锚共享）；rgb 直接吃彩色帧。
        两者共用同一个引擎函数 find_template——本文件不再有第二套匹配实现。
        """
        rgb_tpl = load_template(tpl_name, [IMAGE_DIR])
        if rgb_tpl is None:
            return None
        if colorspace == "gray":
            import cv2
            cached = self._gray_view.get(tpl_name)
            if cached is not None and cached[0] is rgb_tpl:
                gray_tpl = cached[1]
            else:
                # 源数组对象变化 = engine 已按新指纹重读（热修）→ 重转灰度投影。
                # 持源数组强引用做 `is` 身份判定（不依赖 id() 复用语义）。
                gray_tpl = cv2.cvtColor(rgb_tpl, cv2.COLOR_RGB2GRAY)
                self._gray_view[tpl_name] = (rgb_tpl, gray_tpl)
            if gray_frame is None:
                return None
            box, s = best_match_score(gray_frame, gray_tpl,
                                      scales=self.match_scales, roi=px_roi)
        else:
            box, s = best_match_score(frame_rgb, rgb_tpl,
                                      scales=self.match_scales, roi=px_roi)
        if box is None and s == 0.0:
            # 「尺寸不足」按几何判定：模板缩到最小档仍大于 ROI → 该 ROI 永远无法命中。
            # 不能拿 s==0.0 直接当判据——首帧黑屏等退化帧的 NCC 也恒为 0.0（曾误报
            # hall_peak_appraise_card「永远无法命中」，次帧即 0.729 弱命中）。
            tpl_used = gray_tpl if colorspace == "gray" else rgb_tpl
            _th, _tw = tpl_used.shape[:2]
            _ms = min(self.match_scales) if self.match_scales else 1.0
            if _tw * _ms > px_roi[2] or _th * _ms > px_roi[3]:
                # 节流窗口一律 monotonic（墙钟校时跳变会把窗口拉长或清零）
                now = time.monotonic()
                if now - getattr(self, "_last_size_warn", 0.0) > 10.0:
                    self._last_size_warn = now
                    logger.log(
                        f"[鉴宝检测器] ROI 尺寸不足（{roi_key}/{Path(tpl_name).stem} "
                        f"搜索区 {px_roi[2]}×{px_roi[3]}），该模板永远无法命中，请调大 ROI",
                        "WARNING",
                    )
        return box, s

    def _resolve_threshold(self, spec, tpl_name: str) -> float:
        """该 ROI+命中模板的实际阈值：per-模板仲裁表（带扩展名/裸名两查）
        → 锚点 threshold → 全局 default。与历史实现逐行同构。"""
        ths = (spec.arbitration.get("template_thresholds") or {})
        per_tpl = ths.get(tpl_name)
        if per_tpl is None:
            per_tpl = ths.get(Path(tpl_name).stem)
        if per_tpl is not None:
            return float(per_tpl)
        if isinstance(spec.threshold, float):
            return spec.threshold
        return float(self.match_threshold)

    def _scan(self, frame_rgb: np.ndarray, active_rois: set[str] | None,
              record: bool = True
              ) -> tuple[str | None, int | None, str | None, tuple | None]:
        """按计划优先级从高到低扫描锚点，命中短路。返回 (stage, round, 模板, 框)。

        active_rois：本帧只匹配这些锚点键；None = 全量匹配（调试/断点/测试用）。
        未命中的锚点不参与扫描 → 非当前阶段的背景元素不会干扰判定（配合阶段感知
        清单），也让阶段内阈值可以放宽而不担心跨阶段误识别。

        record=False：不写 `_last_detect_scores` / `_last_hit_roi_key` / `_last_round`。
        这三个字段的生产者约定是观察线程，决策线程正在读它们（结算弹窗分类、trace）；
        worker 侧的帧内判定（probe_page）走 record=False，只取返回值。
        """
        H, W = frame_rgb.shape[:2]
        if record:
            self._last_detect_scores = {}
        gray_frame = None  # 帧灰度投影，按首个 gray 锚点惰性转换

        scan_keys = sorted(
            self.plan.detect_anchors,
            key=lambda name: -self.plan.spec[name].stage_priority,
        )
        for roi_key in scan_keys:
            if active_rois is not None and roi_key not in active_rois:
                continue
            spec = self.plan.spec.get(roi_key)
            if spec is None or not spec.templates:
                continue
            px_roi = self._px_roi(tuple(spec.rect), W, H)
            if px_roi is None:
                continue
            if spec.colorspace == "gray" and gray_frame is None:
                import cv2
                gray_frame = cv2.cvtColor(frame_rgb, cv2.COLOR_RGB2GRAY)

            # 聚合匹配：同 ROI 多模板都跑一遍，取最高分（及次高分）
            best = None      # (score, template_name, box)
            second_score = 0.0
            for t in spec.templates:
                got = self._match_score(roi_key, t, frame_rgb, gray_frame,
                                        px_roi, spec.colorspace)
                if got is None:
                    continue
                box, s = got
                s = float(s)
                if best is None or s > best[0]:
                    second_score = best[0] if best else 0.0
                    best = (s, t, box)
                elif s > second_score:
                    second_score = s
            if best is None:
                continue
            score, tpl_name, hit_box = best
            if record:
                self._last_detect_scores[roi_key] = score

            threshold = self._resolve_threshold(spec, tpl_name)
            # 弱匹配：[threshold - 0.25, threshold) 区间，便于发现「差一点命中」但低于 threshold 的情况
            weak_low = max(0.50, threshold - 0.25)
            if weak_low <= score < threshold and not self._weak_alerted(roi_key):
                logger.log(
                    f"[鉴宝检测器] 弱匹配 {Path(tpl_name).stem} @ {roi_key} "
                    f"score={score:.3f}（阈 {threshold:.3f}），可能是 ROI 偏移或模板过期",
                    "DEBUG",
                )
            margin = float((spec.arbitration or {}).get("margin", 0.0))
            if margin > 0.0:
                if score < threshold or (score - second_score) < margin:
                    continue
            elif score < threshold:
                continue

            # 命中
            if record:
                self._last_hit_roi_key = roi_key
            if spec.stage == ROUND_PHASE_STAGE:
                # 探针（record=False）只回答「哪一页」，回合号对它无关：出价面板整族共用
                # 同一块招牌（smart_bid_btn / round_big_banner），令牌按族产出即可（门控
                # 按族放行，见 module._ocr_filter_by_page）。而回合号来自观察线程按周期
                # 维护的 _last_round——探针读它等于把跨线程陈旧值请回来；更糟的是该字段
                # 为空时下面的分支返回 None，并把自带回合号的横幅一并短路掉（实测：同一批
                # 515 帧，读 _last_round 只得 18 帧，不读可得 80 帧，丢的全是 smart_bid_btn
                # 命中帧）。
                if not record:
                    return (ROUND_PHASE_STAGE, None, tpl_name, hit_box)
                round_from_template = bool(
                    (spec.arbitration or {}).get("round_from_template", False))
                if round_from_template:
                    # round_big_banner（回合横幅，1~5 识别率 100%）= 回合号权威来源，
                    # 模板命中的回合号即时生效并更新 _last_round。
                    r = self._round_from_template(tpl_name)
                    if r is not None:
                        if record:
                            self._last_round = r
                        return (f"第{r}回合出价", r, tpl_name, hit_box)
                    return (None, None, tpl_name, hit_box)
                # smart_bid_btn（出价面板开，优先级先于横幅检查）：标准回合用
                # _last_round（横幅上次结果），不碰小字——小字比横幅早 1 帧变号，
                # R2→R3 切换时小字先读 3 会把 R2 尾帧第 4 槽报价误标成 R3（丢失+污染）。
                if self._last_round is not None and self._last_round < 5:
                    return (f"第{self._last_round}回合出价", self._last_round,
                            tpl_name, hit_box)
                # 附加回合（第 5 回合平局追加，_allow_label_fallback 由上层激活）：
                # 横幅模板只有 1~5，识别不到 6+，用小字 OCR 读真实回合号。
                # stage 名 clamp 到第 5 回合（在 STAGE_ORDER 内），raw_r 保留原始号
                # 让上层感知附加回合切换（转场期重置/新 epoch）。
                if self._allow_label_fallback:
                    r = self._detect_round_full(frame_rgb, W, H)
                    if r is not None:
                        return (f"第{min(r, 5)}回合出价", r, tpl_name, hit_box)
                return (None, None, tpl_name, hit_box)
            return (spec.stage or "", None, tpl_name, hit_box)

        # 兜底：横幅/smart 都没命中。标准回合（_last_round < 5）属转场/画面抖动，
        # 返回 None 保持现状（等横幅出现，避免小字提前切号）。
        # 附加回合（_allow_label_fallback=True）时横幅识别不到第 6+ 回合，用小字兜底。
        if self._allow_label_fallback:
            r = self._detect_round_full(frame_rgb, W, H)
            if r is not None:
                return (f"第{min(r, 5)}回合出价", r, None, None)
        if record:
            self._last_hit_roi_key = None  # 无命中：弹窗链阶段区分"等级提升盲点"
        return (None, None, None, None)

    def banner_result(self, frame_rgb: np.ndarray) -> str | None:
        """中标结算阶段：判断竞拍结果横幅命中的是「中标」还是「未中标」模板。

        返回 "win" | "fail" | None（ROI/模板未配置、都不命中）。
        仅在 result_banner ROI 内对 win/fail 两个模板匹配取最高分，阈值与 detect()
        保持一致（win 模板有单独放宽阈值 0.60）。供 treasure_module 记录落盘字段。
        """
        if self.plan is None:
            return None
        spec = self.plan.spec.get("result_banner")
        if spec is None or not spec.templates:
            return None
        H, W = frame_rgb.shape[:2]
        px_roi = self._px_roi(tuple(spec.rect), W, H)
        if px_roi is None:
            return None
        gray_frame = None
        if spec.colorspace == "gray":
            import cv2
            gray_frame = cv2.cvtColor(frame_rgb, cv2.COLOR_RGB2GRAY)
        best_name: str | None = None
        best_score = -1.0
        for t in spec.templates:
            got = self._match_score("result_banner", t, frame_rgb, gray_frame,
                                    px_roi, spec.colorspace)
            if got is None:
                continue
            _box, s = got
            if s > best_score:
                best_score = float(s)
                best_name = t
        if best_name is None:
            return None
        if best_score < self._resolve_threshold(spec, best_name):
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
        """回合小字识别区域：读 `DetectionPlan.spec["round_label_area"].rect`
        （数据面 loader 把**全部**锚点含 ocr 类都纳入 spec，故此 ocr 锚点在 plan
        里可取）。plan 缺失/锚点缺失返回 None（旧 v2 schema 回退死路已随 P4c 清除）。
        """
        if self.plan is None:
            return None
        spec = self.plan.spec.get("round_label_area")
        if spec is None:
            return None
        r4 = list(spec.rect)
        if len(r4) == 4:
            return (float(r4[0]), float(r4[1]), float(r4[2]), float(r4[3]))
        return None

    def _weak_alerted(self, roi_key: str) -> bool:
        """弱匹配告警节流：同一 ROI 最多每 30 秒告警一次，避免刷屏（窗口一律 monotonic）。"""
        now = time.monotonic()
        last = self._weak_alert_ts.get(roi_key, 0.0)
        if now - last < 30.0:
            return True
        self._weak_alert_ts[roi_key] = now
        return False

    def _detect_round_full(self, frame_rgb, W, H) -> int | None:
        """识别不到回合（横幅未命中）时激活一次：OCR 读回合小字区域 → 提取回合号。

        仅当注入的 OCR 引擎可用时执行；引擎未注入/加载失败/文本无数字均返回 None，
        由调用方（_scan）走兜底，不抛异常、不阻塞主流程。
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
