#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""跳转图：把「大厅 → 模块入口页」这类页面跳转交给 MAA Pipeline 跑。

设计只有三条：
  1. 地图（节点/边/阈值/ROI）写在 pipeline JSON 里，不写在 Python 里；
  2. 框架负责跑图循环（截图-识别-动作-走边-超时重试），我们只提供两个桥：
       MRA_Template  识别：在宿主帧上找模板，把命中框交给框架
       MRA_Click     动作：把命中框中心交给 core.clicker.Clicker（自动按
                     click_mode 分派 前台鼠标 / 后台手柄导航+A / 意图不确认）
  3. 目标坐标永远来自识别框，不来自代码里写死的百分比。

于是新增一个活动模块的导航 = 写一份 NavKit v3 资产（见下），不需要再写一遍
导航匹配代码；游戏把入口挪了位置 = 换模板图或改 roi，不需要动 Python。

导航真源（用户 2026-09-07 拍板）：
    页面/锚点/入口链/出价策略等一切导航地图 → NavKit v3 资产
        plugins/<id>/resources/config/<id>_assets.json   模块自己的段
        core/resources/config/global_assets.json          跨模块共用段（大厅骨架）
    由 core/navkit 编译与校验，经 `Assets.load` 在 Python 侧直读执行。

本文件提供的是 MAA Pipeline 执行通路（`Resource.post_pipeline` + 自定义识别/动作桥），
当前尚未接线：全仓无生产实例化点（`plugins/treasure/module.py:804` 仅类型标注；
上方 docstring 中的用法示例不计）。留作后续把图交给框架跑的备选实现，
新模块接入导航请走上面的 v3 资产，不要再另建一套手写入口链。

资源一律留在程序目录内（便携包解压在哪资源就在哪），不往 C 盘复制。
"""
from __future__ import annotations

import json
import time
from contextlib import ExitStack
from pathlib import Path

from maa.custom_action import CustomAction
from maa.custom_recognition import CustomRecognition
from maa.resource import Resource
from maa.tasker import Tasker

from maaracing_assistant.core.clicker import Clicker
from maaracing_assistant.core.logger import logger
from maaracing_assistant.core.pipeline_logger import PipelineLogger
from maaracing_assistant.core.template_match import DEFAULT_SCALES, find_any

# core 自带资源根（stick_speed_model.json 也在这，不新开目录约定）
CORE_RES_DIR = Path(__file__).resolve().parent / "resources"

RECOGNIZER_NAME = "MRA_Template"
ACTION_NAME = "MRA_Click"


def _parse(raw: str) -> dict:
    """框架传进来的节点参数是 JSON 字符串，坏数据按空参数处理（图会走 on_error）。"""
    try:
        val = json.loads(raw) if raw else {}
    except (ValueError, TypeError):
        return {}
    return val if isinstance(val, dict) else {}


class TemplateRecognizer(CustomRecognition):
    """识别桥：在宿主截图帧上多尺度匹配候选模板，命中框交给框架决定走哪条边。

    节点参数（写在 pipeline JSON 的 custom_recognition_param 里）：
        templates      ["activity_page_template"]   候选图名（不带扩展名），取最高分
        threshold      0.70                          匹配下限
        roi            [0.55,0.60,1.00,0.85]         可选，归一化搜索区（换分辨率不失效）
        scales         [0.5,0.7,...]                 可选，覆盖默认尺度表
        fallback_pct   [0.880,0.720]                 可选，模板全落空时退到百分比坐标
        expect_absent  true                          可选，模板消失才算命中（终点确认用）
    """

    def __init__(self, graph: "NavGraph"):
        super().__init__()
        self._graph = graph

    def analyze(self, context, argv):
        p = _parse(argv.custom_recognition_param)
        frame = self._graph.frame()
        if frame is None:
            return self.AnalyzeResult(box=None, detail={"error": "截图失败"})

        names = [n for n in p.get("templates", []) if isinstance(n, str) and n]
        if not names and not p.get("fallback_pct"):
            # 模板清单为空且没有固定坐标兜底才是配置错误。
            # D2 的 point 目标本来就不配模板，由 guarded_by + fallback_pct 证明/定位；
            # 只有 expect_absent 仍必须拒绝空模板，避免"什么都没找到=已消失"假成功。
            logger.log(f"[跳转图] 节点「{argv.node_name}」未配置 templates/fallback_pct，判为识别失败", "ERROR")
            return self.AnalyzeResult(box=None, detail={"error": "templates/fallback_pct 未配置"})
        if p.get("expect_absent") and not names:
            return self.AnalyzeResult(box=None, detail={"error": "expect_absent 不允许空 templates"})
        H, W = frame.shape[:2]
        roi = None
        if p.get("roi"):
            x1, y1, x2, y2 = p["roi"]
            roi = (int(x1 * W), int(y1 * H),
                   int((x2 - x1) * W), int((y2 - y1) * H))

        box, score, hit_name = find_any(
            frame, names, self._graph.image_dirs,
            threshold=float(p.get("threshold", 0.7)),
            scales=p.get("scales") or DEFAULT_SCALES, roi=roi)

        if p.get("expect_absent"):
            # 「模板消失才算到位」：用于点了按钮之后原按钮消失这类终点确认。
            # 命中时返回 1x1 空框只为骗过框架的"有框=成功"，这类节点必须配
            # DoNothing，不能拿去定位点击。
            hit = box is None
            return self.AnalyzeResult(box=(0, 0, 1, 1) if hit else None,
                                      detail={"absent": hit, "score": round(score, 3)})

        if box is None:
            # pct 兜底：只为「按钮小图还没截」的过渡期不阻塞跑图，补上图即失效。
            pct = p.get("fallback_pct")
            if not pct:
                return self.AnalyzeResult(box=None, detail={"score": round(score, 3)})
            cx, cy = int(pct[0] * W), int(pct[1] * H)
            box = (cx - 20, cy - 20, cx + 20, cy + 20)
            hit_name = "fallback_pct"
            logger.log(f"[跳转图] 模板未命中，退到百分比兜底 {pct}", "WARNING")

        return self.AnalyzeResult(
            box=box,
            detail={"template": hit_name, "score": round(score, 3), "name": argv.node_name})


class ClickAction(CustomAction):
    """动作桥：识别框中心 → Clicker 点击（前台鼠标 / 后台手柄导航+A / 意图）。

    节点参数：
        wait_after_ms  800    点击后的停顿（页面动画）
        timeout_s      20.0   手柄导航到位的等待上限（超时算失败，走 on_error）
    """

    def __init__(self, graph: "NavGraph"):
        super().__init__()
        self._graph = graph

    def run(self, context, argv):
        p = _parse(argv.custom_action_param)
        x1, y1, x2, y2 = argv.box
        W, H = self._graph.frame_size()
        if W <= 0 or H <= 0:
            return False
        cx, cy = ((x1 + x2) / 2) / W, ((y1 + y2) / 2) / H
        box_norm = (abs(x2 - x1) / W, abs(y2 - y1) / H)

        ok = self._graph.click(cx, cy, box_norm,
                               timeout_s=float(p.get("timeout_s", 20.0)))
        self._graph.ctx.lifecycle.sleep(float(p.get("wait_after_ms", 800)) / 1000.0)
        if not ok:
            logger.log(f"[跳转图] 节点「{argv.node_name}」点击未到位", "WARNING")
        return ok


class NavGraph:
    """一个模块的跳转图实例：装资源、注册桥、跑一段图并返回成败。

    用法（模块内）：
        self.graph = NavGraph(self.ctx)
        self.graph.add_plugin(RES_DIR / "pipeline", RES_DIR / "image")
        self.graph.run("<模块名>_从大厅进入", reached="已到达<模块>页")  # 公共段
        self.graph.run("<模块名>_开始挑战", reached="已到达<下一目标页>")    # 模块段
    """

    CLICK_POLL_S = 0.05   # 点击结果轮询间隔

    def __init__(self, ctx):
        self.ctx = ctx
        self.image_dirs = [CORE_RES_DIR / "image"]
        # core 侧公共图目录按存在性纳入：导航真源已收口到 v3 资产，
        # 没有手写公共图时不该让 load() 对着不存在的路径去 post_pipeline。
        core_pipeline = CORE_RES_DIR / "pipeline"
        self._pipeline_dirs = [core_pipeline] if core_pipeline.is_dir() else []
        self._resource = Resource()
        self._tasker = Tasker()
        self._resource.register_custom_recognition(RECOGNIZER_NAME, TemplateRecognizer(self))
        self._resource.register_custom_action(ACTION_NAME, ClickAction(self))
        self._clicker: Clicker | None = None
        self._loaded = False
        self._last_frame = None   # 最近一次识别帧（只为动作桥换算框中心提供尺寸）

    # ---------- 装配 ----------

    def add_plugin(self, pipeline_dir: Path, image_dir: Path) -> None:
        """登记一个插件的图与模板目录（在 load() 之前调用）。

        pipeline_dir 可以是目录，也可以是单个 json/jsonc 文件（框架两种都支持）；
        传单个文件可避免把插件里其他用途的 pipeline（如对局回合链）一起装进跳转图。
        """
        if Path(pipeline_dir).exists():
            self._pipeline_dirs.append(Path(pipeline_dir))
        if Path(image_dir).exists():
            self.image_dirs.append(Path(image_dir))

    def load(self) -> bool:
        """加载公共图 + 各模块图，绑定 Tasker。重复调用无副作用。"""
        if self._loaded:
            return True
        for d in self._pipeline_dirs:
            job = self._resource.post_pipeline(str(d)).wait()
            if job.failed:
                logger.log(f"[跳转图] pipeline 加载失败: {d}", "ERROR")
                return False
        self._tasker.add_context_sink(PipelineLogger())
        self.ctx.bind_tasker(self._tasker, self._resource)
        self._loaded = True
        logger.log(f"[跳转图] 已加载 {len(self._pipeline_dirs)} 个图目录、"
                   f"{len(self.image_dirs)} 个模板目录")
        return True

    # ---------- 桥要用的宿主能力（截图/点击都收口到这里）----------

    def frame(self):
        """取一帧供识别。WGC 后端本身就是读中心采集器的缓存帧（零阻塞），
        所以这里不再自设 TTL 缓存——缓存会和框架的节点重试节奏打架：
        反复喂同一张旧帧，页面真切换了也看不出来，只能干等到 timeout。"""
        self._last_frame = self.ctx.capture.screenshot()
        return self._last_frame

    def frame_size(self) -> tuple[int, int]:
        """识别帧尺寸：动作桥用它把像素框换算成 Clicker 要的归一化坐标。"""
        if self._last_frame is None:
            return (0, 0)
        H, W = self._last_frame.shape[:2]
        return (W, H)

    def click(self, cx: float, cy: float, box_norm, timeout_s: float) -> bool:
        """执行一次点击并等到位。模式与意图每次同步（设置页可热切）。"""
        clicker = self._ensure_clicker()
        clicker.set_mode(self.ctx.click_mode)
        clicker.set_intent(self.ctx.intent_mode)
        if not clicker.submit_click(cx, cy, box=box_norm):
            return False
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            if not self.ctx.lifecycle.running:
                clicker.cancel()
                return False
            res = clicker.consume_result()
            if res is not None:
                return bool(res.get("ok"))
            time.sleep(self.CLICK_POLL_S)
        clicker.cancel()
        return False

    def _ensure_clicker(self) -> Clicker:
        if self._clicker is None:
            self._clicker = Clicker(self.ctx.hwnd, self.ctx.click_mode)
        return self._clicker

    # ---------- 跑图 ----------

    def run(self, entry: str, reached: str | None = None) -> bool:
        """跑一条以 entry 为起点的图，阻塞到图结束（终点节点无 next）或失败。

        reached：终点确认节点名。图"跑完"不等于"跑对"——中间节点超时后框架
        也可能正常收尾，所以要求那个确认页面的识别节点真的 completed 才算到位。

        手柄租约只在跑图期间持有：跑图结束立刻归还，模块外层的
        reset_device()（销毁手柄复位）才不会被租约不变量挡住。
        """
        if not self.load():
            return False
        # 手柄租约只在跑图期间持有：结束立刻归还，模块外层的 reset_device()
        # （销毁手柄复位）才不会被「有活跃租约禁止断开设备」这条不变量挡住。
        with ExitStack() as stack:
            if self.ctx.click_mode == "gamepad":
                from maaracing_assistant.core.capabilities import BUTTON_A
                gpad = stack.enter_context(self.ctx.gamepad.acquire())
                self._ensure_clicker().bind_gamepad(
                    self.ctx.capture, gpad, confirm_button=BUTTON_A)
            logger.log(f"[跳转图] 起跑「{entry}」")
            job = self._tasker.post_task(entry)
            while not job.status.done:
                if not self.ctx.lifecycle.running:
                    self._tasker.post_stop().wait()
                    logger.log("[跳转图] 收到停止信号，已中断跑图")
                    return False
                time.sleep(0.2)
            ok = bool(job.succeeded)
            if ok and reached:
                node = self._tasker.get_latest_node(reached)
                ok = bool(node and node.completed)
                if not ok:
                    logger.log(f"[跳转图] 终点确认节点「{reached}」未命中，判为未到位", "WARNING")
            logger.log(f"[跳转图] 「{entry}」{'完成' if ok else '失败'}")
            return ok

    def stop(self) -> None:
        """中断在跑的图（模块 stop 里调用）。"""
        try:
            self._tasker.post_stop()
        except Exception:  # noqa: BLE001 —— 未起跑时停止是正常路径
            pass

    def shutdown(self) -> None:
        """释放点击器（停后台导航线程），模块 cleanup 里调用。"""
        if self._clicker is not None:
            self._clicker.shutdown()
            self._clicker = None
